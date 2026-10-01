"""create_squid must not send task-level defaults as squid params, and
get_my_scraper must not return delivery credentials (card MLhj4ohs)."""
import json

import httpx

from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.tools.primitives import create_squid_impl
from lobstr_mcp.tools.scrapers import get_my_scraper_impl

CID = "a" * 32
# LinkedIn-like crawler: a required task field with a default, a squid setting
# and a function toggle.
CRAWLER = {
    "id": CID, "name": "LinkedIn Leads",
    "input": [
        {"name": "url", "type": "string", "level": "task", "required": True, "default": ""},
        {"name": "max_results", "type": "number", "level": "squid"},
        {"name": "email_enrichment", "type": "boolean", "level": "function"},
    ],
    "result": ["name"],
}


def recording_client(routes, posts):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append((request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json=routes.get(request.url.path, {}))
    return LobstrClient("https://api.lobstr.io/v1", "t", transport=httpx.MockTransport(handler))


def test_create_squid_config_does_not_send_task_defaults_as_squid_params():
    posts = []
    c = recording_client({
        f"/v1/crawlers/{CID}": CRAWLER,
        "/v1/squids": {"id": "sq1", "name": "N"},
        "/v1/squids/sq1": {"id": "sq1"},
    }, posts)
    out = create_squid_impl(c, CID, config={"max_results": 20, "functions": {"email_enrichment": True}})
    assert out["squid_id"] == "sq1"
    params = dict(posts)["/v1/squids/sq1"]["params"]
    assert "url" not in params
    assert params["max_results"] == 20
    assert params["functions"] == {"email_enrichment": True}


def test_get_my_scraper_drops_delivery_credentials():
    posts = []
    c = recording_client({"/v1/squids/sq1": {
        "id": "sq1",
        "ftp_fields": {"host": "h", "username": "u", "password": "p", "is_active": True},
        "s3_fields": {"bucket": "b", "aws_access_key": "a", "aws_secret_key": "s"},
        "slack_fields": {"channel": "#c", "token": "x"},
        "webhook_fields": {"url": "https://example.com", "is_active": False},
    }}, posts)
    out = get_my_scraper_impl(c, "sq1")
    assert out["ftp_fields"] == {"host": "h", "username": "u", "is_active": True}
    assert out["s3_fields"] == {"bucket": "b"}
    assert out["slack_fields"] == {"channel": "#c"}
    assert out["webhook_fields"] == {"url": "https://example.com", "is_active": False}
