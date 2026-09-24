"""Coverage for the composable primitives: create_squid, add_tasks, estimate_run."""
import httpx
import pytest

from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.tools.primitives import add_tasks_impl, create_squid_impl, estimate_run_impl


def client_for(routes, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((request.method, request.url.path))
        return httpx.Response(200, json=routes.get(request.url.path, {}))
    return LobstrClient("https://api.lobstr.io/v1", "t",
                        transport=httpx.MockTransport(handler))


# A 32-hex crawler id short-circuits resolve_crawler_id (no catalog lookup).
CID = "a" * 32
SQUID = {"id": "sq1", "name": "N", "crawler": CID, "is_active": True, "params": {}}


def test_create_squid_bare_makes_no_update_call():
    seen = []
    c = client_for({"/v1/squids": SQUID}, seen)
    out = create_squid_impl(c, CID)
    assert out["squid_id"] == "sq1"
    assert ("POST", "/v1/squids") in seen
    # no config -> the squid is created but not re-saved
    assert ("POST", "/v1/squids/sq1") not in seen


def test_create_squid_with_config_saves_params():
    seen = []
    c = client_for({"/v1/squids": SQUID, "/v1/squids/sq1": {"id": "sq1"}}, seen)
    out = create_squid_impl(c, CID, name="My", config={"max_results": 50})
    assert out["squid_id"] == "sq1"
    assert ("POST", "/v1/squids/sq1") in seen  # squid-level params persisted


def test_create_squid_config_rejected_deletes_the_orphan_and_says_so():
    # POST /v1/squids/sq1 (the config-apply call) rejected — the create call
    # above it already succeeded, so a squid exists with no usable config.
    # Rather than leaving it behind (the old behaviour), this cleans it up:
    # DELETE /v1/squids/sq1 (unhandled by the special-case below, so it falls
    # through to the handler's default 200) succeeds.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/squids/sq1":
            return httpx.Response(400, json={"errors": {
                "message": "functions.enrich_emails is not a valid squid param",
                "type": "InvalidParam", "code": 400}})
        return httpx.Response(200, json={"/v1/squids": SQUID}.get(request.url.path, SQUID))

    c = LobstrClient("https://api.lobstr.io/v1", "t",
                     transport=httpx.MockTransport(handler))
    out = create_squid_impl(c, CID, name="My", config={"enrich_emails": True})
    msg = out["message"]
    # squid_id is still reported (useful context / for a support ticket) even
    # though the squid itself no longer exists.
    assert out["squid_id"] == "sq1"
    assert out["scraper"] == CID
    assert out["error_code"]
    assert out["upstream_status"] == 400  # a real rejection, unlike the transport case below
    assert out["deleted"] is True
    assert "sq1" in msg
    assert "was deleted" in msg
    assert "no concurrency slot spent" in msg
    # deleted, so nothing left to reuse — must not point at run_scraper(squid_id=...)
    # or deactivate_scraper(squid_id=...) for an id that no longer resolves.
    assert "run_scraper" not in msg
    assert "deactivate_scraper" not in msg
    assert "call create_squid again" in msg


def test_create_squid_config_rejected_and_cleanup_delete_also_fails_keeps_orphan():
    # The rarer case: the config-apply call is rejected AND the cleanup
    # delete itself fails — the squid really is left behind this time, so the
    # response must say so plainly and give both ways to deal with it (the
    # pre-fix wording), unlike the successful-cleanup path above.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/squids/sq1":
            return httpx.Response(400, json={"errors": {
                "message": "functions.enrich_emails is not a valid squid param",
                "type": "InvalidParam", "code": 400}})
        if request.method == "DELETE" and request.url.path == "/v1/squids/sq1":
            return httpx.Response(500, json={"errors": {"message": "server error",
                                                         "type": "ServerError", "code": 500}})
        return httpx.Response(200, json={"/v1/squids": SQUID}.get(request.url.path, SQUID))

    c = LobstrClient("https://api.lobstr.io/v1", "t",
                     transport=httpx.MockTransport(handler))
    out = create_squid_impl(c, CID, name="My", config={"enrich_emails": True})
    msg = out["message"]
    assert out["squid_id"] == "sq1"
    assert out["scraper"] == CID
    assert out["error_code"]
    assert out["upstream_status"] == 400
    assert out["deleted"] is False
    assert "sq1" in msg
    assert "run_scraper" in msg  # points at the actual fix path
    # It really was rejected — nothing saved — so this path (unlike the
    # transport-failure one) is allowed to say so plainly.
    assert "its configuration was rejected, so it exists with no saved config" in msg
    # `input` there is the crawler's FULL input, task-level fields included,
    # and FLAT — run_scraper does the level-splitting/nesting itself; a caller
    # reading the config clause above (function-level params nested under
    # "functions") must not carry that nesting rule into `input` too, or the
    # value is silently classified as task-level and shipped in the task row
    # instead of being applied, and the run still bills (review follow-up:
    # the old wording didn't say `input` was flat, so the two clauses could
    # be read together the wrong way).
    assert "its task-level fields together with the corrected squid- and function-level ones, " \
        "all FLAT" in msg
    assert "run_scraper's `input` takes no nesting" in msg
    # must not tell the caller to pass confirm=true — create_squid documents
    # itself as not running anything, so a remediation hint here must not
    # silently bill a run the caller never asked for; the risk is named
    # instead (this call WILL run unless cost is unknown/over threshold —
    # not merely "can"), left for the caller to decide.
    assert "input=<corrected config>, confirm=true)" not in msg
    assert "confirm=true)" not in msg
    assert ("This starts a run and spends credits immediately, unless the cost is unknown or "
            "above your confirmation threshold") in msg
    assert "it is not run-free like the option above" in msg
    # the run-free option (rebuild under a new name) must read first, the
    # run-triggering one (reuse via run_scraper) second — and deactivating
    # the half-built squid is offered as part of the first option, since
    # taking it otherwise leaves that squid holding a concurrency slot.
    i_rebuild = msg.index("call create_squid again under a different name")
    i_deactivate = msg.index("call deactivate_scraper(squid_id='sq1')")
    i_reuse = msg.index("call run_scraper(squid_id='sq1'")
    assert i_rebuild < i_deactivate < i_reuse


def test_create_squid_config_apply_transport_failure_still_returns_squid_id():
    # No LobstrAPIError involved at all: the second call fails before a
    # response comes back (timeout, connection reset, ...). Nothing in
    # lobstr_client.py wraps that into LobstrAPIError (only _as_lobstr_error's
    # `except APIError` does, and that's status-code errors only), so a bare
    # `except LobstrAPIError` around the update_squid call lets this propagate
    # and lose the squid id exactly like the original bug.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/squids/sq1":
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, json={"/v1/squids": SQUID}.get(request.url.path, SQUID))

    c = LobstrClient("https://api.lobstr.io/v1", "t",
                     transport=httpx.MockTransport(handler))
    out = create_squid_impl(c, CID, name="My", config={"max_results": 50})
    assert out["squid_id"] == "sq1"
    assert out["scraper"] == CID
    assert out["error_code"] == "upstream_unavailable"
    assert "sq1" in out["message"]
    # There is no HTTP response here at all (the request never came back),
    # unlike the rejected-config case above — nothing to hang an upstream
    # status on, and asserting its absence catches a future change that
    # started fabricating one.
    assert "upstream_status" not in out
    # The request may have reached the server before failing, so the config
    # may actually be saved — this path must NOT claim "rejected" (that
    # overclaim would send a caller off to build a duplicate squid under a
    # new name and burn a slot for nothing when the original was fine). It's
    # fine for the message to say "not rejected" as a contrast; what it must
    # never say is the rejected path's own opening claim.
    assert "its configuration was rejected, so it exists with no saved config" not in out["message"]
    assert "whether the configuration was saved is unknown" in out["message"]
    assert "get_my_scraper(squid_id='sq1')" in out["message"]  # check before acting


