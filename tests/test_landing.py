import asyncio

import httpx

from lobstr_mcp import server


def _app():
    return server.build_server().http_app()


async def _get(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path)


def test_landing_page_served_at_root():
    r = asyncio.run(_get(_app(), "/"))
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    # The page must advertise the real, current endpoint and stay truthful about
    # the tools it fronts — those strings double as a guard against drift.
    assert "https://mcp.lobstr.io/mcp" in body
    assert "run_scraper" in body and "search_scrapers" in body


def test_brand_assets_served_with_correct_media_type():
    app = _app()
    svg = asyncio.run(_get(app, "/lobstr/assets/lobstr-wordmark.svg"))
    assert svg.status_code == 200
    assert svg.headers["content-type"] == "image/svg+xml"

    # the footer crab is the homepage's logo-red.svg — it must be servable
    crab = asyncio.run(_get(app, "/lobstr/assets/logo-red.svg"))
    assert crab.status_code == 200
    assert crab.headers["content-type"] == "image/svg+xml"

    font = asyncio.run(_get(app, "/lobstr/assets/fonts/segoeui.woff2"))
    assert font.status_code == 200
    assert font.headers["content-type"] == "font/woff2"


def test_asset_route_rejects_traversal_and_unknown_paths():
    app = _app()
    for bad in ("/lobstr/assets/fonts/../../server.py",
                "/lobstr/assets/nope.txt",
                "/lobstr/assets/../pyproject.toml"):
        r = asyncio.run(_get(app, bad))
        assert r.status_code == 404, bad
