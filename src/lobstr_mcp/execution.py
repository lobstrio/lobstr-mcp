"""Execute-path orchestration: run_scraper, get_run, get_results.

Hides the internal Squid/Task/Run workflow behind a single AI-facing action,
and returns structured, model-actionable results (including structured errors
rather than raw HTTP failures).

run_scraper also resolves/attaches the platform account an account-backed
crawler needs (see account_linking.py): unambiguous auto-pick, or an explicit
account_id. It's gated on `runs:execute` alone, same as every other write this
tool already does to the squid (matching attach_account's scope note) — but it
must not leak the *list* of connected accounts to a caller who only holds that
scope, so an ambiguous pick is reported without naming candidates (see
_redact_account_error); attach_account and list_accounts, which do name them,
require `account:read` too.
"""
from __future__ import annotations

import httpx

from lobstr_mcp.account_linking import (
    AccountsShapeError,
    account_health_note,
    crawler_account_type,
    pick_or_explain,
    resolve_account,
    squid_account_ids,
)
from lobstr_mcp.errors import LobstrAPIError, structured, to_error_dict, transport_error_dict
from lobstr_mcp.lobstr_client import resolve_crawler_id
from lobstr_mcp.safeguards import (
    DERIVED_IDEMPOTENCY_TTL,
    CostEstimate,
    compute_idempotency_key,
    estimate_cost,
    validate_input,
)
from lobstr_mcp.schema_translator import translate_input_schema

_SQUID_LEVELS = ("squid", "cluster")
# Squid inputs the API accepts only nested under params.functions.
_FUNCTION_LEVEL = "function"
_RESULT_LIST_KEYS = ("data", "results", "records")


def _estimate_dict(est: CostEstimate) -> dict:
    return {"credits": est.credits, "basis": est.basis, "currency": est.currency,
            "rate_per_row": est.rate}


def _redact_account_error(error: dict) -> dict:
    """run_scraper is gated on `runs:execute` only, not `account:read` — unlike
    attach_account/list_accounts. Don't let it leak the user's connected
    account inventory (ids/usernames) through an ambiguous-pick or a locked-
    account error; point at the properly-scoped tools instead."""
    code = error.get("error_code")
    if code == "multiple_accounts_available":
        return {
            "error_code": code,
            "message": ("Multiple healthy accounts match this scraper's required platform; "
                       "run_scraper won't guess between them. Call list_accounts to see "
                       "them, then call run_scraper again with account_id (or call "
                       "attach_account(squid_id, account_id) first)."),
            "required_type": error.get("required_type"),
        }
    if code == "account_locked":
        return {
            "error_code": code,
            "message": ("A connected account matches this scraper's required platform but "
                       "is currently locked — routine on LinkedIn/Sales Navigator, not a "
                       "broken account. Auto-pick skips it. Call list_accounts to see it, "
                       "then call run_scraper again with account_id if you want to use it "
                       "anyway (or call attach_account(squid_id, account_id) first)."),
            "required_type": error.get("required_type"),
        }
    return error


def _detail(exc: Exception) -> str:
    return str(exc) or type(exc).__name__


def _new_squid_write_failed(client, squid_id: str, scraper: str, base: dict) -> dict:
    """Config/tasks write on a squid run_scraper just created was rejected —
    delete the now-useless squid instead of leaving an orphan, and name
    squid_id either way (mirrors create_squid_impl's own rejection handling)."""
    deleted = False
    try:
        client.delete_squid(squid_id)
        deleted = True
    except Exception:
        deleted = False
    base = dict(base)
    base["squid_id"] = squid_id
    base["scraper"] = scraper
    base["deleted"] = deleted
    tail = ((f" Squid {squid_id} (created for this call) was deleted, so nothing is left "
            "behind — no concurrency slot spent. Fix the input and call run_scraper again.")
           if deleted else
           (f" Squid {squid_id} was created but its configuration could not be saved, and "
            "the automatic cleanup delete also failed — it still exists with no usable "
            f"config. Check get_my_scraper(squid_id='{squid_id}') or delete it by hand."))
    base["message"] = f"{base['message']}{tail}"
    return base


def _snapshot_tasks(client, squid_id: str) -> tuple[list[dict] | None, int]:
    """The task rows a squid has right now, as re-addable param dicts.

    Returns (rows, total_seen). `rows` is None when the listing itself failed,
    which means a later rewrite cannot be undone — the caller must say so
    rather than quietly proceed as if it could. `total_seen` is how many tasks
    the API reported, so a partial read (a row whose params came back empty or
    in an unexpected shape) is visible as len(rows) < total_seen instead of
    passing for a complete backup.
    """
    try:
        tasks = client.list_tasks(squid_id)
    except (LobstrAPIError, httpx.TransportError):
        return None, 0
    if not isinstance(tasks, list):
        return None, 0
    rows = []
    for task in tasks:
        params = task.get("params") if isinstance(task, dict) else None
        if isinstance(params, dict) and params:
            rows.append(params)
    return rows, len(tasks)


