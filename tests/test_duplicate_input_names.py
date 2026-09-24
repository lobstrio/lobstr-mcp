"""A crawler that declares two different inputs under one name.

The live Google Maps Leads Scraper declares `country` twice, on purpose:

* task-level, required, in the "location" group — it goes into the search URL
  path (`/maps/search/{category}+in+{city}+{country}`): what you search for;
* squid-level, optional, default "United States", 185 allowed values — it sets
  Google's `gl=` region parameter: where you search from.

`build.py` writes both into `public_params` with no dedup across levels, so
`GET /crawlers/{hash}/params` lists `country` under BOTH `task` and `squid`
(verified against the live crawler row, not only reconstructed from
lobstr.json). `_placement_from_params` filled the task names first and let the
squid loop overwrite them, so `param_levels["country"]` came out "squid":
run_scraper sent the required task field as a squid setting and the API, which
builds its required-group check from `public_params["task"]` alone and never
consults the squid, answered `ParamsNeeded: Missing required parameters:
country`.

The fix keeps `country` task-level under its plain name and publishes the
shadowed squid-level entry under a mechanical alias (`<level>_<name>`, here
`squid_country`) that run_scraper maps back to `country` on the wire. Both
inputs stay reachable; neither is silently dropped — losing the squid one is
not cosmetic, it falls back to `gl=US` and changes which businesses Google
returns.

Nothing here matches on the name `country`; the next crawler that reuses a name
is handled with no code change.
"""
import json

import httpx

from lobstr_mcp.config import Settings
from lobstr_mcp.execution import run_scraper_impl
from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.safeguards import IdempotencyStore, validate_input
from lobstr_mcp.schema_translator import translate_input_schema
from lobstr_mcp.tools.scrapers import get_scraper_details_impl

SETTINGS = Settings(
    lobstr_api_base="https://api.lobstr.io/v1", dev_token=None, request_timeout=30.0,
    run_confirm_threshold=100, public_base_url="https://mcp.lobstr.io",
    service_credential=None, consent_url="https://app.lobstr.io/connect-ai",
)

# --- the live shapes ---------------------------------------------------------
# GET /crawlers/{hash} as it answers for the Google Maps Leads Scraper: the two
# `country` entries in declaration order (the squid one last, which is what
# last-wins used to keep), trimmed to the inputs this file reasons about.
CRAWLER = {"id": "gm", "name": "Google Maps Leads Scraper", "input": [
    {"name": "url", "type": "string", "group": "url", "level": "task",
     "default": None, "required": True,
     "example": "https://www.google.com/maps/search/restaurant/@43.29,5.36,14z",
     "description": "A string representing the Google Maps Search URL."},
    {"name": "category", "type": "string", "group": "location", "level": "task",
     "required": True, "is_params": True, "example": "Italian restaurant",
     "description": "What are you searching for?"},
    {"name": "country", "type": "string", "group": "location", "level": "task",
     "required": True, "is_params": True, "example": "United States",
     "display": "Country", "description": "The country to search in"},
    {"name": "city", "type": "string", "group": "location", "level": "task",
     "required": True, "is_params": True, "example": "Ablon",
     "description": "City or town name (e.g. San Francisco, Akutan)"},
    {"name": "max_results", "type": "int", "level": "squid", "default": 200,
     "max": 200, "required": False,
     "description": "Maximum number of results retrieved *per task*."},
    {"name": "extract_emails_from_website", "type": "boolean", "level": "squid",
     "default": True, "function": True, "priority": 3, "required": False,
     "description": "Extract email addresses from the business's website."},
    {"name": "country", "type": "string", "level": "squid",
     "default": "United States", "display": "Search Region", "required": False,
     "example": "United States", "allowed": ["Afghanistan", "Albania"],
     "description": ("Defines the geographic location context used by Google "
                     "Maps when returning your search results. For example, "
                     "setting this to 'France' ensures that searches are "
                     "served from a French regional context.")},
    {"name": "language", "type": "string", "level": "squid", "required": True,
     "default": "English (United States)", "allowed": ["Afrikaans"],
     "description": "Specifies the language Google Maps will use."},
]}

