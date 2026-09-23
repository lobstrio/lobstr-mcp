"""Sentry error + performance monitoring — opt-in via LOBSTR_MCP_SENTRY_DSN.

No-op unless LOBSTR_MCP_SENTRY_DSN is set, so local and test runs stay silent.
(The DSN var is prefixed, not the bare SENTRY_DSN, to avoid colliding with a
shared/reserved SENTRY_DSN in the host env.) PII is on
(SENTRY_ENVIRONMENT-tagged), but a before_send scrubber strips the headers and
request bodies that would otherwise ship bearer tokens, the MCP service
credential, or single-use OAuth grants to Sentry.
"""
from __future__ import annotations

import os

from lobstr_mcp._version import __version__

# lower-cased header names whose values must never leave the box
_SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-mcp-service-credential",
}
_REDACTED = "[Filtered]"
_initialized = False


# One release per deployed version, so Sentry groups errors by build.
SENTRY_RELEASE = f"lobstr-mcp@{__version__}"

# uvicorn's own sanity check (uvicorn/protocols/http/h11_impl.py:run_asgi) logs
# this at error level, with no exception, whenever the ASGI callable returns
# having sent `http.response.start` but never a closing
# `http.response.body{more_body: False}` frame. On `/mcp` that happens when a
# `docker compose up -d --build` redeploy sends SIGTERM while a client has a
# long-lived Streamable HTTP/SSE stream open: sse_starlette's
# `EventSourceResponse` (used by `mcp`'s SSE POST branch) reacts to the
# shutdown by cancelling its internal task group before `_stream_response`
# sends that closing frame (`AppStatus.should_exit` /
# `_listen_for_exit_signal_with_grace` in sse_starlette/sse.py) -- confirmed
# by driving `EventSourceResponse` directly and by uvicorn's own guard, which
# only suppresses this log for a *transport-level* disconnect
# (`self.disconnected`), not a shutdown-time cancellation. Nothing in this
# repo's routes is on this path, and no exception or user is attached to it
# (Sentry MCP-3). Filtered rather than fixed upstream: patching
# sse_starlette/mcp is out of scope here.
_NOISY_UVICORN_ASGI_LOG = "ASGI callable returned without completing response."


def _is_noisy_uvicorn_asgi_log(event) -> bool:
    if event.get("logger") != "uvicorn.error":
        return False
    logentry = event.get("logentry")
    message = (logentry.get("message") if isinstance(logentry, dict) else None) \
        or event.get("message") or ""
    return _NOISY_UVICORN_ASGI_LOG in message


def _scrub(event, hint):
    """Drop secrets from an event's request before it is sent to Sentry, and
    drop the uvicorn shutdown-time ASGI log noise described above (MCP-3)."""
    if _is_noisy_uvicorn_asgi_log(event):
        return None
    req = event.get("request")
    if isinstance(req, dict):
        headers = req.get("headers")
        if isinstance(headers, dict):
            for key in list(headers):
                if key.lower() in _SENSITIVE_HEADERS:
                    headers[key] = _REDACTED
        # request bodies can carry a token or an OAuth grant — never ship them
        if req.get("data") is not None:
            req["data"] = _REDACTED
        req.pop("cookies", None)
    return event


def init_sentry() -> bool:
    """Start Sentry when LOBSTR_MCP_SENTRY_DSN is set. Idempotent; True if active.

    The DSN var is prefixed (not the bare SENTRY_DSN the SDK auto-reads) so it
    can't collide with a shared/reserved SENTRY_DSN in the host env — these boxes
    already run other Sentry-instrumented services. dsn/environment/release are
    passed explicitly so no ambient SENTRY_* env leaks into the MCP's config.
    """
    global _initialized
    if _initialized:
        return True
    dsn = os.getenv("LOBSTR_MCP_SENTRY_DSN")
    if not dsn:
        return False

    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT") or None,
        release=SENTRY_RELEASE,
        traces_sample_rate=float(os.getenv("LOBSTR_MCP_SENTRY_TRACES_RATE", "0.1")),
        send_default_pii=True,
        before_send=_scrub,
        before_send_transaction=_scrub,
    )
    _initialized = True
    return True
