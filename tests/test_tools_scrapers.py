import httpx
from lobstr_mcp.lobstr_client import LobstrClient, resolve_crawler_id
from lobstr_mcp.tools.scrapers import (
    search_scrapers_impl,
    get_scraper_details_impl,
    list_my_scrapers_impl,
    get_my_scraper_impl,
)


def test_resolve_crawler_id_id_passthrough_slug_and_miss():
    cid = "c" * 32

    class C:
        calls = 0

        def list_crawlers(self):
            C.calls += 1
            return [{"id": cid, "slug": "my-slug"}]

    assert resolve_crawler_id(C(), cid) == cid          # id: returned as-is...
    assert C.calls == 0                                  # ...without hitting the catalog
    assert resolve_crawler_id(C(), "my-slug") == cid     # slug -> id
    assert resolve_crawler_id(C(), "no-such") == "no-such"  # miss -> passthrough

# Field names follow a live GET /crawlers item (see LIVE_CRAWLER below).
CRAWLERS = [
    {"id": "gm", "name": "Google Maps", "description": "Scrape businesses",
     "slug": "google-maps", "credits_per_row": 1, "is_available": True},
    {"id": "rm", "name": "Rightmove", "description": "UK property listings",
     "slug": "rightmove", "credits_per_row": 1, "is_available": True},
]
CRAWLER_GM = {
    "id": "gm", "name": "Google Maps", "description": "Scrape businesses",
    "credits_per_row": 1,
    "input": [{"name": "query", "type": "string", "level": "task", "required": True}],
    "result": ["title", "address", "phone"],
}
SQUIDS = [{"id": "sq1", "name": "My Rightmove"}, {"id": "sq2", "name": "My GM"}]


