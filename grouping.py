# The "Refresh groups" pipeline: incremental embedding, drift-gated metadata
# generation, tag consolidation, clustering, and group naming.
# Also hosts semantic search, which shares the embedding call.

import bisect
import json
import logging
import os
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import cache

import httpx
import numpy as np
from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry

import db

EMBED_MODEL = "text-embedding-3-small"
GEN_MODEL = "openai:gpt-4.1-mini"  # metadata generation, tag merging, group naming

# All derived text (titles, tags, summaries, group names) is written in this
# language regardless of the conversation's language — the archive reads as one.
ARCHIVE_LANGUAGE = os.environ.get("ARCHIVE_LANGUAGE", "Korean")

# Regenerate metadata when cos(current mean, anchor) drops below this.
# Measured 2026-07-14: last-10% growth ≈ 0.999 (skip), doubling ≈ 0.983–0.989 (fire),
# 25% foreign-topic inflow ≈ 0.980–0.995 (mostly fire). A false fire costs one cheap
# call, a missed fire costs stale metadata — so tuned to the sensitive side.
DRIFT_MIN_COS = 0.99

# Small-cluster merge threshold (centered cosine); big groups are LLM-assigned.
# Re-measured 2026-07-29 over 36 conversations: singletons stay flat at 5 from 0.26
# to 0.30, jump to 7 at 0.31 and to 11 at 0.32 — so 0.30 is the last stable value
# before the archive shatters. Below 0.26 unrelated topics start fusing (object
# detection absorbed the interview-prep conversations at 0.25). The previous 0.35
# was tuned against 15 conversations and had drifted past the cliff.
T_SMALL = 0.30
MIN_CHARS = 30  # messages shorter than this ("thanks", "ok") don't vote in the mean
MIN_GROUP_FOR_CENTERING = 4  # smaller language groups fall back to the global mean

# Conversations are cut into passages of this many characters before being embedded
# for search. Measured 2026-07-15 (with passages of about this size) over 66 queries:
# whole-conversation average 48% top-1, per-message 59%, these passages 68% — single
# messages are too short to carry context, whole conversations too blurred. Passages
# also cost a fifth of per-message storage (13 MB vs 65 MB here).
CHUNK_CHARS = 1500

# Each passage is embedded together with the last this-many characters before it, so
# a sentence or a question/answer pair straddling a cut still scores as a whole.
# Positions are unaffected: they cover the messages of the passage's own text only.
CHUNK_OVERLAP = 200
# A cut moves back to the last space or newline within this many characters, so
# words stay whole; with none in reach it falls exactly on CHUNK_CHARS.
CUT_SNAP = 100

# Bump whenever the passage rule changes. A conversation indexed under another
# version is re-cut from scratch on its next indexing (save or refresh), so old and
# new passages never mix inside one conversation.
# 1: whole messages glued past CHUNK_CHARS, 6,000-character cap (dropped 11% of text)
# 2: hard cut every CHUNK_CHARS with CHUNK_OVERLAP
CHUNK_VERSION = 2

# Meaning-search floor. Measured 2026-07-29 over the same 66 queries: right answers
# scored a median 0.486 (lowest 0.274), everything else a median 0.285. Cutting here
# keeps 98% of the right answers while removing 59% of the noise — without it every
# query returns the whole archive, since vector search always ranks something first.
MIN_SIMILARITY = 0.30

