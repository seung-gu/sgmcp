# Dashboard web routes — served by the same process as MCP, kept in their own module

import logging
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

import auth
import db
import grouping

_STATIC_DIR = Path(__file__).parent / "static"
# HttpOnly: page scripts can't read it; Secure: https only (local http excepted);
# SameSite=Lax: another site's POST doesn't carry it
_COOKIE = {"httponly": True, "secure": auth.PUBLIC_URL.startswith("https://"), "samesite": "lax"}


def _signed_in(request: Request) -> bool:
    return auth.session_valid(request.cookies.get("sgmcp_session", ""))


def _unauthorized() -> JSONResponse:
    return JSONResponse({"error": "unauthorized"}, status_code=403)


def register(mcp) -> None:
    """Register the dashboard routes, and the WorkOS sign-in in front of them, on the FastMCP app."""

    @mcp.custom_route("/auth/login", methods=["GET"])
    async def login(request: Request):
        # OAuth 2.1 sign-in: WorkOS signs the user in, then sends the browser to /auth/callback
        url, started = auth.start_sign_in()
        response = RedirectResponse(url)
        # state and the PKCE verifier wait in a sealed cookie until the browser comes back
        response.set_cookie("sgmcp_login", started, max_age=600, **_COOKIE)
        return response

    @mcp.custom_route("/auth/callback", methods=["GET"])
    async def callback(request: Request):
        try:
            session = await run_in_threadpool(
                auth.finish_sign_in,
                request.cookies.get("sgmcp_login", ""),
                request.query_params.get("code"),
                request.query_params.get("state"),
            )
        except auth.SignInError as e:
            return HTMLResponse(str(e), status_code=403)
        response = RedirectResponse("/dashboard")
        response.set_cookie("sgmcp_session", session, max_age=auth.SIGN_IN_HOURS * 3600, **_COOKIE)
        response.delete_cookie("sgmcp_login")
        return response

    @mcp.custom_route("/", methods=["GET"])
    async def root(request: Request):
        return RedirectResponse("/dashboard")  # the dashboard used to live here: keep old links working

    @mcp.custom_route("/dashboard", methods=["GET"])
    async def dashboard_page(request: Request):
        if not _signed_in(request):
            return RedirectResponse("/auth/login")
        return HTMLResponse((_STATIC_DIR / "index.html").read_text())

    @mcp.custom_route("/api/conversations", methods=["GET"])
    async def api_conversations(request: Request):
        if not _signed_in(request):
            return _unauthorized()
        return JSONResponse(await run_in_threadpool(db.list_conversations, 200))

    @mcp.custom_route("/api/search", methods=["GET"])
    async def api_search(request: Request):
        if not _signed_in(request):
            return _unauthorized()
        query = request.query_params.get("q", "").strip()
        if not query:
            return JSONResponse([])
        if request.query_params.get("mode") != "meaning":
            return JSONResponse(await run_in_threadpool(db.search_conversations, query, 50))
        try:
            return JSONResponse(await run_in_threadpool(grouping.semantic_search, query, 50))
        except Exception as e:  # missing API key, embedding call failed
            return JSONResponse({"error": f"semantic search failed: {e}"}, status_code=400)

    @mcp.custom_route("/api/conversation/{key}", methods=["GET"])
    async def api_conversation(request: Request):
        if not _signed_in(request):
            return _unauthorized()
        row = await run_in_threadpool(db.get_conversation, request.path_params["key"])
        if row is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(row)

    @mcp.custom_route("/api/projection", methods=["GET"])
    async def api_projection(request: Request):
        if not _signed_in(request):
            return _unauthorized()
        return JSONResponse(await run_in_threadpool(db.projection_rows))

    @mcp.custom_route("/api/refresh", methods=["POST"])
    async def api_refresh(request: Request):
        if not _signed_in(request):
            return _unauthorized()
        try:
            stats = await run_in_threadpool(grouping.refresh)
        except RuntimeError as e:  # no API key, or a model that kept failing its checks
            logging.getLogger(__name__).warning("refresh failed: %s", e)
            return JSONResponse({"error": str(e)}, status_code=400)
        except Exception as e:  # a deadlock, a failed model call: name it on the dashboard
            logging.getLogger(__name__).exception("refresh failed")
            return JSONResponse({"error": f"refresh failed: {type(e).__name__}: {e}"}, status_code=500)
        return JSONResponse(stats)
