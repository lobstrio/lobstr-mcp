import json
import httpx
from lobstr_mcp.lobstr_client import LobstrClient


def capturing_client(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["query"] = dict(request.url.params)
        captured["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, json={"ok": True, "id": "x1"})
    return LobstrClient("https://api.lobstr.io/v1", "t",
                        transport=httpx.MockTransport(handler))


def test_create_squid():
    cap = {}
    capturing_client(cap).create_squid("gm", name="My GM")
    assert cap["method"] == "POST" and cap["path"] == "/v1/squids"
    assert cap["body"] == {"crawler": "gm", "name": "My GM"}


def test_add_tasks():
    cap = {}
    capturing_client(cap).add_tasks("sq1", [{"query": "dentists"}])
    assert cap["method"] == "POST" and cap["path"] == "/v1/tasks"
    assert cap["body"] == {"squid": "sq1", "tasks": [{"query": "dentists"}]}


def test_start_run():
    cap = {}
    capturing_client(cap).start_run("sq1")
    assert cap["path"] == "/v1/runs" and cap["body"] == {"squid": "sq1"}


def test_update_squid():
    cap = {}
    capturing_client(cap).update_squid("sq1", {"concurrency": 2})
    assert cap["path"] == "/v1/squids/sq1" and cap["body"] == {"concurrency": 2}


def test_get_run_stats():
    cap = {}
    capturing_client(cap).get_run_stats("run1")
    assert cap["method"] == "GET" and cap["path"] == "/v1/runs/run1/stats"


def test_get_results_query():
    cap = {}
    capturing_client(cap).get_results(run="run1", page=2, page_size=50)
    assert cap["path"] == "/v1/results"
    assert cap["query"] == {"page": "2", "run": "run1", "page_size": "50"}


def test_get_balance():
    cap = {}
    capturing_client(cap).get_balance()
    assert cap["method"] == "GET" and cap["path"] == "/v1/user/balance"
