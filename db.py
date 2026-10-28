# Conversation storage and derived-data queries — Postgres (requires DATABASE_URL)

import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

EMBED_DIMENSIONS = 1536  # text-embedding-3-small

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        conversation_key TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT '',
        tags TEXT[] NOT NULL DEFAULT '{}',
        messages JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS project TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS embedding REAL[]",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS cluster_small INT",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS cluster_small_label TEXT",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS cluster_big INT",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS cluster_big_label TEXT",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS px REAL",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS py REAL",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pz REAL",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS embedded_message_count INT NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS embedded_vector_count INT NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS tagged_embedding REAL[]",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS source_url TEXT NOT NULL DEFAULT ''",
    # embedding_vec mirrors `embedding` in pgvector's own type so semantic search can
    # use the HNSW index; `embedding` stays the canonical column the pipeline reads.
    "CREATE EXTENSION IF NOT EXISTS vector",
    f"ALTER TABLE conversations ADD COLUMN IF NOT EXISTS embedding_vec vector({EMBED_DIMENSIONS})",
    """
    CREATE INDEX IF NOT EXISTS conversations_embedding_hnsw
        ON conversations USING hnsw (embedding_vec vector_cosine_ops)
    """,
    # Search index. A conversation's average vector answers "what is this about",
    # which is what clustering needs but too blurry to search: measured 48% top-1.
    # Scoring a conversation by its best-matching passage instead gives 68%, so
    # search runs against these — body passages plus one title+summary card.
    f"""
    CREATE TABLE IF NOT EXISTS conversation_chunks (
        conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        chunk_index INT NOT NULL,
        vec vector({EMBED_DIMENSIONS}) NOT NULL,
        PRIMARY KEY (conversation_id, kind, chunk_index)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS conversation_chunks_hnsw
        ON conversation_chunks USING hnsw (vec vector_cosine_ops)
    """,
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS chunked_message_count INT NOT NULL DEFAULT 0",
    # Where each body passage sits in its conversation, so a search hit can say which
    # messages to read. NULL for cards, and for passages indexed before these existed
    # until their conversation is indexed again.
    "ALTER TABLE conversation_chunks ADD COLUMN IF NOT EXISTS first_message INT",
    "ALTER TABLE conversation_chunks ADD COLUMN IF NOT EXISTS last_message INT",
    # Which passage rule (grouping.CHUNK_VERSION) a conversation's passages were cut by.
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS chunk_version INT NOT NULL DEFAULT 1",
    # One row per message, replacing the `messages` array: appending a turn then writes
    # a few rows instead of rewriting the whole array (2.7 MB, ~2 s for the largest
    # conversation). Filled alongside the array until reads switch over.
    """
    CREATE TABLE IF NOT EXISTS messages (
        conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        idx INT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        at TIMESTAMPTZ,
        PRIMARY KEY (conversation_id, idx)
    )
    """,
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS message_count INT",
    # What assistants searched for and which messages they then read — the only way
    # to see whether they follow a search hit through or stop at the first window.
    """
    CREATE TABLE IF NOT EXISTS recall_log (
        id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        at TIMESTAMPTZ NOT NULL DEFAULT now(),
        tool TEXT NOT NULL,
        args JSONB NOT NULL,
        result JSONB NOT NULL
    )
    """,
]

# Ask the model to request the conversation URL when it's still missing and the
# message count hits this (== 1 mod 40, i.e. round-trips 1, 21, 41, …).
URL_PROMPT_EVERY = 40

_schema_ready = False


def _connect() -> psycopg.Connection:
    global _schema_ready
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    if not _schema_ready:
        for statement in _SCHEMA:
            conn.execute(statement)
        conn.commit()
        _schema_ready = True
    return conn


def _isoformat(row: dict) -> dict:
    for field in ("created_at", "updated_at"):
        if field in row and row[field] is not None:
            row[field] = row[field].isoformat()
    return row


# How many of the last stored messages must match the incoming list, position by
# position, before only the new tail is written. A message removed or inserted
# earlier shifts these positions too, so a match means nothing before them changed.
# Reading them costs 10–70 KB, against rewriting up to 2.7 MB.
TAIL_CHECK = 20