def _restore_tasks(client, squid_id: str, snapshot: tuple[list[dict] | None, int],
                   failure: dict) -> dict:
    """Put a squid's task rows back after a rewrite failed past the point of
    no return, and tell the caller exactly what state it is in.

    `failure` is the structured error for the step that failed; this adds what
    happened to the saved inputs. If the rows cannot be put back, they are
    returned verbatim under `lost_tasks` — the caller can re-add them with
    add_tasks, and it is the only remaining copy.
    """
    rows, total_seen = snapshot
    out = dict(failure)
    out["squid_id"] = squid_id

    if rows is None:
        out["tasks_lost"] = True
        out["message"] = (
            f"{out['message']} This client could not read them beforehand, so it cannot put "
            f"them back — check get_my_scraper(squid_id='{squid_id}') and re-add with add_tasks.")
        return out

    if not rows:
        out["tasks_lost"] = False
        out["tasks_restored"] = 0
        out["message"] = f"{out['message']} It had no saved inputs, so nothing was destroyed."
        return out

    try:
        client.add_tasks(squid_id, rows)
    except (LobstrAPIError, httpx.TransportError) as exc:
        out["tasks_lost"] = True
        out["lost_tasks"] = rows
        out["message"] = (
            f"{out['message']} Restoring the {len(rows)} deleted row(s) also failed "
            f"({_detail(exc)}); they're in `lost_tasks` — re-add with "
            f"add_tasks(squid_id='{squid_id}', tasks=[...]).")
        return out

    out["tasks_lost"] = False
    out["tasks_restored"] = len(rows)
    partial = ("" if len(rows) == total_seen else
               f" ({total_seen - len(rows)} of {total_seen} could not be restored)")
    out["message"] = (f"{out['message']} Its {len(rows)} saved input(s) were put back{partial}; "
                      "no run was started.")
    return out


def _replace_squid_input(client, squid_id: str, update_body: dict,
                         task_params: dict,
                         snapshot: tuple[list | None, int]) -> tuple[dict | None, int]:
    """Rewrite a reused squid's saved config and *destroy* the task rows it
    already had, replacing them with the single new one.

    Only reached when the caller asked for exactly that (run_scraper's
    replace_tasks=true). The default path never deletes anything — see
    _save_squid_input.

    Returns (None, rows_destroyed) on success, or (structured error, 0)
    describing what survived.

    The API offers no way to swap a squid's tasks atomically: POST
    /squids/{id}/empty drops every row, and staging the new row first is not a
    substitute — crawlers carry a max task count, so adding before emptying can
    be refused outright on a full squid. So the destructive step is made as
    late and as reversible as possible:

    1. save the config first — an input the API rejects that local validation
       could not know about (it only sees the schema translated from the
       crawler's /params) fails here, with nothing deleted yet;
    2. empty the squid — the snapshot of its existing rows was taken by the
       caller before any of this, because how many there are is also what the
       run costs;
    3. add the new row; if that fails, put the snapshot back.

    Local validation is deliberately not widened to prevent the rejection: the
    server can always refuse something this client did not anticipate, and that
    is the case being handled here.
    """
    try:
        client.update_squid(squid_id, update_body)
    except LobstrAPIError as exc:
        failed = to_error_dict(exc)
        return failed | {
            "squid_id": squid_id, "tasks_lost": False,
            "message": f"{failed['message']} Nothing was changed — no inputs were touched.",
        }, 0
    except httpx.TransportError as exc:
        # A transport failure (timeout, reset) may still have been applied
        # upstream — unlike a rejection, this client cannot tell. What it can
        # state is that no input was deleted, because nothing destructive has
        # run yet.
        return {
            "error_code": "upstream_unavailable", "squid_id": squid_id,
            "tasks_lost": False,
            "message": (f"Saving the new configuration failed before a response came back "
                        f"({_detail(exc)}). No inputs were deleted; check "
                        f"get_my_scraper(squid_id='{squid_id}') before retrying."),
        }, 0

    try:
        client.empty_squid(squid_id)
    except (LobstrAPIError, httpx.TransportError) as exc:
        # An outright rejection deleted nothing, but a transport failure may
        # have emptied the squid anyway, so both go through restore: re-adding
        # rows that are still there is deduplicated by the API, whereas
        # assuming they survived would leave a silently empty squid.
        failed = (to_error_dict(exc) if isinstance(exc, LobstrAPIError) else
                  {"error_code": "upstream_unavailable",
                   "message": f"Clearing the scraper's old inputs failed ({_detail(exc)})."})
        return _restore_tasks(client, squid_id, snapshot, failed), 0

    try:
        client.add_tasks(squid_id, [task_params])
    except (LobstrAPIError, httpx.TransportError) as exc:
        failed = (to_error_dict(exc) if isinstance(exc, LobstrAPIError) else
                  {"error_code": "upstream_unavailable",
                   "message": f"Adding the new input failed ({_detail(exc)})."})
        return _restore_tasks(client, squid_id, snapshot, failed), 0

    return None, snapshot[1]


