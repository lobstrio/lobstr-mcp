"""End-to-end test through the MCP protocol layer (FastMCP in-memory client)
against a mocked Lobstr API — exercises tool registration, dispatch, and the
full discover -> run -> monitor -> results flow together."""
import asyncio
import json
import httpx
from fastmcp import Client
from lobstr_mcp import server
from lobstr_mcp.lobstr_client import LobstrClient

CRAWLER_GM = {
    "id": "gm", "name": "Google Maps", "description": "Scrape businesses",
    "pricing": "per result",
    "input": [{"name": "query", "type": "string", "level": "task", "required": True}],
    "result": ["title", "address", "phone"],
}
RESULT_ROWS = [{"title": f"Biz {i}", "address": f"{i} St", "phone": f"07{i}"} for i in range(3)]


def routes():
    return {
        ("GET", "/v1/crawlers"): {"total_results": 1, "limit": 50, "page": 1,
                                  "total_pages": 1, "data": [CRAWLER_GM]},
        ("GET", "/v1/crawlers/gm"): CRAWLER_GM,
        # A no-account crawler's real /params response omits "account"
        # entirely (verified live) — not {"type": "none"}, a shape the API
        # never produces.
        ("GET", "/v1/crawlers/gm/params"): {},
        ("GET", "/v1/user/balance"): {"object": "plan", "available": 1000},
        ("POST", "/v1/squids"): {"id": "sq1"},
        ("POST", "/v1/squids/sq1"): {},
        ("POST", "/v1/tasks"): {"duplicated_count": 0, "tasks": [{"id": "t1"}]},
        ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
        ("GET", "/v1/runs/run1/stats"): {"id": "run1", "is_done": False, "status": "running"},
        ("GET", "/v1/runs/run1"): {"id": "run1", "status": "running", "credit_used": 0},
        ("GET", "/v1/results"): {"total_results": 3, "page": 1, "total_pages": 1, "data": RESULT_ROWS},
    }


def make_server():
    r = routes()

    def handler(request):
        key = (request.method, request.url.path)
        return httpx.Response(200, json=r[key])

    def factory():
        return LobstrClient("https://api.lobstr.io/v1", "t",
                            transport=httpx.MockTransport(handler))

    return server.build_server(client_factory=factory)


def test_full_flow_discover_run_monitor_results():
    mcp = make_server()

    async def go():
        async with Client(mcp) as c:
            found = (await c.call_tool("search_scrapers", {"query": "maps"})).data
            assert found["results"][0]["id"] == "gm"

            details = (await c.call_tool("get_scraper_details", {"scraper": "gm"})).data
            assert details["input_schema"]["required"] == ["query"]

            # unknown cost -> confirmation required
            unconf = (await c.call_tool(
                "run_scraper", {"scraper": "gm", "input": {"query": "dentists in Manchester"}})).data
            assert unconf["needs_confirmation"] is True

            # confirm -> executes, returns run_id
            run = (await c.call_tool("run_scraper", {
                "scraper": "gm", "input": {"query": "dentists in Manchester"},
                "confirm": True})).data
            assert run["run_id"] == "run1"

            status = (await c.call_tool("get_run", {"run_id": "run1"})).data
            assert status["status"] == "running"

            results = (await c.call_tool("get_results", {
                "run_id": "run1", "fields": ["title"]})).data
            assert results["returned"] == 3
            assert results["results"][0] == {"title": "Biz 0"}

    asyncio.run(go())


def test_validation_error_surfaces_through_protocol():
    mcp = make_server()

    async def go():
        async with Client(mcp) as c:
            out = (await c.call_tool("run_scraper", {
                "scraper": "gm", "input": {}, "confirm": True})).data
            assert out["error_code"] == "validation_error"

    asyncio.run(go())
