"""The dev, authenticated and OAuth servers must expose the same tools.

The primitives (create_squid / add_tasks / estimate_run) were wired into two of
the three builders when they landed and missed in build_oauth_server, the one
production runs, so mcp.lobstr.io served 16 tools while the code had 19.
"""
import asyncio

from fastmcp import Client

from lobstr_mcp import server
from lobstr_mcp.config import Settings

SETTINGS = Settings(
    lobstr_api_base="https://api.lobstr.io/v1",
    dev_token=None,
    request_timeout=30.0,
    run_confirm_threshold=100,
    public_base_url="https://mcp.lobstr.io",
    service_credential="x",
    consent_url="https://app.lobstr.io/connect-ai",
)

PRIMITIVES = {"create_squid", "add_tasks", "update_scraper", "estimate_run"}


def _tools(mcp) -> set[str]:
    async def go():
        async with Client(mcp) as c:
            return {t.name for t in await c.list_tools()}
    return asyncio.run(go())


def test_all_three_builders_register_the_same_tools(monkeypatch):
    monkeypatch.delenv("LOBSTR_DEV_TOKEN", raising=False)
    dev = _tools(server.build_server(settings=SETTINGS))
    authenticated, _ = server.build_authenticated_server(settings=SETTINGS)
    oauth, _, _ = server.build_oauth_server(settings=SETTINGS)
    assert PRIMITIVES <= dev
    assert _tools(authenticated) == dev
    assert _tools(oauth) == dev
    assert len(dev) == 22  # +update_scraper, +wait_for_run
