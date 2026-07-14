# 외부 프로그램(Claude Code hook 등)이 대화를 밀어넣는 쓰기 전용 문

import os
import secrets

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

import db


def _authorized(request: Request) -> bool:
    expected = os.environ.get("INGEST_TOKEN", "")
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return bool(expected) and secrets.compare_digest(given, expected)


def register(mcp) -> None:
    """FastMCP 앱에 ingest 라우트를 등록한다."""

    @mcp.custom_route("/api/ingest", methods=["POST"])
    async def api_ingest(request: Request):
        if not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=403)
        body = await request.json()
        try:
            result = await run_in_threadpool(
                db.save_conversation,
                body["conversation_key"],
                body["title"],
                body["messages"],
                body.get("source", ""),
                body.get("tags", []),
                body.get("project", ""),
                body.get("summary", ""),
            )
        except KeyError as e:
            return JSONResponse({"error": f"필수 필드 누락: {e}"}, status_code=400)
        return JSONResponse(result)
