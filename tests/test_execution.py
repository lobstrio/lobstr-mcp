import json
import httpx
from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.safeguards import IdempotencyStore
from lobstr_mcp.execution import run_scraper_impl, get_run_impl, get_results_impl
from lobstr_mcp.config import Settings

SETTINGS = Settings(
    lobstr_api_base="https://api.lobstr.io/v1", dev_token=None, request_timeout=30.0,
    run_confirm_threshold=100, public_base_url="https://mcp.lobstr.io",
    service_credential=None, consent_url="https://app.lobstr.io/connect-ai",
)

CRAWLER_GM = {
    "id": "gm", "name": "Google Maps",
    "input": [{"name": "query", "type": "string", "level": "task", "required": True}],
    "result": ["title", "address"],
}


def routed_client(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        resp = routes[(request.method, request.url.path)]
        return resp(request, body) if callable(resp) else httpx.Response(200, json=resp)
    return LobstrClient("https://api.lobstr.io/v1", "t",
                        transport=httpx.MockTransport(handler))


def happy_routes(crawler=CRAWLER_GM, balance=1000):
    return {
        ("GET", "/v1/crawlers/gm"): crawler,
        ("GET", "/v1/user/balance"): {"balance": balance},
        ("POST", "/v1/squids"): {"id": "sq1"},
        # saving settings is what makes the squid runnable (is_ready)
        ("POST", "/v1/squids/sq1"): {},
        ("POST", "/v1/tasks"): {"duplicated_count": 0, "tasks": [{"id": "t1"}]},
        ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
    }


def test_validation_error_blocks_execution():
    client = routed_client({("GET", "/v1/crawlers/gm"): CRAWLER_GM})
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), "gm", {})
    assert out["error_code"] == "validation_error"
    assert out["errors"] == ["query is required"]


def test_unknown_cost_requires_confirmation():
    client = routed_client(happy_routes())
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), "gm", {"query": "x"})
    assert out["needs_confirmation"] is True
    assert out["estimate"]["credits"] is None


def test_confirm_executes_and_returns_run_id():
    client = routed_client(happy_routes())
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), "gm",
                           {"query": "x"}, confirm=True)
    assert out["run_id"] == "run1"
    assert out["squid_id"] == "sq1"
    assert out["status"] == "pending"


def test_insufficient_credits_is_the_apis_refusal_not_a_local_guess():
    """The API decides affordability, not this client — a low
    local balance figure alone starts the run; only the API's own
    NotEnoughCredits, surfaced through the normal structured-error path,
    produces `insufficient_credits`."""
    crawler = {**CRAWLER_GM, "credits_per_task": 50}
    routes = happy_routes(crawler=crawler, balance=10)
    routes[("POST", "/v1/runs")] = lambda request, body: httpx.Response(
        400, json={"errors": {"message": "Not enough credits.",
                              "type": "NotEnoughCredits", "code": 400}})
    client = routed_client(routes)
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), "gm",
                           {"query": "x"}, confirm=True)
    assert out["error_code"] == "insufficient_credits"
    assert out["upstream_type"] == "NotEnoughCredits"


def test_a_low_balance_alone_does_not_block_the_run():
    """The old client-side guard compared the estimate to the balance and
    refused outright — which also blocked a staff/admin account the API
    would have let run regardless, including at a negative balance."""
    crawler = {**CRAWLER_GM, "credits_per_task": 50}
    client = routed_client(happy_routes(crawler=crawler, balance=10))
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), "gm",
                           {"query": "x"}, confirm=True)
    assert out["run_id"] == "run1"
    assert out["remaining"] == 10
    assert "credit_warning" in out


def test_idempotency_returns_same_run_without_re_executing():
    store = IdempotencyStore()
    client = routed_client(happy_routes())
    first = run_scraper_impl(client, SETTINGS, store, "gm", {"query": "x"}, confirm=True)
    assert first["run_id"] == "run1"
    # second call: no execute routes needed; served from idempotency store
    client2 = routed_client({("GET", "/v1/crawlers/gm"): CRAWLER_GM})
    second = run_scraper_impl(client2, SETTINGS, store, "gm", {"query": "x"}, confirm=True)
    assert second["status"] == "already_submitted"
    assert second["run_id"] == "run1" and second["idempotent"] is True


