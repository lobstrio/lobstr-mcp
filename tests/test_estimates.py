"""The numbers run_scraper and estimate_run quote.

Three reports, one cause each:

1. an invalid parameter on a thin balance came back as `insufficient_credits`
   instead of the API's parameter error. `validate_input` skips keys it has no
   spec for, so a bad one passes local validation; the balance guard then fired
   before any request carrying it was sent, and the caller was told to buy
   credits when the real problem was the input;
2. two quotes for one squid — 20 credits from run_scraper's pre-check, 26 from
   `estimate_run`. The API's estimate adds each paid step that is on
   (`ClusterEstimationView`): 20 rows x 1 credit/row, plus a step at 1 credit on
   the 0.3 of rows it succeeds for = 6. This client counted the rows only;
3. `estimated_time` 4.5x to 10x optimistic on filtered runs. The API derives it
   from throughput x row cap with no term for discarded rows at all, so it
   cannot be anything but a floor for a run that filters.

Plus the `task_count=1` assumption behind all of them: a run scrapes every row
its squid holds, and the estimate was quoted for one.
"""
import json

import httpx

from lobstr_mcp.config import Settings
from lobstr_mcp.execution import run_scraper_impl
from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.safeguards import IdempotencyStore, estimate_cost, paid_steps
from lobstr_mcp.tools.primitives import estimate_run_impl

SETTINGS = Settings(
    lobstr_api_base="https://api.lobstr.io/v1", dev_token=None, request_timeout=30.0,
    run_confirm_threshold=100, public_base_url="https://mcp.lobstr.io",
    service_credential=None, consent_url="https://app.lobstr.io/connect-ai",
)

# The Google Maps shape: priced per row, with one paid step on by default whose
# worker_stats say it succeeds on 30% of rows, and one off by default.
CRAWLER = {"id": "gm", "name": "Google Maps Leads Scraper", "credits_per_row": 1,
           "input": [
               {"name": "query", "type": "string", "level": "task", "required": True},
               {"name": "max_results", "type": "int", "level": "squid", "default": 200},
               {"name": "max_unique_results_per_run", "type": "int", "level": "squid"},
               {"name": "extract_emails_from_website", "type": "boolean",
                "level": "squid", "function": True, "default": True,
                "worker_stats": {"success_ratio": 0.3},
                "credits_per_function": {"current": 1, "legacy": 10}},
               {"name": "collect_business_details", "type": "boolean",
                "level": "squid", "function": True, "default": False,
                "worker_stats": {"success_ratio": 1},
                "credits_per_function": {"current": 1, "legacy": 10}},
           ]}


# --- 2. one question, one number --------------------------------------------


def test_the_pre_check_counts_the_paid_steps_the_api_counts():
    """The reported 20-against-26: 20 rows x 1 credit, + 20 x 0.3 x 1 for the
    step that is on by default."""
    est = estimate_cost(CRAWLER, task_count=1, run_result_cap=20,
                        settings={"max_unique_results_per_run": 20})
    assert est.credits == 26
    assert "extract_emails_from_website on ~6 row(s)" in est.basis
    # the step that is off by default is not billed and not quoted
    assert "collect_business_details" not in est.basis


def test_a_step_turned_off_by_the_caller_drops_out_of_the_quote():
    est = estimate_cost(CRAWLER, task_count=1, run_result_cap=20,
                        settings={"functions": {"extract_emails_from_website": False}})
    assert est.credits == 20


def test_a_step_turned_on_by_the_caller_is_added():
    est = estimate_cost(CRAWLER, task_count=1, run_result_cap=20,
                        settings={"functions": {"collect_business_details": True}})
    assert est.credits == 46  # 20 + 6 (emails, 0.3) + 20 (details, 1.0)


def test_paid_steps_reads_flat_state_as_well_as_nested():
    assert [s["name"] for s in paid_steps(CRAWLER, {})] == ["extract_emails_from_website"]
    flat = paid_steps(CRAWLER, {"collect_business_details": True})
    assert {s["name"] for s in flat} == {"extract_emails_from_website",
                                         "collect_business_details"}
    assert paid_steps(CRAWLER, {"extract_emails_from_website": False}) == []


