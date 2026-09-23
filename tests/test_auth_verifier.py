import asyncio
from lobstr_mcp.auth.token_store import TokenStore, TokenLink
from lobstr_mcp.auth.verifier import LobstrTokenVerifier


def test_verifies_known_token_to_access_token():
    store = TokenStore()
    store.put("MCP1", TokenLink("u1", "LOBSTR_TOK", ["crawlers:read"]))
    verifier = LobstrTokenVerifier(store)

    at = asyncio.run(verifier.verify_token("MCP1"))
    assert at is not None
    assert at.scopes == ["crawlers:read"]
    assert at.subject == "u1"
    assert at.claims["lobstr_token"] == "LOBSTR_TOK"
    assert at.claims["lobstr_user_id"] == "u1"


def test_unknown_token_returns_none():
    verifier = LobstrTokenVerifier(TokenStore())
    assert asyncio.run(verifier.verify_token("nope")) is None


def test_expired_token_returns_none():
    store = TokenStore()
    store.put("MCP1", TokenLink("u1", "LOBSTR_TOK", ["crawlers:read"], expires_at=1.0))
    verifier = LobstrTokenVerifier(store)
    # store.get() uses real time.time(); expiry at epoch 1.0 is long past
    assert asyncio.run(verifier.verify_token("MCP1")) is None