def test_get_run_normalizes_status():
    client = routed_client({
        ("GET", "/v1/runs/run1/stats"):
            {"id": "run1", "is_done": False, "started_at": "2026-01-01"},
        ("GET", "/v1/runs/run1"): {"id": "run1", "status": "running",
                                   "credit_used": 0},
    })
    out = get_run_impl(client, "run1")
    assert out["run_id"] == "run1" and out["status"] == "running" and out["is_done"] is False


def test_get_results_caps_and_selects_fields():
    rows = [{"title": f"t{i}", "address": f"a{i}", "phone": i} for i in range(5)]
    client = routed_client({("GET", "/v1/results"):
                            {"total_results": 5, "page": 1, "total_pages": 1, "data": rows}})
    out = get_results_impl(client, run_id="run1", fields=["title"], max_rows=3)
    assert out["returned"] == 3
    assert out["results"][0] == {"title": "t0"}
    assert set(out["available_fields"]) == {"title", "address", "phone"}
    assert out["total_results"] == 5


def test_get_results_surfaces_free_plan_export_limit_cleanly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errors": {
            "message": "You have reached the free plan limit of 30 results. "
                       "Upgrade to a premium plan to access more results.",
            "type": "ExportLimitReached", "code": 400}})
    client = LobstrClient("https://api.lobstr.io/v1", "t",
                          transport=httpx.MockTransport(handler))
    out = get_results_impl(client, run_id="run1", page=4)
    assert out["error_code"] == "export_limit_reached"
    assert out["upstream_status"] == 400
    assert "30 results" in out["message"]


def test_get_results_requires_run_or_squid():
    client = routed_client({})
    out = get_results_impl(client)
    assert out["error_code"] == "invalid_request"


def test_balance_check_reads_the_real_available_field():
    """GET /v1/user/balance returns {available, consumed, ...} — there is no
    "balance" key. `remaining` (available minus consumed) rides on the
    response either way; it no longer gates the run."""
    crawler = {**CRAWLER_GM, "credits_per_task": 50}
    routes = happy_routes(crawler=crawler)
    routes[("GET", "/v1/user/balance")] = {"object": "plan", "available": 10,
                                           "consumed": 0}
    client = routed_client(routes)
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), "gm",
                           {"query": "x"}, confirm=True)
    assert out["run_id"] == "run1"
    assert out["remaining"] == 10
    assert "credit_warning" in out


# --- squid readiness + settings shape (found in P4 live testing) --------------

CRAWLER_WITH_SQUID_PARAM = {
    "id": "gm", "name": "Google Maps",
    "input": [
        {"name": "query", "type": "string", "level": "task", "required": True},
        {"name": "max_results", "type": "int", "level": "squid", "required": False},
    ],
    "result": ["title"],
}


def _capture_routes(crawler=CRAWLER_GM):
    seen = {"settings": []}
    routes = happy_routes(crawler=crawler)
    routes[("GET", "/v1/user/balance")] = {"object": "plan", "available": 1000}

    def settings_route(request, body):
        seen["settings"].append(body)
        return httpx.Response(201, json={})

    routes[("POST", "/v1/squids/sq1")] = settings_route
    return routes, seen


def test_settings_are_always_saved_so_the_squid_becomes_runnable():
    """POST /v1/runs returns 400 SquidNotReady unless the squid's settings have
    been saved. The save was skipped whenever the scraper had no squid-level
    inputs, so those scrapers could never be run at all."""
    routes, seen = _capture_routes()
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           "gm", {"query": "x"}, confirm=True)
    assert out.get("run_id") == "run1", out
    assert len(seen["settings"]) == 1, "settings must be saved even with no squid params"


def test_squid_level_inputs_are_nested_under_params():
    """The API only recognizes squid settings under a "params" key (plus name,
    concurrency, …); flat keys were silently discarded."""
    routes, seen = _capture_routes(crawler=CRAWLER_WITH_SQUID_PARAM)
    run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(), "gm",
                     {"query": "x", "max_results": 5}, confirm=True)
    body = seen["settings"][0]
    assert body["params"] == {"max_results": 5}, body
    assert "max_results" not in body, "must not be sent flat"
    assert body.get("name"), "a name is required for the save to take effect"


