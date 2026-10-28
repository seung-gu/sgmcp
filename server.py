# sgmcp — personal conversation-archive MCP server

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
    result = db.save_conversation(key, [m.model_dump() for m in messages], source, project)
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
            description="only the latest turn's messages — do not resend messages already saved",
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
    """Appends the latest turn to an existing conversation. Use this for per-turn saving.
    Sending the same content twice in a row is ignored automatically (duplicate guard).

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
            description="false: summary-centric, lightweight. true: include the verbatim "
            "messages — only when continuing the conversation or quoting exactly"
        ),
    ] = False,
    last: Annotated[
        int,
        Field(
            description="how many of the most recent messages to return with them; 0 returns "
            "every message — use it only when the earlier part is really needed",
            ge=0,
        ),
    ] = 20,
) -> dict:
    """Loads one stored conversation. Messages come back trimmed to the most recent
    ones; message_count and messages_omitted say how much was left out."""
    conversation = db.get_conversation(conversation_key)
    if conversation is None:
        raise ValueError(
            f"'{conversation_key}' not found. Check the key with list_conversations."
        )
    messages = conversation["messages"]
    conversation["message_count"] = len(messages)
    if not full and conversation.get("summary"):
        conversation.pop("messages")
    elif last and len(messages) > last:
        conversation["messages"] = messages[-last:]
        conversation["messages_omitted"] = len(messages) - last
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
    For a topic whose exact wording you don't know, use search_conversations_by_meaning."""
    return db.search_conversations(query, limit)


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
    they are saved."""
    try:
        return grouping.semantic_search(query, limit)
    except Exception as e:  # missing API key, embedding call failed
        raise ValueError(
            f"Meaning search is unavailable ({e}). Use search_conversations instead."
        ) from e


dashboard.register(mcp)
ingest.register(mcp)


if __name__ == "__main__":
    # remote deployment over HTTP — endpoint is /mcp
    mcp.run(transport="streamable-http")
