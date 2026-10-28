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


def save_conversation(
    key: str,
    messages: list[dict],
    source: str,
    project: str = "",
    source_url: str = "",
) -> dict:
    """Create a conversation, or overwrite it wholesale (repair). Title, tags and
    summary are filled in by server-side derivation.

    Derived artifacts (embedding, anchor) are reset only when the message count
    shrinks — i.e. the content was genuinely rewritten. A re-backfill or a normal
    growth keeps them, so incremental embedding picks up from where it left off
    (an overwrite that keeps or grows the count doesn't waste a re-embed)."""
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
            "SELECT messages, source_url FROM conversations WHERE conversation_key = %s FOR UPDATE",
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


def search_conversations(query: str, limit: int) -> list[dict]:
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
    return [_isoformat(row) for row in rows]


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(str(float(x)) for x in vector) + "]"


def semantic_search(vector: list[float], limit: int, min_similarity: float) -> list[dict]:
    """Rank conversations by meaning rather than spelling: pull the closest passages
    (HNSW does this), then score each conversation by its single best one.

    Vector search always returns *something*, so without a floor every query lists
    the whole archive. Anything below min_similarity is dropped rather than shown."""
    literal = _vector_literal(vector)
    with _connect() as conn:
        rows = conn.execute(
            f"""
            WITH nearest AS (
                SELECT conversation_id, min(distance) AS distance
                FROM (
                    SELECT conversation_id, vec <=> %s::vector AS distance
                    FROM conversation_chunks
                    ORDER BY vec <=> %s::vector
                    LIMIT %s
                ) close_chunks
                GROUP BY conversation_id
            )
            SELECT {_LIST_COLUMNS}, 1 - nearest.distance AS similarity
            FROM nearest
            JOIN conversations ON conversations.id = nearest.conversation_id
            WHERE nearest.distance <= %s
            ORDER BY nearest.distance
            LIMIT %s
            """,
            (literal, literal, limit * 20, 1 - min_similarity, limit),
        ).fetchall()
    return [_isoformat(row) for row in rows]


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


def rows_needing_chunks(key: str | None = None) -> list[dict]:
    """Conversations whose search passages are missing or behind the message list,
    with how far each was indexed — enough to embed only what changed. Pass `key`
    to check a single conversation."""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT id, messages, chunked_message_count,
                   (SELECT count(*) FROM conversation_chunks
                    WHERE conversation_id = conversations.id AND kind = 'body') AS body_chunks
            FROM conversations
            WHERE chunked_message_count <> jsonb_array_length(messages)
              AND (%(key)s::text IS NULL OR conversation_key = %(key)s)
            ORDER BY id
            """,
            {"key": key},
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
    row_id: int, start: int, vectors: list[list[float]], message_count: int, indexed_count: int
) -> bool:
    """Swap in body passages from chunk `start` on, keeping the ones before it.

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
                "INSERT INTO conversation_chunks (conversation_id, kind, chunk_index, vec)"
                " VALUES (%s, 'body', %s, %s::vector)",
                [(row_id, start + i, _vector_literal(v)) for i, v in enumerate(vectors)],
            )
        conn.execute(
            "UPDATE conversations SET chunked_message_count = %s WHERE id = %s",
            (message_count, row_id),
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