def test_the_run_wide_cap_is_not_multiplied_by_the_task_count():
    """max_unique_results_per_run caps the whole run; max_results caps a task.
    Collapsing them quoted 50x the cap for a 50-row squid."""
    run_wide = estimate_cost(CRAWLER, task_count=50, run_result_cap=20,
                             max_results_per_task=200,
                             settings={"functions": {"extract_emails_from_website": False}})
    assert run_wide.credits == 20
    assert "capped for the whole run" in run_wide.basis
    per_task = estimate_cost(CRAWLER, task_count=50, max_results_per_task=10,
                             settings={"functions": {"extract_emails_from_website": False}})
    assert per_task.credits == 500
    assert "10 per task x 50 task(s)" in per_task.basis


def test_an_uncapped_run_is_still_unknown_rather_than_guessed():
    est = estimate_cost(CRAWLER, task_count=3, settings={})
    assert est.credits is None
    assert "depends on how many rows the run produces" in est.basis
    assert "extract_emails_from_website" in est.basis


def test_the_pre_check_points_at_the_authoritative_figure():
    est = estimate_cost(CRAWLER, task_count=1, run_result_cap=20, settings={})
    assert "estimate_run(squid_id=...)" in est.basis
    assert "Upper bound" in est.basis


# --- the squid's real row count ---------------------------------------------


def routes_for(*, tasks, squid_params=None, balance=None, seen=None, calls=None,
               runs_response=None):
    squid = {"id": "sq1", "crawler": "gm", "name": "My GM",
             "params": squid_params if squid_params is not None else {}}

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if calls is not None:
            calls.append(key)  # every call, regardless of body (unlike `seen`)
        if seen is not None and request.content:
            seen[key] = json.loads(request.content)
        if key == ("POST", "/v1/runs") and runs_response is not None:
            body = json.loads(request.content) if request.content else None
            return runs_response(request, body)
        if key == ("GET", "/v1/tasks"):
            return httpx.Response(200, json={"total_pages": 1, "page": 1, "data": tasks})
        if key == ("GET", "/v1/crawlers/gm/params"):
            return httpx.Response(200, json={"task": {"query": {"type": "string"}},
                                             "squid": {"max_results": {"type": "int"}}})
        return httpx.Response(200, json={
            ("GET", "/v1/squids/sq1"): squid,
            ("GET", "/v1/crawlers/gm"): CRAWLER,
            ("GET", "/v1/user/balance"): balance or {"available": 1000000, "consumed": 0},
            ("POST", "/v1/squids/sq1"): {},
            ("POST", "/v1/squids/sq1/empty"): {},
            ("POST", "/v1/tasks"): {"duplicated_count": 0, "tasks": [{"id": "t"}]},
            ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
        }[key])

    return LobstrClient("https://api.lobstr.io/v1", "t",
                        transport=httpx.MockTransport(handler))


SAVED_ROWS = [{"id": f"t{i}", "params": {"query": f"q{i}"}} for i in range(50)]


def test_a_rerun_is_quoted_for_every_row_the_squid_holds():
    """50 saved rows at a 10-row cap and 1 credit/row is 500 credits, not 10.
    The old quote was for a single row and the shortfall was named in words
    only, after the confirmation gate had already been passed."""
    client = routes_for(tasks=SAVED_ROWS, squid_params={
        "max_results": 10, "functions": {"extract_emails_from_website": False}})
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), squid_id="sq1")
    assert out["needs_confirmation"] is True
    assert out["estimate"]["credits"] == 500
    assert out["rows_this_run"] == 50
    assert "scrapes all 50 input row(s)" in out["message"]


def test_the_saved_settings_are_what_the_rerun_is_quoted_against():
    """A reused squid's caps and toggles live in its saved params, not in the
    input — with no input at all there used to be nothing to quote from."""
    client = routes_for(tasks=SAVED_ROWS[:1], squid_params={"max_results": 10})
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), squid_id="sq1")
    # 10 rows x 1 credit + the default-on step on 3 of them
    assert out["estimate"]["credits"] == 13


def test_replacing_the_rows_is_quoted_for_the_one_row_that_survives():
    client = routes_for(tasks=SAVED_ROWS, squid_params={
        "max_results": 10, "functions": {"extract_emails_from_website": False}})
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), squid_id="sq1",
                           input={"query": "x"}, replace_tasks=True, confirm=True)
    assert out["estimate"]["credits"] == 10
    assert out["tasks_action"] == "replaced"


