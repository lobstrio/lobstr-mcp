"""Thin adapter over the official lobstrio SDK.

The MCP used to hand-roll its own httpx client (auth, pagination, error
parsing). That duplicated what `lobstrio` already does, so this module now
delegates transport/auth/retries/pagination to the SDK and keeps only the
MCP-specific surface on top:

- returns plain dicts (via each model's ``.raw``) so the tool layer, the
  TOON/JSON rendering, and the tests are unaffected;
- keeps the MCP's own pagination cap and structured-error contract, which the
  SDK does not impose;
- a few write / pass-through endpoints go through the SDK's low-level HTTP
  client so their request/response shapes stay byte-identical.
"""
from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from typing import Any, Iterator

from lobstrio import APIError, LobstrClient as _SDKClient

from lobstr_mcp.errors import LobstrAPIError

from lobstr_mcp._version import __version__

# Identifies MCP-originated traffic to the lobstr API (vs the SDK/CLI/browser).
USER_AGENT = f"lobstr-mcp/{__version__}"

# Crawler ids are 32-char hex hashes; anything else (e.g. a slug like
# "google-maps-leads-scraper") we treat as a slug to resolve.
_CRAWLER_ID = re.compile(r"^[0-9a-f]{32}$")

# Cap page walks so a bogus total_pages can't spin forever. The SDK walks every
# page by default; we pass this as its max_pages.
_MAX_PAGES = 50


def resolve_crawler_id(client: LobstrClient, scraper: str) -> str:
    """Accept a crawler id *or* a slug and return the id.

    Callers get slugs from search_scrapers (and users type them), but the
    /crawlers/{hash} endpoint only takes the id. Resolve a slug via the catalog;
    if the value is already an id, or the catalog is unavailable / has no match,
    return it unchanged so the caller's get_crawler surfaces a normal error.
    """
    if scraper and _CRAWLER_ID.match(scraper):
        return scraper
    try:
        for crawler in client.list_crawlers():
            if scraper in (crawler.get("slug"), crawler.get("id")):
                return crawler.get("id") or scraper
    except Exception:
        pass
    return scraper


@contextmanager
def _as_lobstr_error(path: str) -> Iterator[None]:
    """Translate the SDK's ``APIError`` into the MCP's ``LobstrAPIError``.

    The tool error contract (errors.py) keys off the parsed upstream envelope
    (``errors.type`` / ``errors.message``, else ``detail`` / ``message``). The
    SDK carries the raw body on the exception, so we re-run that parsing here
    and leave errors.py and every tool untouched.
    """
    try:
        yield
    except APIError as exc:
        body = exc.body if isinstance(exc.body, dict) else {}
        error_type: str | None = None
        message: str | None = None
        envelope = body.get("errors")
        if isinstance(envelope, dict):
            et, msg = envelope.get("type"), envelope.get("message")
            error_type = et if isinstance(et, str) else None
            message = msg if isinstance(msg, str) else None
        else:
            for key in ("detail", "message"):
                val = body.get(key)
                if isinstance(val, str):
                    message = val
                    break
        raise LobstrAPIError(exc.status_code, error_type=error_type,
                             message=message, path=path) from exc


