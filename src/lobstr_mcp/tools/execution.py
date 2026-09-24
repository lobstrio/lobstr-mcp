from __future__ import annotations

from lobstr_mcp.auth.scopes import EXECUTE_SCOPES, RESULTS_READ_SCOPES, RUN_READ_SCOPES
from lobstr_mcp.execution import (
    abort_run_impl,
    get_results_impl,
    get_results_url_impl,
    get_run_impl,
    list_runs_impl,
    run_scraper_impl,
    wait_for_run_impl,
)
from lobstr_mcp.render import toon_result


def register_execution_tools(mcp, client_factory, settings, idem_store,
                             authorizer=None) -> None:
    def authz(scopes):
        if authorizer:
            authorizer(scopes)

    @mcp.tool(annotations={"title": "Run Scraper",
                           "readOnlyHint": False,
                           # Creates a run and spends credits. It destroys
                           # saved inputs only when the caller explicitly
                           # passes replace_tasks=true, so the tool's default
                           # behaviour is a create/write, not a destructive op.
                           "destructiveHint": False,
                           "idempotentHint": False,
                           "openWorldHint": True})
    def run_scraper(scraper: str | None = None, input: dict | None = None,
                    confirm: bool = False, idempotency_key: str | None = None,
                    squid_id: str | None = None, account_id: str | None = None,
                    replace_tasks: bool = False) -> dict:
        """Configure and run a Lobstr scraper. Consumes credits. Jobs whose
        estimated cost exceeds the credit threshold (or whose cost is unknown)
        return needs_confirmation; call again with confirm=true to execute.
        Returns a run_id to monitor with get_run.

        Pass `scraper` (a crawler slug/id) to run a NEW scraper — this creates
        a saved scraper (squid) holding only the input you pass. Pass
        `squid_id` (from list_my_scrapers) to RE-RUN a scraper that already
        exists instead of piling up new ones.

        A squid keeps a list of saved inputs (task rows) and EVERY run scrapes
        all of them — these may be rows its owner saved in the dashboard, not
        yours. So, re-running with `squid_id`:
        - no `input`: it runs the saved inputs exactly as they are;
        - `input` with a task-level field (url, query, …) and the squid has no
          saved inputs yet: yours is added and is the only one;
        - `input` with a task-level field and the squid ALREADY has saved
          inputs: this refuses and changes nothing, because your input can only
          be applied by deleting those rows or by scraping them alongside
          yours. The error (`squid_has_tasks`) gives their count and `options`,
          a list of ready-to-call tool calls (run them as-is, run yours on a
          separate scraper, add yours and run all — `cost_multiplier_if_added`
          says how many times the single-input cost — or replace_tasks=true);
        - `input` with only scraper-level settings (max_results, …): they are
          merged into the saved settings, the saved inputs are untouched, and
          the run scrapes all of them.

        `replace_tasks=true` (only meaningful with `squid_id` and a task-level
        `input`) DELETES every input that squid has saved and leaves only
        yours. Pass it only when the caller asked to replace them. If a step of
        the replacement fails the previous inputs are put back; in the rare
        case they can't be, the response says so and carries them verbatim
        under `lost_tasks`.

        Every successful response says what happened to the saved inputs:
        `tasks_action` ("created", "added", "replaced" or "unchanged"),
        `task_count` (how many rows this run scrapes, null if unknown) and a
        one-line `tasks_note`. Check them before reporting what the run does —
        `task_count` above 1 means the run is scraping rows you did not pass.

        The cost estimate covers every row the run scrapes, and the paid extra
        steps that are on, so a re-run over many saved rows asks for
        confirmation where a single-row run would not. It is an upper bound
        (it assumes the row cap is reached); `estimate_run(squid_id=...)` is
        the API's own figure. Neither includes email verification, billed
        separately after the scrape when `auto_verify_emails` is on — when it
        is, `estimate.verification_note` sizes it from `credits_per_email`;
        add that on top yourself, it's never folded into `estimate.credits`.
        Affordability is the API's call, not this tool's:
        it refuses an ordinary account whose period spend has reached its
        allowance and lets a staff/admin account run regardless — a refusal
        comes back as `insufficient_credits` with nothing spent, config and
        rows already saved. A thin-but-affordable balance instead rides on the
        success response as a one-line `credit_warning`, never a refusal.

        Some scrapers need a connected platform account (e.g. LinkedIn Leads,
        Sales Navigator Leads) — without one the run is created but fails with
        done_reason "no_accounts". This is handled automatically when exactly
        one healthy, unlocked, right-type account is connected; pass
        `account_id` to pick a specific one (never refused for being
        unhealthy — the response carries `account_warning` instead). With
        several such candidates, this returns an error instead of guessing —
        call list_accounts, then retry with account_id, or call
        attach_account first. With none connected at all, there's nothing to
        list — connect one from your Lobstr dashboard first (a locked
        candidate is reported separately from "none," since LinkedIn/Sales
        Navigator locks are routine and transient: attach it explicitly by id
        if you want to use it anyway). Existing account links are never
        removed, only added to. The response's `account_attached` reports
        whether the squid has an account attached now (true whether this call
        attached it or an earlier one did) — not whether this specific call
        was the one that attached it.

        `idempotency_key`, when passed, is your own retry token: calling again
        with it replays the same run_id. Without one, an identical call
        (scraper/squid_id + input) is deduped for a couple of minutes only —
        enough for a retry storm, not a genuine later re-run — and scoped to
        your own account.

        If the API never answers (timeout, dropped connection), the error says
        which of two things happened, and they need different handling:
        `request_state` "not_sent" means the request never reached Lobstr, so
        nothing ran and retrying is safe; "unknown" means it may have been
        applied — check `list_runs(squid_id=...)` for a run that already
        started before retrying, or the same run gets paid for twice.

        Some scrapers accept alternative input sets (e.g. Google Maps: a `url`
        OR `category`+`country`+`city`) — call get_scraper_details first and read
        its `input_modes`. A few name two different inputs the same at two
        levels; the schema publishes the second under a prefixed alias (e.g.
        `squid_country`, Google's search region, beside the task-level
        `country` that goes into the search itself) and `input` takes that
        alias — this call sends it under the API's own name. Concepts &
        pricing: https://docs.lobstr.io/mcp"""
        authz(EXECUTE_SCOPES)
        return run_scraper_impl(client_factory(), settings, idem_store,
                                scraper=scraper, input=input, confirm=confirm,
                                idempotency_key=idempotency_key, squid_id=squid_id,
                                account_id=account_id, replace_tasks=replace_tasks)

    @mcp.tool(annotations={"title": "Get Run Status", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def get_run(run_id: str, full: bool = False, toon: bool = False) -> dict:
        """Check the status/progress of a run. Returns JSON; pass toon=true for
        compact TOON. Pass full=true to also include the raw stats blob and a
        `credits_breakdown` (per-function credits/attempts; omitted on older
        runs).

        `is_done` is true only once the run, any email verification, and its
        export are all done — not merely once scraping stopped. While
        verification is still running, `status` reads "verifying_emails"
        (credits for it are still being billed); `run_status` always carries
        the API's own raw status. `email_verification` (when present) gives
        its own progress/counts, `export_done` says whether the downloadable
        file is ready. `total_unique_results` sits next to `total_results`;
        get_results' own total_results is the count to trust for fetchable
        rows.

        wait_for_run(run_id=...) polls this for you."""
        authz(RUN_READ_SCOPES)
        out = get_run_impl(client_factory(), run_id, full=full)
        return toon_result(out) if toon else out

    @mcp.tool(annotations={"title": "Wait For Run", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def wait_for_run(run_id: str, timeout_seconds: float = 30.0,
                     toon: bool = False) -> dict:
        """Poll get_run until it's fully done or `timeout_seconds` elapses
        (capped at 50s regardless of what's passed). Returns get_run's shape;
        if still going, `status` is "still_running" and `timed_out: true` —
        call again to keep checking."""
        authz(RUN_READ_SCOPES)
        out = wait_for_run_impl(client_factory(), run_id, timeout_seconds=timeout_seconds)
        return toon_result(out) if toon else out

    @mcp.tool(annotations={"title": "Get Results", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def get_results(run_id: str | None = None, squid_id: str | None = None,
                    page: int = 1, page_size: int | None = None,
                    fields: list[str] | None = None, full: bool = False,
                    toon: bool = False) -> dict:
        """Retrieve one page of results for a run or squid. Returns JSON; pass
        toon=true for compact TOON (fewer tokens). `page_size` (default 10, 25
        when full=true, capped at 100) is the number of rows fetched AND
        returned — `returned`/`total_pages`/`next` all describe that same
        page_size, so raising it is how you get more rows per call, not
        `page`. By default empty fields are dropped; the response lists
        `available_fields` you can request via `fields`. Pass full=true to keep
        every field (including empty ones) and a bigger default page. Provide
        exactly one of run_id or squid_id.

        Free-plan accounts are capped at the first 30 results by the API; past
        that this returns `export_limit_reached` rather than more rows —
        upgrading the plan is the only fix, not a different page_size."""
        authz(RESULTS_READ_SCOPES)
        out = get_results_impl(client_factory(), run_id=run_id, squid_id=squid_id,
                               page=page, page_size=page_size, fields=fields, full=full)
        return toon_result(out) if toon else out

    @mcp.tool(annotations={"title": "List Runs", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def list_runs(squid_id: str, limit: int = 20, toon: bool = False) -> dict:
        """List recent runs for a squid (a scraper you've configured) — status,
        result counts, credits, timings. Returns JSON; pass toon=true for
        compact TOON."""
        authz(RUN_READ_SCOPES)
        out = list_runs_impl(client_factory(), squid_id, limit=limit)
        return toon_result(out) if toon else out

    @mcp.tool(annotations={"title": "Get Results Download URL", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def get_results_url(run_id: str, format: str = "csv") -> dict:
        """Get a signed URL to download a run's full result set as a file — use
        this instead of get_results when the caller wants the whole dataset
        rather than paged rows. `format` is "csv" (default), "xlsx", "json" or
        "jsonl". A format other than csv on a large run can take a moment to
        build server-side; when it isn't ready yet this returns
        `status: "processing"` instead of `download_url` — call again shortly."""
        authz(RESULTS_READ_SCOPES)
        return get_results_url_impl(client_factory(), run_id, format=format)

    @mcp.tool(annotations={"title": "Abort Run",
                           "readOnlyHint": False, "destructiveHint": False,
                           "idempotentHint": True, "openWorldHint": True})
    def abort_run(run_id: str) -> dict:
        """Stop a running job. The run stops shortly after; poll get_run to
        confirm. Already-scraped results are kept."""
        authz(EXECUTE_SCOPES)
        return abort_run_impl(client_factory(), run_id)