def test_upstream_failure_returns_the_structured_error_contract():
    routes, _ = _capture_routes()
    routes[("POST", "/v1/runs")] = lambda request, body: httpx.Response(
        400, json={"errors": {"message": "Squid not ready, please update the "
                              "settings first.", "type": "SquidNotReady",
                              "code": 400}})
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           "gm", {"query": "x"}, confirm=True)
    assert out["error_code"] == "scraper_not_ready", out
    assert out["upstream_status"] == 400
    assert "hint" in out


def test_get_run_uses_the_authoritative_status_and_reports_credits():
    """/runs/{hash}/stats carries no status field, so deriving it from is_done
    reported "running" for a job the API calls "pending". The run detail
    endpoint has the real status plus credit_used."""
    routes = {
        ("GET", "/v1/runs/run1/stats"): {
            "id": "run1", "is_done": False, "percent_done": "0.0%",
            "total_tasks": 1, "total_tasks_done": 0, "total_results": 0},
        ("GET", "/v1/runs/run1"): {
            "id": "run1", "status": "pending", "credit_used": 0,
            "total_results": 0, "done_reason": None},
    }
    out = get_run_impl(routed_client(routes), "run1")
    assert out["status"] == "pending"
    assert out["credits_consumed"] == 0
    assert out["total_results"] == 0
    assert out["progress"] == "0.0%"


def test_get_run_survives_a_missing_run_detail():
    """The detail call is supplementary — stats alone must still answer."""
    routes = {
        ("GET", "/v1/runs/run1/stats"): {"id": "run1", "is_done": True},
        ("GET", "/v1/runs/run1"): lambda request, body: httpx.Response(
            404, json={"errors": {"message": "gone", "type": "HTTPNotFound",
                                  "code": 404}}),
    }
    out = get_run_impl(routed_client(routes), "run1")
    assert out["status"] == "done"


GM_PARAMS_ROUTES = {
    "task": {"url": "string"},
    "squid": {"max_results": "int",
              "functions": {"extract_emails_from_website": {"default": True}}},
}

CRAWLER_GM_FUNCS = {
    "id": "gm", "name": "Google Maps",
    "input": [
        {"name": "url", "type": "string", "level": "task", "required": True},
        {"name": "max_results", "type": "int", "level": "squid"},
        {"name": "extract_emails_from_website", "type": "boolean",
         "level": "squid"},
    ],
    "result": ["name"],
}


def test_function_toggles_are_nested_under_params_functions():
    """The API rejects a function toggle sent as a top-level squid param:
    InvalidParam "The specified parameter extract_emails_from_website is
    invalid." — which failed the entire run."""
    seen = {"settings": []}
    routes = happy_routes(crawler=CRAWLER_GM_FUNCS)
    routes[("GET", "/v1/user/balance")] = {"object": "plan", "available": 1000}
    routes[("GET", "/v1/crawlers/gm/params")] = GM_PARAMS_ROUTES

    def settings_route(request, body):
        seen["settings"].append(body)
        return httpx.Response(201, json={})

    routes[("POST", "/v1/squids/sq1")] = settings_route

    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           "gm", {"url": "u", "max_results": 3,
                                  "extract_emails_from_website": False},
                           confirm=True)
    assert out.get("run_id") == "run1", out
    params = seen["settings"][0]["params"]
    assert params["max_results"] == 3
    assert params["functions"] == {"extract_emails_from_website": False}
    assert "extract_emails_from_website" not in params, "must not be sent flat"


def test_no_functions_key_when_none_supplied():
    seen = {"settings": []}
    routes = happy_routes(crawler=CRAWLER_GM_FUNCS)
    routes[("GET", "/v1/user/balance")] = {"object": "plan", "available": 1000}
    routes[("GET", "/v1/crawlers/gm/params")] = GM_PARAMS_ROUTES
    routes[("POST", "/v1/squids/sq1")] = lambda request, body: (
        seen["settings"].append(body) or httpx.Response(201, json={}))

    run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(), "gm",
                     {"url": "u", "max_results": 3}, confirm=True)
    assert "functions" not in seen["settings"][0]["params"]


# --- Phase A: LLM-friendly slimming (defaults) + full=true escape hatch --------

def _results_client(rows, total=None):
    return routed_client({("GET", "/v1/results"): {
        "total_results": total if total is not None else len(rows),
        "page": 1, "total_pages": 1, "data": rows}})


