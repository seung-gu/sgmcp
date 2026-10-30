import asyncio
import logging
import os
import time
from functools import cache
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
import jwt
from cryptography.fernet import Fernet, InvalidToken
from workos import WorkOSClient, WorkOSError
from workos.session import seal_data, unseal_data


AUTH_ISSUER = os.environ.get("MCP_AUTH_ISSUER")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000")
RESOURCE = f"{PUBLIC_URL}/mcp"   # this server's MCP address; a token's aud must equal it
AUTH_ORG = os.environ.get("MCP_AUTH_ORG")   # WorkOS organization (org_...) that holds the approved users


# --- /mcp: check the AuthKit access tokens MCP clients bring (this server as resource server) ---
# MCP authorization (OAuth 2.1):
# https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization
# https://daleseo.com/mcp-auth/

class AuthKitVerifier(TokenVerifier):
    """Accepts tokens signed by our AuthKit for this server, from members of AUTH_ORG only."""

    def __init__(self):
        # Built only when auth is on: without AUTH_ISSUER the URL is "None/...", which PyJWT rejects
        self.jwks = jwt.PyJWKClient(f"{AUTH_ISSUER}/oauth2/jwks")

    async def verify_token(self, token: str) -> AccessToken | None:
        # The bearer middleware hands over every route's bearer value, the ingest hook's INGEST_TOKEN
        # on /api/ingest included: a value that isn't a JWT at all is not ours to log
        if token.count(".") != 2:
            return None
        try:
            # pick the public key matching the header's kid; fetching the key set blocks, so off the event loop
            key = await asyncio.to_thread(self.jwks.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],                    # signature algorithm
                issuer=AUTH_ISSUER,                      # check iss
                audience=RESOURCE,                       # check aud
                options={"require": ["exp"]},            # PyJWT checks exp only when present: make it mandatory
            )
            # PyJWT has no option for org_id, so check it here: not in the organization = not approved yet
            if not approved(claims.get("org_id")):
                raise jwt.InvalidTokenError(f"not approved: {claims['sub']} has org_id {claims.get('org_id')}")
        except jwt.PyJWTError as e:
            # Every PyJWT error subclasses PyJWTError, so this catches all rejections: unknown kid,
            # bad signature, wrong iss/aud, missing aud/exp, expired, malformed, and not approved
            logging.getLogger(__name__).warning("token rejected: %s", e)
            return None
        logging.getLogger(__name__).info("token accepted: %s", claims["sub"])
        return AccessToken(token=token, client_id=claims["client_id"], scopes=[], expires_at=claims["exp"])


def approved(org_id: str | None) -> bool:
    """A member of AUTH_ORG. With AUTH_ORG unset nobody is, or None == None would let everyone in."""
    return bool(AUTH_ORG) and org_id == AUTH_ORG


def fastmcp_options() -> dict:
    """FastMCP's auth arguments; none when MCP_AUTH_ISSUER is unset, which leaves /mcp open."""
    if not AUTH_ISSUER:
        logging.getLogger(__name__).warning("MCP_AUTH_ISSUER not set: /mcp is open")
        return {}
    return {
        "token_verifier": AuthKitVerifier(),
        "auth": AuthSettings(issuer_url=AUTH_ISSUER, resource_server_url=RESOURCE),
    }


# --- Browser sign-in for the dashboard (this server as OAuth client): sign in through AuthKit,
# check the organization once, then keep our own sealed session ---
# OAuth 2.1 code flow: https://datatracker.ietf.org/doc/draft-ietf-oauth-v2-1/
# PKCE: https://www.rfc-editor.org/rfc/rfc7636

_COOKIE_PASSWORD = os.environ.get("WORKOS_COOKIE_PASSWORD", "")
SIGN_IN_HOURS = 12  # a member removed from the organization keeps access until this runs out

try:
    Fernet(_COOKIE_PASSWORD)
except ValueError:  # unset, or not a Fernet key: every sign-in and session check would answer 500
    logging.getLogger(__name__).warning("WORKOS_COOKIE_PASSWORD is not a Fernet key: dashboard sign-in fails")


class SignInError(Exception):
    """A sign-in that can't finish: none under way in this browser, not the one it started, or not approved."""


@cache
def _workos() -> WorkOSClient:
    # The client ID names our WorkOS application; the API key proves this server is that application
    return WorkOSClient(api_key=os.environ["WORKOS_API_KEY"], client_id=os.environ["WORKOS_CLIENT_ID"])


def start_sign_in() -> tuple[str, str]:
    """(the WorkOS sign-in URL, the sealed state and PKCE verifier to keep until the browser comes back)."""
    pkce = _workos().user_management.get_authorization_url_with_pkce(  # pkce: Proof Key for Code Exchange
        redirect_uri=f"{PUBLIC_URL}/auth/callback", provider="authkit"
    )
    started = {"state": pkce["state"], "code_verifier": pkce["code_verifier"]}
    return pkce["url"], seal_data(started, _COOKIE_PASSWORD)


def finish_sign_in(started_sealed: str, code: str | None, state: str | None) -> str:
    """Trade the code WorkOS sent back for the user; return the sealed session to keep for SIGN_IN_HOURS."""
    try:
        started = unseal_data(started_sealed, _COOKIE_PASSWORD)  # unseal what start_sign_in kept
    except InvalidToken as e:
        raise SignInError("No sign-in under way in this browser.") from e
    # .get: a session cookie put in the login cookie's place unseals too, with no state in it
    if not code or not state or state != started.get("state"):
        raise SignInError("Not the sign-in this browser started.")
    try:
        result = _workos().user_management.authenticate_with_code_pkce(
            code=code, code_verifier=started["code_verifier"]
        )
    except WorkOSError as e:  # a used or expired code (back button, reload), or WorkOS unreachable
        logging.getLogger(__name__).warning("sign-in code exchange failed: %s", e)
        raise SignInError("WorkOS could not finish the sign-in. Start again from /dashboard.") from e
    if not approved(result.organization_id):
        raise SignInError("Signed in, but not approved yet.")
    return seal_data({"until": time.time() + SIGN_IN_HOURS * 3600}, _COOKIE_PASSWORD)


def session_valid(session_sealed: str) -> bool:
    """A session from finish_sign_in that hasn't run out."""
    try:
        session = unseal_data(session_sealed, _COOKIE_PASSWORD)
    except InvalidToken:  # no cookie, or one this server did not seal
        return False
    return session.get("until", 0) > time.time()  # a login cookie in its place has no "until"
