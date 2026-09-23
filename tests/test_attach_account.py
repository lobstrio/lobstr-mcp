"""attach_account_impl: reads-before-writes, narrow auto-pick, actionable
errors (a squid created with an empty account list can never
launch a run needing a synced account)."""
import httpx
from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.tools.accounts import attach_account_impl

LI_CRAWLER = {"id": "li", "name": "LinkedIn Leads", "account": {"type": "linkedin-sync"}}
NO_ACCOUNT_CRAWLER = {"id": "gm", "name": "Google Maps", "account": None}


def routed_client(routes, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.method, request.url.path))
        resp = routes[(request.method, request.url.path)]
        return resp(request) if callable(resp) else httpx.Response(200, json=resp)
    return LobstrClient("https://api.lobstr.io/v1", "t",
                        transport=httpx.MockTransport(handler))


def test_auto_pick_attaches_the_single_healthy_candidate():
    seen = []
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li", "accounts": []},
        ("GET", "/v1/crawlers/li"): LI_CRAWLER,
        ("GET", "/v1/accounts"): {"data": [
            {"id": "a1", "type": "linkedin-sync", "status": "200"}]},
        ("POST", "/v1/squids/sq1"): lambda r: httpx.Response(201, json={"accounts": ["a1"]}),
    }
    out = attach_account_impl(routed_client(routes, seen), "sq1")
    assert out["account_id"] == "a1"
    assert out["auto_picked"] is True
    assert out["accounts"] == ["a1"]
    assert ("POST", "/v1/squids/sq1") in seen


def test_several_candidates_returns_them_instead_of_guessing():
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li", "accounts": []},
        ("GET", "/v1/crawlers/li"): LI_CRAWLER,
        ("GET", "/v1/accounts"): {"data": [
            {"id": "a1", "type": "linkedin-sync", "status": "200", "username": "u1"},
            {"id": "a2", "type": "linkedin-sync", "status": "200", "username": "u2"}]},
    }
    out = attach_account_impl(routed_client(routes), "sq1")
    assert out["error_code"] == "multiple_accounts_available"
    assert {c["id"] for c in out["candidates"]} == {"a1", "a2"}


def test_no_candidates_names_the_required_platform():
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li", "accounts": []},
        ("GET", "/v1/crawlers/li"): LI_CRAWLER,
        ("GET", "/v1/accounts"): {"data": []},
    }
    out = attach_account_impl(routed_client(routes), "sq1")
    assert out["error_code"] == "no_account_available"
    assert out["required_type"] == "linkedin-sync"


def test_wrong_type_account_is_rejected_with_a_hint():
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li", "accounts": []},
        ("GET", "/v1/crawlers/li"): LI_CRAWLER,
        ("GET", "/v1/accounts/wrong"): {"id": "wrong", "type": "sales-nav-sync",
                                        "status": "200"},
    }
    out = attach_account_impl(routed_client(routes), "sq1", account_id="wrong")
    assert out["error_code"] == "account_type_mismatch"
    assert out["required_type"] == "linkedin-sync"
    assert out["given_type"] == "sales-nav-sync"


def test_unknown_account_hash_is_rejected_with_a_hint():
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li", "accounts": []},
        ("GET", "/v1/crawlers/li"): LI_CRAWLER,
        ("GET", "/v1/accounts/nope"): lambda r: httpx.Response(
            404, json={"errors": {"message": "not found", "type": "HTTPNotFound",
                                  "code": 404}}),
    }
    out = attach_account_impl(routed_client(routes), "sq1", account_id="nope")
    assert out["error_code"] == "account_not_found"


def test_crawler_needing_no_account_rejects_the_call():
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "gm", "accounts": []},
        ("GET", "/v1/crawlers/gm"): NO_ACCOUNT_CRAWLER,
    }
    out = attach_account_impl(routed_client(routes), "sq1", account_id="a1")
    assert out["error_code"] == "no_account_needed"


def test_squid_with_existing_accounts_never_auto_picks():
    """Never auto-pick for a squid that already has accounts — the caller must
    be explicit, so nothing is silently replaced."""
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li",
                                    "accounts": [{"id": "a0", "status": "200"}]},
        ("GET", "/v1/crawlers/li"): LI_CRAWLER,
    }
    out = attach_account_impl(routed_client(routes), "sq1")
    assert out["error_code"] == "account_already_attached"
    assert out["current_accounts"] == ["a0"]