class LobstrClient:
    """MCP-facing client. Wraps a lobstrio ``LobstrClient`` and returns dicts."""

    def __init__(self, base_url: str, token: str, *,
                 transport: Any | None = None, timeout: float = 30.0) -> None:
        self._sdk = _SDKClient(token=token, base_url=base_url, timeout=timeout,
                               user_agent=USER_AGENT, transport=transport)
        # The SDK's low-level HTTP client (parses JSON, raises APIError) for the
        # endpoints we pass through unchanged; and its underlying httpx.Client.
        self._llhttp = self._sdk._http
        self._http = self._sdk._http._client

    # --- reads (typed SDK methods -> raw dict) ---

    def list_crawlers(self) -> list[dict]:
        with _as_lobstr_error("/crawlers"):
            return [c.raw for c in self._sdk.crawlers.iter(max_pages=_MAX_PAGES)]

    def get_crawler(self, crawler_hash: str) -> dict:
        with _as_lobstr_error(f"/crawlers/{crawler_hash}"):
            return self._sdk.crawlers.get(crawler_hash).raw

    def get_crawler_params(self, crawler_hash: str) -> dict:
        with _as_lobstr_error(f"/crawlers/{crawler_hash}/params"):
            return self._sdk.crawlers.params(crawler_hash).raw

    def list_squids(self) -> list[dict]:
        with _as_lobstr_error("/squids"):
            return [s.raw for s in self._sdk.squids.iter(max_pages=_MAX_PAGES)]

    def get_squid(self, squid_hash: str) -> dict:
        with _as_lobstr_error(f"/squids/{squid_hash}"):
            return self._sdk.squids.get(squid_hash).raw

    def page_squids(self, *, name: str | None = None, page: int = 1,
                    page_size: int = 50) -> dict:
        """One page of the user's squids WITH the pagination envelope
        (total_results, page, total_pages, next, data). `name` filters
        server-side. Use this for the AI-facing listing; list_squids() walks
        every page and is for internal resolution only."""
        params: dict = {"page": page, "limit": page_size}
        if name:
            params["name"] = name
        with _as_lobstr_error("/squids"):
            return self._llhttp.get("/squids", params=params)

    def get_run_stats(self, run_hash: str) -> dict:
        with _as_lobstr_error(f"/runs/{run_hash}/stats"):
            return self._sdk.runs.stats(run_hash).raw

    def get_run(self, run_hash: str) -> dict:
        """Run detail — authoritative status plus credit_used."""
        with _as_lobstr_error(f"/runs/{run_hash}"):
            return self._sdk.runs.get(run_hash).raw

    def get_run_credits(self, run_hash: str) -> dict:
        """Per-function credit breakdown: total + [{function, credits,
        attempts}, ...]."""
        with _as_lobstr_error(f"/runs/{run_hash}/credits"):
            return self._llhttp.get(f"/runs/{run_hash}/credits")

    def get_balance(self) -> dict:
        with _as_lobstr_error("/user/balance"):
            return self._sdk.balance().raw

    def whoami(self) -> dict:
        with _as_lobstr_error("/me"):
            return self._llhttp.get("/me")

    def list_runs(self, squid_hash: str, *, limit: int = 20) -> list[dict]:
        with _as_lobstr_error("/runs"):
            return [r.raw for r in self._sdk.runs.list(squid=squid_hash, limit=limit)]

    def get_run_download_url(self, run_hash: str, file_format: str | None = None):
        """A signed download URL, or `{"status": "processing", ...}` when a
        non-default format on a large run is still being built — not an
        error, the caller should retry."""
        params = {"file_format": file_format} if file_format else None
        with _as_lobstr_error(f"/runs/{run_hash}/download"):
            data = self._llhttp.get(f"/runs/{run_hash}/download", params=params)
        if isinstance(data, dict) and "s3" in data:
            return data["s3"]
        return data

    def abort_run(self, run_hash: str) -> dict:
        with _as_lobstr_error(f"/runs/{run_hash}/abort"):
            return self._sdk.runs.abort(run_hash)

    def list_tasks(self, squid_hash: str) -> list[dict]:
        """Every task row currently saved on a squid.

        Only the task `params` are round-trippable (they are what POST /tasks
        takes), so that is what callers rebuild from — see execution.py, which
        snapshots them before emptying a squid it is about to rewrite.
        """
        with _as_lobstr_error("/tasks"):
            return [{"id": t.id, "is_active": t.is_active, "params": t.params}
                    for t in self._sdk.tasks.iter(squid=squid_hash,
                                                  max_pages=_MAX_PAGES)]

    def empty_squid(self, squid_hash: str) -> dict:
        with _as_lobstr_error(f"/squids/{squid_hash}/empty"):
            return self._sdk.squids.empty(squid_hash)

    def set_squid_active(self, squid_hash: str, active: bool) -> dict:
        # POST /squids/{id} with is_active toggles activation; the API frees the
        # concurrency slot (reset_slots) on deactivate. Pass-through so the flag
        # reaches the API (the SDK's typed update() whitelists other keys).
        with _as_lobstr_error(f"/squids/{squid_hash}"):
            return self._llhttp.post(f"/squids/{squid_hash}", json={"is_active": active})

    def list_accounts(self, *, limit: int = 50, page: int = 1,
                      type: str | None = None, status: int | str | None = None) -> dict:
        params: dict = {"limit": limit, "page": page}
        if type:
            params["type"] = type
        if status is not None:
            params["status"] = status
        with _as_lobstr_error("/accounts"):
            return self._llhttp.get("/accounts", params=params)

    def get_account(self, account_id: str) -> dict:
        with _as_lobstr_error(f"/accounts/{account_id}"):
            return self._llhttp.get(f"/accounts/{account_id}")

    # --- execute path (P3): writes + pass-through ---
    # These go through the SDK's low-level HTTP client rather than its typed
    # helpers, so requests/responses stay byte-identical to the old client:
    # create_squid keeps SDK typing; update_squid must forward an arbitrary
    # settings dict (the SDK's update() whitelists kwargs); tasks.add() returns
    # a model without .raw; and start_run must keep the upstream error message
    # verbatim (the SDK's runs.start() rewrites it).

    def create_squid(self, crawler: str, name: str | None = None) -> dict:
        with _as_lobstr_error("/squids"):
            return self._sdk.squids.create(crawler, name=name).raw

    def update_squid(self, squid_hash: str, settings: dict) -> dict:
        with _as_lobstr_error(f"/squids/{squid_hash}"):
            return self._llhttp.post(f"/squids/{squid_hash}", json=settings)

    def delete_squid(self, squid_hash: str) -> dict:
        """Delete a squid outright (not reversible, unlike deactivate/empty)."""
        with _as_lobstr_error(f"/squids/{squid_hash}"):
            return self._sdk.squids.delete(squid_hash)

    def add_tasks(self, squid_hash: str, tasks: list[dict]) -> dict:
        with _as_lobstr_error("/tasks"):
            return self._llhttp.post("/tasks", json={"squid": squid_hash, "tasks": tasks})

    def estimate_squid(self, squid_hash: str) -> dict:
        # Authoritative API estimate (POST /squid/estimate); needs the squid to
        # exist with >=1 task. SDK squids.estimate returns the raw estimate dict.
        with _as_lobstr_error("/squid/estimate"):
            return self._sdk.squids.estimate(squid_hash)

    def start_run(self, squid_hash: str) -> dict:
        with _as_lobstr_error("/runs"):
            return self._llhttp.post("/runs", json={"squid": squid_hash})

    def get_results(self, *, squid: str | None = None, run: str | None = None,
                    task: str | None = None, page: int = 1,
                    page_size: int | None = None) -> dict:
        params: dict = {"page": page}
        if squid:
            params["squid"] = squid
        if run:
            params["run"] = run
        if task:
            params["task"] = task
        if page_size:
            params["page_size"] = page_size
        with _as_lobstr_error("/results"):
            return self._llhttp.get("/results", params=params)

    def user_scope(self) -> str:
        """Opaque, stable-per-identity string for scoping local state (the
        idempotency store) — never the raw token."""
        auth_header = self._http.headers.get("authorization", "")
        return hashlib.sha256(auth_header.encode("utf-8")).hexdigest()[:32]

    def close(self) -> None:
        self._sdk.close()
