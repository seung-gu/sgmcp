# The "Refresh groups" pipeline: incremental embedding, drift-gated metadata
# generation, tag consolidation, clustering, and group naming.
# Also hosts semantic search, which shares the embedding call.

import bisect
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from typing import Literal

import httpx
import numpy as np
from pydantic import BaseModel, Field, create_model
from pydantic_ai import Agent, ModelRetry

import db

EMBED_MODEL = "text-embedding-3-small"
GEN_MODEL = "openai:gpt-4.1-mini"  # metadata generation, tag merging
# Names the group tree (two calls per regroup). Compared 2026-10-28 on 36 groups with
# the same two-step prompt: misfiled groups gpt-4.1-mini ~6, gpt-5.4-mini ~5, gpt-5.5
# ~3, at about $0.01 / $0.02 / $0.18 a regroup — the 5.4 mini is the balance.
TREE_MODEL = "openai:gpt-5.4-mini"
MIN_BIG, MAX_BIG = 5, 10  # big categories in the tree

# All derived text (titles, tags, summaries, group names) is written in this
# language regardless of the conversation's language — the archive reads as one.
ARCHIVE_LANGUAGE = os.environ.get("ARCHIVE_LANGUAGE", "Korean")

# Regenerate metadata when cos(current mean, anchor) drops below this.
# Measured 2026-07-14: last-10% growth ≈ 0.999 (skip), doubling ≈ 0.983–0.989 (fire),
# 25% foreign-topic inflow ≈ 0.980–0.995 (mostly fire). A false fire costs one cheap
# call, a missed fire costs stale metadata — so tuned to the sensitive side.
DRIFT_MIN_COS = 0.99

# Small-cluster merge threshold: the average cosine between two clusters' members
# (average linkage), after per-language centering, on the passage mean blended with
# the card; big categories are LLM-named. Measured 2026-10-28 over 87 conversations
# against the pairs two LLM filings both grouped (155) or both kept apart: 0.28
# scores F1 0.62 (precision 0.78, recall 0.52; 0.58–0.62 anywhere from 0.22 to 0.32).
# The previous centroid linkage on the passage mean alone scored 0.37 at 0.30: a
# growing cluster's blurred centre kept absorbing neighbours, into a 16-member blob.
T_SMALL = 0.28
MIN_CHARS = 30  # messages shorter than this ("thanks", "ok") stay out of passages, so out of the mean
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


