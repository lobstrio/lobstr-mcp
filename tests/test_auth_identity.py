import pytest
from fastmcp.server.auth import AccessToken
import lobstr_mcp.auth.identity as identity
from lobstr_mcp.auth.identity import (
    client_from_access_token,
    resolve_authenticated_client,
    NotAuthenticatedError,
)
from lobstr_mcp.auth.scopes import ScopeError, CRAWLERS_READ, RUNS_EXECUTE
from lobstr_mcp.config import Settings

SETTINGS = Settings(
    lobstr_api_base="https://api.lobstr.io/v1",
    dev_token=None,
    request_timeout=30.0,
    run_confirm_threshold=100,
    public_base_url="https://mcp.lobstr.io",
    service_credential=None,
    consent_url="https://app.lobstr.io/connect-ai",
)


def access(scopes, lobstr_token="LT"):
    claims = {"lobstr_token": lobstr_token} if lobstr_token else {}
    return AccessToken(token="MCP1", client_id="u1", scopes=scopes,
                       expires_at=None, claims=claims)


def test_client_from_access_token_uses_lobstr_token():
    client = client_from_access_token(access([CRAWLERS_READ]), SETTINGS)
    assert client._http.headers["Authorization"] == "Token LT"


def test_client_from_access_token_without_lobstr_token_raises():
    with pytest.raises(NotAuthenticatedError):
        client_from_access_token(access([CRAWLERS_READ], lobstr_token=None), SETTINGS)


def test_resolve_returns_client_when_scoped(monkeypatch):
    monkeypatch.setattr(identity, "get_access_token",
                        lambda: access([CRAWLERS_READ]))
    client = resolve_authenticated_client([CRAWLERS_READ], SETTINGS)
    assert client._http.headers["Authorization"] == "Token LT"


def test_resolve_raises_when_unauthenticated(monkeypatch):
    monkeypatch.setattr(identity, "get_access_token", lambda: None)
    with pytest.raises(NotAuthenticatedError):
        resolve_authenticated_client([CRAWLERS_READ], SETTINGS)


def test_resolve_raises_on_missing_scope(monkeypatch):
    monkeypatch.setattr(identity, "get_access_token",
                        lambda: access([CRAWLERS_READ]))
    with pytest.raises(ScopeError):
        resolve_authenticated_client([RUNS_EXECUTE], SETTINGS)


def test_authorizer_allows_and_denies(monkeypatch):
    authorizer = identity.make_authorizer()
    monkeypatch.setattr(identity, "get_access_token",
                        lambda: access([CRAWLERS_READ]))
    authorizer([CRAWLERS_READ])  # granted -> no raise
    with pytest.raises(ScopeError):
        authorizer([RUNS_EXECUTE])  # missing -> raise


def test_authorizer_denies_when_unauthenticated(monkeypatch):
    authorizer = identity.make_authorizer()
    monkeypatch.setattr(identity, "get_access_token", lambda: None)
    with pytest.raises(NotAuthenticatedError):
        authorizer([CRAWLERS_READ])
