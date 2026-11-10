"""create_squid and update_scraper set a squid's webhook through POST /delivery.

fluxea.io, 09/11/2026: an agent passed webhook_fields inside update_scraper's
`config`, which went into params; the API refused it three times with "The
specified parameter webhook_fields is invalid."
"""
import json

import httpx

from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.tools.primitives import create_squid_impl, update_scraper_impl

CID = "a" * 32
CRAWLER = {
    "id": CID, "name": "Leboncoin",
    "input": [
        {"name": "url", "type": "string", "level": "task", "required": True},
        {"name": "max_results", "type": "number", "level": "squid"},
    ],
    "result": ["title"],
}
NO_WEBHOOK = {"url": None, "retry": False, "is_active": False,
              "events": {"run.done": True, "run.error": False, "run.paused": False,
                         "run.running": False}}


def recording_client(routes, posts, refuse=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append((request.url.path, json.loads(request.content or b"{}")))
        if refuse and request.url.path == refuse:
            return httpx.Response(400, json={"errors": {"message": "nope", "type": "InvalidParam"}})
        body = routes.get(request.url.path, {})
        if request.url.path == "/v1/delivery" and request.method == "POST":
            body = json.loads(request.content)
        return httpx.Response(200, json=body)
    return LobstrClient("https://api.lobstr.io/v1", "t", transport=httpx.MockTransport(handler))


def squid_routes(webhook_fields=NO_WEBHOOK):
    return {"/v1/squids/sq1": {"id": "sq1", "name": "S", "crawler": CID,
                               "params": {}, "webhook_fields": webhook_fields},
            f"/v1/crawlers/{CID}": CRAWLER, "/v1/squids": {"id": "sq1", "name": "S"}}


def test_update_sends_webhook_to_delivery_with_defaults():
    posts = []
    out = update_scraper_impl(recording_client(squid_routes(), posts), "sq1",
                              webhook={"url": "https://n8n.example/hook"})
    assert posts == [("/v1/delivery", {"squid": "sq1", "webhook_fields": {
        "url": "https://n8n.example/hook", "is_active": True, "retry": True,
        "events": {"run.running": False, "run.paused": False, "run.done": True,
                   "run.error": True}}})]
    assert out["webhook"] == {"url": "https://n8n.example/hook",
                              "events": ["run.done", "run.error"],
                              "retry": True, "is_active": True}


def test_webhook_fields_inside_config_is_lifted_out_of_params():
    posts = []
    update_scraper_impl(recording_client(squid_routes(), posts), "sq1", config={
        "max_results": 5,
        "webhook_fields": {"url": "https://n8n.example/hook", "is_active": True, "retry": True,
                           "events": {"run.done": True, "run.error": True,
                                      "run.paused": False, "run.running": False}}})
    squid_post = dict(posts)["/v1/squids/sq1"]
    assert "webhook_fields" not in squid_post["params"]
    assert squid_post["params"]["max_results"] == 5
    assert dict(posts)["/v1/delivery"]["webhook_fields"]["events"]["run.error"] is True


def test_update_keeps_saved_values_for_left_out_keys():
    saved = {"url": "https://n8n.example/hook", "retry": False, "is_active": True,
             "events": {"run.done": True, "run.error": False, "run.paused": True,
                        "run.running": False}}
    posts = []
    update_scraper_impl(recording_client(squid_routes(saved), posts), "sq1",
                        webhook={"is_active": False})
    assert posts == [("/v1/delivery", {"squid": "sq1", "webhook_fields": {
        "url": "https://n8n.example/hook", "is_active": False, "retry": False,
        "events": {"run.running": False, "run.paused": True, "run.done": True,
                   "run.error": False}}})]


def test_bad_webhook_changes_nothing():
    posts = []
    out = update_scraper_impl(recording_client(squid_routes(), posts), "sq1", config={"max_results": 5},
                              webhook={"url": "n8n.example", "events": ["run.finished"]})
    assert posts == []
    assert out["error_code"] == "validation_error"
    assert any("http(s) URL" in e for e in out["errors"])
    assert any("run.finished" in e for e in out["errors"])


def test_create_validates_webhook_before_creating_the_squid():
    posts = []
    out = create_squid_impl(recording_client(squid_routes(), posts), CID,
                            webhook={"url": "https://n8n.example/hook", "events": []})
    assert posts == []
    assert out["error_code"] == "validation_error"


def test_create_sets_webhook():
    posts = []
    out = create_squid_impl(recording_client(squid_routes(), posts), CID,
                            config={"max_results": 3},
                            webhook={"url": "https://n8n.example/hook", "events": ["run.done"]})
    paths = [p for p, _ in posts]
    assert paths == ["/v1/squids", "/v1/squids/sq1", "/v1/delivery"]
    assert out["webhook"]["events"] == ["run.done"]


def test_refused_webhook_after_settings_says_settings_saved():
    posts = []
    out = update_scraper_impl(recording_client(squid_routes(), posts, refuse="/v1/delivery"), "sq1",
                              config={"max_results": 5}, webhook={"url": "https://n8n.example/hook"})
    assert out["settings_saved"] is True
    assert out["message"] == "Settings saved, but the webhook was refused: nope"
