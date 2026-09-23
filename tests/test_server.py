import asyncio
import httpx
from fastmcp import Client
from lobstr_mcp import server
from lobstr_mcp.lobstr_client import LobstrClient


def build_with_fixture():
    routes = {"/v1/crawlers": [{"id": "gm", "name": "Google Maps",
                                "description": "biz", "pricing": "1cr"}]}

    def handler(request):
        return httpx.Response(200, json=routes[request.url.path])

    def factory():
        return LobstrClient("https://api.lobstr.io/v1", "t",
                            transport=httpx.MockTransport(handler))

    return server.build_server(client_factory=factory)


def test_lists_four_read_tools():
    mcp = build_with_fixture()

    async def go():
        async with Client(mcp) as c:
            tools = {t.name for t in await c.list_tools()}
            assert {"search_scrapers", "get_scraper_details",
                    "list_my_scrapers", "get_my_scraper",
                    "run_scraper", "get_run", "get_results"} <= tools
            res = await c.call_tool("search_scrapers", {"query": "maps"})
            return res

    res = asyncio.run(go())
    assert res.data["count"] == 1


def test_search_scrapers_json_by_default_toon_on_demand():
    """Default output is JSON; TOON is opt-in via toon=true. Structured .data
    is available either way."""
    mcp = build_with_fixture()

    def call(args):
        async def go():
            async with Client(mcp) as c:
                return await c.call_tool("search_scrapers", args)
        return asyncio.run(go())

    # default: JSON (not TOON)
    default = call({"query": "maps"})
    assert default.data["count"] == 1
    assert "results[1]{" not in default.content[0].text  # not the TOON table header

    # on demand: TOON
    toon = call({"query": "maps", "toon": True})
    assert toon.data["count"] == 1
    text = toon.content[0].text
    assert text.startswith("count: 1")
    assert "results[1]{" in text


def _ann(t):
    """Registered tool annotations as a plain snake_case dict. mcp-sdk versions
    dump these either snake_case (read_only_hint) or camelCase (readOnlyHint);
    normalize so the assertions hold regardless of the pinned version."""
    import re
    a = t.annotations
    if a is None:
        return {}
    d = a.model_dump() if hasattr(a, "model_dump") else dict(a)
    return {re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower(): v for k, v in d.items()}


def test_every_tool_has_directory_required_annotations():
    """Both the Anthropic and OpenAI directories reject tools missing a title
    or the applicable action hint. Assert every registered tool carries a title
    and readOnlyHint, and that the one write tool declares destructiveHint
    explicitly (not left to default)."""
    mcp = build_with_fixture()

    async def go():
        async with Client(mcp) as c:
            return await c.list_tools()

    tools = asyncio.run(go())
    assert tools, "no tools registered"
    for t in tools:
        ann = _ann(t)
        assert ann.get("title"), f"{t.name} is missing a title annotation"
        # All three action hints must be explicit on every tool — OpenAI's
        # directory requires readOnlyHint, destructiveHint and openWorldHint
        # spelled out (Anthropic needs title + read/destructive).
        for hint in ("read_only_hint", "destructive_hint", "open_world_hint"):
            assert ann.get(hint) is not None, f"{t.name} is missing {hint}"

    run = next(t for t in tools if t.name == "run_scraper")
    run_ann = _ann(run)
    assert run_ann.get("read_only_hint") is False, "run_scraper must be a write tool"
    assert run_ann.get("destructive_hint") is not None, \
        "run_scraper must set destructiveHint explicitly"
