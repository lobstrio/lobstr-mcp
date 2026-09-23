"""Live smoke test against a running lobstr-mcp instance.

Runs the discover -> details flow (and optionally a real run) over the MCP
protocol. Point it at a deployed dev server whose LOBSTR_DEV_TOKEN is a real
Lobstr API token, so this exercises the live api.lobstr.io end to end.

Usage:
    python scripts/smoke_test.py http://localhost:8000/mcp
    python scripts/smoke_test.py https://<test-server-host>:8000/mcp --run "dentists in Manchester"
"""
import argparse
import asyncio

from fastmcp import Client


async def main(url: str, run_query: str | None) -> None:
    async with Client(url) as c:
        tools = {t.name for t in await c.list_tools()}
        print("tools:", sorted(tools))

        found = (await c.call_tool("search_scrapers", {"query": "google maps"})).data
        print(f"search_scrapers -> {found['count']} scrapers")
        if not found["results"]:
            print("no scrapers returned; stopping")
            return
        top = found["results"][0]
        print("  top:", top["id"], "-", top["name"])

        details = (await c.call_tool("get_scraper_details", {"scraper": top["id"]})).data
        req = details["input_schema"].get("required", [])
        print(f"get_scraper_details -> required inputs: {req}")

        if run_query is not None:
            first = req[0] if req else "query"
            run = (await c.call_tool("run_scraper", {
                "scraper": top["id"], "input": {first: run_query}, "confirm": True})).data
            print("run_scraper ->", run)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="MCP endpoint, e.g. http://localhost:8000/mcp")
    ap.add_argument("--run", dest="run_query", default=None,
                    help="if set, actually run the top scraper with this input (costs credits)")
    args = ap.parse_args()
    asyncio.run(main(args.url, args.run_query))