def _save_squid_input(client, squid_id: str, update_body: dict,
                      task_params: dict) -> dict | None:
    """The non-destructive counterpart of _replace_squid_input: save a reused
    squid's config and, when the input carries a task-level field, add that one
    row. Nothing is ever deleted, so there is nothing to snapshot or restore.

    Only called when the squid has no saved task rows to lose, or when the
    input carries none (squid-level settings only, which the API merges into
    the saved ones). Returns None on success, or a structured error.
    """
    try:
        client.update_squid(squid_id, update_body)
    except (LobstrAPIError, httpx.TransportError) as exc:
        failed = (to_error_dict(exc) if isinstance(exc, LobstrAPIError) else
                  {"error_code": "upstream_unavailable",
                   "message": ("Saving the new configuration failed before a response came "
                               f"back ({_detail(exc)}) — whether it was saved is unknown, "
                               "not rejected.")})
        return failed | {
            "squid_id": squid_id, "tasks_lost": False,
            "message": f"{failed['message']} No saved input was deleted — nothing was started.",
        }

    if not task_params:
        return None

    try:
        client.add_tasks(squid_id, [task_params])
    except (LobstrAPIError, httpx.TransportError) as exc:
        failed = (to_error_dict(exc) if isinstance(exc, LobstrAPIError) else
                  {"error_code": "upstream_unavailable",
                   "message": f"Adding the new input failed ({_detail(exc)})."})
        return failed | {
            "squid_id": squid_id, "tasks_lost": False,
            "message": f"{failed['message']} No saved input was deleted; nothing was started.",
        }

    return None


def _refuse_to_touch_saved_tasks(squid_id: str, scraper: str, task_params: dict,
                                 existing_count: int) -> dict:
    """Refuse a task-level input on a squid that already has saved rows.

    A run scrapes every row a squid holds, so there is no way to apply one new
    input to a squid that already has 50 without either deleting those 50 or
    scraping all 51. Both are surprises the caller has to choose, not defaults:
    the first destroys what the account's owner built in the dashboard, the
    second silently spends ~51x the estimated credits. So nothing is written
    and every way forward is named, with the rows left where they are.
    """
    n = existing_count
    fields = ", ".join(sorted(task_params)) or "none"
    return {
        "error_code": "squid_has_tasks", "squid_id": squid_id,
        "existing_task_count": n, "tasks_lost": False, "new_task": task_params,
        "message": (f"This scraper already has {n} saved input row(s) ({fields} was not "
                   "applied); nothing was changed and no run was started — see `options`."),
        # Cost of option 3 below (adding yours to the n saved rows) relative to
        # running one input alone — the run scrapes every row a squid holds.
        "cost_multiplier_if_added": n + 1,
        "options": [
            f"run_scraper(squid_id='{squid_id}')  # run the {n} saved row(s) as they are",
            f"run_scraper(scraper='{scraper}', input=...)  # your input, on a separate scraper",
            f"add_tasks(squid_id='{squid_id}', tasks=[<new_task>]) "
            f"then run_scraper(squid_id='{squid_id}')  # run all {n + 1} together",
            f"run_scraper(squid_id='{squid_id}', input=..., replace_tasks=true)"
            f"  # delete the {n} saved row(s), keep only yours",
        ],
    }


def _tasks_note(action: str, task_count: int | None, replaced_count: int) -> str:
    """One plain sentence about what this call did to the squid's saved inputs.

    The caller has to be able to tell "your input is the only thing this run
    scrapes" from "your input runs alongside 50 others" without inspecting the
    squid, so every success path says which it is.
    """
    if action == "created":
        return ("This is a new scraper holding only the input you passed; the run scrapes "
                "that one input.")
    if action == "replaced":
        if replaced_count:
            return (f"replace_tasks=true: this scraper's {replaced_count} previously saved "
                    "input(s) were deleted and replaced by the one you passed, so the run "
                    "scrapes yours alone.")
        return ("The scraper had no saved inputs to replace; the run scrapes the one you "
                "passed, alone.")
    if action == "added":
        return ("The scraper had no saved inputs, so yours was added and is now its only "
                "one; the run scrapes it alone.")
    if task_count is None:
        return ("The scraper's saved inputs were left as they are and the run scrapes all "
                "of them, but how many could not be read, so the estimate covers one row.")
    if task_count == 0:
        return ("The scraper has no saved inputs at all, so this run has nothing to scrape "
                "— add one with add_tasks, or pass a task-level input.")
    # The estimate is computed for this many rows (see _rows_this_run), so the
    # note no longer has to warn that it is quoted for one.
    return (f"The scraper's {task_count} saved input(s) were left exactly as they are — only "
            f"the settings were saved — and the run scrapes all {task_count}, which is what "
            "the estimate covers.")