def client_for(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        return httpx.Response(200, json=routes[path])
    return LobstrClient("https://api.lobstr.io/v1", "t",
                        transport=httpx.MockTransport(handler))


def test_search_returns_only_matches():
    # only Rightmove mentions "property"; the catalog dump is what we're cutting
    client = client_for({"/v1/crawlers": CRAWLERS})
    out = search_scrapers_impl(client, "property")
    assert out["count"] == 1
    assert out["total_available"] == 2
    assert out["results"][0]["id"] == "rm"


def test_search_empty_query_browses_all_under_cap():
    client = client_for({"/v1/crawlers": CRAWLERS})
    out = search_scrapers_impl(client, "")
    assert out["count"] == 2
    assert out["total_available"] == 2


def test_search_caps_matches_and_flags_truncation():
    many = [{"id": f"m{i}", "name": f"Maps Scraper {i}", "slug": f"maps-{i}",
             "description": "maps", "credits_per_row": 1, "is_available": True}
            for i in range(15)]
    client = client_for({"/v1/crawlers": many})
    out = search_scrapers_impl(client, "maps")
    assert out["count"] == 10          # capped
    assert out["total_available"] == 15
    assert out["truncated"] is True
    assert "full=true" in out["hint"]


def test_search_full_returns_the_whole_catalog():
    many = [{"id": f"m{i}", "name": f"Maps Scraper {i}", "slug": f"maps-{i}",
             "description": "maps", "credits_per_row": 1, "is_available": True}
            for i in range(15)]
    client = client_for({"/v1/crawlers": many})
    out = search_scrapers_impl(client, "maps", full=True)
    assert out["count"] == 15
    assert "truncated" not in out


def test_details_builds_schema_and_outputs():
    # A no-account crawler's real /params response omits "account" entirely
    # (verified live) — not {"type": "none"}, a shape the API never produces
    # (the serializer makes it structurally impossible: a module with no
    # account type serialises to null, never a "none" string, at any depth).
    client = client_for({
        "/v1/crawlers/gm": CRAWLER_GM,
        "/v1/crawlers/gm/params": {},
    })
    out = get_scraper_details_impl(client, "gm")
    assert out["id"] == "gm"
    assert out["input_schema"]["required"] == ["query"]
    assert out["input_schema"]["properties"]["query"]["type"] == "string"
    assert out["param_levels"]["query"] == "task"
    assert out["output_fields"] == ["title", "address", "phone"]
    assert out["credits_per_row"] == 1


def test_details_no_account_needed_is_null():
    # The crawler endpoint's own real shape for "no account needed" is a
    # null "account" field, not an absent key and not {"type": "none"} —
    # exercise the null case explicitly (CRAWLER_GM simply omits the key,
    # covered by test_details_builds_schema_and_outputs above; both read as
    # None through crawler_account_type()).
    crawler = {**CRAWLER_GM, "account": None}
    client = client_for({
        "/v1/crawlers/gm": crawler,
        "/v1/crawlers/gm/params": {},
    })
    out = get_scraper_details_impl(client, "gm")
    assert out["required_account_type"] is None


def test_details_notes_the_zip_code_behavior_for_google_maps():
    crawler = {**CRAWLER_GM, "slug": "google-maps-leads-scraper"}
    client = client_for({"/v1/crawlers/gm": crawler, "/v1/crawlers/gm/params": {}})
    out = get_scraper_details_impl(client, "gm")
    assert "ZIP/postal code" in out["note"]
    assert "city" in out["note"]


def test_details_has_no_note_for_an_unrelated_crawler():
    client = client_for({"/v1/crawlers/gm": CRAWLER_GM, "/v1/crawlers/gm/params": {}})
    out = get_scraper_details_impl(client, "gm")
    assert "note" not in out


def test_details_surfaces_required_account_type():
    # The crawler endpoint carries the account type under "account"; dropping
    # it left a model to discover the requirement only from a failed run
    # (done_reason "no_accounts"). A crawler that needs an
    # account returns the object with the real slug on both endpoints, the
    # params endpoint additionally marking it required (verified live).
    crawler = {**CRAWLER_GM, "account": {"type": "linkedin-sync"}}
    client = client_for({
        "/v1/crawlers/gm": crawler,
        "/v1/crawlers/gm/params": {"account": {"type": "linkedin-sync", "required": True}},
    })
    out = get_scraper_details_impl(client, "gm")
    assert out["required_account_type"] == "linkedin-sync"


def test_list_my_scrapers():
    client = client_for({"/v1/squids": SQUIDS})
    out = list_my_scrapers_impl(client)
    assert out["count"] == 2
    assert out["scrapers"][0] == {"id": "sq1", "name": "My Rightmove"}


SQUIDS_ENVELOPE = {
    "total_results": 3, "page": 1, "total_pages": 2,
    "next": "https://api.lobstr.io/v1/squids?page=2",
    "data": [
        {"id": "sq1", "name": "My GM", "crawler": "gm", "crawler_name": "Google Maps",
         "last_run_status": "done", "last_run_at": "2026-09-01"},
        {"id": "sq2", "name": "My RM", "crawler": "rm", "crawler_name": "Rightmove"},
    ],
}


def test_list_my_scrapers_surfaces_pagination_and_crawler():
    client = client_for({"/v1/squids": SQUIDS_ENVELOPE})
    out = list_my_scrapers_impl(client, page=1, page_size=2)
    assert (out["total"], out["total_pages"], out["page"], out["count"]) == (3, 2, 1, 2)
    assert out["next"].endswith("page=2")
    assert out["scrapers"][0] == {
        "id": "sq1", "name": "My GM", "crawler": "Google Maps",
        "last_run_status": "done", "last_run_at": "2026-09-01"}


def test_list_my_scrapers_name_filter_is_server_side():
    cap = {}

    def handler(request: httpx.Request) -> httpx.Response:
        cap["query"] = dict(request.url.params)
        return httpx.Response(200, json={"total_results": 0, "page": 1,
                                         "total_pages": 1, "data": [], "next": None})

    client = LobstrClient("https://api.lobstr.io/v1", "t",
                          transport=httpx.MockTransport(handler))
    list_my_scrapers_impl(client, name="dentist")
    assert cap["query"].get("name") == "dentist"


CROSS_CRAWLER_SQUIDS = [
    {"id": "a", "name": "GM one", "crawler": "gm", "crawler_name": "Google Maps"},
    {"id": "b", "name": "RM", "crawler": "rm", "crawler_name": "Rightmove"},
    {"id": "c", "name": "GM two", "crawler": "gm", "crawler_name": "Google Maps"},
]


def test_list_my_scrapers_crawler_filter_is_client_side():
    # crawler has no server filter: resolve the slug -> id, walk squids (a bare
    # list here, which must terminate — exercises the SDK's bare-list fix), and
    # keep only the matching ones.
    client = client_for({
        "/v1/crawlers": [{"id": "gm", "slug": "google-maps"}],
        "/v1/squids": CROSS_CRAWLER_SQUIDS,
    })
    out = list_my_scrapers_impl(client, crawler="google-maps")
    assert out["count"] == 2 and out["total"] == 2
    assert {s["id"] for s in out["scrapers"]} == {"a", "c"}


def test_get_my_scraper():
    client = client_for({"/v1/squids/sq1": {"id": "sq1", "name": "My Rightmove",
                                            "input": {"query": "flats"}}})
    out = get_my_scraper_impl(client, "sq1")
    assert out["id"] == "sq1"
    assert out["input"] == {"query": "flats"}


def test_get_my_scraper_strips_heavy_icon_and_ui_state():
    client = client_for({"/v1/squids/sq1": {
        "id": "sq1", "name": "X", "params": {"max_results": 3},
        "icon": "PHN2ZyB" * 500, "ui_state": {"foo": "bar"}}})
    out = get_my_scraper_impl(client, "sq1")
    assert out["id"] == "sq1" and out["params"] == {"max_results": 3}
    assert "icon" not in out and "ui_state" not in out


# --- real crawler field names (found in P4 live testing) ----------------------
# A live GET /crawlers/{hash} exposes credits_per_row / credits_per_email /
# slug / is_premium / is_available. There is no "pricing" or "platform" key, so
# the summary previously reported pricing_hint=None for every scraper.

LIVE_CRAWLER = {
    "id": "4734d096", "name": "Google Maps Leads Scraper",
    "slug": "google-maps-leads-scraper", "description": "Grab all results.",
    "credits_per_row": {"legacy": 1, "current": 1},
    "credits_per_email": {"legacy": 100, "current": 2},
    "is_premium": False, "is_available": True, "rank": 1,
}


def test_search_summary_reports_real_pricing_and_slug():
    from lobstr_mcp.tools.scrapers import search_scrapers_impl

    class C:
        def list_crawlers(self):
            return [LIVE_CRAWLER]

    out = search_scrapers_impl(C(), "google maps")
    top = out["results"][0]
    assert top["credits_per_row"] == 1, "the {legacy,current} dict must be resolved"
    assert top["credits_per_email"] == 2, "current rate, not legacy"
    assert top["slug"] == "google-maps-leads-scraper"
    assert top["is_available"] is True


# --- example stripping (translator maps input "example" -> schema "examples") --

CRAWLER_WITH_EXAMPLE = {
    "id": "gm", "name": "Google Maps",
    "input": [{"name": "query", "type": "string", "level": "task",
               "required": True, "example": "restaurants"}],
    "result": ["title"],
}


def test_details_strips_examples_by_default():
    # No-account crawler: real /params omits "account" entirely.
    client = client_for({"/v1/crawlers/gm": CRAWLER_WITH_EXAMPLE,
                         "/v1/crawlers/gm/params": {}})
    out = get_scraper_details_impl(client, "gm")
    assert "examples" not in out["input_schema"]["properties"]["query"]


def test_details_full_keeps_examples():
    client = client_for({"/v1/crawlers/gm": CRAWLER_WITH_EXAMPLE,
                         "/v1/crawlers/gm/params": {}})
    out = get_scraper_details_impl(client, "gm", full=True)
    assert out["input_schema"]["properties"]["query"]["examples"] == ["restaurants"]


# --- accept a slug OR an id on get_scraper_details --------------------------

def test_details_accepts_a_slug_and_resolves_to_id():
    cid = "a" * 32
    client = client_for({
        "/v1/crawlers": [{"id": cid, "slug": "google-maps-leads-scraper", "name": "GM"}],
        f"/v1/crawlers/{cid}": {
            "id": cid, "name": "GM",
            "input": [{"name": "query", "type": "string", "level": "task", "required": True}],
            "result": ["title"]},
        f"/v1/crawlers/{cid}/params": {},
    })
    out = get_scraper_details_impl(client, "google-maps-leads-scraper")
    assert out["id"] == cid
    assert out["input_schema"]["required"] == ["query"]


def test_details_surfaces_input_modes_from_groups():
    crawler = {"id": "gm", "name": "GM", "result": ["title"], "input": [
        {"name": "url", "type": "string", "level": "task", "required": True, "group": "url"},
        {"name": "category", "type": "string", "level": "task", "required": True, "group": "location"},
        {"name": "country", "type": "string", "level": "task", "required": True, "group": "location"},
        {"name": "city", "type": "string", "level": "task", "required": True, "group": "location"},
        {"name": "language", "type": "string", "level": "squid", "required": True}]}
    client = client_for({"/v1/crawlers/gm": crawler, "/v1/crawlers/gm/params": {}})
    out = get_scraper_details_impl(client, "gm")
    assert out["input_modes"]["always"] == ["language"]
    assert ["url"] in out["input_modes"]["either"]
    assert out["input_schema"]["required"] == ["language"]  # only always-required, not the alternatives


def test_details_accepts_an_id_without_listing_the_catalog():
    cid = "b" * 32  # a 32-hex id resolves directly; no /v1/crawlers list route needed
    client = client_for({
        f"/v1/crawlers/{cid}": {
            "id": cid, "name": "GM",
            "input": [{"name": "q", "type": "string", "level": "task", "required": True}],
            "result": []},
        f"/v1/crawlers/{cid}/params": {},
    })
    out = get_scraper_details_impl(client, cid)
    assert out["id"] == cid