def test_get_results_drops_empty_fields_by_default():
    rows = [{"title": "t0", "phone": "", "website": None, "score": 0, "tags": []}]
    out = get_results_impl(_results_client(rows), run_id="run1")
    assert out["results"][0] == {"title": "t0", "score": 0}  # 0 kept; ""/None/[] dropped
    # dropped columns still advertised so the model knows it can ask for them
    assert set(out["available_fields"]) == {"title", "phone", "website", "score", "tags"}


def test_get_results_full_keeps_empty_fields():
    rows = [{"title": "t0", "phone": "", "website": None}]
    out = get_results_impl(_results_client(rows), run_id="run1", full=True)
    assert out["results"][0] == {"title": "t0", "phone": "", "website": None}


def test_get_results_default_cap_is_ten_full_is_twentyfive():
    rows = [{"title": f"t{i}"} for i in range(30)]
    assert get_results_impl(_results_client(rows), run_id="run1")["returned"] == 10
    assert get_results_impl(_results_client(rows), run_id="run1", full=True)["returned"] == 25


def _paged_api_client(all_rows, seen_page_sizes=None):
    """A mock /v1/results that actually respects page_size (unlike
    _results_client, which always answers with every row regardless of what
    was asked for) — the shape needed to catch get_results_impl truncating a
    page the caller asked to be bigger."""
    def handler(request: httpx.Request) -> httpx.Response:
        page_size = int(request.url.params.get("page_size", 10))
        page = int(request.url.params.get("page", 1))
        if seen_page_sizes is not None:
            seen_page_sizes.append(page_size)
        start = (page - 1) * page_size
        page_rows = all_rows[start:start + page_size]
        total_pages = max(1, -(-len(all_rows) // page_size))
        return httpx.Response(200, json={"total_results": len(all_rows), "page": page,
                                         "total_pages": total_pages, "data": page_rows})
    return LobstrClient("https://api.lobstr.io/v1", "t",
                        transport=httpx.MockTransport(handler))


def test_get_results_page_size_is_honoured_not_capped_at_ten():
    # Bug #12: a page_size of 50 used to still only return 10 rows (max_rows
    # was fixed regardless of page_size), and the API's own page_size (sent as
    # None) defaulted server-side to 10 while total_pages here still reported
    # whatever the untouched page_size implied — the two never matched.
    rows = [{"title": f"t{i}"} for i in range(50)]
    seen = []
    out = get_results_impl(_paged_api_client(rows, seen), run_id="run1", page_size=50)
    assert seen == [50]
    assert out["returned"] == 50
    assert out["page_size"] == 50
    assert out["total_pages"] == 1


def test_get_results_page_size_defaults_match_what_is_sent_to_the_api():
    rows = [{"title": f"t{i}"} for i in range(25)]
    seen = []
    out = get_results_impl(_paged_api_client(rows, seen), run_id="run1")
    assert seen == [10]
    assert out["returned"] == 10
    assert out["total_pages"] == 3  # ceil(25/10), consistent with page_size actually used


def test_get_results_page_size_is_capped_at_a_sane_max():
    rows = [{"title": f"t{i}"} for i in range(500)]
    seen = []
    out = get_results_impl(_paged_api_client(rows, seen), run_id="run1", page_size=10_000)
    assert seen == [100]
    assert out["page_size"] == 100


def test_run_scraper_accepts_a_slug():
    cid = "d" * 32
    routes = {
        ("GET", "/v1/crawlers"): [{"id": cid, "slug": "gm-slug", "name": "GM"}],
        ("GET", f"/v1/crawlers/{cid}"): {
            "id": cid, "name": "GM",
            "input": [{"name": "query", "type": "string", "level": "task", "required": True}],
            "result": ["title"], "credits_per_task": 5},
        ("GET", "/v1/user/balance"): {"object": "plan", "available": 1000},
        ("POST", "/v1/squids"): {"id": "sq1"},
        ("POST", "/v1/squids/sq1"): {},
        ("POST", "/v1/tasks"): {"duplicated_count": 0, "tasks": [{"id": "t1"}]},
        ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
    }
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           "gm-slug", {"query": "x"}, confirm=True)
    assert out["run_id"] == "run1"
    assert out["scraper"] == cid  # response carries the canonical id, not the slug


CRAWLER_GM_GROUPED = {
    "id": "gm", "name": "Google Maps", "result": ["title"], "credits_per_task": 1,
    "input": [
        {"name": "url", "type": "string", "level": "task", "required": True, "group": "url"},
        {"name": "category", "type": "string", "level": "task", "required": True, "group": "location"},
        {"name": "country", "type": "string", "level": "task", "required": True, "group": "location"},
        {"name": "city", "type": "string", "level": "task", "required": True, "group": "location"},
        {"name": "language", "type": "string", "level": "squid", "required": True},
    ],
}


def test_run_scraper_url_only_mode_is_accepted():
    routes = happy_routes(crawler=CRAWLER_GM_GROUPED)
    routes[("GET", "/v1/user/balance")] = {"available": 1000}
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           "gm", {"url": "https://maps.google/...", "language": "en"}, confirm=True)
    assert out.get("run_id") == "run1", out  # not blocked by missing category/country/city


