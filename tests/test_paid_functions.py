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