# A conversation's sections are built from its passages scoring within SECTION_MARGIN
# of its best one; passages at most SECTION_GAP apart (in passage order) join one
# section. First guesses from one query on 2026-10-28 ("GitBook card", top ten
# passages within 0.14 of the best and at most 10 apart) — re-measure with a query set.
SECTION_MARGIN = 0.15
SECTION_GAP = 10
MAX_SECTIONS = 3


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Search passages are long enough that a full batch can exceed the per-minute
    token allowance, so a 429 is waited out rather than failing the whole refresh."""
    api_key = os.environ["OPENAI_API_KEY"]
    vectors: list[list[float]] = []
    with httpx.Client(timeout=120) as client:
        for i in range(0, len(texts), 100):
            for attempt in range(6):
                response = client.post(
                    "https://api.openai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": EMBED_MODEL, "input": texts[i : i + 100]},
                )
                if response.status_code != 429:
                    break
                time.sleep(2**attempt)
            response.raise_for_status()
            vectors.extend(item["embedding"] for item in response.json()["data"])
    return vectors


def _passages(messages: list[dict]) -> list[tuple[int, int, str]]:
    """Cut a conversation into passages of CHUNK_CHARS characters, skipping the short
    chatter that MIN_CHARS already excludes from the mean. Each passage comes as
    (first message index, last message index, text to embed): the indexes cover the
    messages its own characters come from, and the text also starts with the
    CHUNK_OVERLAP characters before it.

    The cut ignores message boundaries, so sizes stay even and a long message is
    split rather than truncated; the overlap is what keeps a sentence or a
    question/answer pair that straddles a cut together. Every cut depends only on
    the text before it, so appending messages changes the last passage alone."""
    owners, starts, parts, offset = [], [], [], 0
    for i, message in enumerate(messages):
        content = (message.get("content") or "").strip()
        if len(content) < MIN_CHARS:
            continue
        owners.append(i)
        starts.append(offset)
        parts.append(content)
        offset += len(content) + 1  # the newline joining it to the next one
    stream = "\n".join(parts)

    def owner(at: int) -> int:
        return owners[bisect.bisect_right(starts, at) - 1]

    passages, start = [], 0
    while start < len(stream):
        end = start + CHUNK_CHARS
        if end < len(stream):
            space = max(stream.rfind(" ", end - CUT_SNAP, end), stream.rfind("\n", end - CUT_SNAP, end))
            if space > start:
                end = space + 1
        else:
            end = len(stream)
        text = stream[max(start - CHUNK_OVERLAP, 0) : end]
        passages.append((owner(start), owner(end - 1), text))
        start = end
    return passages


def _card(title: str, tags: list[str], summary: str) -> str:
    return f"{title}. {summary} {' '.join(tags or [])}"


def _index_body_chunks(row: dict) -> int:
    """Bring one conversation's search passages up to date, embedding only what changed.

    Passages are cut in message order, so appended messages can only change the last
    passage (it may have been partial) and add new ones after it; every earlier
    passage stays identical. A message list shorter than what was indexed means the
    conversation was rewritten, so that case starts over — the same rule
    db.save_conversation applies to the mean vector."""
    messages = row["messages"]
    rewritten = len(messages) < row["chunked_message_count"]
    recut = row["chunk_version"] != CHUNK_VERSION
    start = 0 if rewritten or recut else max(row["body_chunks"] - 1, 0)
    passages = _passages(messages)
    texts = [text for _, _, text in passages[start:]]
    vectors = _embed_texts(texts) if texts else []
    positions = [(first, last) for first, last, _ in passages]
    db.replace_body_chunks(
        row["id"],
        start,
        vectors,
        positions,
        len(messages),
        row["chunked_message_count"],
        CHUNK_VERSION,
    )
    return len(vectors)


# One worker: saves arrive every turn, and indexing them one at a time keeps two
# jobs off the same conversation and memory flat on the small machine.
_indexer = ThreadPoolExecutor(max_workers=1)


def index_soon(key: str) -> None:
    """Queue one conversation's search passages for indexing, off the request path,
    so a save never waits on — or fails because of — the embedding call. A failed
    run leaves chunked_message_count where it was, so the next save retries it."""
    if os.environ.get("OPENAI_API_KEY"):
        _indexer.submit(_index_conversation, key)


def _index_conversation(key: str) -> None:
    try:
        for row in db.rows_needing_chunks(CHUNK_VERSION, key):
            _index_body_chunks(row)
    except Exception:
        logging.getLogger(__name__).exception("indexing %s failed; the next save retries", key)


def _refresh_body_chunks() -> int:
    """Index search passages for every conversation that gained messages. The counter
    starts at zero, so the first run indexes the whole archive."""
    return sum(_index_body_chunks(row) for row in db.rows_needing_chunks(CHUNK_VERSION))


def _refresh_cards() -> int:
    """Build every missing card. Whatever invalidates a card deletes it (see
    db.set_metadata and db.apply_tag_mapping), so 'missing' is the only state to
    repair — and repairing it is safe to retry, which a stale card would not be."""
    rebuilt = 0
    for row in db.rows_needing_card():
        card = _card(row["title"], row["tags"], row["summary"])
        db.replace_card_chunk(row["id"], _embed_texts([card])[0])
        rebuilt += 1
    return rebuilt


def semantic_search(query: str, limit: int) -> list[dict]:
    """Search by meaning: embed the query and let Postgres rank by cosine distance.

    Ranking only needs relative order, so the stored vectors work as they are —
    unlike clustering, which needs absolute distances and hence per-language
    centering (see _center_by_language).

    Each result also gets `sections`: where in the conversation the match sits, so an
    assistant can open that stretch instead of the whole conversation."""
    results = db.semantic_search(_embed_texts([query])[0], limit, MIN_SIMILARITY)
    for result in results:
        result["sections"] = _sections(result.pop("hits"))
    texts = db.messages_at(
        [(r["conversation_key"], s["best_message"]) for r in results for s in r["sections"]]
    )
    for result in results:
        for section in result["sections"]:
            text = texts.get((result["conversation_key"], section["best_message"]), "")
            section["excerpt"] = text[: db.EXCERPT_CHARS]
    return results


def _sections(hits: list[dict]) -> list[dict]:
    """Merge a conversation's best-scoring passages into stretches of messages, best
    first. The stretch's length is what tells an assistant whether the topic is a
    passing remark or a long discussion it should keep reading."""
    if not hits:
        return []
    floor = max(h["similarity"] for h in hits) - SECTION_MARGIN
    close = sorted((h for h in hits if h["similarity"] >= floor), key=lambda h: h["chunk_index"])
    groups = [[close[0]]]
    for hit in close[1:]:
        if hit["chunk_index"] - groups[-1][-1]["chunk_index"] <= SECTION_GAP:
            groups[-1].append(hit)
        else:
            groups.append([hit])
    sections = []
    for group in groups:
        top = max(group, key=lambda h: h["similarity"])
        start, end = group[0]["first_message"], group[-1]["last_message"]
        sections.append(
            {
                "start": start,
                "end": end,
                "length": end - start + 1,
                "best_message": top["first_message"],
                "similarity": round(top["similarity"], 3),
            }
        )
    return sorted(sections, key=lambda s: s["similarity"], reverse=True)[:MAX_SECTIONS]


def _update_conversation_vector(row: dict, texts: list[str]) -> int:
    """Embed new messages and fold them into the running weighted mean.

    Message vectors are weighted by character length — long substantive messages
    should place the conversation more than short chatter (equal weighting let
    frequent-but-short meta remarks dominate; see ARCHITECTURE.md).
    `embedded_vector_count` stores the accumulated weight (total characters).
    Vectors are summed per 100-message batch and discarded — bounded memory on
    the 256MB machine.
    """
    old_weight = row["embedded_vector_count"]
    total_messages = len(row["messages"])
    if not texts:
        # every new message was filtered out — just advance the counters
        db.set_embedding_state(row["id"], row["embedding"], total_messages, old_weight)
        return 0
    weighted_sum = None
    new_weight = 0.0
    for i in range(0, len(texts), 100):
        chunk = texts[i : i + 100]
        vectors = np.array(_embed_texts(chunk), dtype=np.float32)
        weights = np.array([len(t) for t in chunk], dtype=np.float32)
        batch = (vectors * weights[:, None]).sum(axis=0)
        weighted_sum = batch if weighted_sum is None else weighted_sum + batch
        new_weight += float(weights.sum())
    if row["embedding"] is not None and old_weight > 0:
        mean = (np.array(row["embedding"], dtype=np.float32) * old_weight + weighted_sum) / (
            old_weight + new_weight
        )
    else:
        mean = weighted_sum / new_weight
    db.set_embedding_state(
        row["id"], [float(x) for x in mean], total_messages, int(old_weight + new_weight)
    )
    return len(texts)


# --- language helpers ---


def _hangul_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if "가" <= c <= "힣") / len(letters)


def _output_language_ok(text: str) -> bool:
    """Cheap check that generated text is in ARCHIVE_LANGUAGE (ko/en only; else trust the prompt)."""
    lang = ARCHIVE_LANGUAGE.strip().lower()
    if lang in ("korean", "ko", "한국어"):
        return _hangul_ratio(text) > 0.3
    if lang in ("english", "en"):
        return _hangul_ratio(text) < 0.05
    return True


def _language_of(row: dict) -> str:
    """Language group of a conversation (for centering), from title + first message."""
    text = row["title"] + " " + (row.get("sample") or "")
    if not any(c.isalpha() for c in text):
        return "unknown"
    return "ko" if _hangul_ratio(text) > 0.3 else "other"


# --- generation agents (created lazily: constructing one requires OPENAI_API_KEY) ---


class ConversationMetadata(BaseModel):
    title: str = Field(max_length=80, description="one-line title")
    tags: list[str] = Field(
        min_length=1, max_length=5, description="topic tags, most important first"
    )
    summary: str = Field(max_length=600, description="2-4 sentence summary")


class TagMergeMap(BaseModel):
    merge: dict[str, str] = Field(default_factory=dict)


class ClusterAssignment(BaseModel):
    cluster_id: int
    name: str = Field(min_length=1, max_length=60, description="the group's name only")
    big_category: str = Field(
        min_length=1,
        max_length=60,
        description="the big category's name only — never a sentence or a prefix such "
        "as 'new category:'; use the exact existing name when it fits",
    )


class GroupNaming(BaseModel):
    assignments: list[ClusterAssignment] = Field(default_factory=list)


@cache
def _metadata_agent() -> Agent:
    agent = Agent(GEN_MODEL, output_type=ConversationMetadata, retries=2)

    @agent.output_validator
    def check_language(ctx, output: ConversationMetadata) -> ConversationMetadata:
        if not _output_language_ok(output.title + " " + output.summary):
            raise ModelRetry(f"Write the title and summary in {ARCHIVE_LANGUAGE}.")
        return output

    return agent


@cache
def _merge_agent() -> Agent:
    return Agent(GEN_MODEL, output_type=TagMergeMap, retries=2)


# A label must be a name, not a sentence about the answer. Observed leak:
# "새로운 대분류: 해외 생활과 법률 및 복지" — the model narrated that it was coining
# a category and the narration became the category.
_LABEL_NOISE = re.compile(
    r"^\s*(new\s+(big\s+)?categor(y|ies)|새로운?\s*(대)?분류|신규\s*분류|category|대분류)\s*[:\-–]\s*",
    re.IGNORECASE,
)


def _clean_label(label: str) -> str:
    return _LABEL_NOISE.sub("", label).strip(" :-–\"'").strip()


@cache
def _naming_agent() -> Agent:
    agent = Agent(GEN_MODEL, output_type=GroupNaming, retries=2)

    @agent.output_validator
    def check_labels(ctx, output: GroupNaming) -> GroupNaming:
        joined = " ".join(a.name + " " + a.big_category for a in output.assignments)
        if output.assignments and not _output_language_ok(joined):
            raise ModelRetry(f"Write group names in {ARCHIVE_LANGUAGE}.")
        for a in output.assignments:
            if _clean_label(a.name) != a.name or _clean_label(a.big_category) != a.big_category:
                raise ModelRetry(
                    "Give the bare name of each group and category. Do not prefix it with "
                    "'new category:' or any other commentary."
                )
        return output

    return agent


def _generate_metadata(messages: list[dict]) -> dict:
    """Derive title/tags/summary from a conversation sample (one guarded call)."""
    excerpt = "\n".join(f"[{m['role']}] {m['content'][:400]}" for m in messages)
    prompt = (
        f"Below is a sample of one conversation. Derive its metadata "
        f"(title, tags, summary) in {ARCHIVE_LANGUAGE}; keep technical terms as-is.\n"
        "Tags must name what the conversation is about, most important first. Do not tag "
        "the tools or conventions used to conduct it (MCP, saving rules, instructions) "
        "unless they are the actual subject.\n\n"
        f"{excerpt}"
    )
    output = _metadata_agent().run_sync(prompt).output
    return {
        "title": output.title,
        "tags": [t[:30] for t in output.tags],
        "summary": output.summary,
    }


def _merge_suggestions(vocabulary: list[str]) -> dict[str, str]:
    """Ask which tags are the same topic in different spellings (one call)."""
    prompt = (
        "Below are the tags used in a conversation archive. Merge only tags that "
        "clearly refer to the same topic in different spellings; when unsure, leave them. "
        "Pick the most concise, common spelling as the representative "
        "(map: tag to drop -> representative).\n\n" + ", ".join(vocabulary)
    )
    return _merge_agent().run_sync(prompt).output.merge


def _assign_and_name(
    rows: list[dict], small_clusters: list[list[int]], changed: list[int], roofs: str
) -> dict[int, dict] | None:
    """Name changed small clusters and assign them to big categories (one call).

    Unchanged clusters appear only as a candidate roster and are never renamed.
    Returns {cluster_index: {"name", "big"}} or None on failure (caller falls back).
    """
    if not changed:
        return {}
    payload = {
        str(ci): [rows[m]["tags"] or [] for m in small_clusters[ci]] for ci in changed
    }
    prompt = (
        "You maintain the group tree of a conversation archive. For the CHANGED small "
        "clusters below (and only those):\n"
        "1) name each one after the theme running through its member tags "
        f"(2-5 words, in {ARCHIVE_LANGUAGE})\n"
        "2) assign it to one of the existing big categories by name, or coin a new "
        "big category if none fits\n"
        "Do not alter existing categories. Answer only for the changed clusters.\n\n"
        f"Existing big categories (name — small clusters [representative tags]):\n{roofs}\n\n"
        f"Changed small clusters (tags per member conversation, importance-ordered):\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        output = _naming_agent().run_sync(prompt).output
    except Exception:
        return None
    changed_set = set(changed)
    return {
        a.cluster_id: {
            # cleaned again here: the validator can exhaust its retries and still return
            "name": _clean_label(a.name)[:40],
            "big": _clean_label(a.big_category)[:40],
        }
        for a in output.assignments
        if a.cluster_id in changed_set
    }


def _consolidate_tags() -> dict[str, str]:
    """Global tag merge: fetch a merge map, flatten chains, apply to the DB."""
    vocabulary = db.all_tags()
    if len(vocabulary) < 2:
        return {}
    try:
        raw = _merge_suggestions(vocabulary)
    except Exception:
        return {}  # not fatal — the next refresh retries
    mapping = {}
    for source in raw:
        target, seen = source, set()
        while target in raw and target not in seen:
            seen.add(target)
            target = raw[target]
        if source != target and source in vocabulary:
            mapping[source] = target
    if mapping:
        db.apply_tag_mapping(mapping)
    return mapping


def _refresh_metadata(rows: list[dict]) -> set[int]:
    """Regenerate title/tags/summary for conversations flagged as drifted by
    all_embedded (anchor missing, or moved past DRIFT_MIN_COS). Returns the
    regenerated row ids."""
    regenerated: set[int] = set()
    for row in rows:
        if not row["drifted"]:
            continue  # no meaningful movement since the anchor
        try:
            meta = _generate_metadata(db.messages_excerpt(row["id"]))
            # drops the card too; _refresh_cards rebuilds it after the tag merge, so
            # it is built from the final tags rather than the pre-merge spellings
            db.set_metadata(row["id"], meta["title"], meta["tags"], meta["summary"])
            row["title"], row["tags"], row["summary"] = (
                meta["title"], meta["tags"], meta["summary"],
            )
            regenerated.add(row["id"])
        except Exception:
            continue  # failed rows are retried by the next refresh
    return regenerated


def _load_matrix(rows: list[dict]) -> np.ndarray:
    """Vectors for clustering and the projection, in `rows` order. Fetched here
    instead of being carried inside `rows`, so the metadata phase — the slow part,
    full of LLM calls — never holds them."""
    vectors = db.embeddings_by_id()
    return np.array([vectors[row["id"]] for row in rows], dtype=np.float32)


def _center_by_language(matrix: np.ndarray, rows: list[dict]) -> np.ndarray:
    """Subtract per-language means so cosine measures topical difference, not the
    shared "same language, same register" component. Groups smaller than
    MIN_GROUP_FOR_CENTERING fall back to the global mean (own-mean would degenerate)."""
    if len(rows) < MIN_GROUP_FOR_CENTERING:
        return matrix
    centered = matrix.copy()
    global_mean = matrix.mean(axis=0)
    groups: dict[str, list[int]] = {}
    for i, row in enumerate(rows):
        groups.setdefault(_language_of(row), []).append(i)
    for members in groups.values():
        center = (
            matrix[members].mean(axis=0)
            if len(members) >= MIN_GROUP_FOR_CENTERING
            else global_mean
        )
        centered[members] -= center
    return centered


def _agglomerate(vectors: np.ndarray, threshold: float) -> list[list[int]]:
    """Repeatedly merge the cluster pair with the highest centroid cosine until
    it drops below the threshold. Returns member-index lists."""
    clusters = [[i] for i in range(len(vectors))]
    centroids = vectors.copy()
    while len(clusters) > 1:
        unit = centroids / (np.linalg.norm(centroids, axis=1, keepdims=True) + 1e-9)
        sim = unit @ unit.T
        np.fill_diagonal(sim, -1.0)
        i, j = divmod(int(sim.argmax()), sim.shape[1])
        if sim[i, j] < threshold:
            break
        clusters[i].extend(clusters[j])
        del clusters[j]
        centroids[i] = vectors[clusters[i]].mean(axis=0)
        centroids = np.delete(centroids, j, axis=0)
    return clusters


def _roof_digest(rows: list[dict], small_clusters: list[list[int]], kept: dict) -> str:
    """Candidate roster of unchanged clusters, grouped by big category —
    name plus two representative tags each (counting, no LLM)."""
    roofs: dict[str, list[str]] = {}
    for ci, (small_label, big_label) in kept.items():
        top = Counter(
            t for m in small_clusters[ci] for t in (rows[m]["tags"] or [])
        ).most_common(2)
        rep = ", ".join(tag for tag, _ in top)
        roofs.setdefault(big_label or "(uncategorized)", []).append(f"{small_label}({rep})")
    return "\n".join(f"- {big}: " + " / ".join(s) for big, s in roofs.items()) or "(none yet)"


def _label(rows: list[dict], members: list[int]) -> str:
    """Fallback label: the most common tag, else the shortest title."""
    tag_counts = Counter(tag for m in members for tag in (rows[m]["tags"] or []))
    if tag_counts:
        return tag_counts.most_common(1)[0][0]
    shortest = min((rows[m]["title"] for m in members), key=len)
    return shortest[:30] or rows[members[0]]["conversation_key"][:30]


def _pca3(vectors: np.ndarray) -> np.ndarray:
    centered = vectors - vectors.mean(axis=0)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    coords = centered @ vt[: min(3, vt.shape[0])].T
    if coords.shape[1] < 3:  # pad missing axes when there are very few rows
        coords = np.pad(coords, ((0, 0), (0, 3 - coords.shape[1])))
    return coords


def refresh() -> dict:
    """Embed new messages, then — only if any conversation drifted — regenerate its
    metadata and recompute merging, clustering, naming and coordinates."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set (list/search still work without it)")

    # per-conversation incremental embedding; progress persists per conversation,
    # so an interrupted run resumes where it stopped
    embedded_count = 0
    for row in db.rows_needing_embedding():
        new_messages = row["messages"][row["embedded_message_count"] :]
        texts = [
            m["content"][:6000]
            for m in new_messages
            if len(m.get("content", "").strip()) >= MIN_CHARS
        ]
        embedded_count += _update_conversation_vector(row, texts)

    # ahead of the drift gate on purpose: new messages must become searchable even
    # when the conversation's overall subject hasn't moved. _refresh_cards also runs
    # here so a card dropped by a run that failed later is repaired next time.
    chunked_count = _refresh_body_chunks() + _refresh_cards()

    rows = db.all_embedded(DRIFT_MIN_COS)
    if not rows:
        return {
            "embedded": embedded_count,
            "chunks_indexed": chunked_count,
            "metadata_generated": 0,
            "tags_merged": 0,
            "total": 0,
            "clusters_small": 0,
            "clusters_big": 0,
        }

    # drift gate: if nothing fired, groups/names/coordinates stay exactly as they are
    regenerated = _refresh_metadata(rows)
    if not regenerated:
        return {
            "embedded": embedded_count,
            "chunks_indexed": chunked_count,
            "metadata_generated": 0,
            "tags_merged": 0,
            "total": len(rows),
            "clusters_small": 0,
            "clusters_big": 0,
            "skipped": "no changes",
        }

    # global tag merge (also reflected in the in-memory rows used for labels)
    mapping = _consolidate_tags()
    if mapping:
        for row in rows:
            merged = []
            for tag in row["tags"] or []:
                canon = mapping.get(tag, tag)
                if canon not in merged:
                    merged.append(canon)
            row["tags"] = merged

    # cards dropped by metadata regeneration and by the tag merge are rebuilt here,
    # once both are settled, so each card matches the text finally stored
    chunked_count += _refresh_cards()

    matrix = _load_matrix(rows)
    matrix /= np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    matrix = _center_by_language(matrix, rows)
    small_clusters = _agglomerate(matrix, T_SMALL)

    # change detection: a cluster whose member set matches last time and contains
    # no regenerated conversation keeps its stored name and big category
    prev_members: dict[int, set] = {}
    for row in rows:
        if row["cluster_small"] is not None:
            prev_members.setdefault(row["cluster_small"], set()).add(row["id"])
    kept: dict[int, tuple[str, str]] = {}
    changed: list[int] = []
    for ci, members in enumerate(small_clusters):
        ids = {rows[m]["id"] for m in members}
        prev_ids = {rows[m]["cluster_small"] for m in members}
        if (
            len(prev_ids) == 1
            and None not in prev_ids
            and prev_members.get(next(iter(prev_ids))) == ids
            and not (ids & regenerated)
            and rows[members[0]]["cluster_small_label"]
        ):
            first = rows[members[0]]
            kept[ci] = (first["cluster_small_label"], first["cluster_big_label"] or "")
        else:
            changed.append(ci)

    # name/assign only the changed clusters (one call); fallback: most-common tag,
    # standing alone as its own big category, healed by a later refresh
    names = _assign_and_name(rows, small_clusters, changed, _roof_digest(rows, small_clusters, kept))
    small_label: dict[int, str] = {}
    big_label: dict[int, str] = {}
    for ci in range(len(small_clusters)):
        if ci in kept:
            small_label[ci], big_label[ci] = kept[ci]
        elif names is not None and ci in names and names[ci]["name"]:
            small_label[ci] = names[ci]["name"]
            big_label[ci] = names[ci]["big"] or names[ci]["name"]
        else:
            fallback = _label(rows, small_clusters[ci])
            small_label[ci] = fallback
            big_label[ci] = fallback

    big_ids = {name: i for i, name in enumerate(sorted(set(big_label.values())))}
    coords = _pca3(matrix)

    updates = []
    for ci, members in enumerate(small_clusters):
        for m in members:
            updates.append(
                {
                    "id": rows[m]["id"],
                    "cluster_small": ci,
                    "cluster_small_label": small_label[ci],
                    "cluster_big": big_ids[big_label[ci]],
                    "cluster_big_label": big_label[ci],
                    "px": float(coords[m, 0]),
                    "py": float(coords[m, 1]),
                    "pz": float(coords[m, 2]),
                }
            )
    db.apply_grouping(updates)

    return {
        "embedded": embedded_count,
        "chunks_indexed": chunked_count,
        "metadata_generated": len(regenerated),
        "tags_merged": len(mapping),
        "named": len(changed),
        "total": len(rows),
        "clusters_small": len(small_clusters),
        "clusters_big": len(big_ids),
    }
