"""run_scraper's responses carried multi-sentence prose that
restated fields already in the response (tasks_note, the old credit refusal's
message, the squid_has_tasks four-option paragraph). Notes are now at most one
short sentence, and the squid_has_tasks options moved to a structured
`options` list plus `cost_multiplier_if_added` instead of a paragraph.

Real measurements (json.dumps, character count) taken while making the change:
a typical successful run_scraper response: 474 -> 493 chars (grew slightly —
it gained a legitimate new field, `remaining`, not prose); a squid_has_tasks
refusal: 947 -> 691 chars (-27%), with the same four options now in `options`
rather than embedded in a sentence.

These assertions are loose ceilings, not the exact figures above — tight
enough that the prose can't silently regrow back into a paragraph, loose
enough not to break on an unrelated new field.
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


def _success_client():
    def handler(request: httpx.Request) -> httpx.Response:
        routes = {
            ("GET", "/v1/crawlers/gm"): CRAWLER_GM,
            ("GET", "/v1/user/balance"): {"available": 1000},
            ("POST", "/v1/squids"): {"id": "sq1"},
            ("POST", "/v1/squids/sq1"): {},
            ("POST", "/v1/tasks"): {"duplicated_count": 0, "tasks": [{"id": "t1"}]},
            ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
        }
        return httpx.Response(200, json=routes[(request.method, request.url.path)])
    return LobstrClient("https://api.lobstr.io/v1", "t", transport=httpx.MockTransport(handler))


def _squid_has_tasks_client():
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key == ("GET", "/v1/tasks"):
            return httpx.Response(200, json={
                "total_results": 2, "page": 1, "total_pages": 1,
                "data": [{"id": f"t{i}", "is_active": True, "params": p}
                        for i, p in enumerate(OLD_TASKS)]})
        routes = {
            ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "gm", "name": "My GM"},
            ("GET", "/v1/crawlers/gm"): CRAWLER_GM,
            ("GET", "/v1/user/balance"): {"available": 1000},
        }
        return httpx.Response(200, json=routes[key])
    return LobstrClient("https://api.lobstr.io/v1", "t", transport=httpx.MockTransport(handler))


def test_a_typical_success_response_stays_compact():
    out = run_scraper_impl(_success_client(), SETTINGS, IdempotencyStore(), "gm",
                           {"query": "x"}, confirm=True)
    size = len(json.dumps(out))
    assert size < 700, f"typical success response grew to {size} chars — check for new prose"
    assert len(out["tasks_note"]) < 150  # one short sentence


def test_a_squid_has_tasks_refusal_stays_compact():
    out = run_scraper_impl(_squid_has_tasks_client(), SETTINGS, IdempotencyStore(),
                           confirm=True, squid_id="sq1", input={"query": "dentists nice"})
    size = len(json.dumps(out))
    assert size < 850, f"squid_has_tasks response grew to {size} chars — check for new prose"
    assert len(out["message"]) < 150  # a short pointer, detail lives in `options`
    assert isinstance(out["options"], list) and len(out["options"]) == 4
