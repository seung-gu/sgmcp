# 대화 저장/로드 로직 — Postgres (DATABASE_URL 환경변수 필요)

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
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS embedded_message_count INT NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS embedded_vector_count INT NOT NULL DEFAULT 0",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS cluster_small INT",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS cluster_small_label TEXT",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS cluster_big INT",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS cluster_big_label TEXT",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS px REAL",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS py REAL",
    "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS pz REAL",
]

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
    title: str,
    messages: list[dict],
    source: str,
    tags: list[str],
    project: str = "",
    summary: str = "",
) -> dict:
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO conversations (conversation_key, title, messages, source, tags, project, summary)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (conversation_key) DO UPDATE
                SET title = EXCLUDED.title,
                    messages = EXCLUDED.messages,
                    source = EXCLUDED.source,
                    tags = EXCLUDED.tags,
                    project = CASE WHEN EXCLUDED.project <> '' THEN EXCLUDED.project
                                   ELSE conversations.project END,
                    summary = CASE WHEN EXCLUDED.summary <> '' THEN EXCLUDED.summary
                                   ELSE conversations.summary END,
                    updated_at = now()
            RETURNING id, (created_at = updated_at) AS is_new
            """,
            (key, title, Jsonb(messages), source, tags, project, summary),
        ).fetchone()
    return {
        "conversation_key": key,
        "message_count": len(messages),
        "saved_as": "새 대화" if row["is_new"] else "기존 대화 갱신",
    }


def append_messages(
    key: str, new_messages: list[dict], summary: str = "", project: str = ""
) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT messages FROM conversations WHERE conversation_key = %s FOR UPDATE",
            (key,),
        ).fetchone()
        if row is None:
            return None
        current = row["messages"]
        n = len(new_messages)
        # 중복 방어: 직전에 붙인 것과 동일한 턴이면 쓰지 않는다 (재시도 대비)
        if n == 0 or current[-n:] == new_messages:
            return {
                "conversation_key": key,
                "appended": 0,
                "total_messages": len(current),
                "note": "이미 저장된 턴이라 건너뜀",
            }
        updated = conn.execute(
            """
            UPDATE conversations
            SET messages = messages || %s,
                summary = CASE WHEN %s <> '' THEN %s ELSE summary END,
                project = CASE WHEN %s <> '' THEN %s ELSE project END,
                updated_at = now()
            WHERE conversation_key = %s
            RETURNING jsonb_array_length(messages) AS total
            """,
            (Jsonb(new_messages), summary, summary, project, project, key),
        ).fetchone()
    return {
        "conversation_key": key,
        "appended": n,
        "total_messages": updated["total"],
    }


def get_conversation(key: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT conversation_key, title, source, tags, project, summary,
                   messages, created_at, updated_at
            FROM conversations
            WHERE conversation_key = %s
            """,
            (key,),
        ).fetchone()
    return _isoformat(row) if row else None


_LIST_COLUMNS = """
    conversation_key, title, source, tags, project, summary,
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


# --- 이하 대시보드/그룹화 파이프라인용 ---


def rows_needing_embedding() -> list[dict]:
    """아직 임베딩에 반영되지 않은 메시지가 있는 대화들."""
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
            SELECT id, conversation_key, title, tags, embedding
            FROM conversations
            WHERE embedding IS NOT NULL
            ORDER BY id
            """
        ).fetchall()


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
            SELECT conversation_key, title, project, tags, updated_at,
                   cluster_small, cluster_small_label, cluster_big, cluster_big_label,
                   px, py, pz
            FROM conversations
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return [_isoformat(row) for row in rows]