def _rows_this_run(*, reuse: bool, has_task_input: bool, replace_tasks: bool,
                   rows: list | None, saved_count: int) -> int | None:
    """How many task rows the run this call is about to start will scrape.

    Every run scrapes every row its squid holds, so this is the multiplier on
    the price of one row. None when the rows could not be read at all — which
    is reported as unknown rather than assumed to be one.
    """
    if not reuse:
        return 1  # a squid created here holds only the row passed to it
    if rows is None:
        return None
    if not has_task_input:
        return saved_count
    if replace_tasks:
        return 1  # the saved rows are deleted and replaced by the one passed
    # A task-level input on a squid that already holds rows is refused further
    # down (_refuse_to_touch_saved_tasks); on an empty one it becomes the only
    # row.
    return saved_count + 1 if saved_count else 1


def _effective_settings(existing_squid: dict, squid_params: dict) -> dict:
    """The squid-level settings the run executes under: what the squid already
    has saved, with this call's input on top (function toggles merged rather
    than replaced, since the update body only carries the ones being changed).
    """
    saved = existing_squid.get("params")
    effective = dict(saved) if isinstance(saved, dict) else {}
    saved_functions = effective.get("functions")
    effective.update(squid_params)
    new_functions = squid_params.get("functions")
    if isinstance(saved_functions, dict) and isinstance(new_functions, dict):
        effective["functions"] = {**saved_functions, **new_functions}
    return effective


def _remaining_credits(available, consumed) -> float | int | None:
    """What is left to spend this period, or None when `available` isn't a
    usable number. Not a refusal — see _credit_warning — just the figure the
    API's own gate would compute (`available` never has `consumed` taken off
    it — see check_credits)."""
    if not isinstance(available, (int, float)) or isinstance(available, bool):
        return None
    if isinstance(consumed, (int, float)) and not isinstance(consumed, bool):
        return available - consumed
    return available


def _credit_warning(est: CostEstimate, remaining) -> str | None:
    """One short, non-blocking sentence when the balance looks light for this
    run's estimate, or None. This never refuses a run: the API is the sole
    authority on affordability — it refuses only an ordinary account whose
    period spend has reached its allowance, and lets a staff/admin account run
    regardless, even at a negative balance — a client-side refusal here
    previously blocked runs the API would have accepted."""
    if est.credits is None or remaining is None or remaining >= est.credits:
        return None
    if remaining <= 0:
        return f"This account has {remaining} credit(s) left this period; the API may refuse the run."
    return f"Only {remaining} credit(s) left this period, estimate is up to {est.credits}."


@structured(verify_with="list_runs(squid_id=...), where a run that started is listed, "
                        "and get_my_scraper(squid_id=...) for the saved configuration")
