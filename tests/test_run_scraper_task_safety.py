"""Replacing a reused squid's saved tasks must be recoverable when it fails
— and, since it destroys what the account's owner saved, must
be asked for: every call here passes replace_tasks=True.

A rejected run_scraper(squid_id=..., input=...) used to empty the squid first,
so anything the API refused afterwards — a param the crawler's /params doesn't
describe (auto_verify_emails), which local validation cannot know about — left
the squid with no inputs at all and the caller with no copy of them. That was
the failure path; the success path replaced them just as silently, which is
covered in test_run_scraper_task_default.py.
"""
import json

import httpx

from lobstr_mcp.config import Settings
from lobstr_mcp.execution import run_scraper_impl
from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.safeguards import IdempotencyStore

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

OLD_TASKS = [{"query": "dentists paris"}, {"query": "dentists lyon"}]


def stateful_squid(tasks, *, reject=None, timeout_after_emptying=False):
    """A mock API around one existing squid that really keeps its task rows:
    GET /tasks lists them, /empty clears them, POST /tasks appends. `reject`
    maps (method, path) to responses answered one per call, in order, before
    the normal behaviour resumes — so "the first add fails, the restore
    succeeds" is expressible.

    Returns (client, state); state["tasks"] is what the squid holds now and
    state["calls"] is every call made, in order.
    """
    state = {"tasks": [dict(t) for t in tasks], "calls": []}
    rejections = {k: list(v) for k, v in (reject or {}).items()}

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        state["calls"].append(key)
        pending = rejections.get(key)
        if pending:
            return pending.pop(0)
        if key == ("GET", "/v1/tasks"):
            return httpx.Response(200, json={
                "total_results": len(state["tasks"]), "page": 1, "total_pages": 1,
                "data": [{"id": "t%d" % i, "is_active": True, "params": p}
                         for i, p in enumerate(state["tasks"])]})
        if key == ("POST", "/v1/squids/sq1/empty"):
            state["tasks"] = []
            if timeout_after_emptying:
                # worst kind of transport failure: it went through, but this
                # client never learns that it did
                raise httpx.ConnectTimeout("timed out")
            return httpx.Response(200, json={})
        if key == ("POST", "/v1/tasks"):
            state["tasks"].extend(json.loads(request.content)["tasks"])
            return httpx.Response(200, json={"duplicated_count": 0,
                                             "tasks": [{"id": "t"}]})
        static = {
            ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "gm", "name": "My GM"},
            ("GET", "/v1/crawlers/gm"): CRAWLER_GM,
            ("GET", "/v1/user/balance"): {"available": 1000},
            ("POST", "/v1/squids/sq1"): {},
            ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
        }
        return httpx.Response(200, json=static[key])

    client = LobstrClient("https://api.lobstr.io/v1", "t",
                          transport=httpx.MockTransport(handler))
    return client, state


def rejected(message, type="ParamsInvalid"):
    return httpx.Response(400, json={"errors": {"message": message, "type": type,
                                                "code": 400}})


def test_reuse_saves_config_before_clearing_the_old_tasks():
    # Ordering is the fix's first line of defence: the config save is what the
    # API rejects in practice, so it must run while the old rows are still
    # there to lose.
    client, state = stateful_squid(OLD_TASKS)
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(),
                           input={"query": "dentists nice"}, confirm=True,
                           squid_id="sq1", replace_tasks=True)
    assert out["run_id"] == "run1"
    calls = state["calls"]
    assert (calls.index(("POST", "/v1/squids/sq1"))
            < calls.index(("POST", "/v1/squids/sq1/empty")))
    assert state["tasks"] == [{"query": "dentists nice"}]  # replaced, not piled up


def test_reuse_keeps_tasks_when_the_config_is_rejected():
    client, state = stateful_squid(
        OLD_TASKS,
        reject={("POST", "/v1/squids/sq1"): [
            rejected("auto_verify_emails is not a valid squid param")]})
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(),
                           input={"query": "x", "auto_verify_emails": True},
                           confirm=True, squid_id="sq1", replace_tasks=True)
    assert out["error_code"] == "upstream_rejected"
    assert out["tasks_lost"] is False
    assert state["tasks"] == OLD_TASKS  # untouched
    assert ("POST", "/v1/squids/sq1/empty") not in state["calls"]
    assert ("POST", "/v1/runs") not in state["calls"]