# GET /crawlers/{hash}/params for the same crawler, in the shape build.py emits
# it (`public_params[level][name]` per declared level, function toggles nested
# under squid.functions) — `country` therefore in both sections. Verbatim from
# the live payload, with the 185-value `allowed` list trimmed.
PARAMS = {
    "task": {
        "url": {"type": "string", "group": "url", "default": None,
                "required": True, "attribute": "url", "is_params": False},
        "category": {"type": "string", "group": "location", "default": None,
                     "required": True, "attribute": "category", "is_params": True},
        "country": {"type": "string", "group": "location", "default": None,
                    "required": True, "attribute": "country", "is_params": True},
        "city": {"type": "string", "group": "location", "default": None,
                 "required": True, "attribute": "city", "is_params": True},
    },
    "squid": {
        "country": {"type": "string", "allowed": ["Afghanistan", "Albania"],
                    "default": "United States", "required": False,
                    "attribute": "country", "is_params": False},
        "language": {"type": "string", "allowed": ["Afrikaans"],
                     "default": "English (United States)", "required": True,
                     "attribute": "language", "is_params": False},
        "max_results": {"type": "int", "default": 200, "required": False,
                        "attribute": "max_results", "is_params": False, "max": 200},
        "functions": {
            "extract_emails_from_website": {
                "default": True, "sort": 2, "skip_squid": True,
                "credits_per_function": {"current": 1, "legacy": 10}},
        },
    },
}


# --- the schema --------------------------------------------------------------


def test_the_task_level_country_keeps_the_plain_name_and_the_task_level():
    """The bug itself: with /params present, `country` must not be levelled
    "squid". Sent as a squid param it is dropped from the required-group check
    and the run dies with "Missing required parameters: country"."""
    out = translate_input_schema(CRAWLER, params=PARAMS)
    assert out["levels"]["country"] == "task"
    prop = out["json_schema"]["properties"]["country"]
    assert prop["description"] == "The country to search in"
    # the squid entry's default must not leak onto the task input
    assert "default" not in prop


def test_the_shadowed_squid_entry_is_published_under_a_mechanical_alias():
    out = translate_input_schema(CRAWLER, params=PARAMS)
    props = out["json_schema"]["properties"]
    assert "squid_country" in props, "the region setting must stay reachable"
    assert out["levels"]["squid_country"] == "squid"
    assert out["wire_names"] == {"squid_country": "country"}
    assert props["squid_country"]["type"] == "string"
    assert props["squid_country"]["default"] == "United States"


def test_the_alias_description_tells_the_two_apart():
    """A model reading only the schema has no other clue that these are
    different fields, and picking the wrong one is not a validation error — it
    is a run served from the wrong region."""
    out = translate_input_schema(CRAWLER, params=PARAMS)
    desc = out["json_schema"]["properties"]["squid_country"]["description"]
    assert "two different inputs both named \"country\"" in desc
    assert "squid-level" in desc and "task-level" in desc
    # both originals, so the distinction is readable without a second call
    assert "geographic location context" in desc
    assert "The country to search in" in desc
    # and how to use it
    assert 'set this one as "squid_country"' in desc
    assert 'real name "country"' in desc


def test_the_alias_is_optional_and_input_modes_are_unchanged():
    """`country` is required task-level but sits in input_modes.either.
    `language` is required but also carries a default (the live shape), so it
    is not actually mandatory — published `required`/`always` are empty; the
    alias must not add to either of those."""
    out = translate_input_schema(CRAWLER, params=PARAMS)
    schema, modes = out["json_schema"], out["input_modes"]
    assert schema["required"] == []
    assert modes["either"] == [["url"], ["category", "country", "city"]]
    assert modes["always"] == []
    assert all("squid_country" not in g for g in modes["either"])
    assert validate_input({"category": "dentist", "city": "Paris",
                           "country": "France", "language": "French",
                           "squid_country": "France"}, schema,
                          input_modes=modes) == []
    # the alias is type-checked like any other property
    assert validate_input({"url": "https://x", "language": "French",
                           "squid_country": 7}, schema, input_modes=modes) == \
        ["squid_country must be string"]


