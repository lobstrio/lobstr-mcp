"""Composable execution primitives: create_squid, add_tasks, estimate_run.

`run_scraper` stays the one-call default (create → configure → add tasks → run in
one shot). These primitives expose the same steps individually so an agent can
build or reuse a squid incrementally — create it, add tasks across several calls,
get an authoritative cost estimate, then run it via run_scraper(squid_id=...).

`config`/`tasks` values go through the same alias translation as run_scraper's
`input` (see schema_translator.translate_input_schema), so a published alias
(e.g. `squid_country`) works here too, not just the API's own name.
"""
from __future__ import annotations

import httpx

from lobstr_mcp.auth.scopes import EXECUTE_SCOPES, READ_SCOPES
from lobstr_mcp.errors import LobstrAPIError, structured, to_error_dict
from lobstr_mcp.lobstr_client import LobstrClient, resolve_crawler_id
from lobstr_mcp.render import toon_result
from lobstr_mcp.schema_translator import translate_input_schema


def _translated_schema(client: LobstrClient, crawler_id: str) -> dict:
    """Crawler schema (properties/levels/wire_names); tolerates /params
    being unavailable."""
    crawler = client.get_crawler(crawler_id)
    try:
        crawler_params = client.get_crawler_params(crawler_id)
    except Exception:
        crawler_params = None
    return translate_input_schema(crawler, params=crawler_params)


def _apply_wire_names(values: dict, wire_names: dict) -> dict:
    """Map each alias key to the API's real name; also inside "functions"."""
    out = {wire_names.get(k, k): v for k, v in values.items() if k != "functions"}
    functions = values.get("functions")
    if isinstance(functions, dict):
        out["functions"] = {wire_names.get(k, k): v for k, v in functions.items()}
    return out


def _squid_and_wire_names(client: LobstrClient, squid_id: str) -> tuple[dict, dict]:
    """(wire_names, squid) for an existing squid; best-effort, skips
    translation on failure rather than failing the caller's request."""
    try:
        squid = client.get_squid(squid_id)
    except Exception:
        return {}, {}
    squid = squid if isinstance(squid, dict) else {}
    crawler_id = squid.get("crawler")
    if not crawler_id:
        return {}, squid
    try:
        translated = _translated_schema(client, crawler_id)
    except Exception:
        return {}, squid
    return translated.get("wire_names") or {}, squid


def _remediation_tail(squid_id: str) -> str:
    """The two ways out of a squid left by a failed config-apply call, shared
    by both failure paths below (only the opening clause differs — what each
    path actually knows about whether the config saved).

    `config` accepts squid-level params directly, and function-level toggles
    nested under a "functions" key within it (see get_scraper_details'
    param_levels) — never task-level fields, those go through add_tasks. A
    function-level param sent flat instead of nested is the rejection seen in
    production.

    run_scraper's `input` is different: it must be flat. run_scraper
    classifies every key by level itself and does the "functions" nesting
    internally — nesting a value there by hand isn't rejected, it's just an
    unrecognized key, so validation lets it through, classifies it as
    task-level, and ships it in the task row: the setting is silently
    dropped and the run still bills. Keep this clause worded so it can't be
    read together with the `config` clause above the wrong way.
    """
    return (
        "Retrying create_squid with the same name will fail (name taken). To fix it "
        "without running anything: call create_squid again under a different name with "
        "the corrected config (check get_scraper_details' param_levels first — "
        "function-level params go under a \"functions\" key in config, never flat; "
        "task-level fields don't belong in config at all, add them with add_tasks) — then, "
        f"once you've confirmed squid_id='{squid_id}' isn't needed, call "
        f"deactivate_scraper(squid_id='{squid_id}') so the half-built one isn't left holding "
        "a concurrency slot for nothing. To fix and reuse this same squid instead, call "
        f"run_scraper(squid_id='{squid_id}', input=<the crawler's full input: its task-level "
        "fields together with the corrected squid- and function-level ones, all FLAT — unlike "
        "`config`, run_scraper's `input` takes no nesting, it classifies each key by level and "
        "nests the function-level ones under \"functions\" itself>). This starts a run and "
        "spends credits immediately, unless the cost is unknown or above your confirmation "
        "threshold, in which case it asks for confirm=true first — it is not run-free like the "
        "option above."
    )


