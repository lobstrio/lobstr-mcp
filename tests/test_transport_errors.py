"""A transport failure must reach the model as the error contract, not as a
raw exception.

Production, release 0.3.1: run_scraper POSTed /squids/<hash>, the upstream hung
up without sending response headers, httpx raised RemoteProtocolError, and the
calling model got "Error calling tool 'run_scraper': Server disconnected
without sending a response." — no error_code, no upstream_status, no hint.
Nothing wrapped it: lobstr_client._as_lobstr_error only translates the SDK's
APIError and errors.structured only caught LobstrAPIError, so every tool leaked
these. GET /me hanging on the API's serial Stripe calls is the same hole seen
from a read tool.

The envelope has to separate two cases a model must act on differently: a
request that never reached the API (nothing applied, retry freely) from one
whose answer was lost (may have been applied, a blind retry can duplicate it).
"""
import httpx
import pytest

from lobstr_mcp.errors import LobstrAPIError, structured, transport_error_dict
from lobstr_mcp.execution import run_scraper_impl
from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.safeguards import IdempotencyStore
from lobstr_mcp.tools.primitives import add_tasks_impl
from lobstr_mcp.tools.user import check_credits_impl, whoami_impl
from tests.test_run_scraper_task_safety import (
    CRAWLER_GM,
    OLD_TASKS,
    SETTINGS,
    stateful_squid,
)


def client_raising(exc, *, only=None):
    """A client whose transport raises `exc` — on every request, or only on the
    (method, path) in `only`, the rest answered from the run_scraper fixtures."""
    static = {
        ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "gm", "name": "My GM"},
        ("GET", "/v1/crawlers/gm"): CRAWLER_GM,
        ("GET", "/v1/user/balance"): {"available": 1000, "consumed": 10},
        ("GET", "/v1/tasks"): {"total_results": 0, "page": 1, "total_pages": 1, "data": []},
        ("POST", "/v1/squids/sq1"): {},
        ("POST", "/v1/tasks"): {"duplicated_count": 0, "tasks": [{"id": "t"}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if only is None or key == only:
            raise exc
        return httpx.Response(200, json=static[key])

    return LobstrClient("https://api.lobstr.io/v1", "t",
                        transport=httpx.MockTransport(handler))


# --- the mapping itself -----------------------------------------------------


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("[Errno 111] Connection refused"),
    httpx.ConnectTimeout("timed out"),
    httpx.PoolTimeout("no connection available"),
])
def test_a_request_that_never_left_is_reported_as_safe_to_retry(exc):
    out = transport_error_dict(exc, verify_with="get_run(run_id=...)")
    assert out["error_code"] == "upstream_unavailable"
    assert out["request_state"] == "not_sent"
    assert out["retry_safe"] is True
    assert out["upstream_status"] is None
    assert "nothing upstream was started, changed or charged" in out["message"]
    # a remedy a model can follow, with a stopping condition so it can't loop
    assert "report that instead of retrying again" in out["hint"]


@pytest.mark.parametrize("exc", [
    httpx.RemoteProtocolError("Server disconnected without sending a response."),
    httpx.ReadTimeout("timed out"),
    httpx.WriteTimeout("timed out"),
])
def test_a_lost_answer_on_a_write_is_reported_as_unknown_not_rejected(exc):
    out = transport_error_dict(exc, verify_with="list_runs(squid_id=...)")
    assert out["error_code"] == "upstream_unavailable"
    assert out["request_state"] == "unknown"
    # the dangerous case: never tell a model to retry a write that may have
    # landed, and name the read tool that settles it
    assert out["retry_safe"] is False
    assert "unknown, not rejected" in out["message"]
    assert "apply the same write twice" in out["message"]
    assert "list_runs(squid_id=...)" in out["message"]
    assert "list_runs(squid_id=...)" in out["hint"]


def test_a_lost_answer_on_a_read_tool_stays_retry_safe():
    # A repeated read duplicates nothing, so warning about it would send the
    # model looking for state it cannot have changed.
    out = transport_error_dict(httpx.ReadTimeout("timed out"))
    assert out["request_state"] == "unknown"
    assert out["retry_safe"] is True
    assert "only reads" in out["message"]
    assert "unknown, not rejected" in out["message"]


def test_the_exception_type_is_carried_for_the_caller():
    out = transport_error_dict(
        httpx.RemoteProtocolError("Server disconnected without sending a response."))
    assert out["transport_error"] == (
        "RemoteProtocolError: Server disconnected without sending a response.")


def test_an_unknown_transport_subclass_defaults_to_unknown_not_safe():
    # Anything httpx adds later is assumed to have reached the server: the
    # wrong guess that way costs a read, the other way costs a duplicate run.
    class NewTransportError(httpx.TransportError):
        pass

    out = transport_error_dict(NewTransportError("who knows"),
                               verify_with="get_run(run_id=...)")
    assert out["request_state"] == "unknown"
    assert out["retry_safe"] is False


# --- the wrapper ------------------------------------------------------------


def test_the_wrapper_returns_the_envelope_instead_of_raising():
    @structured(verify_with="get_run(run_id=...)")
    def impl():
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    out = impl()
    assert out["error_code"] == "upstream_unavailable"
    assert out["request_state"] == "unknown"


