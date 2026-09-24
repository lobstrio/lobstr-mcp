"""Coverage for the tools added on top of the SDK rebase: check_credits,
whoami, list_runs, get_results_url, abort_run, empty_scraper."""
import json

import httpx

from lobstr_mcp.execution import abort_run_impl, get_results_url_impl, list_runs_impl
from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.tools.accounts import get_account_impl, list_accounts_impl
from lobstr_mcp.tools.scrapers import deactivate_scraper_impl, empty_scraper_impl
from lobstr_mcp._version import __version__
from lobstr_mcp.tools.user import check_credits_impl, whoami_impl


def client_for(routes, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.method, request.url.path))
        return httpx.Response(200, json=routes.get(request.url.path, {}))
    return LobstrClient("https://api.lobstr.io/v1", "t",
                        transport=httpx.MockTransport(handler))


def test_check_credits():
    c = client_for({"/v1/user/balance": {
        "available": 500, "consumed": 12, "interval": "monthly",
        "reset_time": "2026-10-01T00:00:00Z",
        "used_slots": 1, "total_available_slots": 4}})
    out = check_credits_impl(c)
    assert (out["available"], out["consumed"]) == (500, 12)
    assert (out["used_slots"], out["total_slots"]) == (1, 4)
    # Which period the two figures cover travels with them;
    # tests/test_credits_and_identity.py covers the wording.
    assert (out["interval"], out["reset_time"]) == ("monthly", "2026-10-01T00:00:00Z")
    assert out["credits_note"] and out["slots_note"]


def test_whoami():
    c = client_for({"/v1/me": {
        "first_name": "Ada", "last_name": "Lovelace", "email": "a@b.co",
        "is_staff": False, "credit_interval": "monthly",
        "plan": [{"type": "current", "name": "pro", "status": "active",
                  "total_consumed": 16382.6}]}})
    out = whoami_impl(c)
    assert out["email"] == "a@b.co"
    assert out["name"] == "Ada Lovelace"
    assert out["plan"] == "pro" and out["plan_status"] == "active"
    assert out["server_version"] == __version__
    # No credit figure here: check_credits is the only tool that reports one.
    assert "16382.6" not in json.dumps(out)


def test_list_runs():
    c = client_for({"/v1/runs": {"data": [
        {"id": "r1", "status": "done", "total_results": 10, "credit_used": 10,
         "started_at": "t1", "ended_at": "t2"},
        {"id": "r2", "status": "running", "total_results": 0}]}})
    out = list_runs_impl(c, "sq1")
    assert out["count"] == 2
    assert out["runs"][0] == {"run_id": "r1", "status": "done", "total_results": 10,
                              "credits_consumed": 10, "started_at": "t1", "ended_at": "t2"}


def test_get_results_url():
    c = client_for({"/v1/runs/r1/download": {"s3": "https://s3.example/results.csv"}})
    assert get_results_url_impl(c, "r1") == {
        "run_id": "r1", "format": "csv", "download_url": "https://s3.example/results.csv"}


def test_get_results_url_passes_format_through():
    seen = []
    c = client_for({"/v1/runs/r1/download": {"s3": "https://s3.example/results.xlsx"}}, seen)
    out = get_results_url_impl(c, "r1", format="xlsx")
    assert out["format"] == "xlsx"
    assert out["download_url"] == "https://s3.example/results.xlsx"


def test_get_results_url_rejects_an_unknown_format():
    c = client_for({})
    out = get_results_url_impl(c, "r1", format="pdf")
    assert out["error_code"] == "invalid_request"


def test_get_results_url_reports_processing_instead_of_erroring():
    c = client_for({"/v1/runs/r1/download": {"status": "processing", "progress": 40}})
    out = get_results_url_impl(c, "r1", format="jsonl")
    assert out["status"] == "processing"
    assert out["progress"] == 40
    assert "download_url" not in out


