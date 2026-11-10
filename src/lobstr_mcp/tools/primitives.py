"""Composable execution primitives: create_squid, add_tasks, update_scraper,
estimate_run.

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
from lobstr_mcp.execution import _effective_settings
from lobstr_mcp.lobstr_client import LobstrClient, resolve_crawler_id
from lobstr_mcp.render import toon_result
from lobstr_mcp.safeguards import validate_input, verification_cost_note
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


WEBHOOK_EVENTS = ("run.running", "run.paused", "run.done", "run.error")
DEFAULT_WEBHOOK_EVENTS = ("run.done", "run.error")
_WEBHOOK_KEYS = ("url", "events", "retry", "is_active")


def _lift_webhook(cfg: dict, webhook):
    """A webhook is a squid field, not a crawler setting: one passed inside
    `config` (as `webhook`, or the API's own `webhook_fields`) is lifted out,
    like `concurrency`. Sent inside params, the API refused it with "The
    specified parameter webhook_fields is invalid." (fluxea.io, 09/11/2026)."""
    for key in ("webhook", "webhook_fields"):
        if key in cfg:
            lifted = cfg.pop(key)
            if webhook is None:
                webhook = lifted
    return webhook


def _webhook_fields(webhook, saved=None) -> tuple[dict | None, list[str]]:
    """(the API's webhook_fields, errors) for a `webhook` argument.

    A key left out keeps the value of the webhook already saved on the squid
    (`saved`, when it has a URL), else its default: events run.done and
    run.error, retry on. `is_active` is true unless passed false."""
    if not isinstance(webhook, dict):
        return None, ['webhook must be an object, e.g. {"url": "https://example.com/hook", '
                      '"events": ["run.done"]}']
    errors = []
    unknown = sorted(set(webhook) - set(_WEBHOOK_KEYS))
    if unknown:
        errors.append(f"unknown webhook key(s) {unknown}: use {', '.join(_WEBHOOK_KEYS)}")
    saved = saved if isinstance(saved, dict) and saved.get("url") else {}

    url = webhook.get("url") or saved.get("url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        errors.append("webhook.url must be an http(s) URL")

    events = webhook.get("events")
    if events is None:
        events = saved.get("events") or list(DEFAULT_WEBHOOK_EVENTS)
    if isinstance(events, str):
        events = [events]
    if isinstance(events, dict):  # the API's own shape: {"run.done": true, ...}
        bad = [e for e, on in events.items() if not isinstance(on, bool)]
        if bad:
            errors.append(f"webhook.events values must be true or false: {bad}")
        unknown_events = [e for e in events if e not in WEBHOOK_EVENTS]
        events = [e for e, on in events.items() if on is True]
    elif isinstance(events, list):
        unknown_events = [e for e in events if e not in WEBHOOK_EVENTS]
    else:
        unknown_events, events = [], []
        errors.append("webhook.events must be a list of event names")
    if unknown_events:
        errors.append(f"unknown webhook event(s) {unknown_events}: use {', '.join(WEBHOOK_EVENTS)}")
    elif not events and not any(e.startswith("webhook.events") for e in errors):
        errors.append("webhook.events is empty: pick at least one of "
                      f"{', '.join(WEBHOOK_EVENTS)}, or pass is_active false to turn it off")

    retry = webhook.get("retry", saved.get("retry", True))
    is_active = webhook.get("is_active", True)
    for key, value in (("retry", retry), ("is_active", is_active)):
        if not isinstance(value, bool):
            errors.append(f"webhook.{key} must be true or false")
    if errors:
        return None, errors
    return {"url": url, "is_active": is_active, "retry": retry,
            "events": {e: e in events for e in WEBHOOK_EVENTS}}, []


def _webhook_view(fields: dict) -> dict:
    """A saved webhook as the tools show it: events as a list, like the input."""
    events = fields.get("events") or {}
    return {"url": fields.get("url"), "events": [e for e in WEBHOOK_EVENTS if events.get(e)],
            "retry": fields.get("retry"), "is_active": fields.get("is_active")}


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
        f"deactivate_scraper(squid_id='{squid_id}') so the half-built one isn't left "
        "behind for nothing. To fix and reuse this same squid instead, call "
        f"run_scraper(squid_id='{squid_id}', input=<the crawler's full input: its task-level "
        "fields together with the corrected squid- and function-level ones, all FLAT — unlike "
        "`config`, run_scraper's `input` takes no nesting, it classifies each key by level and "
        "nests the function-level ones under \"functions\" itself>). This starts a run and "
        "spends credits immediately, unless the cost is unknown or above your confirmation "
        "threshold, in which case it asks for confirm=true first — it is not run-free like the "
        "option above."
    )


def _cleanup_squid(client: LobstrClient, squid_id: str) -> bool:
    """Delete a squid whose config was just rejected; returns whether the
    delete itself succeeded."""
    try:
        client.delete_squid(squid_id)
        return True
    except Exception:
        return False


def _config_rejected(client: LobstrClient, squid_id: str, crawler_id: str,
                     upstream_message: str) -> dict:
    """Config rejected: delete the now-useless squid rather than leaving an
    orphan; if that cleanup fails too, name squid_id and both ways out."""
    if _cleanup_squid(client, squid_id):
        return {
            "squid_id": squid_id, "scraper": crawler_id, "deleted": True,
            "message": (
                f"Configuration was rejected: {upstream_message} Squid {squid_id} (created "
                "for this call) was deleted, so nothing is left behind — no concurrency slot "
                "spent, nothing to clean up by hand. Fix the config (check "
                "get_scraper_details' param_levels: function-level params go under a "
                "\"functions\" key, never flat; task-level fields don't belong in config at "
                "all, add them with add_tasks) and call create_squid again."
            ),
        }
    return {
        "squid_id": squid_id, "scraper": crawler_id, "deleted": False,
        "message": (
            f"Squid {squid_id} was created but its configuration was rejected, so it exists "
            f"with no saved config and is not runnable yet: {upstream_message} It could not "
            "be deleted automatically either, so it is still there under that id. "
            + _remediation_tail(squid_id)
        ),
    }


def _config_apply_unknown(squid_id: str, crawler_id: str, detail: str) -> dict:
    """The config-apply call failed before a response came back (a transport
    failure — see the call site). Unlike a rejection, the request may have
    reached the server and been applied; this client has no way to tell, so
    it must not claim "rejected" here — that overclaim would lead a caller to
    build a duplicate under a new name and leave it behind for nothing when the
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
                      concurrency: int | None = None,
                      webhook: dict | None = None) -> dict:
    """Create a squid from a crawler without running it. `concurrency` and
    `webhook` are squid-level fields, not crawler params — lifted out of
    `config` if passed there and sent on their own either way."""
    crawler_id = resolve_crawler_id(client, scraper)
    cfg = dict(config) if config else {}
    cfg_concurrency = cfg.pop("concurrency", None)
    effective_concurrency = concurrency if concurrency is not None else cfg_concurrency
    webhook = _lift_webhook(cfg, webhook)
    webhook_fields = None
    if webhook is not None:
        # Checked before the squid exists, like config, so a bad webhook leaves no orphan.
        webhook_fields, errors = _webhook_fields(webhook)
        if errors:
            return {"error_code": "validation_error", "errors": errors, "scraper": crawler_id}

    wire_names: dict = {}
    if cfg:
        # Best-effort schema lookup; a failure just skips translation/validation.
        try:
            translated = _translated_schema(client, crawler_id)
        except Exception:
            translated = None
        if translated:
            wire_names = translated.get("wire_names") or {}
            # Validate before creating the squid, so an obviously-wrong
            # config never gets the chance to leave an orphan.
            flattened = {k: v for k, v in cfg.items() if k != "functions"}
            functions = cfg.get("functions")
            if isinstance(functions, dict):
                flattened.update(functions)
            errors = validate_input(flattened, translated["json_schema"], check_required=False)
            if errors:
                return {"error_code": "validation_error", "errors": errors,
                        "scraper": crawler_id}
            levels = translated.get("levels") or {}
            for k, v in (translated.get("defaults_to_fill") or {}).items():
                # task-level defaults belong to add_tasks; as squid params the API rejects them
                if levels.get(k) == "task":
                    continue
                target = cfg.setdefault("functions", {}) if levels.get(k) == "function" else cfg
                target.setdefault(k, v)

    squid = client.create_squid(crawler=crawler_id, name=name)
    squid_id = squid.get("id")

    body: dict = {"name": name or squid.get("name") or crawler_id}
    if cfg:
        body["params"] = _apply_wire_names(cfg, wire_names)
    if effective_concurrency is not None:
        body["concurrency"] = effective_concurrency

    if cfg or effective_concurrency is not None:
        try:
            client.update_squid(squid_id, body)
        except LobstrAPIError as exc:
            base = to_error_dict(exc)
            return base | _config_rejected(client, squid_id, crawler_id, base["message"])
        except httpx.TransportError as exc:
            # Unknown whether it landed — do not delete a squid that might be fine.
            return _config_apply_unknown(squid_id, crawler_id, str(exc) or type(exc).__name__)
    out = {"squid_id": squid_id, "scraper": crawler_id,
           "name": name or squid.get("name"), "concurrency": effective_concurrency,
           "message": "Squid created. Add inputs with add_tasks, then run it with "
                      "run_scraper(squid_id=...). estimate_run gives the cost first."}
    if webhook_fields is not None:
        try:
            saved = client.set_webhook(squid_id, webhook_fields)
        except LobstrAPIError as exc:
            base = to_error_dict(exc)
            return base | _config_rejected(client, squid_id, crawler_id, base["message"])
        except httpx.TransportError as exc:
            return _config_apply_unknown(squid_id, crawler_id, str(exc) or type(exc).__name__)
        saved = saved.get("webhook_fields") if isinstance(saved, dict) else None
        out["webhook"] = _webhook_view(saved or webhook_fields)
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


@structured(verify_with="estimate_run(squid_id=...), whose `tasks.count` is how many input "
                        "rows the scraper holds now")
def add_tasks_impl(client: LobstrClient, squid_id: str, tasks: list[dict]) -> dict:
    """Add task rows (inputs) to an existing squid. Each task is a dict of the
    crawler's task-level inputs (e.g. {"url": "..."})."""
    if not tasks:
        return {"error_code": "invalid_request",
                "message": "provide at least one task row in `tasks`"}
    wire_names, _ = _squid_and_wire_names(client, squid_id)
    translated_tasks = [_apply_wire_names(t, wire_names) if isinstance(t, dict) else t
                        for t in tasks]
    result = client.add_tasks(squid_id, translated_tasks)
    result = result if isinstance(result, dict) else {}
    added = result.get("tasks")
    added_count = len(added) if isinstance(added, list) else len(tasks)
    return {"squid_id": squid_id, "added": added_count,
            "duplicated": result.get("duplicated_count"),
            "message": "Tasks added. Check cost with estimate_run, then run with "
                       "run_scraper(squid_id=...)."}


@structured(verify_with="get_my_scraper(squid_id=...)")
def update_scraper_impl(client: LobstrClient, squid_id: str, name: str | None = None,
                        config: dict | None = None,
                        concurrency: int | None = None,
                        webhook: dict | None = None) -> dict:
    """Change an existing squid's settings without running it or touching
    its saved tasks. No orphan risk: the squid already exists."""
    cfg = dict(config) if config else {}
    cfg_concurrency = cfg.pop("concurrency", None)
    effective_concurrency = concurrency if concurrency is not None else cfg_concurrency
    webhook = _lift_webhook(cfg, webhook)

    if name is None and not cfg and effective_concurrency is None and webhook is None:
        return {"error_code": "invalid_request",
                "message": "provide at least one of name, config, concurrency, webhook"}

    wire_names, squid = _squid_and_wire_names(client, squid_id)
    webhook_fields = None
    if webhook is not None:
        webhook_fields, errors = _webhook_fields(webhook, squid.get("webhook_fields"))
        if errors:
            return {"error_code": "validation_error", "errors": errors, "squid_id": squid_id,
                    "message": "Nothing was changed."}
    body: dict = {}
    if name is not None:
        body["name"] = name
    if cfg:
        # the API replaces params wholesale, so send the saved ones too
        body["params"] = _effective_settings(squid, _apply_wire_names(cfg, wire_names))
    if effective_concurrency is not None:
        body["concurrency"] = effective_concurrency
    if "params" in body and "name" not in body:
        # A save only takes effect with a recognized field like name riding along.
        body["name"] = squid.get("name") or squid.get("crawler") or squid_id

    updated = client.update_squid(squid_id, body) if body else {}
    updated = updated if isinstance(updated, dict) else {}
    out = {"squid_id": squid_id,
           "name": updated.get("name", body.get("name")),
           "concurrency": updated.get("concurrency", effective_concurrency),
           "message": "Scraper settings updated."}
    if webhook_fields is not None:
        try:
            saved = client.set_webhook(squid_id, webhook_fields)
        except LobstrAPIError as exc:
            if not body:
                raise
            # The settings above are saved; say so rather than "nothing changed".
            return to_error_dict(exc) | {
                "squid_id": squid_id, "settings_saved": True,
                "message": f"Settings saved, but the webhook was refused: {exc.message}"}
        saved = saved.get("webhook_fields") if isinstance(saved, dict) else None
        out["webhook"] = _webhook_view(saved or webhook_fields)
    return out


# What the API's estimate counts, and what it does not. Both are things a
# model has to know to use the numbers, and neither is in the payload.
_CREDITS_NOTE = (
    "total_credits is authoritative; run_scraper's own is an upper bound. Neither counts "
    "result filters, or email verification (see verification_note)."
)
_TIME_NOTE = (
    "estimated_time is a floor, not an ETA — a filtered run takes longer than this "
    "suggests; poll get_run for real progress."
)
# auto_verify_emails is a squid-level column, not a crawler param, and the
# API's estimate leaves its cost out entirely.
def _verification_note(client: LobstrClient, squid_id: str) -> str | None:
    """None unless the squid has email verification on."""
    try:
        squid = client.get_squid(squid_id)
    except Exception:
        return None
    if not isinstance(squid, dict):
        return None
    crawler_id = squid.get("crawler")
    crawler = {}
    if crawler_id:
        try:
            crawler = client.get_crawler(crawler_id)
        except Exception:
            crawler = {}
    return verification_cost_note(crawler if isinstance(crawler, dict) else {},
                                  bool(squid.get("auto_verify_emails")))


@structured
def estimate_run_impl(client: LobstrClient, squid_id: str) -> dict:
    """Authoritative pre-run estimate from the API for a squid that already has
    tasks (credits, projected results, time)."""
    est = client.estimate_squid(squid_id)
    est = est if isinstance(est, dict) else {}
    out = {"squid_id": squid_id,
           "total_credits": est.get("total_credits"),
           "estimate_note": _CREDITS_NOTE,
           "estimated_time": est.get("estimated_time"),
           "estimated_time_note": _TIME_NOTE,
           "max_results": est.get("max_results"),
           "services": est.get("services"),
           "tasks": est.get("tasks"),
           "recommended_upgrade_plan": est.get("recommended_upgrade_plan")}
    verification_note = _verification_note(client, squid_id)
    if verification_note:
        out["verification_note"] = verification_note
    return out


def register_primitive_tools(mcp, client_factory, authorizer=None) -> None:
    def authz(scopes):
        if authorizer:
            authorizer(scopes)

    @mcp.tool(annotations={"title": "Create Squid", "readOnlyHint": False,
                           "destructiveHint": False, "idempotentHint": False,
                           "openWorldHint": True})
    def create_squid(scraper: str, name: str | None = None,
                     config: dict | None = None,
                     concurrency: int | None = None,
                     webhook: dict | None = None) -> dict:
        """Create a new squid from a crawler — WITHOUT running it. `config`
        takes "squid"-level params directly (including any published alias,
        e.g. `squid_country`) and "function"-level ones nested under a
        "functions" key; never "task"-level fields (use add_tasks).
        `concurrency` is a top-level field, not a crawler param — pass it
        here or inside `config` (lifted out either way). Add inputs with
        add_tasks, run with run_scraper(squid_id=...), or change settings
        later with update_scraper. For a one-shot scrape, prefer run_scraper.

        `config` is validated before the squid is created. If the API still
        rejects it afterwards, the now-useless squid is deleted
        (`deleted: true`); only if that cleanup itself fails does it still
        exist, named in the error. A save that fails outright (e.g. a
        timeout) leaves the squid, unknown whether it saved — not deleted.
        Retrying with the same name fails while the squid still exists.

        `webhook` (optional) makes lobstr.io POST to your URL when a run of
        this squid changes state: {"url": "https://...", "events": [...],
        "retry": bool, "is_active": bool}. events: run.running, run.paused,
        run.done, run.error (default run.done and run.error); retry (default
        true) tries a failed delivery 3 more times, 15 minutes apart. It is a
        squid field, not a crawler param: never inside `config`."""
        authz(EXECUTE_SCOPES)
        return create_squid_impl(client_factory(), scraper, name=name, config=config,
                                 concurrency=concurrency, webhook=webhook)

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

    @mcp.tool(annotations={"title": "Update Scraper", "readOnlyHint": False,
                           "destructiveHint": False, "idempotentHint": False,
                           "openWorldHint": True})
    def update_scraper(squid_id: str, name: str | None = None,
                       config: dict | None = None,
                       concurrency: int | None = None,
                       webhook: dict | None = None) -> dict:
        """Change an existing squid's `name`, `config` (same shape/aliases as
        create_squid's), `concurrency` and/or `webhook` — WITHOUT running it or
        touching saved tasks. Pass at least one. No orphan risk: the squid
        already exists, so a rejected config is just a normal error.

        `webhook` has create_squid's shape ({"url", "events", "retry",
        "is_active"}); a key you leave out keeps the squid's saved webhook
        value, and is_active is true unless you pass false (how to turn it
        off). get_my_scraper shows the saved one under `webhook_fields`."""
        authz(EXECUTE_SCOPES)
        return update_scraper_impl(client_factory(), squid_id, name=name, config=config,
                                   concurrency=concurrency, webhook=webhook)

    @mcp.tool(annotations={"title": "Estimate Run", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def estimate_run(squid_id: str, toon: bool = False) -> dict:
        """Authoritative pre-run cost & result estimate for a squid that already
        has tasks — credits, projected results, and estimated time, straight from
        the API (more accurate than run_scraper's built-in upper-bound). The squid
        must have at least one task.

        `total_credits` covers the per-row price plus each paid extra step
        that is on; per-row result *filters* are priced too and are in
        neither this figure nor run_scraper's — and neither is email
        verification, billed separately after the scrape when
        `auto_verify_emails` is on. When it is, `verification_note` sizes it
        from the crawler's `email_verification_credits`; add that to `total_credits`
        yourself, it is not folded in.

        `estimated_time` is a floor, not an ETA: it assumes every row fetched
        is kept, so a filtered run routinely takes several times longer
        without anything being wrong. The response repeats both caveats in
        `estimate_note` and `estimated_time_note` — report a run against them
        rather than calling a run late or failed on this number.

        Pass toon=true for compact TOON."""
        authz(READ_SCOPES)
        out = estimate_run_impl(client_factory(), squid_id)
        return toon_result(out) if toon else out