def run_scraper_impl(client, settings, idem_store, scraper: str | None = None,
                     input: dict | None = None, confirm: bool = False,
                     idempotency_key: str | None = None,
                     squid_id: str | None = None,
                     account_id: str | None = None,
                     replace_tasks: bool = False) -> dict:
    input = input or {}
    reuse = bool(squid_id)

    if not scraper and not reuse:
        return {"error_code": "invalid_request",
                "message": "provide `scraper` (to run a new scraper) or `squid_id` (to re-run an existing one)"}

    # Reuse: run an existing squid instead of spawning a new one (avoids squid
    # sprawl). The crawler is taken from the squid itself.
    existing_squid: dict = {}
    if reuse:
        existing_squid = client.get_squid(squid_id)
        scraper = scraper or existing_squid.get("crawler")

    # accept a slug or an id; everything below then works off the crawler id
    scraper = resolve_crawler_id(client, scraper)
    crawler = client.get_crawler(scraper)
    # /params is authoritative about where each input goes — in particular which
    # squid inputs are function toggles that must be nested under
    # params.functions. Tolerate its absence rather than failing the run.
    try:
        crawler_params = client.get_crawler_params(scraper)
    except Exception:
        crawler_params = None
    translated = translate_input_schema(crawler, params=crawler_params)
    schema, levels = translated["json_schema"], translated["levels"]
    input_modes = translated.get("input_modes")
    # alias -> the name the API knows, for the inputs a crawler declares twice
    # under one name (Google Maps' `country`, task-level in the search URL and
    # squid-level as Google's region). The alias is this client's vocabulary
    # only; everything below sends the real name.
    wire_names = translated.get("wire_names") or {}

    # Settings-only input on a reused squid (no task-level field) merges into
    # saved config and touches no task row, so task-level required fields
    # don't apply — only when a task-level field is being added/replaced.
    if input or not reuse:
        has_task_input = any(
            levels.get(k) not in _SQUID_LEVELS and levels.get(k) != _FUNCTION_LEVEL
            for k in input)
        check_required = (not reuse) or has_task_input
        errors = validate_input(input, schema, input_modes=input_modes,
                                check_required=check_required)
        if errors:
            return {"error_code": "validation_error", "errors": errors}

    # Scoped per user so different callers never dedupe each other. An
    # explicit key is the caller's own retry token (normal TTL); a derived
    # one only guards a retry storm, not a genuine later re-run.
    user_scope = client.user_scope()
    if idempotency_key:
        key = f"{user_scope}:{idempotency_key}"
        key_ttl = None
    else:
        key = "derived:" + compute_idempotency_key(user_scope, squid_id or scraper, input)
        key_ttl = DERIVED_IDEMPOTENCY_TTL
    existing = idem_store.get(key)
    if existing:
        return {"status": "already_submitted", "run_id": existing, "idempotent": True}

    # Account-backed crawlers (e.g. LinkedIn Leads, Sales Navigator Leads) need
    # a synced account attached to the squid before a run can start — without
    # one the run is created but fails asynchronously with done_reason
    # "no_accounts". Resolve/attach before estimating cost: a run that cannot
    # possibly succeed shouldn't reach the confirmation gate.
    account_type = crawler_account_type(crawler)
    account_id_given = account_id is not None
    try:
        current_account_ids = squid_account_ids(existing_squid) if reuse else []
    except AccountsShapeError:
        return {"error_code": "accounts_unreadable",
                "message": ("This squid's current accounts couldn't be read in a shape "
                           "this client recognizes, so nothing was changed — attaching "
                           "now could have silently dropped an existing link. Check it "
                           "directly (get_my_scraper) and try again."),
                "squid_id": squid_id}
    accounts_to_save: list[str] | None = None
    resolved_account_id: str | None = None
    account_warning: str | None = None
    if account_type is None:
        if account_id_given:
            return {"error_code": "no_account_needed",
                    "message": (f"{crawler.get('name') or 'This scraper'} does not use a "
                               "platform account; remove account_id and try again.")}
    elif account_id_given:
        account, error = resolve_account(client, account_id, account_type)
        if error:
            return error
        # An explicit account_id is never refused for being unhealthy — that
        # choice is the caller's — but the run may pause later if it isn't.
        resolved_account_id = account["id"]
        account_warning = account_health_note(account)
        if account["id"] not in current_account_ids:
            accounts_to_save = [*current_account_ids, account["id"]]
    elif not current_account_ids:
        # Nothing attached yet and none was specified — auto-pick only when
        # unambiguous (see account_linking.pick_or_explain); never guess.
        account, error = pick_or_explain(client, account_type)
        if error:
            return _redact_account_error(error)
        resolved_account_id = account["id"]
        accounts_to_save = [account["id"]]
    # else: the squid already has an account attached — leave it alone.

    # Saving the settings is what flips the squid to is_ready; without it
    # POST /runs answers 400 SquidNotReady. The API recognizes squid inputs
    # solely under "params" (flat keys are discarded), and the save only takes
    # effect when the body also carries a recognized field such as "name".
    # Each key goes out under wire_names[k] when it is an alias, else its own
    # name: an alias the API has never heard of is rejected with InvalidParam,
    # which fails the whole run.
    squid_params = {wire_names.get(k, k): v for k, v in input.items()
                    if levels.get(k) in _SQUID_LEVELS}
    functions = {wire_names.get(k, k): v for k, v in input.items()
                 if levels.get(k) == _FUNCTION_LEVEL}
    if functions:
        squid_params["functions"] = functions
    task_params = {wire_names.get(k, k): v for k, v in input.items()
                   if levels.get(k) not in _SQUID_LEVELS
                   and levels.get(k) != _FUNCTION_LEVEL}

    # Read the squid's saved rows once, before estimating: how many there are
    # decides what this run costs, and the same snapshot is what the write path
    # below restores from. A run scrapes EVERY row the squid holds, so quoting
    # the price of one row for a squid holding 50 understated the bill 50-fold;
    # saying so afterwards in `tasks_note` came after the confirmation gate had
    # already let the call through.
    saved_rows, saved_count = (None, 0)
    if reuse:
        saved_rows, saved_count = _snapshot_tasks(client, squid_id)
    rows_this_run = _rows_this_run(reuse=reuse, has_task_input=bool(task_params),
                                   replace_tasks=replace_tasks, rows=saved_rows,
                                   saved_count=saved_count)
    # The settings the run actually executes under: what the squid already has
    # saved, with this call's input on top. A reused squid's caps and paid
    # toggles are in its saved params, not in the input.
    effective = _effective_settings(existing_squid if reuse else {}, squid_params)
    est = estimate_cost(crawler, task_count=rows_this_run or 1,
                        max_results_per_task=effective.get("max_results"),
                        run_result_cap=effective.get("max_unique_results_per_run"),
                        settings=effective)
    if not confirm and (est.credits is None or est.credits > settings.run_confirm_threshold):
        msg = ("Cost cannot be estimated before running; confirm to proceed."
               if est.credits is None else
               f"Estimated {est.credits} credits exceeds the {settings.run_confirm_threshold} "
               "credit threshold; confirm to proceed.")
        if rows_this_run and rows_this_run > 1:
            msg += (f" This run scrapes all {rows_this_run} input row(s) the scraper holds, "
                    "which is what the estimate covers.")
        elif rows_this_run is None:
            msg += (" How many input rows this scraper holds could not be read, so the "
                    "estimate covers one row and the real total may be a multiple of it.")
        return {"needs_confirmation": True, "estimate": _estimate_dict(est),
                "message": msg, "rows_this_run": rows_this_run,
                "hint": "call run_scraper again with confirm=true to execute"}

    # Informational only — never a refusal. The API alone decides whether a
    # run can start (see _credit_warning); this is fetched before any write
    # only so `remaining`/`credit_warning` can ride on whatever response
    # comes back, success or failure.
    balance = client.get_balance()
    # GET /v1/user/balance returns {object, available, consumed, ...}; "balance"
    # was never a real key.
    available = balance.get("available", balance.get("balance"))
    remaining = _remaining_credits(available, balance.get("consumed"))
    credit_warning = _credit_warning(est, remaining)

    # The rows were read above, so a re-run with no input can say how many it
    # scrapes instead of reporting null.
    tasks_action, replaced_count = "unchanged", 0
    task_count = saved_count if (reuse and saved_rows is not None) else None

    if reuse:
        squid_id = existing_squid["id"]
        # Only write to the squid when new input is supplied; otherwise re-run
        # it exactly as saved. Deleting the rows it already holds happens only
        # when the caller asked for it with replace_tasks — see
        # _refuse_to_touch_saved_tasks for why that is not the default.
        if input:
            update_body = {
                "name": existing_squid.get("name") or crawler.get("name") or scraper,
                "params": squid_params,
            }
            if accounts_to_save:
                update_body["accounts"] = accounts_to_save
            if replace_tasks:
                failure, replaced_count = _replace_squid_input(
                    client, squid_id, update_body, task_params,
                    (saved_rows, saved_count))
                if failure is not None:
                    return failure
                tasks_action, task_count = "replaced", 1
            else:
                rows, existing_count = saved_rows, saved_count
                if rows is None:
                    # The rows couldn't be read, so this client cannot tell
                    # whether there is anything to protect. Adding a row is
                    # only safe on a squid known to be empty; saving settings
                    # alone deletes nothing, so it goes ahead with the count
                    # reported as unknown.
                    existing_count = None
                    if task_params:
                        return {
                            "error_code": "tasks_unreadable", "squid_id": squid_id,
                            "tasks_lost": False,
                            "message": ("This scraper's saved inputs could not be read, so "
                                       "nothing was changed and no run was started; retry, "
                                       "or pass replace_tasks=true to overwrite anyway."),
                            "options": [
                                f"run_scraper(squid_id='{squid_id}')",
                                f"run_scraper(scraper='{scraper}', input=...)",
                                f"run_scraper(squid_id='{squid_id}', input=..., replace_tasks=true)",
                            ],
                        }
                elif task_params and existing_count:
                    return _refuse_to_touch_saved_tasks(squid_id, scraper, task_params,
                                                        existing_count)
                failure = _save_squid_input(client, squid_id, update_body, task_params)
                if failure is not None:
                    return failure
                tasks_action = "added" if task_params else "unchanged"
                task_count = 1 if task_params else existing_count
        elif accounts_to_save:
            # No new input, but this squid had no account and one was
            # resolved/attached above — save it without touching params/tasks.
            client.update_squid(squid_id, {"accounts": accounts_to_save})
    else:
        squid = client.create_squid(crawler=scraper)
        squid_id = squid["id"]
        update_body = {
            "name": squid.get("name") or crawler.get("name") or scraper,
            "params": squid_params,
        }
        if accounts_to_save:
            update_body["accounts"] = accounts_to_save
        # A rejected write here used to lose squid_id entirely (generic
        # handler); now delete the orphan on a confirmed rejection, or keep
        # it and say so on a transport failure (unknown if it landed).
        try:
            client.update_squid(squid_id, update_body)
            client.add_tasks(squid_id, [task_params])
        except LobstrAPIError as exc:
            return _new_squid_write_failed(client, squid_id, scraper, to_error_dict(exc))
        except httpx.TransportError as exc:
            failed = transport_error_dict(
                exc, verify_with=f"get_my_scraper(squid_id='{squid_id}')")
            failed["squid_id"] = squid_id
            failed["scraper"] = scraper
            return failed
        tasks_action, task_count = "created", 1

    # Config and rows are written; only POST /runs itself can still fail, and
    # credits are one of several reasons it might (NotEnoughCredits maps to
    # insufficient_credits via the normal structured-error path — see
    # errors.to_error_dict). The one case that needs handling here rather than
    # letting it propagate: a replace_tasks rewrite already deleted the old
    # rows above, so a refusal now must restore them, the same as any other
    # failure past that point (_replace_squid_input).
    try:
        run = client.start_run(squid_id)
    except (LobstrAPIError, httpx.TransportError) as exc:
        failed = (to_error_dict(exc) if isinstance(exc, LobstrAPIError) else
                 transport_error_dict(exc, verify_with="list_runs(squid_id=...)"))
        failed |= {"squid_id": squid_id, "scraper": scraper,
                  "tasks_action": tasks_action, "task_count": task_count,
                  "estimate": _estimate_dict(est)}
        if remaining is not None:
            failed["remaining"] = remaining
        if tasks_action == "replaced":
            return _restore_tasks(client, squid_id, (saved_rows, saved_count), failed)
        return failed
    run_id = run["id"]
    idem_store.put(key, run_id, ttl=key_ttl)

    # account_attached answers "does this squid have an account attached now",
    # not "did this call attach one" — a reused squid whose account was
    # attached by an earlier call (current_account_ids already non-empty, so
    # nothing new to save: the "leave it alone" branch above) must not read as
    # False, or a client re-checking after the fact is misled into attaching
    # again needlessly.
    # The id when the caller supplied account_id (echoing back their own input
    # isn't a leak, and holds even when the account was already attached and
    # nothing was written — a null there would read as failure); otherwise a
    # bare bool of the current state, so an auto-picked or already-attached
    # account's id is never exposed to a runs:execute-only token.
    account_attached = (resolved_account_id if account_id_given
                        else bool(accounts_to_save) or bool(current_account_ids))
    # What this call did to the squid's saved inputs, and how many rows the run
    # actually scrapes (null when this client didn't read them). A model that
    # only sees run_id cannot tell "your input is all this run scrapes" from
    # "your input plus 50 rows somebody else saved" — the difference is the
    # bill.
    result = {"run_id": run_id, "squid_id": squid_id, "scraper": scraper,
              "reused_squid": reuse, "status": run.get("status"),
              "estimate": _estimate_dict(est), "submitted_input": input,
              "account_attached": account_attached,
              "tasks_action": tasks_action, "task_count": task_count,
              "tasks_note": _tasks_note(tasks_action, task_count, replaced_count)}
    if tasks_action == "replaced":
        result["replaced_task_count"] = replaced_count
    if account_warning:
        result["account_warning"] = account_warning
    if remaining is not None:
        result["remaining"] = remaining
    if credit_warning:
        result["credit_warning"] = credit_warning
    return result