def test_reuse_restores_tasks_when_adding_the_new_one_is_rejected():
    # The config saved fine, so the squid was emptied — and then the API
    # refused the new task row. The old rows must come back.
    client, state = stateful_squid(
        OLD_TASKS,
        reject={("POST", "/v1/tasks"): [rejected("Invalid or empty task found.",
                                                 type="InvalidTask")]})
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(),
                           input={"query": "x"}, confirm=True, squid_id="sq1",
                           replace_tasks=True)
    assert out["error_code"] == "upstream_rejected"
    assert out["tasks_lost"] is False
    assert out["tasks_restored"] == 2
    assert state["tasks"] == OLD_TASKS  # put back verbatim
    assert ("POST", "/v1/runs") not in state["calls"]


def test_reuse_reports_the_lost_tasks_verbatim_when_the_restore_also_fails():
    # Worst case: the squid is empty and cannot be refilled. Silence here is
    # worse than the original bug — the response must carry the rows.
    client, state = stateful_squid(
        OLD_TASKS,
        reject={("POST", "/v1/tasks"): [
            rejected("Invalid or empty task found.", type="InvalidTask"),
            rejected("upstream is having a bad day")]})
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(),
                           input={"query": "x"}, confirm=True, squid_id="sq1",
                           replace_tasks=True)
    assert out["tasks_lost"] is True
    assert out["lost_tasks"] == OLD_TASKS
    assert out["squid_id"] == "sq1"
    assert "add_tasks" in out["message"]
    assert state["tasks"] == []


def test_reuse_says_tasks_are_unrecoverable_when_they_could_not_be_read_first():
    # No snapshot means no honest promise of a restore; say so rather than
    # imply the squid is fine.
    client, state = stateful_squid(
        OLD_TASKS,
        reject={("GET", "/v1/tasks"): [rejected("nope", type="HTTPNotFound")],
                ("POST", "/v1/tasks"): [rejected("Invalid or empty task found.",
                                                 type="InvalidTask")]})
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(),
                           input={"query": "x"}, confirm=True, squid_id="sq1",
                           replace_tasks=True)
    assert out["tasks_lost"] is True
    assert "could not read them beforehand" in out["message"]


def test_reuse_of_an_empty_squid_reports_nothing_lost():
    client, state = stateful_squid(
        [],
        reject={("POST", "/v1/tasks"): [rejected("Invalid or empty task found.",
                                                 type="InvalidTask")]})
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(),
                           input={"query": "x"}, confirm=True, squid_id="sq1",
                           replace_tasks=True)
    assert out["tasks_lost"] is False
    assert out["tasks_restored"] == 0


def test_reuse_restores_tasks_when_emptying_never_answers():
    # A transport failure on /empty: it may or may not have gone through, so
    # the rows are re-added rather than assumed safe (the API deduplicates).
    client, state = stateful_squid(OLD_TASKS, timeout_after_emptying=True)
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(),
                           input={"query": "x"}, confirm=True, squid_id="sq1",
                           replace_tasks=True)
    assert out["error_code"] == "upstream_unavailable"
    assert out["tasks_lost"] is False
    assert out["tasks_restored"] == 2
    assert state["tasks"] == OLD_TASKS


def test_reuse_keeps_tasks_when_the_config_save_never_answers():
    # A transport failure, not a rejection: whether the config was saved is
    # unknown, but nothing destructive has run, so the inputs are safe.
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key == ("POST", "/v1/squids/sq1"):
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(200, json={
            ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "gm", "name": "My GM"},
            ("GET", "/v1/crawlers/gm"): CRAWLER_GM,
            ("GET", "/v1/user/balance"): {"available": 1000},
            # read before the estimate (the rows are what the run costs), and
            # the same read is the snapshot a restore would use
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
