"""A LinkedIn account's daily limit and the paused run's auto-relaunch must be
visible on get_run / get_account (card MLhj4ohs)."""
import httpx

from lobstr_mcp.execution import get_run_impl
from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.tools.accounts import get_account_impl


def client_for(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=routes.get(request.url.path, {}))
    return LobstrClient("https://api.lobstr.io/v1", "t", transport=httpx.MockTransport(handler))


PAUSED_ON_LIMIT = {"id": "run1", "squid": "sq1", "status": "paused", "is_done": False,
                   "done_reason": "limit_exceeded", "done_reason_desc": "Account's limit has exceeded.",
                   "next_launch_at": "2026-10-02T10:00:00Z"}


def test_get_run_names_the_account_limit_and_the_relaunch():
    out = get_run_impl(client_for({
        "/v1/runs/run1/stats": {"id": "run1", "is_done": False},
        "/v1/runs/run1": PAUSED_ON_LIMIT,
        "/v1/squids/sq1": {"id": "sq1", "last_run_limited_accounts": ["acc1"]},
    }), "run1")
    assert out["done_reason_code"] == "limit_exceeded"
    assert out["account_limit"]["limited_accounts"] == ["acc1"]
    assert "not a lobstr.io credits problem" in out["account_limit"]["message"]
    assert out["next_launch_at"] == "2026-10-02T10:00:00Z"
    assert "abort_run" in out["relaunch_note"]


def test_get_run_done_run_has_no_limit_or_relaunch_fields():
    out = get_run_impl(client_for({
        "/v1/runs/run1/stats": {"id": "run1", "is_done": True},
        "/v1/runs/run1": {"id": "run1", "squid": "sq1", "status": "done", "done_reason": "tasks_done"},
    }), "run1")
    assert out["done_reason_code"] == "tasks_done"
    assert "account_limit" not in out and "relaunch_note" not in out


def test_get_account_locked_is_not_usable_even_if_status_says_success():
    out = get_account_impl(client_for({"/v1/accounts/acc1": {
        "id": "acc1", "type": "linkedin-sync", "username": "u", "status": "200",
        "status_code_description": "Success!", "lock_time": "2099-01-01 00:00:00",
        "resets_in": 3600}}), "acc1")
    assert out["status"] == "Success!"
    assert out["usable_now"] is False
    assert "locked" in out["health_note"] and out["lock_time"] == "2099-01-01 00:00:00"
    assert out["resets_in_seconds"] == 3600


def test_get_account_expired_cookies_is_not_usable():
    out = get_account_impl(client_for({"/v1/accounts/acc1": {
        "id": "acc1", "type": "linkedin-sync", "status": "200",
        "status_code_info": "cookies_expired", "status_code_description": "Success!"}}), "acc1")
    assert out["usable_now"] is False and "cookies" in out["health_note"]


def test_get_account_healthy():
    out = get_account_impl(client_for({"/v1/accounts/acc1": {
        "id": "acc1", "type": "linkedin-sync", "status": "200", "status_code_description": "Success!"}}), "acc1")
    assert out["usable_now"] is True and "health_note" not in out
