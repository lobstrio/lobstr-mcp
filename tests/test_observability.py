import lobstr_mcp.observability as obs
from lobstr_mcp.observability import _is_noisy_uvicorn_asgi_log, _scrub, init_sentry


def test_scrub_redacts_sensitive_headers_body_and_cookies():
    event = {"request": {
        "headers": {
            "Authorization": "Bearer secret-token",
            "X-MCP-Service-Credential": "the-cred",
            "Cookie": "sid=1",
            "User-Agent": "curl/8",
        },
        "data": {"grant": "single-use-grant"},
        "cookies": {"sid": "1"},
    }}
    out = _scrub(event, {})
    h = out["request"]["headers"]
    assert h["Authorization"] == "[Filtered]"
    assert h["X-MCP-Service-Credential"] == "[Filtered]"
    assert h["Cookie"] == "[Filtered]"
    assert h["User-Agent"] == "curl/8"          # non-sensitive header kept
    assert out["request"]["data"] == "[Filtered]"  # body dropped (could hold a grant/token)
    assert "cookies" not in out["request"]


def test_scrub_tolerates_missing_request():
    assert _scrub({}, {}) == {}
    assert _scrub({"request": {}}, {}) == {"request": {}}


def test_init_sentry_is_noop_without_dsn(monkeypatch):
    monkeypatch.delenv("LOBSTR_MCP_SENTRY_DSN", raising=False)
    obs._initialized = False
    assert init_sentry() is False
    assert obs._initialized is False


def test_scrub_drops_the_uvicorn_shutdown_asgi_log_noise():
    """MCP-3: a redeploy's SIGTERM mid-SSE-stream makes uvicorn log this at
    error level with no exception and no user; it should never reach Sentry."""
    event = {"logger": "uvicorn.error",
              "logentry": {"message": "ASGI callable returned without completing response."}}
    assert _scrub(event, {}) is None
    assert _is_noisy_uvicorn_asgi_log(event) is True


def test_scrub_drops_the_uvicorn_log_noise_via_plain_message_field():
    # sentry_sdk's logging integration can put the text on `message` instead
    # of `logentry` depending on how the record was formatted.
    event = {"logger": "uvicorn.error",
              "message": "ASGI callable returned without completing response."}
    assert _scrub(event, {}) is None


def test_scrub_keeps_other_uvicorn_error_logs():
    event = {"logger": "uvicorn.error", "logentry": {"message": "Something else entirely."}}
    assert _scrub(event, {}) == event


def test_scrub_keeps_non_uvicorn_loggers_with_the_same_text():
    # only uvicorn.error's own sanity-check message is noise; the same text
    # from application code (were it ever logged) should still be reported.
    event = {"logger": "lobstr_mcp.server",
              "logentry": {"message": "ASGI callable returned without completing response."}}
    assert _scrub(event, {}) == event