def test_the_bare_wrapper_still_maps_api_errors():
    @structured
    def impl():
        raise LobstrAPIError(429)

    assert impl()["error_code"] == "rate_limited"


def test_the_wrapper_does_not_swallow_ordinary_bugs():
    # A KeyError here is a bug in this server; it must keep its traceback
    # rather than read to a model as a retryable API outage.
    @structured
    def impl():
        raise KeyError("boom")

    with pytest.raises(KeyError):
        impl()


# --- real tools -------------------------------------------------------------


def test_whoami_survives_the_stripe_read_timeout():
    # GET /me can hang on the API's serial Stripe calls past the client's
    # 30s timeout; that used to escape whoami as a raw httpx.ReadTimeout.
    out = whoami_impl(client_raising(httpx.ReadTimeout("timed out")))
    assert out["error_code"] == "upstream_unavailable"
    assert out["retry_safe"] is True
    assert out["upstream_status"] is None


def test_check_credits_survives_a_refused_connection():
    out = check_credits_impl(client_raising(httpx.ConnectError("Connection refused")))
    assert out["error_code"] == "upstream_unavailable"
    assert out["request_state"] == "not_sent"


def test_add_tasks_points_at_the_tool_that_counts_the_rows():
    out = add_tasks_impl(client_raising(httpx.ReadTimeout("timed out")),
                         "sq1", [{"query": "x"}])
    assert out["request_state"] == "unknown"
    assert out["retry_safe"] is False
    assert "estimate_run(squid_id=...)" in out["message"]


def test_run_scraper_survives_the_disconnect_that_produced_the_sentry_issue():
    # The Sentry trace: POST /squids/<hash>, server hangs up mid-flight. The
    # squid-save step has handled that itself since the task-safety fix, and
    # its answer is the more precise one (it knows nothing was deleted), so it
    # must still be what comes back — what changed is that it can no longer
    # escape as a raw exception from any of the steps that have no handler.
    client = client_raising(
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        only=("POST", "/v1/squids/sq1"))
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(),
                           input={"query": "dentists nice"}, confirm=True,
                           squid_id="sq1")
    assert out["error_code"] == "upstream_unavailable"
    assert "Server disconnected without sending a response." in out["message"]
    assert "unknown, not rejected" in out["message"]
    assert out["tasks_lost"] is False


def test_run_scraper_reports_a_lost_squid_read_through_the_wrapper():
    # A step with no handler of its own: reading the squid back. Before the
    # wrapper caught it this raised out of the tool.
    client = client_raising(
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        only=("GET", "/v1/squids/sq1"))
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(),
                           input={"query": "dentists nice"}, confirm=True,
                           squid_id="sq1")
    assert out["error_code"] == "upstream_unavailable"
    assert "Server disconnected without sending a response." in out["message"]
    assert "list_runs(squid_id=...)" in out["message"]


def test_run_scraper_reports_a_lost_start_run_as_may_have_started():
    # The worst one for a model to get wrong: the run may be running and
    # billing, so this must not read as "nothing happened, try again".
    client = client_raising(httpx.ReadTimeout("timed out"),
                            only=("POST", "/v1/runs"))
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(),
                           input={"query": "dentists nice"}, confirm=True,
                           squid_id="sq1")
    assert out["request_state"] == "unknown"
    assert out["retry_safe"] is False
    assert "list_runs(squid_id=...)" in out["message"]


# --- the careful path must keep its own handling ----------------------------
#
# _replace_squid_input catches httpx.TransportError at each of its three steps
# and reports what was deleted and what was put back. The wrapper above is
# outside it, so it must never see those failures first.


def test_replace_tasks_keeps_its_own_envelope_when_emptying_never_answers():
    client, state = stateful_squid(OLD_TASKS, timeout_after_emptying=True)
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(),
                           input={"query": "x"}, confirm=True, squid_id="sq1",
                           replace_tasks=True)
    assert out["error_code"] == "upstream_unavailable"
    assert out["tasks_lost"] is False
    assert out["tasks_restored"] == 2
    assert state["tasks"] == OLD_TASKS
    # the generic envelope's fields are absent: the inner handler answered,
    # and it says more than the wrapper could
    assert "request_state" not in out
    assert "retry_safe" not in out


def test_replace_tasks_keeps_its_own_envelope_when_the_config_save_never_answers():
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key == ("POST", "/v1/squids/sq1"):
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(200, json={
            ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "gm", "name": "My GM"},
            ("GET", "/v1/crawlers/gm"): CRAWLER_GM,
            ("GET", "/v1/user/balance"): {"available": 1000},
            # the saved rows are read before the estimate now
            ("GET", "/v1/tasks"): {"total_pages": 1, "page": 1,
                                   "data": [{"id": "t1", "params": {"query": "old"}}]},
        }[key])

    client = LobstrClient("https://api.lobstr.io/v1", "t",
                          transport=httpx.MockTransport(handler))
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(),
                           input={"query": "x"}, confirm=True, squid_id="sq1",
                           replace_tasks=True)
    assert out["error_code"] == "upstream_unavailable"
    assert out["tasks_lost"] is False
    assert "No inputs were deleted" in out["message"]
    assert "request_state" not in out
