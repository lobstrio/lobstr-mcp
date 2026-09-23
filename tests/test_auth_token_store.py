from lobstr_mcp.auth.token_store import TokenStore, TokenLink


def test_put_get_roundtrip():
    store = TokenStore()
    link = TokenLink("u1", "LT", ["crawlers:read"])
    store.put("MCP1", link)
    assert store.get("MCP1") is link


def test_unknown_returns_none():
    assert TokenStore().get("nope") is None


def test_expired_link_evicted_and_none():
    store = TokenStore()
    store.put("MCP1", TokenLink("u1", "LT", ["crawlers:read"], expires_at=100.0))
    # now (200) is past expiry (100)
    assert store.get("MCP1", now=200.0) is None
    # evicted, so even a "valid" now returns None
    assert store.get("MCP1", now=50.0) is None


def test_non_expired_link_returned():
    store = TokenStore()
    link = TokenLink("u1", "LT", ["crawlers:read"], expires_at=1000.0)
    store.put("MCP1", link)
    assert store.get("MCP1", now=500.0) is link


def test_revoke():
    store = TokenStore()
    store.put("MCP1", TokenLink("u1", "LT", ["crawlers:read"]))
    store.revoke("MCP1")
    assert store.get("MCP1") is None
