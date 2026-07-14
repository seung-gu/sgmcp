# Conversation storage and derived-data queries — Postgres (requires DATABASE_URL)

import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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
                embedded_message_count = %s,
                embedded_vector_count = %s,
                embedded_at = now()
            WHERE id = %s
            """,
            (mean, message_count, vector_count, row_id),
        )


def all_embedded() -> list[dict]:
    with _connect() as conn:
        return conn.execute(
            """
            SELECT id, conversation_key, title, tags, summary, embedding, tagged_embedding,
                   cluster_small, cluster_small_label, cluster_big_label,
                   left(messages->0->>'content', 200) AS sample
            FROM conversations
            WHERE embedding IS NOT NULL
            ORDER BY id
            """
        ).fetchall()


def set_metadata(
    row_id: int, title: str, tags: list[str], summary: str, anchor: list[float]
) -> None:
    """Store derived title/tags/summary and move the anchor to the current vector."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE conversations
            SET title = %s, tags = %s, summary = %s, tagged_embedding = %s::real[]
            WHERE id = %s
            """,
            (title, tags, summary, anchor, row_id),
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
    """Apply a tag merge map to every conversation (e.g. 'MCP server' -> 'MCP')."""
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