def test_create_squid_config_apply_bug_is_not_relabeled_as_upstream_outage():
    # A programming error inside the config-apply call (anything that isn't
    # LobstrAPIError or an httpx transport failure) must propagate as itself,
    # not get caught by a broad `except Exception` and reported to the model
    # as a retryable "upstream_unavailable" — that would hide the traceback
    # and read our own bug as a temporary API problem.
    class BrokenClient(LobstrClient):
        def create_squid(self, crawler, name=None):
            return {"id": "sq1", "name": name}

        def update_squid(self, squid_hash, settings):
            raise ValueError("boom")

    c = BrokenClient("https://api.lobstr.io/v1", "t")
    with pytest.raises(ValueError, match="boom"):
        create_squid_impl(c, CID, name="My", config={"max_results": 50})


def test_add_tasks_reports_added_and_duplicated():
    c = client_for({"/v1/tasks": {"tasks": [{"id": "t1"}, {"id": "t2"}], "duplicated_count": 1}})
    out = add_tasks_impl(c, "sq1", [{"url": "u1"}, {"url": "u2"}])
    assert out["squid_id"] == "sq1"
    assert out["added"] == 2
    assert out["duplicated"] == 1


def test_add_tasks_rejects_empty():
    out = add_tasks_impl(client_for({}), "sq1", [])
    assert out["error_code"] == "invalid_request"


def test_estimate_run_notes_verification_cost_when_on_with_known_rate():
    seen = []
    c = client_for({
        "/v1/squid/estimate": {"total_credits": 26},
        "/v1/squids/sq1": {"id": "sq1", "crawler": CID, "auto_verify_emails": True},
        f"/v1/crawlers/{CID}": {"id": CID, "credits_per_email": 1},
    }, seen)
    out = estimate_run_impl(c, "sq1")
    assert out["total_credits"] == 26
    assert "verification_note" in out
    assert "does NOT include email verification" in out["verification_note"]
    assert "credits_per_email=1" in out["verification_note"]


def test_estimate_run_notes_verification_cost_when_rate_unknown():
    c = client_for({
        "/v1/squid/estimate": {"total_credits": 26},
        "/v1/squids/sq1": {"id": "sq1", "crawler": CID, "auto_verify_emails": True},
        f"/v1/crawlers/{CID}": {"id": CID},  # no credits_per_email published
    })
    out = estimate_run_impl(c, "sq1")
    assert "verification_note" in out
    assert "no credits_per_email to size it" in out["verification_note"]


def test_estimate_run_has_no_verification_note_when_verification_is_off():
    c = client_for({
        "/v1/squid/estimate": {"total_credits": 26},
        "/v1/squids/sq1": {"id": "sq1", "crawler": CID, "auto_verify_emails": False},
    })
    out = estimate_run_impl(c, "sq1")
    assert "verification_note" not in out


def test_estimate_run_surfaces_api_estimate():
    seen = []
    c = client_for({"/v1/squid/estimate": {
        "services": [{"name": "Maps", "results": 300, "credits": 300}],
        "total_credits": 300, "estimated_time": "5 mins - 12 mins",
        "max_results": 800, "tasks": {"count": 8, "preview": []},
        "recommended_upgrade_plan": None}}, seen)
    out = estimate_run_impl(c, "sq1")
    assert ("POST", "/v1/squid/estimate") in seen
    assert out["squid_id"] == "sq1"
    assert out["total_credits"] == 300
    assert out["max_results"] == 800
    assert out["tasks"]["count"] == 8
    assert out["services"][0]["credits"] == 300