def _config_rejected(squid_id: str, crawler_id: str, upstream_message: str) -> dict:
    """The config-apply call got a response and the API rejected it: nothing
    was saved, full stop — this is the one path that can say so."""
    return {
        "squid_id": squid_id, "scraper": crawler_id,
        "message": (
            f"Squid {squid_id} was created but its configuration was rejected, so it exists "
            f"with no saved config and is not runnable yet: {upstream_message} "
            + _remediation_tail(squid_id)
        ),
    }


def _config_apply_unknown(squid_id: str, crawler_id: str, detail: str) -> dict:
    """The config-apply call failed before a response came back (a transport
    failure — see the call site). Unlike a rejection, the request may have
    reached the server and been applied; this client has no way to tell, so
    it must not claim "rejected" here — that overclaim would lead a caller to
    build a duplicate under a new name and burn a slot for nothing when the
    original squid was actually fine."""
    return {
        "squid_id": squid_id, "scraper": crawler_id, "error_code": "upstream_unavailable",
        "message": (
            f"Squid {squid_id} was created, but the request to save its configuration failed "
            f"before a response came back ({detail}) — whether the configuration was saved is "
            f"unknown, not rejected: it may have reached the server anyway. Check first with "
            f"get_my_scraper(squid_id='{squid_id}') (its `params`/`is_ready`) before deciding "
            "what to do next. " + _remediation_tail(squid_id)
        ),
    }


@structured(verify_with="list_my_scrapers(name=...), which lists the scraper if it was created")
def create_squid_impl(client: LobstrClient, scraper: str, name: str | None = None,
                      config: dict | None = None,
                      concurrency: int | None = None) -> dict:
    """Create a squid from a crawler without running it. `concurrency` is a
    squid-level column, not a crawler param — lifted out of `config` if
    passed there and sent top-level either way."""
    crawler_id = resolve_crawler_id(client, scraper)
    cfg = dict(config) if config else {}
    cfg_concurrency = cfg.pop("concurrency", None)
    effective_concurrency = concurrency if concurrency is not None else cfg_concurrency

    wire_names: dict = {}
    if cfg:
        try:
            translated = _translated_schema(client, crawler_id)
            wire_names = translated.get("wire_names") or {}
        except Exception:
            pass
    squid = client.create_squid(crawler=crawler_id, name=name)
    squid_id = squid.get("id")

    body: dict = {"name": name or squid.get("name") or crawler_id}
    if cfg:
        body["params"] = _apply_wire_names(cfg, wire_names)
    if effective_concurrency is not None:
        body["concurrency"] = effective_concurrency

    if cfg or effective_concurrency is not None:
        # The API only saves params when a recognized field (e.g. name) rides
        # along, so always send a name with the params update.
        try:
            client.update_squid(squid_id, body)
        except LobstrAPIError as exc:
            base = to_error_dict(exc)
            return base | _config_rejected(squid_id, crawler_id, base["message"])
        except httpx.TransportError as exc:
            # Nothing upstream of this client wraps a transport failure
            # (timeout, connection reset, ...) into LobstrAPIError — only a
            # non-2xx response goes through that path (lobstr_client.py's
            # _as_lobstr_error only catches the SDK's APIError). The squid
            # still exists either way, so this must not lose its id either.
            # Caught specifically (not a bare `except Exception`): a bug in
            # our own code here must still surface as a bug — with a
            # traceback — not read to a model as a retryable API outage.
            return _config_apply_unknown(squid_id, crawler_id, str(exc) or type(exc).__name__)
    return {"squid_id": squid_id, "scraper": crawler_id,
            "name": name or squid.get("name"), "concurrency": effective_concurrency,
            "message": "Squid created. Add inputs with add_tasks, then run it with "
                       "run_scraper(squid_id=...). estimate_run gives the cost first."}


@structured(verify_with="estimate_run(squid_id=...), whose `tasks.count` is how many input "
                        "rows the scraper holds now")
def add_tasks_impl(client: LobstrClient, squid_id: str, tasks: list[dict]) -> dict:
    """Add task rows (inputs) to an existing squid. Each task is a dict of the
    crawler's task-level inputs (e.g. {"url": "..."})."""
    if not tasks:
        return {"error_code": "invalid_request",
                "message": "provide at least one task row in `tasks`"}
    wire_names, _ = _squid_and_wire_names(client, squid_id)
    tasks = [_apply_wire_names(t, wire_names) if isinstance(t, dict) else t for t in tasks]
    result = client.add_tasks(squid_id, tasks)
    result = result if isinstance(result, dict) else {}
    added = result.get("tasks")
    added_count = len(added) if isinstance(added, list) else len(tasks)
    return {"squid_id": squid_id, "added": added_count,
            "duplicated": result.get("duplicated_count"),
            "message": "Tasks added. Check cost with estimate_run, then run with "
                       "run_scraper(squid_id=...)."}