def _verification_out(detail: dict) -> dict | None:
    """Trimmed `email_verification`; None when the run has no verification
    step at all (different from one that hasn't started yet)."""
    verification = detail.get("email_verification")
    if not isinstance(verification, dict):
        return None
    return {"status": verification.get("status"),
            "progress": verification.get("progress"),
            "verified_emails": verification.get("verified_emails"),
            "total_emails": verification.get("total_emails"),
            "is_done": bool(verification.get("is_done"))}


def _fully_done(stats_is_done, verification: dict | None, export_done) -> bool:
    """Run done AND (no verification, or verification done) AND export done."""
    if not stats_is_done:
        return False
    if verification is not None and not verification["is_done"]:
        return False
    if export_done is False:
        return False
    return True


@structured
def get_run_impl(client, run_id: str, full: bool = False) -> dict:
    stats = client.get_run_stats(run_id)
    # /runs/{hash}/stats has progress but no status field, so deriving status
    # from is_done reported "running" for a queued job. The run detail endpoint
    # carries the real status (pending/running/done/failed/aborted) and the
    # credits actually consumed. It is supplementary: if it fails, stats alone
    # still answers.
    detail: dict = {}
    try:
        detail = client.get_run(run_id) or {}
    except LobstrAPIError:
        detail = {}
    run_status = (detail.get("status") or stats.get("status")
                 or ("done" if stats.get("is_done") else "running"))
    verification = _verification_out(detail)
    export_done = detail.get("export_done")
    is_done = _fully_done(stats.get("is_done"), verification, export_done)

    status = run_status
    note = None
    if verification is not None and not verification["is_done"] and \
            str(run_status).lower() in ("done", "success", "succeeded"):
        status = "verifying_emails"
        note = ("The run itself finished, but email verification is still running — "
                "credits for it are still being billed and results aren't final. Poll "
                "again; wait_for_run(run_id=...) can do this for you, bounded by a "
                "timeout.")

    out = {"run_id": stats.get("id", run_id), "status": status, "run_status": run_status,
            "is_done": is_done,
            "progress": stats.get("percent_done"),
            "tasks_total": stats.get("total_tasks"),
            "tasks_done": stats.get("total_tasks_done"),
            "total_results": detail.get("total_results",
                                        stats.get("total_results")),
            "credits_consumed": detail.get("credit_used"),
            "done_reason": detail.get("done_reason_desc") or detail.get("done_reason"),
            "started_at": stats.get("started_at"),
            "ended_at": stats.get("ended_at"), "duration": stats.get("duration")}
    if "email_verification" in detail:
        out["email_verification"] = verification
    if "export_done" in detail:
        out["export_done"] = export_done
    if note:
        out["note"] = note
    # The raw stats blob duplicates the fields above; only ship it on request.
    if full:
        out["stats"] = stats
    return out