def _passages(messages: list[dict]) -> list[tuple[int, int, int, str]]:
    """Cut a conversation into passages of CHUNK_CHARS characters, skipping the short
    chatter under MIN_CHARS. Each passage comes as (first message index, last message
    index, own chars, text to embed): the indexes and the length cover its own
    characters, while the text also starts with the CHUNK_OVERLAP characters before.

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
        passages.append((owner(start), owner(end - 1), end - start, text))
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
    texts = [text for *_, text in passages[start:]]
    vectors = _embed_texts(texts) if texts else []
    positions = [(first, last, chars) for first, last, chars, _ in passages]
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
    found = db.messages_at(
        [
            (r["conversation_key"], s[field])
            for r in results
            for s in r["sections"]
            for field in ("start", "end", "best_message")
        ]
    )
    for result in results:
        key = result["conversation_key"]
        db.add_section_times(result["sections"], key, found)
        for section in result["sections"]:
            text = found.get((key, section["best_message"]), {}).get("content", "")
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


def _refresh_means() -> int:
    """Set each lagging conversation's mean vector to the average of its search
    passages, weighted by their own length so a short last passage weighs little —
    the same "weight by length" rule the old per-message mean followed (equal weights
    let frequent-but-short remarks dominate; see ARCHITECTURE.md). The passages are
    already embedded, so this makes no embedding calls; it used to re-embed every
    message. Measured 2026-10-28 over 82 conversations: at T_SMALL the small clusters
    pair conversations the same way as the old mean for 99.0% of pairs."""
    updated = 0
    for row in db.rows_needing_mean():
        vectors, chars = db.passage_vectors(row["id"])
        weights = np.array(chars, dtype=np.float32)
        if not vectors or weights.sum() == 0:
            continue
        mean = (np.array(vectors, dtype=np.float32) * weights[:, None]).sum(axis=0) / weights.sum()
        db.set_embedding_state(row["id"], [float(x) for x in mean], row["chunked_message_count"])
        updated += 1
    return updated


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


class BigCategory(BaseModel):
    name: str = Field(description="the category's bare name, 2-4 words")
    holds: str = Field(description="one line: what kind of conversation belongs here")


class BigCategories(BaseModel):
    categories: list[BigCategory]


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


def _bare(label: str) -> str:
    """A label without narration before a colon. Observed leaks: "새로운 대분류: …",
    "새로운 큰 카테고리: …" — the model announced that it was coining a category."""
    return label.rsplit(":", 1)[-1].strip(" -–\"'")[:40]


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


def _name_tree(rows: list[dict], small_clusters: list[list[int]]) -> list[tuple[str, str]]:
    """Name every small cluster and shelve it under a big category, in two calls:
    first invent MIN_BIG–MAX_BIG categories, each with a line on what it holds, then
    place every cluster into exactly one of them. One call doing both filed software
    projects under job hunting; as a closed choice against stated scopes the placement
    holds. Members are shown by title and summary — tags alone named an admin group
    after one member's tag. Returns (small name, big name) per cluster; raises when
    the model fails, leaving the stored tree for the next refresh to retry."""
    listing = "\n".join(
        f"group {ci}:\n"
        + "\n".join(f"  - {rows[m]['title']} — {rows[m]['summary'][:200]}" for m in members)
        for ci, members in enumerate(small_clusters)
    )
    propose = Agent(TREE_MODEL, output_type=BigCategories, retries=2)

    @propose.output_validator
    def check_categories(ctx, output: BigCategories) -> BigCategories:
        if not MIN_BIG <= len(output.categories) <= MAX_BIG:
            raise ModelRetry(f"Give {MIN_BIG}-{MAX_BIG} categories, not {len(output.categories)}.")
        if not _output_language_ok(" ".join(c.name + " " + c.holds for c in output.categories)):
            raise ModelRetry(f"Write the categories in {ARCHIVE_LANGUAGE}.")
        return output

    proposed = propose.run_sync(
        "Below are the topic groups of one person's archive of AI conversations (title — "
        f"summary of each member). Propose {MIN_BIG}-{MAX_BIG} big categories, in "
        f"{ARCHIVE_LANGUAGE}, that together cover every group, the way this person would "
        "shelve them. Categorize by what the person was doing in the conversation (e.g. "
        "building a piece of software, applying for jobs, dealing with an authority), not "
        "by words that merely appear in it. Do not merge unrelated topics just to make a "
        "category bigger: a category may hold a single group when its topic is unlike the "
        "others.\n\n" + listing
    ).output.categories
    scopes = {}
    for c in proposed:
        scopes.setdefault(_bare(c.name), c.holds)

    Placement = create_model(
        "Placement",
        group_id=(int, ...),
        name=(str, Field(description="the group's bare name, 2-6 words")),
        category=(Literal[tuple(scopes)], ...),
    )
    place = Agent(
        TREE_MODEL,
        output_type=create_model("Placements", placements=(list[Placement], ...)),
        retries=2,
    )

    @place.output_validator
    def check_placements(ctx, output):
        missing = set(range(len(small_clusters))) - {p.group_id for p in output.placements}
        if missing:
            raise ModelRetry(f"Place every group; missing: {sorted(missing)}.")
        if not _output_language_ok(" ".join(p.name for p in output.placements)):
            raise ModelRetry(f"Write the group names in {ARCHIVE_LANGUAGE}.")
        return output

    placements = place.run_sync(
        "Place every group below into exactly one of these categories, and name each "
        f"group, in {ARCHIVE_LANGUAGE}, after what its conversations share.\n\n"
        "Categories:\n"
        + "\n".join(f"- {name}: {holds}" for name, holds in scopes.items())
        + "\n\nGroups:\n" + listing
    ).output.placements
    tree = {p.group_id: (_bare(p.name), p.category) for p in placements}
    return [tree[ci] for ci in range(len(small_clusters))]


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
    """Vectors for clustering and the projection, in `rows` order, at unit length:
    the passage mean blended with the card (title, tags, summary), each at unit
    length, so a conversation is placed by what it contains and by what it is about.
    One without a card yet counts on its mean alone. Fetched here instead of being
    carried inside `rows`, so the metadata phase — the slow part, full of LLM calls —
    never holds them."""
    means, cards = db.embeddings_by_id(), db.card_vectors_by_id()

    def unit(vector) -> np.ndarray:
        vector = np.asarray(vector, dtype=np.float32)
        return vector / (np.linalg.norm(vector) + 1e-9)

    matrix = np.array(
        [unit(means[row["id"]]) + unit(cards.get(row["id"], means[row["id"]])) for row in rows]
    )
    return matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9)


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
    """Repeatedly merge the two clusters whose members are most alike on average
    (average linkage) until that average cosine drops below the threshold. Returns
    member-index lists. A merged cluster's similarity to any other is the
    size-weighted mean of its two parts' (Lance–Williams), so each merge updates one
    row instead of recomputing all pairs."""
    unit = vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-9)
    sim = unit @ unit.T
    np.fill_diagonal(sim, -np.inf)
    sizes = np.ones(len(vectors))
    members = [[i] for i in range(len(vectors))]
    while len(vectors) > 1:
        i, j = divmod(int(sim.argmax()), sim.shape[1])
        if sim[i, j] < threshold:
            break
        merged = (sizes[i] * sim[i] + sizes[j] * sim[j]) / (sizes[i] + sizes[j])
        sim[i], sim[:, i] = merged, merged
        sim[j], sim[:, j] = -np.inf, -np.inf
        sim[i, i] = -np.inf
        sizes[i] += sizes[j]
        members[i] += members[j]
        members[j] = []
    return [m for m in members if m]


def _pca3(vectors: np.ndarray) -> np.ndarray:
    centered = vectors - vectors.mean(axis=0)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    coords = centered @ vt[: min(3, vt.shape[0])].T
    if coords.shape[1] < 3:  # pad missing axes when there are very few rows
        coords = np.pad(coords, ((0, 0), (0, 3 - coords.shape[1])))
    return coords


def refresh() -> dict:
    """Index new passages and average them into mean vectors, regenerate the metadata
    of conversations that drifted, then regroup — cluster, name the tree, place on
    the map — when anything drifted or the clusters came out different from the
    stored ones. With neither, groups stay as they are and no LLM is called."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set (list/search still work without it)")

    # ahead of the drift gate on purpose: new messages must become searchable even
    # when the conversation's overall subject hasn't moved. _refresh_cards also runs
    # here so a card dropped by a run that failed later is repaired next time.
    chunked_count = _refresh_body_chunks() + _refresh_cards()
    # mean vectors are averaged from those passages, so they come second
    embedded_count = _refresh_means()

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

    regenerated = _refresh_metadata(rows)
    mapping = {}
    if regenerated:
        mapping = _consolidate_tags()
        # cards dropped by metadata regeneration and by the tag merge are rebuilt
        # here, once both are settled, so each card matches the text finally stored
        chunked_count += _refresh_cards()

    # clustering is cheap math and runs every time; only a changed outcome is named.
    # A naming call that failed leaves the stored clusters behind, so the next
    # refresh sees the difference and tries again.
    matrix = _center_by_language(_load_matrix(rows), rows)
    small_clusters = _agglomerate(matrix, T_SMALL)
    stored: dict[int, set] = {}
    for row in rows:
        if row["cluster_small"] is not None:
            stored.setdefault(row["cluster_small"], set()).add(row["id"])
    same_clusters = {frozenset(ids) for ids in stored.values()} == {
        frozenset(rows[m]["id"] for m in members) for members in small_clusters
    }
    if not regenerated and same_clusters:
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

    names = _name_tree(rows, small_clusters)
    big_ids = {big: i for i, big in enumerate(sorted({big for _, big in names}))}
    coords = _pca3(matrix)

    updates = []
    for ci, members in enumerate(small_clusters):
        small_label, big_label = names[ci]
        for m in members:
            updates.append(
                {
                    "id": rows[m]["id"],
                    "cluster_small": ci,
                    "cluster_small_label": small_label,
                    "cluster_big": big_ids[big_label],
                    "cluster_big_label": big_label,
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
        "total": len(rows),
        "clusters_small": len(small_clusters),
        "clusters_big": len(big_ids),
    }