def _sync_messages(conn, conversation_id: int, messages: list[dict], replace: bool) -> None:
    """Make the messages table hold exactly `messages` for one conversation.

    Normally the stored rows are a prefix of `messages` and only the tail is inserted.
    When the last TAIL_CHECK stored rows differ from the same positions (the
    conversation was rewritten), or `replace` is set, every row is rewritten. A content
    change further back that keeps the count goes unnoticed — which is why an explicit
    repair passes `replace`."""
    stored = conn.execute(
        "SELECT idx, role, content FROM messages WHERE conversation_id = %s"
        " ORDER BY idx DESC LIMIT %s",
        (conversation_id, TAIL_CHECK),
    ).fetchall()
    kept = stored[0]["idx"] + 1 if stored else 0
    unchanged = kept <= len(messages) and all(
        (messages[r["idx"]].get("role"), messages[r["idx"]].get("content"))
        == (r["role"], r["content"])
        for r in stored
    )
    if replace or not unchanged:
        conn.execute("DELETE FROM messages WHERE conversation_id = %s", (conversation_id,))
        kept = 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO messages (conversation_id, idx, role, content) VALUES (%s, %s, %s, %s)",
            [
                (conversation_id, i, m.get("role", ""), m.get("content", ""))
                for i, m in enumerate(messages[kept:], start=kept)
            ],
        )
    conn.execute(
        "UPDATE conversations SET message_count = %s WHERE id = %s",
        (len(messages), conversation_id),
    )


def save_conversation(
    key: str,
    messages: list[dict],
    source: str,
    project: str = "",
    source_url: str = "",
    replace: bool = False,
) -> dict:
    """Create a conversation, or overwrite it wholesale (repair). Title, tags and
    summary are filled in by server-side derivation.

    Derived artifacts (embedding, anchor) are reset only when the message count
    shrinks — i.e. the content was genuinely rewritten. A re-backfill or a normal
    growth keeps them, so incremental embedding picks up from where it left off
    (an overwrite that keeps or grows the count doesn't waste a re-embed).

    In the messages table, `replace` rewrites every row (an explicit repair);
    otherwise only the new tail is written (see _sync_messages)."""
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO conversations (conversation_key, title, messages, source, project, source_url)
            VALUES (%s, '', %s, %s, %s, %s)
            ON CONFLICT (conversation_key) DO UPDATE
                SET messages = EXCLUDED.messages,
                    source = EXCLUDED.source,
                    project = CASE WHEN EXCLUDED.project <> '' THEN EXCLUDED.project
                                   ELSE conversations.project END,
                    source_url = CASE WHEN EXCLUDED.source_url <> '' THEN EXCLUDED.source_url
                                      ELSE conversations.source_url END,
                    embedding = CASE WHEN jsonb_array_length(EXCLUDED.messages)
                                          < conversations.embedded_message_count
                                     THEN NULL ELSE conversations.embedding END,
                    embedding_vec = CASE WHEN jsonb_array_length(EXCLUDED.messages)
                                              < conversations.embedded_message_count
                                         THEN NULL ELSE conversations.embedding_vec END,
                    embedded_message_count = CASE WHEN jsonb_array_length(EXCLUDED.messages)
                                                       < conversations.embedded_message_count
                                                  THEN 0 ELSE conversations.embedded_message_count END,
                    embedded_vector_count = CASE WHEN jsonb_array_length(EXCLUDED.messages)
                                                      < conversations.embedded_message_count
                                                 THEN 0 ELSE conversations.embedded_vector_count END,
                    tagged_embedding = CASE WHEN jsonb_array_length(EXCLUDED.messages)
                                                 < conversations.embedded_message_count
                                            THEN NULL ELSE conversations.tagged_embedding END,
                    updated_at = now()
            RETURNING id, (created_at = updated_at) AS is_new
            """,
            (key, Jsonb(messages), source, project, source_url),
        ).fetchone()
        _sync_messages(conn, row["id"], messages, replace)
    return {
        "conversation_key": key,
        "message_count": len(messages),
        "saved_as": "created" if row["is_new"] else "overwritten",
    }


def append_messages(
    key: str, new_messages: list[dict], project: str = "", source_url: str = ""
) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, messages, source_url FROM conversations WHERE conversation_key = %s FOR UPDATE",
            (key,),
        ).fetchone()
        if row is None:
            return None
        current = row["messages"]
        n = len(new_messages)
        # duplicate guard: ignore a turn identical to the current tail (retry safety)
        if n == 0 or current[-n:] == new_messages:
            return {
                "conversation_key": key,
                "appended": 0,
                "total_messages": len(current),
                "note": "duplicate turn — skipped",
            }
        updated = conn.execute(
            """
            UPDATE conversations
            SET messages = messages || %s,
                project = CASE WHEN %s <> '' THEN %s ELSE project END,
                source_url = CASE WHEN %s <> '' THEN %s ELSE source_url END,
                updated_at = now()
            WHERE conversation_key = %s
            RETURNING jsonb_array_length(messages) AS total
            """,
            (Jsonb(new_messages), project, project, source_url, source_url, key),
        ).fetchone()
        _sync_messages(conn, row["id"], current + new_messages, replace=False)
    total = updated["total"]
    has_url = bool(row["source_url"] or source_url)
    result = {"conversation_key": key, "appended": n, "total_messages": total}
    # periodically nudge the model to ask the user for this conversation's URL
    if not has_url and total % URL_PROMPT_EVERY == 1:
        result["ask_for_url"] = True
    return result


def get_conversation(key: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT conversation_key, title, source, source_url, tags, project, summary,
                   messages, created_at, updated_at
            FROM conversations
            WHERE conversation_key = %s
            """,
            (key,),
        ).fetchone()
    return _isoformat(row) if row else None


