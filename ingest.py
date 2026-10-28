# Write-only door for external programs (the Claude Code hook) to push conversations

import os
import secrets

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

import db
import grouping


def _authorized(request: Request) -> bool:
    expected = os.environ.get("INGEST_TOKEN", "")
    given = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    return bool(expected) and secrets.compare_digest(given, expected)


def register(mcp) -> None:
    """Register the ingest route on the FastMCP app."""

    @mcp.custom_route("/api/ingest", methods=["POST"])
    async def api_ingest(request: Request):
        if not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=403)
        body = await request.json()
        # `start` > 0: `messages` is only the session's end, beginning at that position
        start = body.get("start", 0)
        if not isinstance(start, int) or start < 0:
            return JSONResponse({"error": "start must be a non-negative integer"}, status_code=400)
        try:
            result = await run_in_threadpool(
                db.save_conversation,
                body["conversation_key"],
                body["messages"],
                body.get("source", ""),
                body.get("project", ""),
                body.get("source_url", ""),
                start=start,
            )
        except KeyError as e:
            return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
        if result is None:
            # the stored messages stop before that end, or differ from it
            return JSONResponse({"error": "send the whole session"}, status_code=409)
        grouping.index_soon(body["conversation_key"])
        return JSONResponse(result)
