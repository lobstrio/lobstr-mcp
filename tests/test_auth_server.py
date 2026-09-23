from fastmcp import FastMCP
from lobstr_mcp import server
from lobstr_mcp.auth.token_store import TokenStore, TokenLink


def test_build_authenticated_server_returns_mcp_and_store(monkeypatch):
    monkeypatch.delenv("LOBSTR_DEV_TOKEN", raising=False)
    store = TokenStore()
    store.put("MCP1", TokenLink("u1", "LT", ["crawlers:read"]))

    mcp, returned_store = server.build_authenticated_server(token_store=store)

    assert isinstance(mcp, FastMCP)
    assert returned_store is store
    # an auth provider is attached (resource-server verification is enabled)
    assert getattr(mcp, "auth", None) is not None