def test_run_scraper_location_mode_is_accepted():
    routes = happy_routes(crawler=CRAWLER_GM_GROUPED)
    routes[("GET", "/v1/user/balance")] = {"available": 1000}
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(), "gm",
                           {"category": "cafe", "country": "US", "city": "NYC", "language": "en"},
                           confirm=True)
    assert out.get("run_id") == "run1", out


def test_run_scraper_rejects_when_no_input_group_is_complete():
    out = run_scraper_impl(routed_client({("GET", "/v1/crawlers/gm"): CRAWLER_GM_GROUPED}),
                           SETTINGS, IdempotencyStore(), "gm", {"language": "en"})
    assert out["error_code"] == "validation_error"


def test_run_scraper_reuses_existing_squid_with_new_input():
    # replace_tasks=True: emptying the squid only happens when the caller asks
    # for it (the default path is covered in test_run_scraper_task_default.py).
    seen = {"emptied": False}
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "gm", "name": "My GM"},
        ("GET", "/v1/crawlers/gm"): CRAWLER_GM,
        ("GET", "/v1/user/balance"): {"available": 1000},
        # the old rows are snapshotted before they're dropped, so they can be
        # put back if the rewrite fails (see test_run_scraper_task_safety.py)
        ("GET", "/v1/tasks"): {"total_pages": 1, "page": 1, "data": []},
        ("POST", "/v1/squids/sq1/empty"): lambda r, b: (
            seen.__setitem__("emptied", True) or httpx.Response(200, json={})),
        ("POST", "/v1/squids/sq1"): {},
        ("POST", "/v1/tasks"): {"duplicated_count": 0, "tasks": [{"id": "t1"}]},
        ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
        # no ("POST","/v1/squids"): a create attempt would KeyError this test
    }
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           input={"query": "dentists"}, confirm=True, squid_id="sq1",
                           replace_tasks=True)
    assert out["run_id"] == "run1" and out["squid_id"] == "sq1"
    assert out["reused_squid"] is True
    assert seen["emptied"] is True  # old tasks cleared before re-adding


def test_run_scraper_reruns_existing_squid_as_is():
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "gm", "name": "My GM"},
        ("GET", "/v1/crawlers/gm"): CRAWLER_GM,
        ("GET", "/v1/user/balance"): {"available": 1000},
        # the saved rows are READ, because how many there are is what this run
        # costs; no write route is listed, so writing any of them is still a bug
        ("GET", "/v1/tasks"): {"total_pages": 1, "page": 1,
                               "data": [{"id": "t1", "params": {"url": "https://a"}}]},
        ("POST", "/v1/runs"): {"id": "run2", "status": "pending"},
        # no empty/update/POST tasks routes: with no input, they must NOT be called
    }
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           confirm=True, squid_id="sq1")
    assert out["run_id"] == "run2" and out["reused_squid"] is True


def test_run_scraper_needs_scraper_or_squid_id():
    out = run_scraper_impl(routed_client({}), SETTINGS, IdempotencyStore())
    assert out["error_code"] == "invalid_request"


# --- account attach: a squid with no account can never ------------------------
# launch a run for a crawler that needs one. run_scraper resolves/attaches
# before starting the run instead of letting it fail asynchronously.

CRAWLER_LI = {
    "id": "li", "name": "LinkedIn Leads", "account": {"type": "linkedin-sync"},
    "input": [{"name": "url", "type": "string", "level": "task", "required": True}],
    "result": ["name"],
}


