# Dashboard web routes — served by the same process as MCP, kept in their own module

import os
import secrets
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

import db
import grouping

_STATIC_DIR = Path(__file__).parent / "static"


def _authorized(request: Request) -> bool:
    expected = os.environ.get("DASHBOARD_TOKEN", "")
    given = request.query_params.get("token", "")
    return bool(expected) and secrets.compare_digest(given, expected)


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=403)


def register(mcp) -> None:
    """Register the dashboard routes on the FastMCP app."""

    @mcp.custom_route("/", methods=["GET"])
    async def index(request: Request):
        if not _authorized(request):
            return _unauthorized()
        return HTMLResponse((_STATIC_DIR / "index.html").read_text())

    @mcp.custom_route("/api/conversations", methods=["GET"])
    async def api_conversations(request: Request):
        if not _authorized(request):
            return _unauthorized()
        return JSONResponse(await run_in_threadpool(db.list_conversations, 200))

    @mcp.custom_route("/api/search", methods=["GET"])
    async def api_search(request: Request):
        if not _authorized(request):
            return _unauthorized()
        query = request.query_params.get("q", "").strip()
        if not query:
            return JSONResponse([])
        return JSONResponse(await run_in_threadpool(db.search_conversations, query, 50))

    @mcp.custom_route("/api/conversation/{key}", methods=["GET"])
    async def api_conversation(request: Request):
        if not _authorized(request):
            return _unauthorized()
        row = await run_in_threadpool(db.get_conversation, request.path_params["key"])
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(row)

    @mcp.custom_route("/api/projection", methods=["GET"])
    async def api_projection(request: Request):
        if not _authorized(request):
            return _unauthorized()
        return JSONResponse(await run_in_threadpool(db.projection_rows))

    @mcp.custom_route("/api/refresh", methods=["POST"])
    async def api_refresh(request: Request):
        if not _authorized(request):
            return _unauthorized()
        try:
            stats = await run_in_threadpool(grouping.refresh)
        except RuntimeError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(stats)