_LIST_COLUMNS = """
    conversation_key, title, source, source_url, tags, project, summary,
    jsonb_array_length(messages) AS message_count,
    created_at, updated_at
"""


def list_conversations(limit: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_LIST_COLUMNS}
            FROM conversations
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    return [_isoformat(row) for row in rows]


def conversation_overview(key: str) -> dict | None:
    """Everything about one conversation except its messages, plus their count and
    total size — enough to decide how many messages to send before fetching any."""
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT {_LIST_COLUMNS}, char_length(messages::text) AS chars
            FROM conversations
            WHERE conversation_key = %s
            """,
            (key,),
        ).fetchone()
    return _isoformat(row) if row else None


def conversation_messages(key: str, start: int, stop: int) -> list[dict]:
    """messages[start:stop], sliced in the database so a long conversation never
    travels whole."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT coalesce(jsonb_agg(t.m ORDER BY t.i), '[]'::jsonb) AS messages
            FROM conversations c, jsonb_array_elements(c.messages) WITH ORDINALITY t(m, i)
            WHERE c.conversation_key = %s AND t.i > %s AND t.i <= %s
            """,
            (key, start, stop),
        ).fetchone()["messages"]


def log_recall(tool: str, args: dict, result: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO recall_log (tool, args, result) VALUES (%s, %s, %s)",
            (tool, Jsonb(args), Jsonb(result)),
        )


def messages_at(spots: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
    """The text of selected messages, as (conversation_key, index) -> content."""
    if not spots:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT s.key, s.idx, c.messages -> s.idx ->> 'content' AS content
            FROM unnest(%s::text[], %s::int[]) AS s(key, idx)
            JOIN conversations c ON c.conversation_key = s.key
            """,
            ([key for key, _ in spots], [idx for _, idx in spots]),
        ).fetchall()
    return {(r["key"], r["idx"]): r["content"] or "" for r in rows}


# Keyword hits at most this many messages apart belong to one section.
KEYWORD_SECTION_GAP = 20
EXCERPT_CHARS = 200
EXCERPTED_SECTIONS = 3  # only the newest few sections carry an excerpt, to keep results light


def _keyword_sections(positions: list[int]) -> list[dict]:
    """Group the messages containing a keyword into sections, newest first."""
    groups = [[positions[0]]]
    for p in positions[1:]:
        if p - groups[-1][-1] <= KEYWORD_SECTION_GAP:
            groups[-1].append(p)
        else:
            groups.append([p])
    return [
        {"start": g[0], "end": g[-1], "length": g[-1] - g[0] + 1, "hits": len(g), "best_message": g[-1]}
        for g in reversed(groups)
    ]