def test_rows_that_cannot_be_read_are_reported_as_unknown_not_as_one():
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key == ("GET", "/v1/tasks"):
            return httpx.Response(500, json={"errors": {"message": "nope",
                                                        "type": "ServerError"}})
        if key == ("GET", "/v1/crawlers/gm/params"):
            return httpx.Response(200, json={"task": {}, "squid": {}})
        return httpx.Response(200, json={
            ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "gm",
                                        "params": {"max_results": 200}},
            ("GET", "/v1/crawlers/gm"): CRAWLER,
            ("GET", "/v1/user/balance"): {"available": 1000000, "consumed": 0},
            ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
        }[key])

    client = LobstrClient("https://api.lobstr.io/v1", "t",
                          transport=httpx.MockTransport(handler))
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), squid_id="sq1")
    assert out["needs_confirmation"] is True
    assert out["rows_this_run"] is None
    assert "could not be read" in out["message"]
    assert "may be a multiple of it" in out["message"]
    # and once confirmed, the same caveat rides on the successful response
    ran = run_scraper_impl(client, SETTINGS, IdempotencyStore(), squid_id="sq1",
                           confirm=True)
    assert ran["task_count"] is None
    assert "could not be read" in ran["tasks_note"]


# --- 1. the credit guard must not answer a question it was not asked --------


def test_a_bad_parameter_on_a_thin_balance_returns_the_apis_error():
    """The report: the balance guard fired first and the parameter error never
    happened. The API is the only thing that can name it, so the request has to
    be sent before the guard decides."""
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key == ("POST", "/v1/squids/sq1"):
            return httpx.Response(400, json={"errors": {
                "message": "The specified parameter auto_verify_emails is invalid.",
                "type": "InvalidParam", "code": "invalid_param"}})
        if key == ("GET", "/v1/tasks"):
            return httpx.Response(200, json={"total_pages": 1, "page": 1, "data": []})
        if key == ("GET", "/v1/crawlers/gm/params"):
            return httpx.Response(200, json={"task": {"query": {"type": "string"}},
                                             "squid": {"max_results": {"type": "int"}}})
        return httpx.Response(200, json={
            ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "gm",
                                        "params": {"max_results": 10}},
            ("GET", "/v1/crawlers/gm"): CRAWLER,
            # 13 credits estimated against 5 left: the old code stopped here
            ("GET", "/v1/user/balance"): {"available": 100, "consumed": 95},
        }[key])

    client = LobstrClient("https://api.lobstr.io/v1", "t",
                          transport=httpx.MockTransport(handler))
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), squid_id="sq1",
                           input={"query": "x", "auto_verify_emails": True},
                           confirm=True)
    # the API's own rejection, carried through the error contract
    assert out["upstream_type"] == "InvalidParam"
    assert out["upstream_status"] == 400
    assert "auto_verify_emails is invalid" in out["message"]
    assert "insufficient" not in json.dumps(out), "no credit advice for a bad param"


def _not_enough_credits(request, body):
    return httpx.Response(400, json={"errors": {
        "message": "Not enough credits.", "type": "NotEnoughCredits", "code": 400}})


def test_an_ordinary_account_out_of_credits_gets_the_apis_refusal():
    """This client used to compare the estimate against the
    balance itself and refuse before the API ever saw the request — which
    blocked a staff/admin account the API would have let through. Now the
    API is the only thing that decides, and its refusal (NotEnoughCredits)
    is surfaced through the normal structured-error path (errors.py)."""
    calls: list = []
    client = routes_for(tasks=[], squid_params={"max_results": 10},
                        balance={"available": 100, "consumed": 100}, calls=calls,
                        runs_response=_not_enough_credits)
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), squid_id="sq1",
                           input={"query": "x"}, confirm=True)
    assert out["error_code"] == "insufficient_credits"
    assert out["upstream_type"] == "NotEnoughCredits"
    assert out["remaining"] == 0
    # the config was saved and POST /runs was actually attempted — this is
    # the API's refusal, not a local prediction that never sent the request
    assert ("POST", "/v1/squids/sq1") in calls
    assert ("POST", "/v1/runs") in calls
    assert out["tasks_action"] == "added"


