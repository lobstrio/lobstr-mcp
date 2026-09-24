"""Structured, model-actionable errors (spec §7).

Tools must never surface a raw upstream 4xx/5xx: a bare httpx exception reaches
the model as an opaque "Client error '400 Bad Request'" with nothing to act on.
Every failure becomes a stable `error_code` plus a human message, and a
remediation hint where one exists.

That covers two different failures, and for a long time it only covered the
first:

- the API answered with a non-2xx -> `LobstrAPIError` -> `to_error_dict`;
- no answer came back at all (connection refused, pool or read timeout, the
  server hanging up mid-response) -> `httpx.TransportError` ->
  `transport_error_dict`. Nothing between a tool and httpx wraps these: the
  SDK lets them through and `lobstr_client._as_lobstr_error` only translates
  the SDK's `APIError`, so they used to escape every tool as a raw exception
  and reach the model as "Server disconnected without sending a response." —
  no `error_code`, no `upstream_status`, no hint: exactly what the paragraph
  above says must never happen.
"""
from __future__ import annotations

import httpx


class LobstrAPIError(Exception):
    """A non-2xx from api.lobstr.io, with its error envelope parsed out.

    Lobstr replies {"errors": {"message": ..., "type": ..., "code": ...}}.
    """

    def __init__(self, status: int, *, error_type: str | None = None,
                 message: str | None = None, path: str | None = None) -> None:
        self.status = status
        self.error_type = error_type
        self.message = message or f"upstream returned HTTP {status}"
        self.path = path
        super().__init__(f"{status} {error_type or ''}: {self.message}".strip())


# Upstream error types worth a dedicated, actionable code + hint.
_TYPE_CODES: dict[str, tuple[str, str]] = {
    "SquidNotReady": (
        "scraper_not_ready",
        "The scraper configuration was not saved before starting the run.",
    ),
    "SquidDoesNotExist": ("scraper_not_found", "Check the scraper id."),
    "HTTPNotFound": ("not_found", "Check the id you passed."),
    "InvalidGrant": ("unauthorized", "Re-authorize the connection."),
    "NotEnoughCredits": ("insufficient_credits", "Top up credits and retry."),
    "SlotsLimitExceeded": (
        "slots_limit_exceeded",
        "You've hit your concurrency-slot limit. Free a slot by deactivating a "
        "scraper you're not using (deactivate_scraper) — that keeps its config "
        "and results, unlike deleting — then retry.",
    ),
    "ExportLimitReached": (
        "export_limit_reached",
        "Free-plan accounts can only read the first 30 results of a run. "
        "Upgrading the plan is the only way past this.",
    ),
}

_STATUS_CODES: dict[int, str] = {
    400: "upstream_rejected",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
}


def to_error_dict(exc: LobstrAPIError) -> dict:
    """Map an upstream failure onto the tool error contract."""
    code, hint = _TYPE_CODES.get(exc.error_type or "", (None, None))
    if code is None:
        code = (_STATUS_CODES.get(exc.status)
                or ("upstream_unavailable" if exc.status >= 500 else "upstream_error"))
    out = {"error_code": code, "message": exc.message, "upstream_status": exc.status}
    if exc.error_type:
        out["upstream_type"] = exc.error_type
    if hint:
        out["hint"] = hint
    return out


# Transport failures where the request provably never reached the API, so
# nothing upstream can have been started, changed or charged and a retry cannot
# duplicate anything. Everything else httpx raises — read timeout, write
# timeout, a server that hangs up mid-response (RemoteProtocolError, the case
# seen in production) — counts as "may have been applied": the request went out
# and only the answer was lost. The asymmetry is deliberate. Calling a write
# that landed "safe to retry" duplicates it and bills the user twice; calling a
# request that never landed "unknown" costs the caller one read.
_NEVER_SENT: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
)