def _around(text: str, query: str) -> str:
    at = max(text.lower().find(query.lower()), 0)
    start = max(at - EXCERPT_CHARS // 3, 0)
    return text[start : start + EXCERPT_CHARS]


def search_conversations(query: str, limit: int) -> list[dict]:
    """Keyword search, most recently updated first. Each result also says where in
    the conversation the keyword occurs: every message containing it, grouped into
    sections, so an older stretch is listed even when a newer one exists."""
    pattern = f"%{query}%"
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_LIST_COLUMNS}
            FROM conversations
            WHERE title ILIKE %s OR summary ILIKE %s OR messages::text ILIKE %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (pattern, pattern, pattern, limit),
        ).fetchall()
        hits = conn.execute(
            """
            SELECT c.conversation_key AS key, t.i - 1 AS idx
            FROM conversations c, jsonb_array_elements(c.messages) WITH ORDINALITY t(m, i)
            WHERE c.conversation_key = ANY(%s) AND t.m->>'content' ILIKE %s
            ORDER BY 1, 2
            """,
            ([row["conversation_key"] for row in rows], pattern),
        ).fetchall()
    positions: dict[str, list[int]] = {}
    for hit in hits:
        positions.setdefault(hit["key"], []).append(hit["idx"])
    for row in rows:
        found = positions.get(row["conversation_key"], [])
        row["hits"] = len(found)
        row["sections"] = _keyword_sections(found) if found else []
    texts = messages_at(
        [
            (row["conversation_key"], section["best_message"])
            for row in rows
            for section in row["sections"][:EXCERPTED_SECTIONS]
        ]
    )
    for row in rows:
        for section in row["sections"][:EXCERPTED_SECTIONS]:
            text = texts.get((row["conversation_key"], section["best_message"]), "")
            section["excerpt"] = _around(text, query)
    return [_isoformat(row) for row in rows]


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(float(x)) for x in vector) + "]"


def semantic_search(vector: list[float], limit: int, min_similarity: float) -> list[dict]:
    """Rank conversations by meaning rather than spelling: pull the closest passages
    (HNSW does this), then score each conversation by its single best one. Each
    result keeps its body passages from that pull as `hits`, so the caller can tell
    where in the conversation the match sits.

    Vector search always returns *something*, so without a floor every query lists
    the whole archive. Anything below min_similarity is dropped rather than shown."""
    literal = _vector_literal(vector)
    with _connect() as conn:
        chunks = conn.execute(
            """
            SELECT conversation_id, chunk_index, first_message, last_message,
                   1 - (vec <=> %s::vector) AS similarity
            FROM conversation_chunks
            ORDER BY vec <=> %s::vector
            LIMIT %s
            """,
            (literal, literal, limit * 20),
        ).fetchall()
        best: dict[int, float] = {}
        for chunk in chunks:
            cid = chunk["conversation_id"]
            best[cid] = max(best.get(cid, 0.0), chunk["similarity"])
        ranked = [cid for cid in sorted(best, key=best.get, reverse=True) if best[cid] >= min_similarity]
        ranked = ranked[:limit]
        if not ranked:
            return []
        rows = conn.execute(
            f"SELECT id, {_LIST_COLUMNS} FROM conversations WHERE id = ANY(%s)", (ranked,)
        ).fetchall()
    by_id = {row.pop("id"): row for row in rows}
    results = []
    for cid in ranked:
        row = _isoformat(by_id[cid])
        row["similarity"] = best[cid]
        row["hits"] = [
            {k: chunk[k] for k in ("chunk_index", "first_message", "last_message", "similarity")}
            for chunk in chunks
            if chunk["conversation_id"] == cid and chunk["first_message"] is not None
        ]
        results.append(row)
    return results


# --- derivation pipeline queries (used by grouping.refresh) ---


def rows_needing_embedding() -> list[dict]:
    """Conversations with messages not yet folded into their embedding."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT id, messages, embedding, embedded_message_count, embedded_vector_count
            FROM conversations
            WHERE embedded_message_count < jsonb_array_length(messages)
            ORDER BY id
            """
        ).fetchall()