def test_an_admin_shaped_account_with_a_negative_balance_still_runs():
    """The API lets a staff/admin account run regardless of balance —
    including negative — so this client must not refuse what the API would
    accept. `remaining`/`credit_warning` still ride along, informationally."""
    client = routes_for(tasks=[], squid_params={"max_results": 10},
                        balance={"available": -500, "consumed": 0})
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), squid_id="sq1",
                           input={"query": "x"}, confirm=True)
    assert out["run_id"] == "run1"
    assert out["remaining"] == -500
    assert "credit_warning" in out


def test_a_thin_but_affordable_balance_only_warns_never_refuses():
    client = routes_for(tasks=[], squid_params={"max_results": 10},
                        balance={"available": 15, "consumed": 0})
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), squid_id="sq1",
                           input={"query": "x"}, confirm=True)
    assert out["run_id"] == "run1"
    assert out["remaining"] == 15
    assert "credit_warning" not in out  # 15 covers the 13-credit estimate


def test_a_balance_without_a_consumed_figure_still_reports_remaining():
    client = routes_for(tasks=[], squid_params={"max_results": 10},
                        balance={"available": 5})
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), squid_id="sq1",
                           input={"query": "x"}, confirm=True)
    assert out["run_id"] == "run1"
    assert out["remaining"] == 5
    assert "credit_warning" in out


def test_the_destructive_replace_restores_the_saved_rows_when_the_api_refuses():
    """There is no way to ask the API for a credits preflight before the
    destructive rewrite: POST /runs is the only thing that can refuse, and it
    can only be called once the squid is already in its final state. So the
    ordering guarantee is no longer "nothing destructive before a refusal" —
    it is "a refusal past that point restores what was deleted" (same pattern
    _replace_squid_input already uses for every other failure past the point
    of no return)."""
    calls: list = []
    client = routes_for(tasks=SAVED_ROWS, squid_params={"max_results": 10},
                        balance={"available": 100, "consumed": 100}, calls=calls,
                        runs_response=_not_enough_credits)
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), squid_id="sq1",
                           input={"query": "x"}, replace_tasks=True, confirm=True)
    assert out["error_code"] == "insufficient_credits"
    assert out["tasks_lost"] is False
    assert out["tasks_restored"] == len(SAVED_ROWS)
    # the destructive rewrite really did happen — this is a restore, not a
    # refusal that skipped it
    assert ("POST", "/v1/squids/sq1/empty") in calls
    assert ("POST", "/v1/runs") in calls


# --- 3. the time quote ------------------------------------------------------


def estimate_client(payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)
    return LobstrClient("https://api.lobstr.io/v1", "t",
                        transport=httpx.MockTransport(handler))


API_ESTIMATE = {
    "total_credits": 26, "estimated_time": "1 min - 3 mins", "max_results": 200,
    "services": [{"name": "Google Maps data", "results": 20, "credits": 20},
                 {"name": "Extract Emails from Website", "results": 6, "credits": 6}],
    "tasks": {"count": 1, "preview": []}, "recommended_upgrade_plan": None,
}


def test_the_time_quote_is_labelled_a_floor_with_the_reason():
    """This note was cut from a multi-sentence paragraph (with
    anecdotal timings) to one short sentence; the core warning must survive
    the cut."""
    out = estimate_run_impl(estimate_client(API_ESTIMATE), "sq1")
    assert out["estimated_time"] == "1 min - 3 mins"
    note = out["estimated_time_note"]
    assert "floor, not an ETA" in note
    assert "poll get_run" in note
    assert len(note) < 200  # one short sentence, not the old paragraph


def test_the_credit_quote_says_what_it_covers_and_what_it_leaves_out():
    """Same cut as the time note — one short sentence, core facts kept."""
    out = estimate_run_impl(estimate_client(API_ESTIMATE), "sq1")
    assert out["total_credits"] == 26
    note = out["estimate_note"]
    assert "authoritative" in note
    assert "upper bound" in note
    # the per-row filters are billed and are in neither estimate
    assert "filters" in note
    assert len(note) < 200  # one short sentence, not the old paragraph


def test_estimate_run_still_passes_the_api_payload_through():
    out = estimate_run_impl(estimate_client(API_ESTIMATE), "sq1")
    assert out["services"] == API_ESTIMATE["services"]
    assert out["tasks"] == {"count": 1, "preview": []}
    assert out["max_results"] == 200
    assert out["squid_id"] == "sq1"
