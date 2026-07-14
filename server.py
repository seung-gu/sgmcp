# MCP server for customized mcp (sgmcp)

import os
import uuid
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

import client
import dashboard
import db
import ingest

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
    messages: Annotated[list[Message], Field(description="지금까지의 대화 전체를 시간순으로, 원문 그대로")],
    source: Annotated[
        str, Field(description="대화가 이루어진 클라이언트. 예: claude.ai, claude-code, chatgpt")
    ] = "",
    project: Annotated[str, Field(description="이 대화가 속한 프로젝트 이름 (있는 경우)")] = "",
    conversation_key: Annotated[
        str,
        Field(
            description="비워두면 서버가 키를 발급해 응답으로 돌려줍니다. 새 대화는 반드시 비워두고,"
            " 기존 대화를 통째로 다시 저장(수리)할 때만 그 대화의 키를 넣으세요. 키를 지어내지 마세요."
        ),
    ] = "",
) -> dict:
    """대화 시작 시 최초 1회 생성용 (또는 기록이 꼬였을 때 전체를 덮어쓰는 수리용).
    응답의 conversation_key를 이후 append_messages에 사용하세요.
    제목·태그·요약은 서버가 자동 생성하므로 보내지 않습니다."""
    key = conversation_key or f"cw-{uuid.uuid4()}"
    return db.save_conversation(key, [m.model_dump() for m in messages], source, project)


@mcp.tool()
def append_messages(
    conversation_key: Annotated[
        str,
        Field(description="덧붙일 대상 대화의 식별 키 (save_conversation으로 먼저 생성돼 있어야 함)"),
    ],
    new_messages: Annotated[
        list[Message],
        Field(
            description="직전 턴의 새 메시지들만. 이미 저장된 메시지는 다시 보내지 말 것",
            min_length=1,
        ),
    ],
    project: Annotated[
        str, Field(description="이 대화가 속한 프로젝트 이름 (생략 시 기존 유지)")
    ] = "",
) -> dict:
    """직전 턴의 메시지만 기존 대화 뒤에 덧붙입니다. 매 턴 저장은 이 툴을 쓰세요.
    같은 내용을 연속으로 두 번 보내면 자동으로 무시됩니다(중복 방어)."""
    result = db.append_messages(conversation_key, [m.model_dump() for m in new_messages], project)
    if result is None:
        raise ValueError(
            f"'{conversation_key}'는 존재하지 않는 키입니다. 새 대화라면 save_conversation을"
            " (키를 비워서) 호출하고, 기존 대화를 이어가던 중이라면 list_conversations로"
            " 최근 대화를 조회해 그 conversation_key를 사용하세요. 키를 지어내지 마세요."
        )
    return result


@mcp.tool()
def get_conversation(
    conversation_key: Annotated[str, Field(description="불러올 대화의 식별 키")],
    full: Annotated[
        bool,
        Field(
            description="false면 요약 중심으로 가볍게, true면 메시지 원문 전체 포함."
            " 대화를 이어가거나 정확한 인용이 필요할 때만 true 사용"
        ),
    ] = False,
) -> dict:
    """저장된 대화 하나를 불러옵니다."""
    conversation = db.get_conversation(conversation_key)
    if conversation is None:
        raise ValueError(
            f"'{conversation_key}' 대화를 찾을 수 없습니다. list_conversations로 키를 확인하세요."
        )
    if not full and conversation.get("summary"):
        conversation.pop("messages")
    return conversation


@mcp.tool()
def list_conversations(
    limit: Annotated[int, Field(description="가져올 최대 개수", ge=1, le=100)] = 20,
) -> list[dict]:
    """저장된 대화 목록을 최근 갱신순으로 보여줍니다 (메시지 본문 제외)."""
    return db.list_conversations(limit)


@mcp.tool()
def search_conversations(
    query: Annotated[str, Field(description="제목·요약·대화 내용에서 찾을 검색어")],
    limit: Annotated[int, Field(description="가져올 최대 개수", ge=1, le=50)] = 10,
) -> list[dict]:
    """저장된 대화를 키워드로 검색합니다 (제목 + 요약 + 본문 대상, 대소문자 무시)."""
    return db.search_conversations(query, limit)


dashboard.register(mcp)
ingest.register(mcp)


if __name__ == "__main__":
    # 원격 배포용 HTTP transport — 엔드포인트는 /mcp
    mcp.run(transport="streamable-http")
