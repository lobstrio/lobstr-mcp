import asyncio

import httpx

from lobstr_mcp import server


def _app():
    return server.build_server().http_app()


async def _get(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path)


def test_root_redirects_to_the_mcp_docs():
    # No landing page of its own since 20/10: a browser on mcp.lobstr.io goes to the docs.
    r = asyncio.run(_get(_app(), "/"))
    assert r.status_code == 301
    assert r.headers["location"] == "https://docs.lobstr.io/docs/mcp"


def test_brand_assets_are_gone():
    r = asyncio.run(_get(_app(), "/lobstr/assets/lobstr-wordmark.svg"))
    assert r.status_code == 404
