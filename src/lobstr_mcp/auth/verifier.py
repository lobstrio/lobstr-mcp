"""Resource-server side: verify an incoming MCP bearer token.

Resolves the token to the linked Lobstr identity via the TokenStore and
returns a FastMCP AccessToken (carrying the Lobstr token in `claims`), or
None for unknown/expired tokens.
"""
from __future__ import annotations

from fastmcp.server.auth import AccessToken, TokenVerifier

from lobstr_mcp.auth.token_store import TokenStore


class LobstrTokenVerifier(TokenVerifier):
    def __init__(self, token_store: TokenStore, *,
                 base_url: str | None = None,
                 required_scopes: list[str] | None = None) -> None:
        super().__init__(base_url=base_url, required_scopes=required_scopes)
        self._store = token_store

    async def verify_token(self, token: str) -> AccessToken | None:
        link = self._store.get(token)
        if link is None:
            return None
        return AccessToken(
            token=token,
            client_id=link.lobstr_user_id,
            scopes=list(link.scopes),
            expires_at=int(link.expires_at) if link.expires_at is not None else None,
            subject=link.lobstr_user_id,
            claims={
                "lobstr_token": link.lobstr_api_token,
                "lobstr_user_id": link.lobstr_user_id,
            },
        )