# Worded so it cannot become a loop: a model told only "retry" retries forever.
_RETRY_HINT = ("Retry the same call once or twice. If it keeps failing this way the API is "
               "unreachable from this server, which no retry fixes — report that instead of "
               "retrying again.")


def transport_error_dict(exc: httpx.TransportError, *,
                         verify_with: str | None = None) -> dict:
    """Map "no response came back" onto the tool error contract.

    `upstream_status` is None on purpose: there was no response to have a
    status, and saying so is more useful than omitting the key a caller of a
    failed tool looks for. `request_state` is the part a model has to act on:

    - "not_sent" — the request never reached the API, nothing was applied, and
      retrying is safe;
    - "unknown" — the request was sent and the answer was lost, so it may have
      been applied. On a tool that writes, a blind retry can apply it twice, so
      `verify_with` names the read tool that shows what actually happened.

    `verify_with` is left unset on read-only tools: repeating a read changes
    and charges nothing, so those stay retry-safe even when the fate of the
    request is unknown, and their message says why rather than warning about a
    duplicate that cannot happen.
    """
    detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    never_sent = isinstance(exc, _NEVER_SENT)
    writes = verify_with is not None

    if never_sent:
        message = (f"Could not reach the Lobstr API ({detail}). The request never left this "
                   "client, so nothing upstream was started, changed or charged. Retrying is "
                   "safe.")
        hint = _RETRY_HINT
    elif writes:
        message = (f"The request reached the Lobstr API but no response came back ({detail}) — "
                   "whether it took effect is unknown, not rejected. Treating it as a failure "
                   "and retrying can apply the same write twice, and where the call starts a "
                   "run that means paying for it twice. Check what actually happened with "
                   f"{verify_with}, then retry only if it did not take effect.")
        # The pointer goes last: it is a phrase, not just a tool name, and any
        # clause after it reads as part of it.
        hint = f"Do not retry blind. Check the current state first: {verify_with}."
    else:
        message = (f"The request reached the Lobstr API but no response came back ({detail}) — "
                   "whether it was served is unknown, not rejected. This call only reads, so "
                   "nothing was changed or charged and retrying it is safe.")
        hint = _RETRY_HINT

    return {
        "error_code": "upstream_unavailable",
        "message": message,
        "upstream_status": None,
        "transport_error": detail,
        "request_state": "not_sent" if never_sent else "unknown",
        "retry_safe": never_sent or not writes,
        "hint": hint,
    }


def structured(fn=None, *, verify_with: str | None = None):
    """Wrap a tool implementation so upstream failures return the contract
    instead of raising an opaque transport error at the model.

    Bare (`@structured`) on a read-only tool; with the read tool that checks
    state (`@structured(verify_with="get_run(run_id=...)")`) on every tool that
    writes — see transport_error_dict for what that changes in the envelope the
    model receives.

    Handlers *inside* the wrapped function still win: Python raises to the
    innermost matching `except`, so execution.py's `_replace_squid_input`,
    which catches `httpx.TransportError` at each of its three steps and answers
    with what was deleted and what was put back (`tasks_lost`, `lost_tasks`,
    `tasks_restored`), keeps doing exactly that. This catch only ever sees what
    no inner handler took.

    For the same reason the catch belongs here and *not* in
    `lobstr_client._as_lobstr_error`: turning a transport failure into a
    `LobstrAPIError` down there would route it into those handlers'
    `except LobstrAPIError` branch, which reports "nothing was changed" — true
    only of a request the API actually rejected.
    """
    from functools import wraps

    def decorate(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except LobstrAPIError as exc:
                return to_error_dict(exc)
            except httpx.TransportError as exc:
                # Caught precisely, never as a bare `except Exception`: a bug in
                # this server must still surface as a bug, with a traceback, not
                # read to a model as a retryable API outage.
                return transport_error_dict(exc, verify_with=verify_with)

        return wrapper

    return decorate(fn) if fn is not None else decorate