# What the API's estimate counts, and what it does not. Both are things a
# model has to know to use the numbers, and neither is in the payload.
_CREDITS_NOTE = (
    "total_credits is authoritative; run_scraper's own pre-run estimate is an upper bound, "
    "and neither counts result filters priced per row kept."
)
_TIME_NOTE = (
    "estimated_time is a floor, not an ETA — a filtered run takes longer than this "
    "suggests; poll get_run for real progress."
)


@structured
def estimate_run_impl(client: LobstrClient, squid_id: str) -> dict:
    """Authoritative pre-run estimate from the API for a squid that already has
    tasks (credits, projected results, time)."""
    est = client.estimate_squid(squid_id)
    est = est if isinstance(est, dict) else {}
    return {"squid_id": squid_id,
            "total_credits": est.get("total_credits"),
            "estimate_note": _CREDITS_NOTE,
            "estimated_time": est.get("estimated_time"),
            "estimated_time_note": _TIME_NOTE,
            "max_results": est.get("max_results"),
            "services": est.get("services"),
            "tasks": est.get("tasks"),
            "recommended_upgrade_plan": est.get("recommended_upgrade_plan")}


def register_primitive_tools(mcp, client_factory, authorizer=None) -> None:
    def authz(scopes):
        if authorizer:
            authorizer(scopes)

    @mcp.tool(annotations={"title": "Create Squid", "readOnlyHint": False,
                           "destructiveHint": False, "idempotentHint": False,
                           "openWorldHint": True})
    def create_squid(scraper: str, name: str | None = None,
                     config: dict | None = None,
                     concurrency: int | None = None) -> dict:
        """Create a new squid (a saved, configured scraper instance) from a
        crawler — WITHOUT running it. Optionally set a `name` and `config`:
        check get_scraper_details' param_levels first — `config` takes
        "squid"-level params directly (including any published alias, e.g.
        `squid_country`) and "function"-level ones nested under a "functions"
        key within it, but never "task"-level fields (those go through
        add_tasks, not here). `concurrency` is a top-level field, not a
        crawler param — pass it here or inside `config` (lifted out either
        way). Then add inputs with add_tasks and run it with
        run_scraper(squid_id=...). For a one-shot scrape, prefer run_scraper,
        which does all of this in a single call.

        If `config` is rejected, the squid still exists — its id is in the
        error — with no saved configuration and is not runnable. If saving it
        fails outright instead (e.g. a timeout), the squid still exists but
        whether the configuration saved is unknown, not rejected — the
        request may have reached the server anyway. Either way the error
        explains how to check and what to do next. Retrying create_squid with
        the same name fails either way (the name is taken)."""
        authz(EXECUTE_SCOPES)
        return create_squid_impl(client_factory(), scraper, name=name, config=config,
                                 concurrency=concurrency)

    @mcp.tool(annotations={"title": "Add Tasks", "readOnlyHint": False,
                           "destructiveHint": False, "idempotentHint": False,
                           "openWorldHint": True})
    def add_tasks(squid_id: str, tasks: list[dict]) -> dict:
        """Add task rows (inputs) to an existing squid. `tasks` is a list of
        dicts of the crawler's task-level inputs (e.g. {"url": "..."}),
        including any published alias. Can be called repeatedly before
        running."""
        authz(EXECUTE_SCOPES)
        return add_tasks_impl(client_factory(), squid_id, tasks)

    @mcp.tool(annotations={"title": "Estimate Run", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def estimate_run(squid_id: str, toon: bool = False) -> dict:
        """Authoritative pre-run cost & result estimate for a squid that already
        has tasks — credits, projected results, and estimated time, straight from
        the API (more accurate than run_scraper's built-in upper-bound). The squid
        must have at least one task.

        `total_credits` covers the per-row price plus each paid extra step that
        is on; the per-row result *filters* are priced too and are in neither
        this figure nor run_scraper's. `estimated_time` is a floor, not an ETA:
        it assumes every row fetched is kept, so a filtered run routinely takes
        several times longer without anything being wrong. The response repeats
        both caveats in `estimate_note` and `estimated_time_note` — report a run
        against them rather than calling a run late or failed on this number.

        Pass toon=true for compact TOON."""
        authz(READ_SCOPES)
        out = estimate_run_impl(client_factory(), squid_id)
        return toon_result(out) if toon else out
