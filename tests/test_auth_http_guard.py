"""AuthEndpointGuard: the SDK's OAuth routes must not 500 on a bare OPTIONS
or a non-JSON registration body (Sentry MCP-4)."""
import asyncio
import json

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from lobstr_mcp.auth.http_guard import AuthEndpointGuard


def _inner_app(seen: list):
    """Mimics the mcp SDK: register parses JSON unguarded, token reads a form."""

    async def register(request: Request):
        body = await request.json()  # raises on empty / non-JSON, like the SDK
        seen.append(("register", request.method, body))
        return JSONResponse({"client_id": "c1", "echo": body}, status_code=201)

    async def token(request: Request):
        form = await request.form()
        seen.append(("token", request.method, dict(form)))
        return JSONResponse({"ok": True})

    async def other(request: Request):
        seen.append(("other", request.method, None))
        return JSONResponse({"ok": True})

    return Starlette(routes=[
        Route("/register", register, methods=["POST", "OPTIONS"]),
        Route("/token", token, methods=["POST", "OPTIONS"]),
        Route("/other", other, methods=["GET", "POST", "OPTIONS"]),
    ])


def _call(app, method, path, **kw):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            return await c.request(method, path, **kw)
    return asyncio.run(go())


def test_bare_options_on_auth_paths_gets_204_with_allow():
    seen = []
    app = AuthEndpointGuard(_inner_app(seen))
    for path in ("/register", "/token", "/revoke"):
        r = _call(app, "OPTIONS", path)
        assert r.status_code == 204, path
        assert r.headers["allow"] == "POST, OPTIONS"
    assert seen == []


def test_cors_preflight_still_reaches_the_app():
    seen = []
    app = AuthEndpointGuard(_inner_app(seen))
    r = _call(app, "OPTIONS", "/other",
              headers={"Origin": "https://x", "Access-Control-Request-Method": "POST"})
    assert r.status_code == 200
    assert seen == [("other", "OPTIONS", None)]


def test_empty_or_non_json_register_body_is_a_400_not_a_500():
    seen = []
    app = AuthEndpointGuard(_inner_app(seen))
    for content in (b"", b"not json", b"[1, 2]", b'"string"'):
        r = _call(app, "POST", "/register", content=content,
                  headers={"content-type": "application/json"})
        assert r.status_code == 400, content
        assert r.json()["error"] == "invalid_client_metadata"
    assert seen == []


def test_valid_register_body_is_replayed_to_the_app_intact():
    seen = []
    app = AuthEndpointGuard(_inner_app(seen))
    payload = {"redirect_uris": ["https://x/cb"], "client_name": "t"}
    r = _call(app, "POST", "/register", json=payload)
    assert r.status_code == 201
    assert r.json()["echo"] == payload
    assert seen == [("register", "POST", payload)]


def test_form_encoded_token_post_passes_through_untouched():
    seen = []
    app = AuthEndpointGuard(_inner_app(seen))
    r = _call(app, "POST", "/token", data={"grant_type": "authorization_code", "code": "abc"})
    assert r.status_code == 200
    assert seen == [("token", "POST", {"grant_type": "authorization_code", "code": "abc"})]


def test_unrelated_paths_are_not_touched():
    seen = []
    app = AuthEndpointGuard(_inner_app(seen))
    r = _call(app, "POST", "/other", content=b"garbage")
    assert r.status_code == 200
    assert seen == [("other", "POST", None)]


def test_streamed_body_is_reassembled():
    """The guard drains a chunked body and replays it as one message."""
    seen = []
    app = AuthEndpointGuard(_inner_app(seen))
    payload = json.dumps({"client_name": "chunked"}).encode()

    async def gen():
        yield payload[:5]
        yield payload[5:]

    r = _call(app, "POST", "/register", content=gen(),
              headers={"content-type": "application/json"})
    assert r.status_code == 201
    assert seen[0][2] == {"client_name": "chunked"}
