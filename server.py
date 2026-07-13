# MCP server for customized mcp (sgmcp)

import os
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

import client
import db

mcp = FastMCP(
    "sgmcp",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8000)),
    stateless_http=True,
)


@mcp.tool()
def greet(
    name: Annotated[str, Field(description="인사할 대상의 이름")],
) -> str:
    """이름을 받아 인사말을 돌려줍니다."""
    return client.greet(name)


@mcp.tool()
def add(
    a: Annotated[float, Field(description="첫 번째 수")],
    b: Annotated[float, Field(description="두 번째 수")],
) -> float:
    """두 수를 더합니다."""
    return client.add(a, b)


class Message(BaseModel):
    role: str = Field(description="메시지 화자: user 또는 assistant")
    content: str = Field(description="메시지 내용 (원문 그대로)")


@mcp.tool()
def save_conversation(
    conversation_key: Annotated[
        str,
        Field(
            description="대화 식별 키 (kebab-case). 같은 키로 다시 저장하면 기존 레코드를 갱신하므로,"
            " 하나의 대화에서는 처음 정한 키를 계속 사용할 것. 예: '2026-07-13-uv-migration'"
        ),
    ],
    title: Annotated[str, Field(description="대화 제목 (한 줄 요약)")],
    messages: Annotated[list[Message], Field(description="지금까지의 대화 전체를 시간순으로")],
    source: Annotated[
        str, Field(description="대화가 이루어진 클라이언트. 예: claude.ai, claude-code, chatgpt")
    ] = "",
    tags: Annotated[list[str], Field(description="나중에 찾기 쉽게 붙이는 태그 목록")] = [],
) -> dict:
    """대화를 DB에 저장합니다. 같은 conversation_key로 다시 저장하면 덮어씁니다(업서트)."""
    return db.save_conversation(
        conversation_key, title, [m.model_dump() for m in messages], source, tags
    )


@mcp.tool()
def get_conversation(
    conversation_key: Annotated[str, Field(description="불러올 대화의 식별 키")],
) -> dict:
    """저장된 대화 하나를 메시지 전체와 함께 불러옵니다."""
    conversation = db.get_conversation(conversation_key)
    if conversation is None:
        raise ValueError(f"'{conversation_key}' 대화를 찾을 수 없습니다. list_conversations로 키를 확인하세요.")
    return conversation


@mcp.tool()
def list_conversations(
    limit: Annotated[int, Field(description="가져올 최대 개수", ge=1, le=100)] = 20,
) -> list[dict]:
    """저장된 대화 목록을 최근 갱신순으로 보여줍니다 (메시지 본문 제외)."""
    return db.list_conversations(limit)


@mcp.tool()
def search_conversations(
    query: Annotated[str, Field(description="제목과 대화 내용에서 찾을 검색어")],
    limit: Annotated[int, Field(description="가져올 최대 개수", ge=1, le=50)] = 10,
) -> list[dict]:
    """저장된 대화를 키워드로 검색합니다 (제목 + 본문 대상, 대소문자 무시)."""
    return db.search_conversations(query, limit)


if __name__ == "__main__":
    # 원격 배포용 HTTP transport — 엔드포인트는 /mcp
    mcp.run(transport="streamable-http")