def set_embedding_state(
    row_id: int, mean: list[float] | None, message_count: int, vector_count: int
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE conversations
            SET embedding = %s::real[],
                embedding_vec = %s::real[]::vector,
                embedded_message_count = %s,
                embedded_vector_count = %s,
                embedded_at = now()
            WHERE id = %s
            """,
            (mean, mean, message_count, vector_count, row_id),
        )


def all_embedded(drift_min_cos: float) -> list[dict]:
    """Text fields of every embedded conversation plus a `drifted` flag.

    Drift is measured in Postgres so the vectors stay there — the metadata phase
    only needs to know whether each conversation moved, not by how much. Missing
    anchor counts as drifted (never tagged yet)."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT id, conversation_key, title, tags, summary,
                   cluster_small, cluster_small_label, cluster_big_label,
                   left(messages->0->>'content', 200) AS sample,
                   (tagged_embedding IS NULL
                    OR (embedding::vector <=> tagged_embedding::vector) > %s) AS drifted
            FROM conversations
            WHERE embedding IS NOT NULL
            ORDER BY id
            """,
            (1 - drift_min_cos,),
        ).fetchall()


def embeddings_by_id() -> dict[int, list[float]]:
    """Every embedding, keyed by row id — fetched only for clustering and the 3D
    projection, which are the two steps that genuinely need all vectors at once."""
    with _connect() as conn:
        return {
            row["id"]: row["embedding"]
            for row in conn.execute(
                "SELECT id, embedding FROM conversations WHERE embedding IS NOT NULL"
            ).fetchall()
        }


def rows_needing_chunks(version: int, key: str | None = None) -> list[dict]:
    """Conversations whose search passages are missing, behind the message list, cut
    by another rule than `version`, or still without positions — with how far each
    was indexed, enough to embed only what changed. Pass `key` to check a single
    conversation."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT id, messages, chunked_message_count, chunk_version,
                   (SELECT count(*) FROM conversation_chunks
                    WHERE conversation_id = conversations.id AND kind = 'body') AS body_chunks
            FROM conversations
            WHERE (chunked_message_count <> jsonb_array_length(messages)
                   OR chunk_version <> %(version)s
                   OR EXISTS (SELECT 1 FROM conversation_chunks
                              WHERE conversation_id = conversations.id AND kind = 'body'
                                AND first_message IS NULL))
              AND (%(key)s::text IS NULL OR conversation_key = %(key)s)
            ORDER BY id
            """,
            {"key": key, "version": version},
        ).fetchall()


def rows_needing_card() -> list[dict]:
    """Conversations that already have metadata but no card yet — covers both the
    first run over an existing archive and any conversation whose card was lost."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT id, title, tags, summary
            FROM conversations
            WHERE title <> ''
              AND NOT EXISTS (
                  SELECT 1 FROM conversation_chunks
                  WHERE conversation_id = conversations.id AND kind = 'card'
              )
            ORDER BY id
            """
        ).fetchall()


def replace_body_chunks(
    row_id: int,
    start: int,
    vectors: list[list[float]],
    positions: list[tuple[int, int]],
    message_count: int,
    indexed_count: int,
    version: int,
) -> bool:
    """Swap in body passages from chunk `start` on, keeping the ones before it.
    `positions` holds every passage's (first, last) message, so kept passages that
    predate the position columns get theirs too.

    The vectors were embedded outside any lock, from the state where `indexed_count`
    messages were indexed. If another run (a refresh, or the indexing after a save)
    has moved the counter since, nothing is written; whatever is still missing is
    picked up by the next save or refresh."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT chunked_message_count FROM conversations WHERE id = %s FOR UPDATE",
            (row_id,),
        ).fetchone()
        if row is None or row["chunked_message_count"] != indexed_count:
            return False
        conn.execute(
            "DELETE FROM conversation_chunks"
            " WHERE conversation_id = %s AND kind = 'body' AND chunk_index >= %s",
            (row_id, start),
        )
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO conversation_chunks"
                " (conversation_id, kind, chunk_index, vec, first_message, last_message)"
                " VALUES (%s, 'body', %s, %s::vector, %s, %s)",
                [
                    (row_id, start + i, _vector_literal(v), *positions[start + i])
                    for i, v in enumerate(vectors)
                ],
            )
        kept = positions[:start]
        conn.execute(
            """
            UPDATE conversation_chunks AS k SET first_message = p.first, last_message = p.last
            FROM unnest(%s::int[], %s::int[], %s::int[]) AS p(chunk_index, first, last)
            WHERE k.conversation_id = %s AND k.kind = 'body'
              AND k.chunk_index = p.chunk_index AND k.first_message IS NULL
            """,
            (list(range(len(kept))), [f for f, _ in kept], [l for _, l in kept], row_id),
        )
        conn.execute(
            "UPDATE conversations SET chunked_message_count = %s, chunk_version = %s"
            " WHERE id = %s",
            (message_count, version, row_id),
        )
    return True


