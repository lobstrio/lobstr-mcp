"""Per-request identity: turn the authenticated MCP token into a LobstrClient.

Tools call `resolve_authenticated_client(required_scopes, settings)`; it reads
the current request's AccessToken (via FastMCP's get_access_token dependency),
enforces scopes, and builds a LobstrClient bound to that user's Lobstr token.
"""
from __future__ import annotations

from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token

from lobstr_mcp.auth.scopes import check_scopes
from lobstr_mcp.lobstr_client import LobstrClient


class NotAuthenticatedError(Exception):
    pass


def client_from_access_token(access: AccessToken, settings) -> LobstrClient:
    lobstr_token = (access.claims or {}).get("lobstr_token")
    if not lobstr_token:
        raise NotAuthenticatedError("access token has no linked Lobstr token")
    return LobstrClient(
        settings.lobstr_api_base,
        lobstr_token,
        timeout=settings.request_timeout,
    )


def resolve_authenticated_client(required_scopes, settings) -> LobstrClient:
    access = get_access_token()
    if access is None:
        raise NotAuthenticatedError("no authenticated MCP access token in context")
    check_scopes(access.scopes, required_scopes)
    return client_from_access_token(access, settings)


def current_user_client(settings) -> LobstrClient:
    """Build a LobstrClient for the current request's authenticated user
    (no scope check — scope enforcement is handled per-tool by the authorizer)."""
    access = get_access_token()
    if access is None:
        raise NotAuthenticatedError("no authenticated MCP access token in context")
    return client_from_access_token(access, settings)


def make_authorizer():
    """Return an authorizer(required_scopes) that checks the current request's
    access-token scopes, raising ScopeError / NotAuthenticatedError."""
    def authorizer(required_scopes) -> None:
        access = get_access_token()
        if access is None:
            raise NotAuthenticatedError("no authenticated MCP access token in context")
        check_scopes(access.scopes, required_scopes)
    return authorizer