def _strip_empty(row: dict) -> dict:
    """Drop null/empty values from a result row. Scraper rows are sparse across
    dozens of columns, so most of a row's weight is empty fields the model can't
    use. 0 and False are meaningful and kept."""
    return {k: v for k, v in row.items()
            if v is not None and v != "" and v != [] and v != {}}


_DEFAULT_PAGE_SIZE = 10
_FULL_DEFAULT_PAGE_SIZE = 25
_MAX_PAGE_SIZE = 100


@structured
def get_results_impl(client, *, run_id: str | None = None, squid_id: str | None = None,
                     page: int = 1, page_size: int | None = None,
                     fields: list[str] | None = None, full: bool = False,
                     max_rows: int | None = None) -> dict:
    if not run_id and not squid_id:
        return {"error_code": "invalid_request",
                "message": "provide run_id or squid_id (exactly one)"}

    # page_size is what's sent to the API and what total_pages/next describe;
    # max_rows follows it (used to be fixed at 10/25 regardless of page_size).
    effective_page_size = page_size if page_size else (
        _FULL_DEFAULT_PAGE_SIZE if full else _DEFAULT_PAGE_SIZE)
    effective_page_size = max(1, min(effective_page_size, _MAX_PAGE_SIZE))
    max_rows = effective_page_size if max_rows is None else min(max_rows, effective_page_size)

    # Free-plan accounts are capped at 30 results (API's ExportLimitReached);
    # that propagates as a normal structured error, not caught here.
    payload = client.get_results(run=run_id, squid=squid_id, page=page,
                                 page_size=effective_page_size)

    records: list = []
    for k in _RESULT_LIST_KEYS:
        if isinstance(payload.get(k), list):
            records = payload[k]
            break

    # computed from the untrimmed rows so the model always sees every column it
    # could ask for via `fields`
    available_fields = sorted({k for r in records if isinstance(r, dict) for k in r})
    if fields:
        records = [{k: r.get(k) for k in fields} for r in records if isinstance(r, dict)]

    capped = records[:max_rows]
    if not full:
        capped = [_strip_empty(r) if isinstance(r, dict) else r for r in capped]
    return {"total_results": payload.get("total_results"),
            "page": payload.get("page", page),
            "page_size": effective_page_size,
            "total_pages": payload.get("total_pages"),
            "returned": len(capped),
            "available_fields": available_fields,
            "next": payload.get("next"),
            "results": capped}


