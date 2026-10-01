"""get_scraper_details prices LinkedIn add-ons from credits_per_function; the
crawler's credits_per_email is the verification price (card MLhj4ohs)."""
import httpx

from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.tools.scrapers import get_scraper_details_impl

CID = "b" * 32
SALES_NAV = {
    "id": CID, "name": "Sales Navigator Leads Scraper",
    "credits_per_row": {"legacy": 5, "current": 1},
    "credits_per_email": {"legacy": 1, "current": 2},
    "input": [
        {"name": "url", "type": "string", "level": "task", "required": True},
        {"name": "email_enrichment", "type": "boolean", "level": "squid", "default": True, "function": True,
         "credits_per_function": {"current": 9, "legacy": 400},
         "cost_description": "{credits} credits per profile enriched. No email = 0 credits."},
        {"name": "mobile_enrichment", "type": "boolean", "level": "squid", "default": False, "function": True,
         "credits_per_function": {"current": 300, "legacy": 6000},
         "cost_description": "{credits} credits per phone found."},
    ],
    "result": ["name"],
}


def client_for(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=routes.get(request.url.path, {}))
    return LobstrClient("https://api.lobstr.io/v1", "t", transport=httpx.MockTransport(handler))


def test_paid_functions_carry_the_enrichment_and_phone_prices():
    out = get_scraper_details_impl(client_for({f"/v1/crawlers/{CID}": SALES_NAV}), CID)
    assert "credits_per_email" not in out
    assert out["email_verification_credits"] == 2
    assert out["paid_functions"] == [
        {"name": "email_enrichment", "credits": 9, "on_by_default": True,
         "billed": "9 credits per profile enriched. No email = 0 credits."},
        {"name": "mobile_enrichment", "credits": 300, "on_by_default": False,
         "billed": "300 credits per phone found."},
    ]


def test_crawler_without_paid_functions_has_an_empty_list():
    crawler = {"id": CID, "name": "X", "input": [{"name": "q", "type": "string", "level": "task"}]}
    out = get_scraper_details_impl(client_for({f"/v1/crawlers/{CID}": crawler}), CID)
    assert out["paid_functions"] == []


# --- function toggles are saved, so on-by-default steps really run -----------
# With no toggle saved the engine runs no paid step at all (core/backend.py
# call_filling_functions), so a squid created empty lost email_enrichment.

import json

from lobstr_mcp.tools.primitives import create_squid_impl, update_scraper_impl

LEADS = {
    "id": CID, "name": "LinkedIn Leads",
    "input": [
        {"name": "url", "type": "string", "level": "task", "required": True},
        {"name": "max_results", "type": "number", "level": "squid"},
        {"name": "email_enrichment", "type": "boolean", "level": "squid", "default": True},
        {"name": "enrich_company", "type": "boolean", "level": "squid", "default": False},
    ],
    "result": ["name"],
}
LEADS_PARAMS = {"task": {"url": "string"},
                "squid": {"max_results": "int",
                          "functions": {"email_enrichment": {"default": True},
                                        "enrich_company": {"default": False}}}}


def posting_client(routes, posts):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append((request.url.path, json.loads(request.content or b"{}")))
        return httpx.Response(200, json=routes.get(request.url.path, {}))
    return LobstrClient("https://api.lobstr.io/v1", "t", transport=httpx.MockTransport(handler))


def test_bare_create_squid_saves_the_default_toggles():
    posts = []
    c = posting_client({f"/v1/crawlers/{CID}": LEADS, f"/v1/crawlers/{CID}/params": LEADS_PARAMS,
                        "/v1/squids": {"id": "sq1", "name": "N"}, "/v1/squids/sq1": {"id": "sq1"}}, posts)
    assert create_squid_impl(c, CID)["squid_id"] == "sq1"
    assert dict(posts)["/v1/squids/sq1"]["params"]["functions"] == {
        "email_enrichment": True, "enrich_company": False}


def test_create_squid_keeps_a_toggle_the_caller_turned_off():
    posts = []
    c = posting_client({f"/v1/crawlers/{CID}": LEADS, f"/v1/crawlers/{CID}/params": LEADS_PARAMS,
                        "/v1/squids": {"id": "sq1", "name": "N"}, "/v1/squids/sq1": {"id": "sq1"}}, posts)
    create_squid_impl(c, CID, config={"functions": {"email_enrichment": False}})
    assert dict(posts)["/v1/squids/sq1"]["params"]["functions"]["email_enrichment"] is False


def test_update_scraper_on_an_empty_squid_saves_the_default_toggles():
    posts = []
    c = posting_client({"/v1/squids/sq1": {"id": "sq1", "name": "N", "crawler": CID, "params": {}},
                        f"/v1/crawlers/{CID}": LEADS, f"/v1/crawlers/{CID}/params": LEADS_PARAMS}, posts)
    update_scraper_impl(c, "sq1", config={"max_results": 20})
    params = dict(posts)["/v1/squids/sq1"]["params"]
    assert params["max_results"] == 20
    assert params["functions"] == {"email_enrichment": True, "enrich_company": False}


def test_update_scraper_leaves_saved_toggles_alone():
    posts = []
    c = posting_client({"/v1/squids/sq1": {"id": "sq1", "name": "N", "crawler": CID,
                                           "params": {"functions": {"email_enrichment": False}}},
                        f"/v1/crawlers/{CID}": LEADS, f"/v1/crawlers/{CID}/params": LEADS_PARAMS}, posts)
    update_scraper_impl(c, "sq1", config={"max_results": 20})
    assert dict(posts)["/v1/squids/sq1"]["params"]["functions"] == {"email_enrichment": False}