def test_a_crawler_with_no_collision_gains_no_alias():
    crawler = {"input": [
        {"name": "url", "type": "string", "level": "task", "required": True},
        {"name": "max_results", "type": "int", "level": "squid", "default": 50},
    ]}
    params = {"task": {"url": {"type": "string", "required": True}},
              "squid": {"max_results": {"type": "int", "default": 50}}}
    out = translate_input_schema(crawler, params=params)
    assert "wire_names" not in out, "absent, not an empty dict"
    assert out == {
        "json_schema": {"type": "object", "properties": {
            "url": {"type": "string"},
            "max_results": {"type": "integer", "default": 50}},
            "required": ["url"]},
        "levels": {"url": "task", "max_results": "squid"},
        "input_modes": None,
    }


def test_the_same_name_twice_at_the_same_level_is_still_one_input():
    """One input written twice, not two inputs: keep the first, alias nothing."""
    crawler = {"input": [
        {"name": "x", "type": "string", "level": "squid", "description": "first"},
        {"name": "x", "type": "int", "level": "squid", "description": "second"},
    ]}
    out = translate_input_schema(crawler)
    assert out["json_schema"]["properties"]["x"]["description"] == "first"
    assert list(out["json_schema"]["properties"]) == ["x"]
    assert "wire_names" not in out


def test_the_tie_break_still_decides_which_entry_keeps_the_plain_name():
    """Declaration order must not matter: the required one keeps the name and
    the level, whichever side it is declared on."""
    reversed_order = {"input": [
        {"name": "country", "type": "string", "level": "squid", "required": False,
         "default": "United States"},
        {"name": "country", "type": "string", "level": "task", "required": True},
    ]}
    out = translate_input_schema(reversed_order, params=PARAMS)
    assert out["levels"]["country"] == "task"
    assert "default" not in out["json_schema"]["properties"]["country"]
    assert out["levels"]["squid_country"] == "squid"
    assert out["wire_names"] == {"squid_country": "country"}


def test_the_alias_never_steals_a_name_the_crawler_already_uses():
    crawler = {"input": [
        {"name": "country", "type": "string", "level": "task", "required": True},
        {"name": "country", "type": "string", "level": "squid", "default": "US"},
        {"name": "squid_country", "type": "string", "level": "squid",
         "description": "a real input that happens to be called that"},
    ]}
    out = translate_input_schema(crawler)
    props = out["json_schema"]["properties"]
    assert props["squid_country"]["description"] == \
        "a real input that happens to be called that"
    assert out["wire_names"] == {"squid_country_2": "country"}
    assert props["squid_country_2"]["default"] == "US"


def test_a_shadowed_function_toggle_is_aliased_at_its_own_level():
    """/params is the only source that knows a squid input is really a function
    toggle, so the alias follows it rather than input[]'s flat level=squid."""
    crawler = {"input": [
        {"name": "verify", "type": "string", "level": "task", "required": True,
         "description": "what to verify"},
        {"name": "verify", "type": "boolean", "level": "squid", "function": True,
         "default": False, "description": "run the verification step"},
    ]}
    params = {"task": {"verify": {"type": "string", "required": True}},
              "squid": {"functions": {"verify": {"default": False}}}}
    out = translate_input_schema(crawler, params=params)
    assert out["levels"]["verify"] == "task"
    assert out["levels"]["function_verify"] == "function"
    assert out["wire_names"] == {"function_verify": "verify"}


def test_placement_prefers_the_task_claim_for_a_name_params_lists_twice():
    from lobstr_mcp.schema_translator import _placement_from_params
    assert _placement_from_params(PARAMS)["country"] == "task"
    # and a name in one section only is unaffected
    assert _placement_from_params(PARAMS)["max_results"] == "squid"
    assert _placement_from_params(PARAMS)["extract_emails_from_website"] == "function"


# --- the route each value takes ---------------------------------------------


def run_and_capture(input_dict, **kwargs):
    """run_scraper on a new squid, returning every request body it sent."""
    bodies: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if request.content:
            bodies[key] = json.loads(request.content)
        if key == ("GET", "/v1/crawlers/gm/params"):
            return httpx.Response(200, json=PARAMS)
        return httpx.Response(200, json={
            ("GET", "/v1/crawlers/gm"): CRAWLER,
            ("GET", "/v1/user/balance"): {"available": 1000},
            ("POST", "/v1/squids"): {"id": "sq1", "name": "Google Maps Leads Scraper"},
            ("POST", "/v1/squids/sq1"): {},
            ("POST", "/v1/tasks"): {"duplicated_count": 0, "tasks": [{"id": "t"}]},
            ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
        }[key])

    client = LobstrClient("https://api.lobstr.io/v1", "t",
                          transport=httpx.MockTransport(handler))
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), scraper="gm",
                           input=input_dict, confirm=True, **kwargs)
    return out, bodies


