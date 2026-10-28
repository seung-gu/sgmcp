# sgmcp — personal conversation-archive MCP server

import logging
import os
import uuid
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

import dashboard
import db
import grouping
import ingest

mcp = FastMCP(
    "sgmcp",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8000)),
    stateless_http=True,
)


# No page of messages exceeds this many characters, and a conversation under it comes
# back whole with full=true: well inside Claude Code's 25k-token tool-output limit,
# which a 300k-character dump overflowed. Raise it if that limit goes up.
PAGE_CHARS = 20_000


class Message(BaseModel):
    role: str = Field(description="speaker: user or assistant")
    content: str = Field(description="message content, verbatim")


@mcp.tool()
def save_conversation(
    messages: Annotated[
        list[Message], Field(description="the conversation so far, in order, verbatim")
    ],
    source: Annotated[
        str, Field(description="client the conversation happened in, e.g. claude.ai, claude-code, chatgpt")
    ] = "",
    project: Annotated[str, Field(description="project this conversation belongs to, if any")] = "",
    conversation_key: Annotated[
        str,
        Field(
            description="Leave empty — the server issues a key and returns it. Only pass a key "
            "when overwriting an existing conversation (repair). Never invent keys."
        ),
    ] = "",
) -> dict:
    """Creates a conversation (call once when a meaningful conversation starts), or
    overwrites an existing one wholesale (repair). Use the conversation_key from the
    response for all later append_messages calls. Title, tags and summary are
    generated server-side — do not send them."""
    key = conversation_key or f"cw-{uuid.uuid4()}"
    # a key means an explicit repair, which may change any message: rewrite them all
    result = db.save_conversation(
        key, [m.model_dump() for m in messages], source, project, replace=bool(conversation_key)
    )
    grouping.index_soon(key)
    return result


@mcp.tool()
def append_messages(
    conversation_key: Annotated[
        str,
        Field(description="key of the conversation to append to (issued by save_conversation)"),
    ],
    new_messages: Annotated[
        list[Message],
        Field(
            description="the messages not saved yet, in order: normally the latest turn, "
            "plus any earlier turn whose save was skipped",
            min_length=1,
        ),
    ],
    project: Annotated[
        str, Field(description="project this conversation belongs to (omit to keep current)")
    ] = "",
    source_url: Annotated[
        str,
        Field(
            description="URL of this conversation, if the user provides one (e.g. the "
            "claude.ai/chat/... address). Enables reopening it from the dashboard. "
            "Omit to keep current; only send a user-supplied URL, never invent one"
        ),
    ] = "",
) -> dict:
    """Appends the messages not saved yet to an existing conversation. Use this for
    per-turn saving. Messages that repeat the end of what is already saved (a retry, or
    a turn sent again) are stored only once (duplicate guard).

    If the response contains "ask_for_url": true, ask the user once to paste this
    conversation's URL from their address bar, then pass it as source_url next time."""
    result = db.append_messages(
        conversation_key, [m.model_dump() for m in new_messages], project, source_url
    )
    if result is None:
        raise ValueError(
            f"'{conversation_key}' does not exist. If this is a new conversation, call "
            "save_conversation with an empty key. If you were continuing an existing one, "
            "call list_conversations to find the most recent conversation and use its "
            "conversation_key. Never invent keys."
        )
    grouping.index_soon(conversation_key)
    return result


@mcp.tool()
def get_conversation(
    conversation_key: Annotated[str, Field(description="key of the conversation to load")],
    full: Annotated[
        bool,
        Field(
            description="false: summary-centric, lightweight. true: include the most recent "
            "verbatim messages — only when continuing the conversation or quoting exactly. "
            "Not needed with start or count, which include messages on their own"
        ),
    ] = False,
    start: Annotated[
        int | None,
        Field(
            description="index of the first message to return (0-based) — e.g. a few "
            "messages before a search result's best_message. Omit to read backwards from "
            "the most recent message",
            ge=0,
        ),
    ] = None,
    count: Annotated[
        int | None,
        Field(description="at most this many messages; omit to fill a page", ge=1),
    ] = None,
) -> dict:
    """Loads one stored conversation. On its own it gives the summary. full=true, `start`
    or `count` adds the verbatim messages one page at a time — about 20,000 characters,
    from `start` forward or, without `start`, the most recent ones — so a short
    conversation comes back whole.
    `showing` is the range returned and `message_count` the total; continue with
    `next_start` or `prev_start`. A single message longer than a page comes back cut,
    marked `truncated`. Each message carries `at`, when it was sent (null where the
    archive never learned it)."""
    conversation = db.conversation_overview(conversation_key)
    if conversation is None:
        raise ValueError(
            f"'{conversation_key}' not found. Check the key with list_conversations."
        )
    total = conversation["message_count"]
    if full or start is not None or count is not None or not conversation.get("summary"):
        page = db.conversation_page(conversation_key, PAGE_CHARS, start, count)
        if len(page) == 1 and len(page[0]["content"]) > PAGE_CHARS:  # over budget only when alone
            page[0] = {**page[0], "content": page[0]["content"][:PAGE_CHARS], "truncated": True}
        lo = page[0]["idx"] if page else min(start or 0, total)
        hi = page[-1]["idx"] + 1 if page else lo
        conversation["messages"] = [{k: v for k, v in m.items() if k != "idx"} for m in page]
        conversation["showing"] = {"start": lo, "end": hi - 1} if page else None
        conversation["next_start"] = hi if hi < total else None
        conversation["prev_start"] = max(lo - max(hi - lo, 1), 0) if lo > 0 else None
    _log_recall(
        "get_conversation",
        {"conversation_key": conversation_key, "full": full, "start": start, "count": count},
        {"message_count": total, "showing": conversation.get("showing")},
    )
    return conversation


