"""A key the scraper does not take is refused before anything is created.

Florian, 21 and 22/11/2026: an agent passed extended_profile_data (a LinkedIn
Leads Scraper setting) to a Sales Navigator run. The key went out as a task
field: the squid was created, POST /v1/tasks refused it, the squid was deleted,
and the agent retried without the key, so without profile details, which this
scraper has as get_profile_details.
"""
import json

import httpx

from lobstr_mcp.config import Settings
from lobstr_mcp.execution import run_scraper_impl
from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.safeguards import IdempotencyStore, unknown_inputs

SETTINGS = Settings(
    lobstr_api_base="https://api.lobstr.io/v1", dev_token=None, request_timeout=30.0,
    run_confirm_threshold=100, public_base_url="https://mcp.lobstr.io",
    service_credential=None, consent_url="https://app.lobstr.io/connect-ai",
)

CRAWLER = {"id": "sn", "name": "Sales Navigator Leads Scraper", "input": [
    {"name": "url", "type": "string", "level": "task", "required": True},
    {"name": "max_results", "type": "int", "level": "squid"},
    {"name": "get_profile_details", "type": "bool", "level": "function"},
]}
PARAMS = {
    "task": {"url": {"type": "string", "required": True}},
    "squid": {"max_results": {"type": "int"},
              "functions": {"get_profile_details": {"type": "boolean"}}},
}
URL = "https://www.linkedin.com/sales/search/people?query=(keywords%3Aroofer)"


def client(params=PARAMS):
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        sent.append(key)
        if key == ("GET", "/v1/crawlers/sn/params"):
            if params is None:
                return httpx.Response(500, json={"errors": {"message": "down", "type": "ServerError"}})
            return httpx.Response(200, json=params)
        routes = {
            ("GET", "/v1/crawlers/sn"): CRAWLER,
            ("GET", "/v1/user/balance"): {"available": 1000, "consumed": 0},
            ("POST", "/v1/squids"): {"id": "sq1"},
            ("POST", "/v1/squids/sq1"): {},
            ("POST", "/v1/tasks"): {"duplicated_count": 0, "tasks": [{"id": "t1"}]},
            ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
        }
        return httpx.Response(200, json=routes[key])

    return LobstrClient("https://api.lobstr.io/v1", "t", transport=httpx.MockTransport(handler)), sent


def test_names_the_nearest_input_and_lists_them_all():
    schema = {"properties": {"url": {}, "max_results": {}, "get_profile_details": {}}}
    assert unknown_inputs({"url": URL, "extended_profile_data": True}, schema,
                          "Sales Navigator Leads Scraper") == [
        "extended_profile_data is not an input of Sales Navigator Leads Scraper: did you mean get_profile_details?",
        "Its inputs: get_profile_details, max_results, url.",
    ]


def test_no_guess_when_nothing_is_near():
    schema = {"properties": {"url": {}}}
    assert unknown_inputs({"keyword": "x"}, schema, "Sales Navigator Leads Scraper")[0] == (
        "keyword is not an input of Sales Navigator Leads Scraper.")


def test_known_inputs_pass():
    assert unknown_inputs({"url": URL}, {"properties": {"url": {}}}, "x") == []


def test_run_scraper_refuses_before_creating_the_squid():
    c, sent = client()
    out = run_scraper_impl(c, SETTINGS, IdempotencyStore(), "sn",
                           {"url": URL, "extended_profile_data": True}, confirm=True)
    assert out["error_code"] == "validation_error"
    assert "did you mean get_profile_details?" in out["errors"][0]
    assert [k for k in sent if k[0] == "POST"] == [], "nothing created, nothing deleted"


def test_the_inputs_it_takes_still_run():
    c, sent = client()
    out = run_scraper_impl(c, SETTINGS, IdempotencyStore(), "sn",
                           {"url": URL, "get_profile_details": True}, confirm=True)
    assert out.get("run_id") == "run1", json.dumps(out)


def test_without_params_the_api_stays_the_judge():
    # /params down: the schema misses the inputs only /params knows, so an
    # unknown key goes out as before and the API names it.
    c, sent = client(params=None)
    out = run_scraper_impl(c, SETTINGS, IdempotencyStore(), "sn",
                           {"url": URL, "extended_profile_data": True}, confirm=True)
    assert out.get("error_code") != "validation_error"
    assert ("POST", "/v1/tasks") in sent
