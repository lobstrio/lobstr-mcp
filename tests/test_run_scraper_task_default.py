"""run_scraper must not throw away a squid's saved inputs unless asked.

The destructive half of the same bug: run_scraper(squid_id=..., input=...)
replaced a squid's entire task list with the one task derived from `input`, on
every call and without a word about it. A customer who built a squid with 50
rows in the dashboard lost all 50 the first time an agent ran it with an input.

Replacement is now opt-in (replace_tasks=True, covered in
test_run_scraper_task_safety.py). By default a task-level input against a squid
that already has rows is refused, because the only two alternatives are both
surprises the caller has to pick: delete those rows, or scrape all of them
alongside the new one (a run scrapes every row a squid holds, so that is the
cost of one input multiplied by the rows somebody else saved).
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
    "input": [
        {"name": "query", "type": "string", "level": "task", "required": True},
        {"name": "max_results", "type": "number", "level": "squid"},
    ],
    "result": ["title", "address"],
}

# Same crawler with the task field optional. A required task-level field is
# rejected by local validation before any of this is reached (an input given at
# all is validated in full, even on a squid that already has that field saved),
# so it is the crawlers without one that exercise the settings-only branch.
CRAWLER_GM_OPTIONAL = {**CRAWLER_GM,
                       "input": [{"name": "query", "type": "string", "level": "task"},
                                 {"name": "max_results", "type": "number",
                                  "level": "squid"}]}

OLD_TASKS = [{"query": "dentists paris"}, {"query": "dentists lyon"}]


def stateful_squid(tasks, *, tasks_unreadable=False, crawler=CRAWLER_GM):
    """A mock API around one existing squid that really keeps its task rows.

    Returns (client, state); state["tasks"] is what the squid holds now,
    state["calls"] every call made in order, and state["saved"] the last body
    POSTed to the squid.
    """
    state = {"tasks": [dict(t) for t in tasks], "calls": [], "saved": None}

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        state["calls"].append(key)
        if key == ("GET", "/v1/tasks"):
            if tasks_unreadable:
                return httpx.Response(404, json={"errors": {"message": "nope",
                                                            "type": "HTTPNotFound",
                                                            "code": 404}})
            return httpx.Response(200, json={
                "total_results": len(state["tasks"]), "page": 1, "total_pages": 1,
                "data": [{"id": "t%d" % i, "is_active": True, "params": p}
                         for i, p in enumerate(state["tasks"])]})
        if key == ("POST", "/v1/squids/sq1"):
            state["saved"] = json.loads(request.content)
            return httpx.Response(200, json={})
        if key == ("POST", "/v1/squids/sq1/empty"):
            state["tasks"] = []
            return httpx.Response(200, json={})
        if key == ("POST", "/v1/tasks"):
            state["tasks"].extend(json.loads(request.content)["tasks"])
            return httpx.Response(200, json={"duplicated_count": 0,
                                             "tasks": [{"id": "t"}]})
        static = {
            ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "gm", "name": "My GM"},
            ("GET", "/v1/crawlers/gm"): crawler,
            ("GET", "/v1/user/balance"): {"available": 1000},
            ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
        }
        return httpx.Response(200, json=static[key])

    client = LobstrClient("https://api.lobstr.io/v1", "t",
                          transport=httpx.MockTransport(handler))
    return client, state


def run(client, **kwargs):
    return run_scraper_impl(client, SETTINGS, IdempotencyStore(), confirm=True,
                            squid_id="sq1", **kwargs)


def test_default_refuses_rather_than_destroying_saved_tasks():
    # The reported bug, on the success path: 50 rows a customer built in the
    # dashboard used to vanish here. Nothing may be written or deleted.
    client, state = stateful_squid(OLD_TASKS)
    out = run(client, input={"query": "dentists nice"})
    assert out["error_code"] == "squid_has_tasks"
    assert out["existing_task_count"] == 2
    assert out["tasks_lost"] is False
    assert out["new_task"] == {"query": "dentists nice"}
    assert state["tasks"] == OLD_TASKS
    assert ("POST", "/v1/squids/sq1/empty") not in state["calls"]
    assert ("POST", "/v1/squids/sq1") not in state["calls"]  # not even the config
    assert ("POST", "/v1/runs") not in state["calls"]  # and no credits spent


def test_the_refusal_names_every_way_forward():
    # A hint that names no remedy sends an agent round a loop; a hint that
    # names only the destructive one is the bug again. The four ways forward
    # are a structured `options` list of ready tool calls (the
    # response prose was cut, detail moved to structured fields), not prose.
    client, _ = stateful_squid(OLD_TASKS)
    out = run(client, input={"query": "x"})
    options = "\n".join(out["options"])
    assert "run_scraper(squid_id='sq1')" in options      # run what's saved
    assert "run_scraper(scraper='gm', input=...)" in options  # a separate scraper
    assert "add_tasks(squid_id='sq1'" in options         # add alongside
    assert "replace_tasks=true" in options               # deliberate replace
    assert out["cost_multiplier_if_added"] == 3  # adding yours to 2 costs 3x one input
    assert len(out["message"]) < 150  # cut to a short pointer, not a paragraph


def test_default_adds_the_task_when_the_squid_has_none():
    # Nothing to protect: no empty call, and the response says the new input
    # is the only one the run scrapes.
    client, state = stateful_squid([])
    out = run(client, input={"query": "dentists nice"})
    assert out["run_id"] == "run1"
    assert out["tasks_action"] == "added"
    assert out["task_count"] == 1
    assert "only one" in out["tasks_note"]
    assert state["tasks"] == [{"query": "dentists nice"}]
    assert ("POST", "/v1/squids/sq1/empty") not in state["calls"]


def test_default_merges_squid_level_settings_without_touching_the_tasks():
    # An input carrying only squid-level settings has no task row in it, so
    # there is nothing to replace: the settings are saved and the saved rows
    # run untouched.
    client, state = stateful_squid(OLD_TASKS, crawler=CRAWLER_GM_OPTIONAL)
    out = run(client, input={"max_results": 25})
    assert out["run_id"] == "run1"
    assert out["tasks_action"] == "unchanged"
    assert out["task_count"] == 2
    # the estimate now covers all the rows the run scrapes, so the note says
    # that rather than warning that it is quoted for one of them
    assert "the run scrapes all 2, which is what the estimate covers" in out["tasks_note"]
    assert state["tasks"] == OLD_TASKS
    assert state["saved"] == {"name": "My GM", "params": {"max_results": 25}}
    assert ("POST", "/v1/squids/sq1/empty") not in state["calls"]


def test_default_refuses_when_the_saved_tasks_cannot_be_read():
    # Unknown is not empty: without a readable list this client cannot tell
    # whether applying the input would discard somebody's rows.
    client, state = stateful_squid(OLD_TASKS, tasks_unreadable=True)
    out = run(client, input={"query": "x"})
    assert out["error_code"] == "tasks_unreadable"
    assert out["tasks_lost"] is False
    assert "replace_tasks=true" in out["message"]
    assert ("POST", "/v1/runs") not in state["calls"]


def test_settings_only_input_still_runs_when_the_tasks_cannot_be_read():
    # Saving settings deletes nothing, so an unreadable task list is reported
    # as an unknown count rather than blocking the run.
    client, state = stateful_squid(OLD_TASKS, tasks_unreadable=True,
                                   crawler=CRAWLER_GM_OPTIONAL)
    out = run(client, input={"max_results": 25})
    assert out["run_id"] == "run1"
    assert out["tasks_action"] == "unchanged"
    assert out["task_count"] is None
    assert "all of them" in out["tasks_note"]


def test_explicit_replace_still_replaces_and_says_how_many_it_destroyed():
    client, state = stateful_squid(OLD_TASKS)
    out = run(client, input={"query": "dentists nice"}, replace_tasks=True)
    assert out["run_id"] == "run1"
    assert out["tasks_action"] == "replaced"
    assert out["replaced_task_count"] == 2
    assert out["task_count"] == 1
    assert "deleted" in out["tasks_note"]
    assert state["tasks"] == [{"query": "dentists nice"}]


def test_explicit_replace_on_an_empty_squid_says_nothing_was_destroyed():
    client, state = stateful_squid([])
    out = run(client, input={"query": "x"}, replace_tasks=True)
    assert out["tasks_action"] == "replaced"
    assert out["replaced_task_count"] == 0
    assert "no saved inputs to replace" in out["tasks_note"]


def test_rerun_without_input_leaves_the_tasks_alone_and_says_so():
    client, state = stateful_squid(OLD_TASKS)
    out = run(client)
    assert out["run_id"] == "run1"
    assert out["tasks_action"] == "unchanged"
    # the rows are read (they are what the run costs), so their count is
    # reported rather than left null
    assert out["task_count"] == 2
    assert "left exactly as they are" in out["tasks_note"]
    assert state["tasks"] == OLD_TASKS
    # no input still means nothing is saved or deleted: no POST to the squid,
    # no /empty (the catalog GETs are slug resolution)
    assert [c for c in state["calls"] if c[0] == "POST"] == [("POST", "/v1/runs")]


def test_a_brand_new_scraper_is_unaffected():
    # scraper=, no squid_id: there is nothing of anyone's to protect.
    routes = {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        routes.setdefault("calls", []).append(key)
        return httpx.Response(200, json={
            ("GET", "/v1/crawlers/gm"): CRAWLER_GM,
            ("GET", "/v1/user/balance"): {"available": 1000},
            ("POST", "/v1/squids"): {"id": "sq9"},
            ("POST", "/v1/squids/sq9"): {},
            ("POST", "/v1/tasks"): {"duplicated_count": 0, "tasks": [{"id": "t1"}]},
            ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
        }[key])

    client = LobstrClient("https://api.lobstr.io/v1", "t",
                          transport=httpx.MockTransport(handler))
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), "gm",
                           {"query": "x"}, confirm=True)
    assert out["run_id"] == "run1"
    assert out["tasks_action"] == "created"
    assert out["task_count"] == 1
    assert ("GET", "/v1/tasks") not in routes["calls"]  # nothing to snapshot