def replace_card_chunk(row_id: int, vector: list[float]) -> None:
    """Swap in the title+summary+tags card, which searches better than body text
    for broad queries ('what was that embedded systems talk about')."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM conversation_chunks WHERE conversation_id = %s AND kind = 'card'",
            (row_id,),
        )
        conn.execute(
            "INSERT INTO conversation_chunks (conversation_id, kind, chunk_index, vec)"
            " VALUES (%s, 'card', 0, %s::vector)",
            (row_id, _vector_literal(vector)),
        )


def set_metadata(row_id: int, title: str, tags: list[str], summary: str) -> None:
    """Store derived title/tags/summary, move the anchor to the current vector, and
    drop the search card in the same transaction — it was built from the old text.
    A missing card is rebuilt by the next refresh; a silently stale one never would
    be, since the anchor has already moved and drift won't fire again."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE conversations
            SET title = %s, tags = %s, summary = %s, tagged_embedding = embedding
            WHERE id = %s
            """,
            (title, tags, summary, row_id),
        )
        conn.execute(
            "DELETE FROM conversation_chunks WHERE conversation_id = %s AND kind = 'card'",
            (row_id,),
        )


def messages_excerpt(row_id: int, segments: int = 20) -> list[dict]:
    """Sample for metadata generation: split the conversation into segments and take
    the longest message per role from each — half user, half assistant, covering
    the whole conversation in chronological order."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT messages FROM conversations WHERE id = %s", (row_id,)
        ).fetchone()
    messages = row["messages"]
    if len(messages) <= segments * 2:
        return messages
    indexed = list(enumerate(messages))
    size = len(messages) / segments
    picked: dict[int, dict] = {}
    for s in range(segments):
        chunk = indexed[int(s * size) : int((s + 1) * size)]
        for role in ("user", "assistant"):
            candidates = [(i, m) for i, m in chunk if m.get("role") == role]
            if candidates:
                i, m = max(candidates, key=lambda im: len(im[1].get("content", "")))
                picked[i] = m
    return [picked[i] for i in sorted(picked)]


def apply_tag_mapping(mapping: dict[str, str]) -> None:
    """Apply a tag merge map to every conversation (e.g. 'MCP server' -> 'MCP').

    Tags are part of the search card, so a conversation whose tags change loses its
    card here and gets a fresh one built later in the same refresh — this reaches
    conversations that never drifted and so never regenerate metadata."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, tags FROM conversations WHERE tags <> '{}'"
        ).fetchall()
        with conn.cursor() as cur:
            for row in rows:
                merged = []
                for tag in row["tags"]:
                    canon = mapping.get(tag, tag)
                    if canon not in merged:
                        merged.append(canon)
                if merged != row["tags"]:
                    cur.execute(
                        "UPDATE conversations SET tags = %s WHERE id = %s",
                        (merged, row["id"]),
                    )
                    cur.execute(
                        "DELETE FROM conversation_chunks"
                        " WHERE conversation_id = %s AND kind = 'card'",
                        (row["id"],),
                    )


def all_tags() -> list[str]:
    """Current tag vocabulary of the archive."""
    with _connect() as conn:
        return [
            r["tag"]
            for r in conn.execute(
                "SELECT DISTINCT unnest(tags) AS tag FROM conversations ORDER BY tag"
            ).fetchall()
        ]


def apply_grouping(rows: list[dict]) -> None:
    """rows: [{id, cluster_small, cluster_small_label, cluster_big, cluster_big_label, px, py, pz}]"""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                UPDATE conversations
                SET cluster_small = %(cluster_small)s,
                    cluster_small_label = %(cluster_small_label)s,
                    cluster_big = %(cluster_big)s,
                    cluster_big_label = %(cluster_big_label)s,
                    px = %(px)s, py = %(py)s, pz = %(pz)s
                WHERE id = %(id)s
                """,
                rows,
            )


def projection_rows() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT conversation_key, title, source, source_url, project, tags, updated_at,
                   cluster_small, cluster_small_label, cluster_big, cluster_big_label,
                   px, py, pz
            FROM conversations
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return [_isoformat(row) for row in rows]
