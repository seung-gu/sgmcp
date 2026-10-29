import logging
import os
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
import jwt


AUTH_ISSUER = os.environ.get("MCP_AUTH_ISSUER")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8000")
RESOURCE = f"{PUBLIC_URL}/mcp"   # this server's MCP address; a token's aud must equal it
AUTH_ORG = os.environ.get("MCP_AUTH_ORG")   # WorkOS organization (org_...) that holds the approved users


class AuthKitVerifier(TokenVerifier):
    """Accepts tokens signed by our AuthKit for this server, from members of AUTH_ORG only."""

    def __init__(self):
        # Built only when auth is on: without AUTH_ISSUER the URL is "None/...", which PyJWT rejects
        self.jwks = jwt.PyJWKClient(f"{AUTH_ISSUER}/oauth2/jwks")

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = self.jwks.get_signing_key_from_jwt(token)   # pick the public key matching the header's kid
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],                    # signature algorithm
                issuer=AUTH_ISSUER,                      # check iss
                audience=RESOURCE,                       # check aud
                options={"require": ["exp"]},            # PyJWT checks exp only when present: make it mandatory
            )
            # PyJWT has no option for org_id, so check it here: not in the organization = not approved yet
            if not AUTH_ORG or claims.get("org_id") != AUTH_ORG:
                raise jwt.InvalidTokenError(f"not approved: {claims['sub']} has org_id {claims.get('org_id')}")
        except jwt.PyJWTError as e:
            # Every PyJWT error subclasses PyJWTError, so this catches all rejections: unknown kid,
            # bad signature, wrong iss/aud, missing aud/exp, expired, malformed, and not approved
            logging.getLogger(__name__).warning("token rejected: %s", e)
            return None
        logging.getLogger(__name__).info("token accepted: %s", claims["sub"])
        return AccessToken(token=token, client_id=claims["client_id"], scopes=[], expires_at=claims["exp"])


def fastmcp_options() -> dict:
    """FastMCP's auth arguments; none when MCP_AUTH_ISSUER is unset, which leaves /mcp open."""
    if not AUTH_ISSUER:
        logging.getLogger(__name__).warning("MCP_AUTH_ISSUER not set: /mcp is open")
        return {}
    return {
        "token_verifier": AuthKitVerifier(),
        "auth": AuthSettings(issuer_url=AUTH_ISSUER, resource_server_url=RESOURCE),
    }
