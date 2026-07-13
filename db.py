# 대화 저장/로드 로직 — Postgres (DATABASE_URL 환경변수 필요)

import os

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

_SCHEMA = """
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
"""

_schema_ready = False


def _connect() -> psycopg.Connection:
    global _schema_ready
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)
    if not _schema_ready:
        conn.execute(_SCHEMA)
        conn.commit()
        _schema_ready = True
    return conn


def _isoformat(row: dict) -> dict:
    for field in ("created_at", "updated_at"):
        if field in row:
            row[field] = row[field].isoformat()
    return row


def save_conversation(
    key: str, title: str, messages: list[dict], source: str, tags: list[str]
) -> dict:
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO conversations (conversation_key, title, messages, source, tags)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (conversation_key) DO UPDATE
                SET title = EXCLUDED.title,
                    messages = EXCLUDED.messages,
                    source = EXCLUDED.source,
                    tags = EXCLUDED.tags,
                    updated_at = now()
            RETURNING id, (created_at = updated_at) AS is_new
            """,
            (key, title, Jsonb(messages), source, tags),
        ).fetchone()
    return {
        "conversation_key": key,
        "message_count": len(messages),
        "saved_as": "새 대화" if row["is_new"] else "기존 대화 갱신",
    }


def get_conversation(key: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT conversation_key, title, source, tags, messages, created_at, updated_at
            FROM conversations
            WHERE conversation_key = %s
            """,
            (key,),
        ).fetchone()
    return _isoformat(row) if row else None


def list_conversations(limit: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT conversation_key, title, source, tags,
                   jsonb_array_length(messages) AS message_count,
                   created_at, updated_at
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
            """
            SELECT conversation_key, title, source, tags,
                   jsonb_array_length(messages) AS message_count,
                   created_at, updated_at
            FROM conversations
            WHERE title ILIKE %s OR messages::text ILIKE %s
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (pattern, pattern, limit),
        ).fetchall()
    return [_isoformat(row) for row in rows]