def test_explicit_attach_merges_never_replaces_existing_accounts():
    seen = []
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li",
                                    "accounts": [{"id": "a0", "status": "200"}]},
        ("GET", "/v1/crawlers/li"): LI_CRAWLER,
        ("GET", "/v1/accounts/a1"): {"id": "a1", "type": "linkedin-sync", "status": "200"},
    }

    def update(request):
        import json
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"accounts": ["a0", "a1"]})

    routes[("POST", "/v1/squids/sq1")] = update
    out = attach_account_impl(routed_client(routes), "sq1", account_id="a1")
    assert out["accounts"] == ["a0", "a1"]  # a0 kept, a1 added
    assert out["previously_attached"] == ["a0"]
    assert seen[0]["accounts"] == ["a0", "a1"]  # full-replace body carries the full set


def test_attaching_an_already_attached_account_is_a_noop():
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li",
                                    "accounts": [{"id": "a0", "status": "200"}]},
        ("GET", "/v1/crawlers/li"): LI_CRAWLER,
        ("GET", "/v1/accounts/a0"): {"id": "a0", "type": "linkedin-sync", "status": "200"},
        # no POST route: an update call here would KeyError this test
    }
    out = attach_account_impl(routed_client(routes), "sq1", account_id="a0")
    assert out["accounts"] == ["a0"]
    assert "already attached" in out["message"].lower()


def test_auto_pick_reports_a_locked_candidate_separately_and_does_not_auto_attach():
    """Locks on LinkedIn/Sales Navigator are routine and transient — the
    remedy is not "connect another account."""
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li", "accounts": []},
        ("GET", "/v1/crawlers/li"): LI_CRAWLER,
        ("GET", "/v1/accounts"): {"data": [
            {"id": "a1", "type": "linkedin-sync", "status": "200",
             "lock_time": "2099-01-01T00:00:00Z"}]},
        # no POST route: a locked account must never be auto-attached
    }
    out = attach_account_impl(routed_client(routes), "sq1")
    assert out["error_code"] == "account_locked"
    assert out["candidates"][0]["id"] == "a1"


def test_explicit_attach_of_a_locked_account_is_not_refused():
    """An explicit account_id is the caller's choice — attach proceeds, with
    the account's status and a warning surfaced, not a refusal."""
    seen = []
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li", "accounts": []},
        ("GET", "/v1/crawlers/li"): LI_CRAWLER,
        ("GET", "/v1/accounts/a1"): {"id": "a1", "type": "linkedin-sync", "status": "200",
                                     "lock_time": "2099-01-01T00:00:00Z"},
    }

    def update(request):
        import json
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"accounts": ["a1"]})

    routes[("POST", "/v1/squids/sq1")] = update
    out = attach_account_impl(routed_client(routes), "sq1", account_id="a1")
    assert "error_code" not in out
    assert out["account_id"] == "a1"
    assert out["account_status"] == "200"
    assert "warning" in out
    assert seen[0]["accounts"] == ["a1"]  # the write still happens


def test_explicit_attach_of_an_expired_cookies_account_warns_but_succeeds():
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li", "accounts": []},
        ("GET", "/v1/crawlers/li"): LI_CRAWLER,
        ("GET", "/v1/accounts/a1"): {"id": "a1", "type": "linkedin-sync", "status": "200",
                                     "status_code_info": "cookies_expired"},
        ("POST", "/v1/squids/sq1"): lambda r: httpx.Response(201, json={"accounts": ["a1"]}),
    }
    out = attach_account_impl(routed_client(routes), "sq1", account_id="a1")
    assert out["account_id"] == "a1"
    assert "warning" in out


def test_auto_picked_account_carries_no_warning():
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li", "accounts": []},
        ("GET", "/v1/crawlers/li"): LI_CRAWLER,
        ("GET", "/v1/accounts"): {"data": [
            {"id": "a1", "type": "linkedin-sync", "status": "200"}]},
        ("POST", "/v1/squids/sq1"): lambda r: httpx.Response(201, json={"accounts": ["a1"]}),
    }
    out = attach_account_impl(routed_client(routes), "sq1")
    assert "warning" not in out


def test_unrecognized_accounts_shape_refuses_to_write():
    """squid.accounts here is a non-empty list of ints — a shape this client
    can't read ids from. Must refuse rather than treat it as empty (which
    would then union to just the new account and drop whatever was there)."""
    routes = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "li", "accounts": [1, 2, 3]},
        ("GET", "/v1/crawlers/li"): LI_CRAWLER,
        # no /v1/accounts, no POST route: either being called is a bug
    }
    out = attach_account_impl(routed_client(routes), "sq1", account_id="a1")
    assert out["error_code"] == "accounts_unreadable"