@structured
def list_runs_impl(client, squid_id: str, limit: int = 20) -> dict:
    runs = client.list_runs(squid_id, limit=limit)
    return {
        "count": len(runs),
        "runs": [{"run_id": r.get("id"), "status": r.get("status"),
                  "total_results": r.get("total_results"),
                  "credits_consumed": r.get("credit_used"),
                  "started_at": r.get("started_at"),
                  "ended_at": r.get("ended_at")} for r in runs],
    }


_TERMINAL_RUN_STATES = {"done", "success", "succeeded", "failed",
                        "aborted", "cancelled", "canceled", "error"}


@structured(verify_with="get_run(run_id=...)")
def abort_run_impl(client, run_id: str) -> dict:
    # POST /runs/{id}/abort is a safe idempotent no-op on a finished run — the
    # API just returns the run's current state instead of aborting. So we abort
    # and surface what the API actually reports rather than hard-claiming
    # "aborted" (the old bug: a DONE run came back as "aborted").
    run = client.abort_run(run_id)
    run = run if isinstance(run, dict) else {}
    status = str(run.get("status") or "").lower()
    done_reason = str(run.get("done_reason") or "").lower()

    # done_reason "aborted" / the transient UPLOADING state => the abort took
    if done_reason == "aborted" or status == "uploading":
        return {"run_id": run_id, "status": "aborting", "aborted": True,
                "message": "Abort requested; the run is stopping. Poll get_run to confirm."}
    # already finished for another reason => nothing was aborted
    if run.get("is_done") or status in _TERMINAL_RUN_STATES:
        return {"run_id": run_id, "status": status or "done", "aborted": False,
                "message": f"Run was already {status or 'finished'} — nothing to abort. "
                           "Already-scraped results are available via get_results."}
    return {"run_id": run_id, "status": status or "aborting", "aborted": True,
            "message": "Abort requested; the run stops shortly. Poll get_run to confirm."}


_DOWNLOAD_FORMATS = ("csv", "xlsx", "json", "jsonl")


@structured
def get_results_url_impl(client, run_id: str, format: str = "csv") -> dict:
    """A signed URL to download the full result set as a file — for when the
    caller wants everything, not the paged/capped rows get_results returns."""
    fmt = (format or "csv").lower()
    if fmt not in _DOWNLOAD_FORMATS:
        return {"error_code": "invalid_request",
                "message": f"format must be one of {', '.join(_DOWNLOAD_FORMATS)}"}
    result = client.get_run_download_url(run_id, file_format=fmt)
    if isinstance(result, dict) and result.get("status") == "processing":
        # A non-default format/field selection on a large run is built in the
        # background (see DownloadResultView) — not an error, just not ready.
        return {"run_id": run_id, "format": fmt, "status": "processing",
                "progress": result.get("progress"),
                "message": ("The file is still being built in this format; call "
                           "get_results_url again shortly.")}
    return {"run_id": run_id, "format": fmt, "download_url": result}