def li_routes(accounts_payload):
    return {
        ("GET", "/v1/crawlers/li"): CRAWLER_LI,
        ("GET", "/v1/user/balance"): {"available": 1000},
        ("GET", "/v1/accounts"): accounts_payload,
        ("POST", "/v1/squids"): {"id": "sq1"},
        ("POST", "/v1/squids/sq1"): {},
        ("POST", "/v1/tasks"): {"duplicated_count": 0, "tasks": [{"id": "t1"}]},
        ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
    }


def test_run_scraper_auto_attaches_the_single_healthy_account():
    seen = {"settings": None}
    routes = li_routes({"data": [{"id": "a1", "type": "linkedin-sync", "status": "200"}]})

    def settings_route(request, body):
        seen["settings"] = body
        return httpx.Response(201, json={})

    routes[("POST", "/v1/squids/sq1")] = settings_route
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           "li", {"url": "https://linkedin.com/in/x"}, confirm=True)
    assert out.get("run_id") == "run1", out
    # auto-picked: only a bool, never the id — this caller holds runs:execute
    # only, not account:read.
    assert out["account_attached"] is True
    assert "account_warning" not in out
    assert seen["settings"]["accounts"] == ["a1"]


def test_run_scraper_several_candidates_blocks_without_leaking_the_list():
    """run_scraper holds runs:execute, not account:read — an ambiguous pick
    must not hand back the candidates' ids/usernames through it."""
    routes = li_routes({"data": [
        {"id": "a1", "type": "linkedin-sync", "status": "200", "username": "u1"},
        {"id": "a2", "type": "linkedin-sync", "status": "200", "username": "u2"}]})
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           "li", {"url": "https://linkedin.com/in/x"}, confirm=True)
    assert out["error_code"] == "multiple_accounts_available"
    assert "candidates" not in out
    assert "a1" not in out["message"] and "u1" not in out["message"]


def test_run_scraper_no_account_available_names_the_required_type():
    routes = li_routes({"data": []})
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           "li", {"url": "https://linkedin.com/in/x"}, confirm=True)
    assert out["error_code"] == "no_account_available"
    assert out["required_type"] == "linkedin-sync"


def test_run_scraper_rejects_a_wrong_type_explicit_account():
    routes = li_routes({"data": []})
    routes[("GET", "/v1/accounts/wrong")] = {"id": "wrong", "type": "sales-nav-sync",
                                             "status": "200"}
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           "li", {"url": "https://linkedin.com/in/x"}, confirm=True,
                           account_id="wrong")
    assert out["error_code"] == "account_type_mismatch"
    assert out["required_type"] == "linkedin-sync"


def test_run_scraper_leaves_an_already_attached_account_alone():
    """A squid re-run with an account already attached must not touch
    `accounts` at all — no candidate lookup, no replace."""
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li",
                                    "accounts": [{"id": "a0", "status": "200"}]},
        ("GET", "/v1/crawlers/li"): CRAWLER_LI,
        ("GET", "/v1/user/balance"): {"available": 1000},
        ("GET", "/v1/tasks"): {"total_pages": 1, "page": 1, "data": []},
        ("POST", "/v1/runs"): {"id": "run2", "status": "pending"},
        # no /v1/accounts, no POST /v1/squids/sq1: either being called is a bug
    }
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           confirm=True, squid_id="sq1")
    assert out["run_id"] == "run2"
    # no account_id given and nothing new attached this call -> still True:
    # the squid HAS an account attached (a0, from before this call), and
    # account_attached reports current state, not "did this call do it"
    # (it used to read False here, misleading a client into
    # thinking it needed to attach one). Still a bare bool, not a0's id: this
    # caller holds runs:execute only, not account:read.
    assert out["account_attached"] is True


def test_run_scraper_rejects_account_id_when_crawler_needs_none():
    out = run_scraper_impl(routed_client({("GET", "/v1/crawlers/gm"): CRAWLER_GM}),
                           SETTINGS, IdempotencyStore(), "gm", {"query": "x"},
                           account_id="a1")
    assert out["error_code"] == "no_account_needed"


def test_run_scraper_locked_only_account_is_redacted_like_multiple_candidates():
    routes = li_routes({"data": [
        {"id": "a1", "type": "linkedin-sync", "status": "200",
         "lock_time": "2099-01-01T00:00:00Z"}]})
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           "li", {"url": "https://linkedin.com/in/x"}, confirm=True)
    assert out["error_code"] == "account_locked"
    assert "candidates" not in out
    assert "a1" not in out["message"]