def test_run_scraper_sends_each_country_where_it_belongs():
    out, bodies = run_and_capture({
        "category": "dentist", "city": "Paris", "country": "France",
        "language": "French", "squid_country": "France", "max_results": 50})
    assert out["run_id"] == "run1"

    settings = bodies[("POST", "/v1/squids/sq1")]
    # the region goes out under the API's own name, inside params
    assert settings["params"]["country"] == "France"
    assert "squid_country" not in settings["params"], \
        "the alias is this client's vocabulary; the API rejects it"
    assert settings["params"]["max_results"] == 50

    # the searched-for country stays in the task row, under its plain name
    assert bodies[("POST", "/v1/tasks")]["tasks"] == [
        {"category": "dentist", "city": "Paris", "country": "France"}]


def test_the_two_countries_can_differ():
    """The whole point of keeping both: search French listings from the US
    region, or the reverse."""
    _, bodies = run_and_capture({
        "category": "dentist", "city": "Paris", "country": "France",
        "language": "French", "squid_country": "United States"})
    assert bodies[("POST", "/v1/squids/sq1")]["params"]["country"] == "United States"
    assert bodies[("POST", "/v1/tasks")]["tasks"][0]["country"] == "France"


def test_a_shadowed_function_toggle_is_nested_under_params_functions():
    crawler = {"id": "gm", "name": "c", "input": [
        {"name": "verify", "type": "string", "level": "task", "required": True},
        {"name": "verify", "type": "boolean", "level": "squid", "function": True,
         "default": False},
    ]}
    params = {"task": {"verify": {"type": "string", "required": True}},
              "squid": {"functions": {"verify": {"default": False}}}}
    bodies: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if request.content:
            bodies[key] = json.loads(request.content)
        if key == ("GET", "/v1/crawlers/gm/params"):
            return httpx.Response(200, json=params)
        return httpx.Response(200, json={
            ("GET", "/v1/crawlers/gm"): crawler,
            ("GET", "/v1/user/balance"): {"available": 1000},
            ("POST", "/v1/squids"): {"id": "sq1"},
            ("POST", "/v1/squids/sq1"): {},
            ("POST", "/v1/tasks"): {"duplicated_count": 0, "tasks": [{"id": "t"}]},
            ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
        }[key])

    client = LobstrClient("https://api.lobstr.io/v1", "t",
                          transport=httpx.MockTransport(handler))
    run_scraper_impl(client, SETTINGS, IdempotencyStore(), scraper="gm",
                     input={"verify": "emails", "function_verify": True},
                     confirm=True)
    settings = bodies[("POST", "/v1/squids/sq1")]
    assert settings["params"]["functions"] == {"verify": True}
    assert "function_verify" not in settings["params"]
    assert bodies[("POST", "/v1/tasks")]["tasks"] == [{"verify": "emails"}]


# --- what get_scraper_details publishes --------------------------------------


def details_client():
    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key == ("GET", "/v1/crawlers/gm/params"):
            return httpx.Response(200, json=PARAMS)
        return httpx.Response(200, json=CRAWLER)
    return LobstrClient("https://api.lobstr.io/v1", "t",
                        transport=httpx.MockTransport(handler))


def test_get_scraper_details_agrees_with_the_routing():
    out = get_scraper_details_impl(details_client(), "gm")
    assert out["param_levels"]["country"] == "task"
    assert out["param_levels"]["squid_country"] == "squid"
    # create_squid's config goes straight to the API, so it needs the API name
    assert out["param_wire_names"] == {"squid_country": "country"}
    assert "squid_country" in out["input_schema"]["properties"]


def test_param_wire_names_is_absent_for_a_crawler_with_no_collision():
    plain = {"id": "gm", "name": "c", "input": [
        {"name": "url", "type": "string", "level": "task", "required": True}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/params"):
            return httpx.Response(200, json={"task": {"url": {"type": "string"}},
                                             "squid": {}})
        return httpx.Response(200, json=plain)

    client = LobstrClient("https://api.lobstr.io/v1", "t",
                          transport=httpx.MockTransport(handler))
    assert "param_wire_names" not in get_scraper_details_impl(client, "gm")
