"""Maps an issued MCP access token to the linked Lobstr identity.

In-memory implementation for now; the interface is deliberately small so it
can be swapped for Redis/Postgres in deployment without touching callers.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class TokenLink:
    lobstr_user_id: str
    lobstr_api_token: str
    scopes: list[str]
    expires_at: float | None = None  # epoch seconds; None = no expiry

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) >= self.expires_at


class TokenStore:
    def __init__(self) -> None:
        self._by_token: dict[str, TokenLink] = {}

    def put(self, mcp_token: str, link: TokenLink) -> None:
        self._by_token[mcp_token] = link

    def get(self, mcp_token: str, now: float | None = None) -> TokenLink | None:
        link = self._by_token.get(mcp_token)
        if link is None:
            return None
        if link.is_expired(now):
            self._by_token.pop(mcp_token, None)
            return None
        return link

    def revoke(self, mcp_token: str) -> None:
        self._by_token.pop(mcp_token, None)