def test_run_scraper_explicit_account_id_returns_the_id_not_a_bool():
    """Echoing back the caller's own account_id isn't a leak; a bare bool
    here would be a step down in information the caller already had."""
    routes = li_routes({"data": []})
    routes[("GET", "/v1/accounts/a1")] = {"id": "a1", "type": "linkedin-sync",
                                          "status": "200"}
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           "li", {"url": "https://linkedin.com/in/x"}, confirm=True,
                           account_id="a1")
    assert out["account_attached"] == "a1"


def test_run_scraper_explicit_account_id_already_attached_still_returns_the_id():
    """A null `account_attached` here would read as failure even though the
    run started fine on the account the caller asked for."""
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li",
                                    "accounts": [{"id": "a0", "status": "200"}]},
        ("GET", "/v1/crawlers/li"): CRAWLER_LI,
        ("GET", "/v1/accounts/a0"): {"id": "a0", "type": "linkedin-sync", "status": "200"},
        ("GET", "/v1/user/balance"): {"available": 1000},
        ("GET", "/v1/tasks"): {"total_pages": 1, "page": 1, "data": []},
        ("POST", "/v1/runs"): {"id": "run2", "status": "pending"},
        # no POST /v1/squids/sq1: a0 is already attached, nothing should be written
    }
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           confirm=True, squid_id="sq1", account_id="a0")
    assert out["account_attached"] == "a0"


def test_run_scraper_explicit_unhealthy_account_warns_but_still_runs():
    """Health never blocks an explicit choice; the caller gets a warning
    instead of a refusal."""
    routes = li_routes({"data": []})
    routes[("GET", "/v1/accounts/a1")] = {"id": "a1", "type": "linkedin-sync",
                                          "status": "429"}
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           "li", {"url": "https://linkedin.com/in/x"}, confirm=True,
                           account_id="a1")
    assert out.get("run_id") == "run1", out
    assert out["account_attached"] == "a1"
    assert "account_warning" in out


def test_run_scraper_reuse_without_input_still_attaches_an_explicit_new_account():
    """Reusing a squid with no new input skips the settings/tasks rewrite, but
    an explicit new account must still be saved — merged with what's already
    there, in one call."""
    seen = {"accounts_body": None}
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li",
                                    "accounts": [{"id": "a0", "status": "200"}]},
        ("GET", "/v1/crawlers/li"): CRAWLER_LI,
        ("GET", "/v1/accounts/a1"): {"id": "a1", "type": "linkedin-sync", "status": "200"},
        ("GET", "/v1/user/balance"): {"available": 1000},
        ("GET", "/v1/tasks"): {"total_pages": 1, "page": 1, "data": []},
        ("POST", "/v1/runs"): {"id": "run2", "status": "pending"},
    }

    def update(request, body):
        seen["accounts_body"] = body
        return httpx.Response(201, json={})

    routes[("POST", "/v1/squids/sq1")] = update
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           confirm=True, squid_id="sq1", account_id="a1")
    assert out["run_id"] == "run2"
    assert out["account_attached"] == "a1"
    # the reuse-without-input branch: only `accounts` is sent, and it carries
    # the union — a0 kept, a1 added — not a1 alone.
    assert seen["accounts_body"] == {"accounts": ["a0", "a1"]}


def test_run_scraper_refuses_to_write_over_an_unrecognized_accounts_shape():
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li", "accounts": [1, 2, 3]},
        ("GET", "/v1/crawlers/li"): CRAWLER_LI,
        ("GET", "/v1/user/balance"): {"available": 1000},
        # no /v1/accounts, no POST /v1/squids/sq1, no /v1/runs: none may be called
    }
    out = run_scraper_impl(routed_client(routes), SETTINGS, IdempotencyStore(),
                           confirm=True, squid_id="sq1")
    assert out["error_code"] == "accounts_unreadable"


def test_get_run_omits_raw_stats_by_default_and_includes_with_full():
    routes = {
        ("GET", "/v1/runs/run1/stats"): {"id": "run1", "is_done": True,
                                         "percent_done": "100%"},
        ("GET", "/v1/runs/run1"): {"id": "run1", "status": "done", "credit_used": 5},
    }
    assert "stats" not in get_run_impl(routed_client(routes), "run1")
    assert get_run_impl(routed_client(routes), "run1", full=True)["stats"]["id"] == "run1"
