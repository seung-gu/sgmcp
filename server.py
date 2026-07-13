# MCP server for customized mcp (sgmcp)

import os
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

import client

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


if __name__ == "__main__":
    # 원격 배포용 HTTP transport — 엔드포인트는 /mcp
    mcp.run(transport="streamable-http")
