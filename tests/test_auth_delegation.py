import httpx
import pytest
from lobstr_mcp.auth.delegation import LobstrDelegationClient, LobstrDelegationError


def make_client(handler):
    return LobstrDelegationClient(
        "https://api.lobstr.io/v1", "svc-cred",
        transport=httpx.MockTransport(handler),
    )


def test_redeem_grant_success_builds_link():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["cred"] = request.headers.get("X-MCP-Service-Credential")
        import json
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "user_id": 42, "token": "LOBSTR_TOK",
            "scopes": ["crawlers:read", "runs:execute"], "expires_at": 1999.0,
        })

    client = make_client(handler)
    link = client.redeem_grant("grant-xyz")
    assert captured["url"] == "https://api.lobstr.io/v1/oauth/mcp-grant/redeem"
    assert captured["cred"] == "svc-cred"
    assert captured["body"] == {"grant": "grant-xyz"}
    assert link.lobstr_user_id == "42"
    assert link.lobstr_api_token == "LOBSTR_TOK"
    assert link.scopes == ["crawlers:read", "runs:execute"]
    assert link.expires_at == 1999.0


def test_redeem_grant_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "expired or unknown grant"})

    client = make_client(handler)
    with pytest.raises(LobstrDelegationError):
        client.redeem_grant("bad-grant")
