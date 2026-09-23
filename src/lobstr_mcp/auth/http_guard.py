"""Guard the OAuth endpoints the `mcp` SDK mounts (`/register`, `/token`,
`/revoke`) against requests its handlers do not expect.

The SDK (1.29.x) declares `methods=["POST", "OPTIONS"]` on those routes and wraps
them in Starlette's CORSMiddleware, which only answers *preflight* requests, the
ones carrying `Access-Control-Request-Method`. A bare `OPTIONS` from curl or a
scanner falls through to the handler, and the registration handler calls
`request.json()` unguarded, so an empty or non-JSON body becomes a 500 and a
Sentry event (MCP-4). This middleware answers those cases itself:

- `OPTIONS` without a preflight header → 204 with an `Allow` header.
- `POST /register` whose body is not a JSON object → 400 in the RFC 7591 error
  shape (`invalid_client_metadata`), the same status the handler returns for a
  JSON body that fails validation.

`/token` and `/revoke` take form-encoded bodies, so only their `OPTIONS` case is
handled here. Everything else passes through untouched.
"""
from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

AUTH_PATHS = frozenset({"/register", "/token", "/revoke"})
JSON_BODY_PATHS = frozenset({"/register"})


class AuthEndpointGuard:
    def __init__(self, app: ASGIApp, *, paths: frozenset[str] = AUTH_PATHS,
                 json_paths: frozenset[str] = JSON_BODY_PATHS) -> None:
        self.app = app
        self.paths = paths
        self.json_paths = json_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] not in self.paths:
            await self.app(scope, receive, send)
            return

        method = scope["method"].upper()

        if method == "OPTIONS" and b"access-control-request-method" not in _raw_keys(scope):
            await _respond(send, 204, b"", extra=[(b"allow", b"POST, OPTIONS")])
            return

        if method == "POST" and scope["path"] in self.json_paths:
            body, replay = await _drain(receive)
            if not _is_json_object(body):
                payload = json.dumps({
                    "error": "invalid_client_metadata",
                    "error_description": "request body must be a JSON object",
                }).encode()
                await _respond(send, 400, payload,
                               extra=[(b"content-type", b"application/json")])
                return
            await self.app(scope, replay, send)
            return

        await self.app(scope, receive, send)


def _raw_keys(scope: Scope) -> set[bytes]:
    return {k.lower() for k, _ in scope.get("headers", [])}


async def _drain(receive: Receive):
    """Read the whole request body, return it and a `receive` that replays it."""
    chunks: list[bytes] = []
    more = True
    while more:
        message: Message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunks.append(message.get("body", b""))
        more = message.get("more_body", False)
    body = b"".join(chunks)
    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return body, replay


def _is_json_object(body: bytes) -> bool:
    if not body.strip():
        return False
    try:
        return isinstance(json.loads(body), dict)
    except ValueError:
        return False


async def _respond(send: Send, status: int, body: bytes, *, extra=()) -> None:
    headers = [(b"content-length", str(len(body)).encode()), *extra]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