def test_abort_run_posts_to_abort_when_running():
    seen = []
    # GET /v1/runs/r1 returns {} (no status) -> treated as running -> abort proceeds
    c = client_for({"/v1/runs/r1/abort": {}}, seen)
    out = abort_run_impl(c, "r1")
    assert ("POST", "/v1/runs/r1/abort") in seen
    assert out["run_id"] == "r1" and out["aborted"] is True and out["status"] == "aborting"


def test_abort_run_noop_on_finished_run():
    # API returns the finished run's data from /abort (no actual abort); surface it.
    c = client_for({"/v1/runs/r1/abort": {
        "id": "r1", "status": "DONE", "is_done": True, "done_reason": "tasks_done"}})
    out = abort_run_impl(c, "r1")
    assert out["aborted"] is False and out["status"] == "done"
    assert "already" in out["message"].lower()


def test_abort_run_reports_aborting_when_it_takes():
    c = client_for({"/v1/runs/r1/abort": {
        "id": "r1", "status": "UPLOADING", "done_reason": "aborted", "is_done": True}})
    out = abort_run_impl(c, "r1")
    assert out["aborted"] is True and out["status"] == "aborting"


def test_empty_scraper_posts_to_empty():
    seen = []
    c = client_for({"/v1/squids/sq1/empty": {}}, seen)
    out = empty_scraper_impl(c, "sq1")
    assert ("POST", "/v1/squids/sq1/empty") in seen
    assert out["squid_id"] == "sq1" and out["status"] == "emptied"


def test_deactivate_scraper_posts_is_active_false():
    import json as _json
    cap = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["method"], cap["path"] = request.method, request.url.path
        cap["body"] = _json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"is_active": False})

    c = LobstrClient("https://api.lobstr.io/v1", "t",
                     transport=httpx.MockTransport(handler))
    out = deactivate_scraper_impl(c, "sq1")
    assert cap["method"] == "POST" and cap["path"] == "/v1/squids/sq1"
    assert cap["body"] == {"is_active": False}
    assert out["is_active"] is False and out["status"] == "deactivated"


# --- platform accounts (metadata only) -----------------------------------

_ACCOUNTS = {"data": [
    {"id": "ac1", "type": "linkedin", "username": "ada@co", "params": {"secret": 1},
     "cookies": {"li_at": "SENSITIVE"}, "status_code_description": "Active",
     "last_synchronization_time": "2026-09-01"},
    {"id": "ac2", "type": "facebook", "username": "ada.fb",
     "status_code_description": "Expired", "last_synchronization_time": "2026-08-01"},
]}


def test_list_accounts_whitelists_and_never_leaks_credentials():
    out = list_accounts_impl(client_for({"/v1/accounts": _ACCOUNTS}))
    assert out["count"] == 2
    li = out["accounts"][0]
    assert li == {"id": "ac1", "platform": "linkedin", "username": "ada@co",
                  "status": "Active", "last_sync": "2026-09-01"}
    blob = repr(out)
    assert "cookies" not in blob and "SENSITIVE" not in blob and "secret" not in blob


def test_list_accounts_platform_filter():
    out = list_accounts_impl(client_for({"/v1/accounts": _ACCOUNTS}), platform="facebook")
    assert out["count"] == 1 and out["accounts"][0]["platform"] == "facebook"


def test_get_account_summarizes_and_lists_squids():
    acc = {"id": "ac1", "type": "linkedin", "username": "ada@co",
           "status_code_description": "Active", "cookies": {"li_at": "X"},
           "squids": [{"id": "sq1", "name": "My LI"}, {"id": "sq2", "name": "Other"}]}
    out = get_account_impl(client_for({"/v1/accounts/ac1": acc}), "ac1")
    assert out["platform"] == "linkedin" and out["status"] == "Active"
    assert out["used_by"] == [{"id": "sq1", "name": "My LI"}, {"id": "sq2", "name": "Other"}]
    assert "cookies" not in repr(out)