@mcp.tool()
def list_conversations(
    limit: Annotated[int, Field(description="maximum number to return", ge=1, le=100)] = 20,
) -> list[dict]:
    """Lists stored conversations, most recently updated first (message bodies excluded)."""
    return db.list_conversations(limit)


@mcp.tool()
def search_conversations(
    query: Annotated[str, Field(description="keyword to search in titles, summaries and content")],
    limit: Annotated[int, Field(description="maximum number to return", ge=1, le=50)] = 10,
) -> list[dict]:
    """Searches stored conversations by keyword (title + summary + content, case-insensitive).
    For a topic whose exact wording you don't know, use search_conversations_by_meaning.
    Each result counts the messages containing the keyword (`hits`) and groups them into
    `sections` (message ranges, newest first; `best_message` is the latest hit), the
    newest few with an `excerpt` around the keyword, each with the time of its first and
    last message (`start_at`, `end_at`). Older sections are listed too —
    check them when the newest one does not answer. Read a section with
    get_conversation, passing `start` a few messages before `best_message`."""
    results = db.search_conversations(query, limit)
    _log_recall(
        "search_conversations",
        {"query": query, "limit": limit},
        {"results": [_logged(r, ("hits",)) for r in results]},
    )
    return results


@mcp.tool()
def search_conversations_by_meaning(
    query: Annotated[
        str, Field(description="what you are looking for, as a phrase or sentence; any language")
    ],
    limit: Annotated[int, Field(description="maximum number to return", ge=1, le=50)] = 10,
) -> list[dict]:
    """Searches stored conversations by meaning rather than spelling, closest first.
    Use when you know the topic but not the exact words; for an exact name or term,
    use search_conversations. New messages become searchable a few seconds after
    they are saved.

    Each result lists `sections`: stretches of the conversation where the topic is
    discussed (`start`–`end`, `length` messages), with the best-matching message
    (`best_message`), an `excerpt` of it, and when the stretch ran (`start_at`,
    `end_at`). To read one, call get_conversation with
    `start` a few messages before `best_message`. If a section is much longer than what
    you have read, the topic continues there: for a specific fact the area around
    `best_message` is usually enough; for what happened or what was decided, keep
    reading with `next_start` until the section's `end`. A result without sections
    matched on its title and summary as a whole."""
    try:
        results = grouping.semantic_search(query, limit)
    except Exception as e:  # missing API key, embedding call failed
        raise ValueError(
            f"Meaning search is unavailable ({e}). Use search_conversations instead."
        ) from e
    _log_recall(
        "search_conversations_by_meaning",
        {"query": query, "limit": limit},
        {"results": [_logged(r, ("similarity",)) for r in results]},
    )
    return results


def _logged(result: dict, fields: tuple[str, ...]) -> dict:
    """The part of a search result worth keeping in recall_log: which conversation,
    and which message ranges were offered."""
    return {
        "conversation_key": result["conversation_key"],
        **{f: result[f] for f in fields},
        "sections": [[s["start"], s["end"], s["best_message"]] for s in result["sections"]],
    }


def _log_recall(tool: str, args: dict, result: dict) -> None:
    """Best effort: a failed log line must never fail the recall itself."""
    try:
        db.log_recall(tool, args, result)
    except Exception:
        logging.getLogger(__name__).exception("recall_log write failed")


dashboard.register(mcp)
ingest.register(mcp)


if __name__ == "__main__":
    # remote deployment over HTTP — endpoint is /mcp
    mcp.run(transport="streamable-http")
