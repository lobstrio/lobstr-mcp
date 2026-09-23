"""account_linking.py: the shared attach/auto-pick rules."""
import httpx
from lobstr_mcp.account_linking import (
    AccountsShapeError,
    account_health_note,
    crawler_account_type,
    find_candidates,
    is_healthy,
    pick_or_explain,
    resolve_account,
    squid_account_ids,
)
from lobstr_mcp.lobstr_client import LobstrClient

import pytest


def routed_client(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        resp = routes[(request.method, request.url.path)]
        return resp(request) if callable(resp) else httpx.Response(200, json=resp)
    return LobstrClient("https://api.lobstr.io/v1", "t",
                        transport=httpx.MockTransport(handler))


def test_crawler_account_type_reads_the_slug():
    assert crawler_account_type({"account": {"type": "linkedin-sync"}}) == "linkedin-sync"


def test_crawler_account_type_none_when_no_account_needed():
    assert crawler_account_type({"account": None}) is None
    assert crawler_account_type({}) is None


def test_squid_account_ids_reads_the_dict_shape():
    """GET /squids/{hash} returns accounts as a list of dicts."""
    squid = {"accounts": [{"id": "a1", "status": "200"}, {"id": "a2"}]}
    assert squid_account_ids(squid) == ["a1", "a2"]


def test_squid_account_ids_reads_the_string_shape():
    """The POST /squids/{hash} update echoes accounts back as plain hash
    strings — the same field, a different shape, on a real response."""
    squid = {"accounts": ["a1", "a2"]}
    assert squid_account_ids(squid) == ["a1", "a2"]


def test_squid_account_ids_empty_when_none_attached():
    assert squid_account_ids({"accounts": []}) == []
    assert squid_account_ids({}) == []
    assert squid_account_ids({"accounts": None}) == []


def test_squid_account_ids_refuses_an_unrecognized_shape_instead_of_going_empty():
    """A non-empty `accounts` list this client can't read ids from must not
    silently become [] — that [] is what the next union write would use,
    narrowing (or wiping) whatever was actually attached."""
    with pytest.raises(AccountsShapeError):
        squid_account_ids({"accounts": [123, 456]})
    with pytest.raises(AccountsShapeError):
        squid_account_ids({"accounts": [{"status": "200"}]})  # dict with no id
    with pytest.raises(AccountsShapeError):
        squid_account_ids({"accounts": ["a1", {"id": "a2"}]})  # mixed shapes
    with pytest.raises(AccountsShapeError):
        squid_account_ids({"accounts": "a1"})  # not a list at all


def test_account_health_note_flags_a_bad_status():
    assert account_health_note({"status": "200"}) is None
    assert "status" in account_health_note({"status": "429"})


def test_account_health_note_flags_cookies_expired():
    note = account_health_note({"status": "200", "status_code_info": "cookies_expired"})
    assert "re-sync" in note or "cookies" in note


def test_account_health_note_flags_a_live_lock_with_a_transient_framing():
    note = account_health_note({"status": "200", "lock_time": "2099-01-01T00:00:00Z"})
    assert note is not None
    assert "routine" in note or "transient" in note or "locked" in note


def test_account_health_note_naive_api_timestamp_format():
    """The real API emits lock_time as a naive "%Y-%m-%d %H:%M:%S", not ISO
    with a zone."""
    assert account_health_note({"status": "200", "lock_time": "2099-01-01 00:00:00"}) is not None
    assert account_health_note({"status": "200", "lock_time": "2020-01-01 00:00:00"}) is None


def test_is_healthy_requires_status_200():
    assert is_healthy({"status": "200"}) is True
    assert is_healthy({"status": "429"}) is False
    assert is_healthy({"status": "200", "status_code_info": "cookies_expired"}) is False


def test_is_healthy_excludes_a_live_lock_but_not_a_past_one():
    assert is_healthy({"status": "200", "lock_time": "2099-01-01T00:00:00Z"}) is False
    assert is_healthy({"status": "200", "lock_time": "2020-01-01T00:00:00Z"}) is True
    assert is_healthy({"status": "200", "lock_time": None}) is True


def test_find_candidates_splits_unlocked_from_locked():
    accounts = [
        {"id": "a1", "type": "linkedin-sync", "status": "200"},
        {"id": "a2", "type": "sales-nav-sync", "status": "200"},  # wrong type
        {"id": "a3", "type": "linkedin-sync", "status": "429"},  # unhealthy, excluded
        {"id": "a4", "type": "linkedin-sync", "status": "200",
         "lock_time": "2099-01-01T00:00:00Z"},  # locked
    ]
    client = routed_client({("GET", "/v1/accounts"): {"data": accounts}})
    unlocked, locked = find_candidates(client, "linkedin-sync")
    assert [a["id"] for a in unlocked] == ["a1"]
    assert [a["id"] for a in locked] == ["a4"]


def test_resolve_account_ok_on_exact_type_match():
    client = routed_client({
        ("GET", "/v1/accounts/a1"): {"id": "a1", "type": "linkedin-sync", "status": "200"},
    })
    account, error = resolve_account(client, "a1", "linkedin-sync")
    assert error is None and account["id"] == "a1"


def test_resolve_account_does_not_refuse_an_unhealthy_account():
    """Health is the caller's call to make on an explicit account_id — only
    the type/ownership checks may refuse."""
    client = routed_client({
        ("GET", "/v1/accounts/a1"): {"id": "a1", "type": "linkedin-sync",
                                     "status": "429"},
    })
    account, error = resolve_account(client, "a1", "linkedin-sync")
    assert error is None
    assert account["id"] == "a1"
    assert account_health_note(account) is not None  # caller decides to warn


def test_resolve_account_flags_a_type_mismatch_as_actionable():
    client = routed_client({
        ("GET", "/v1/accounts/a1"): {"id": "a1", "type": "sales-nav-sync", "status": "200"},
    })
    account, error = resolve_account(client, "a1", "linkedin-sync")
    assert account is None
    assert error["error_code"] == "account_type_mismatch"
    assert error["required_type"] == "linkedin-sync"
    assert error["given_type"] == "sales-nav-sync"
    assert "list_accounts" in error["message"]  # says what to do next


def test_resolve_account_flags_an_unknown_hash():
    client = routed_client({
        ("GET", "/v1/accounts/nope"): lambda r: httpx.Response(
            404, json={"errors": {"message": "not found", "type": "HTTPNotFound",
                                  "code": 404}}),
    })
    account, error = resolve_account(client, "nope", "linkedin-sync")
    assert account is None
    assert error["error_code"] == "account_not_found"
    assert "nope" in error["message"]


def test_pick_or_explain_auto_picks_the_single_unlocked_candidate():
    client = routed_client({("GET", "/v1/accounts"):
                            {"data": [{"id": "a1", "type": "linkedin-sync", "status": "200"}]}})
    account, error = pick_or_explain(client, "linkedin-sync")
    assert error is None and account["id"] == "a1"


def test_pick_or_explain_refuses_to_guess_between_several():
    client = routed_client({("GET", "/v1/accounts"): {"data": [
        {"id": "a1", "type": "linkedin-sync", "status": "200", "username": "u1"},
        {"id": "a2", "type": "linkedin-sync", "status": "200", "username": "u2"},
    ]}})
    account, error = pick_or_explain(client, "linkedin-sync")
    assert account is None
    assert error["error_code"] == "multiple_accounts_available"
    assert {c["id"] for c in error["candidates"]} == {"a1", "a2"}


def test_pick_or_explain_names_the_required_type_when_none_available():
    client = routed_client({("GET", "/v1/accounts"): {"data": []}})
    account, error = pick_or_explain(client, "linkedin-sync")
    assert account is None
    assert error["error_code"] == "no_account_available"
    assert error["required_type"] == "linkedin-sync"


def test_pick_or_explain_reports_a_locked_candidate_separately_from_none():
    """A lock on LinkedIn/Sales Navigator is routine and transient — this must
    not come back as "no account available" telling the user to connect a new
    one; it should name the locked account and say it can be attached
    explicitly."""
    client = routed_client({("GET", "/v1/accounts"): {"data": [
        {"id": "a1", "type": "linkedin-sync", "status": "200",
         "lock_time": "2099-01-01T00:00:00Z"},
    ]}})
    account, error = pick_or_explain(client, "linkedin-sync")
    assert account is None
    assert error["error_code"] == "account_locked"
    assert error["error_code"] != "no_account_available"
    assert error["candidates"][0]["id"] == "a1"
    assert "account_id" in error["message"] or "explicitly" in error["message"]
