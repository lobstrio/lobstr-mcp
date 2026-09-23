"""Squid inputs that exist only in GET /crawlers/{hash}/params.

`auto_verify_emails` is the first of them: the API advertises it in the squid
section of /params for crawlers whose module does email verification, and takes
it like any other squid input (`POST /squids/{hash}` with
`{"params": {"auto_verify_emails": true}}`), but it is deliberately absent from
`crawler["input"]` — that list is what the dashboard renders as a form, and the
dashboard already draws its own "Verify emails" toggle.

translate_input_schema built `properties` from `input[]` alone and used /params
only for placement, so the key became no property at all: run_scraper could not
classify it as squid-level, and it was shipped in the task row instead, where
the setting is silently dropped and the run still bills.

Nothing here matches on the field name. /params carries the type, default,
description and level, and the API will advertise more of these.
"""
import json

import httpx

from lobstr_mcp.config import Settings
from lobstr_mcp.execution import run_scraper_impl
from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.safeguards import IdempotencyStore, validate_input
from lobstr_mcp.schema_translator import translate_input_schema

SETTINGS = Settings(
    lobstr_api_base="https://api.lobstr.io/v1", dev_token=None, request_timeout=30.0,
    run_confirm_threshold=100, public_base_url="https://mcp.lobstr.io",
    service_credential=None, consent_url="https://app.lobstr.io/connect-ai",
)

# As the crawler record ships it: no auto_verify_emails, and `country` listed
# twice (the live Google Maps duplicate the tie-break exists for).
CRAWLER = {"id": "gm", "name": "Google Maps", "input": [
    {"name": "query", "type": "string", "level": "task", "required": True},
    {"name": "country", "type": "string", "level": "task", "required": True},
    {"name": "country", "type": "string", "level": "squid", "required": False,
     "default": "United States"},
    {"name": "max_results", "type": "int", "level": "squid", "default": 100},
]}

# As GET /crawlers/{hash}/params ships it, with the new key in the squid
# section only.
PARAMS = {
    "task": {"query": {"type": "string", "required": True},
             "country": {"type": "string", "required": True}},
    "squid": {
        # listed in input[] too, with its own default and no description:
        # /params must not overwrite either
        "max_results": {"type": "int", "default": 999,
                        "description": "from /params, not from input[]"},
        "auto_verify_emails": {
            "type": "boolean", "default": False, "required": False,
            "attribute": "auto_verify_emails", "is_params": False, "level": "squid",
            "description": ("Verify every e-mail collected during the run. "
                            "Adds a credit cost per e-mail."),
        },
    },
}

PARAMS_WITHOUT = {
    "task": {"query": {"type": "string", "required": True},
             "country": {"type": "string", "required": True}},
    "squid": {"max_results": {"type": "int"}},
}


# --- the schema --------------------------------------------------------------


def test_a_params_only_squid_key_becomes_a_settable_squid_level_property():
    out = translate_input_schema(CRAWLER, params=PARAMS)
    prop = out["json_schema"]["properties"]["auto_verify_emails"]
    assert prop["type"] == "boolean"
    assert prop["default"] is False
    assert prop["description"].startswith("Verify every e-mail collected")
    assert out["levels"]["auto_verify_emails"] == "squid"


def test_the_new_property_is_optional_and_type_checked():
    out = translate_input_schema(CRAWLER, params=PARAMS)
    schema, modes = out["json_schema"], out["input_modes"]
    # not invented as required: input[] does not ask for it
    assert "auto_verify_emails" not in schema["required"]
    assert validate_input({"query": "x", "country": "FR",
                           "auto_verify_emails": True}, schema, input_modes=modes) == []
    assert validate_input({"query": "x", "country": "FR",
                           "auto_verify_emails": "yes"}, schema,
                          input_modes=modes) == ["auto_verify_emails must be boolean"]


def test_a_crawler_whose_params_lack_the_key_gains_no_property():
    out = translate_input_schema(CRAWLER, params=PARAMS_WITHOUT)
    assert "auto_verify_emails" not in out["json_schema"]["properties"]
    assert "auto_verify_emails" not in out["levels"]
    # ... and with no /params payload at all
    bare = translate_input_schema(CRAWLER)
    assert "auto_verify_emails" not in bare["json_schema"]["properties"]


def test_params_never_override_a_key_input_already_defines():
    # `max_results` is in both, with a different default and a description in
    # /params. The input[] definition must survive untouched; /params still
    # decides the level, as it did before.
    out = translate_input_schema(CRAWLER, params=PARAMS)
    prop = out["json_schema"]["properties"]["max_results"]
    assert prop == {"type": "integer", "default": 100}
    assert out["levels"]["max_results"] == "squid"


def test_the_duplicate_country_tie_break_is_unchanged_with_params_present():
    without = translate_input_schema(CRAWLER)
    with_params = translate_input_schema(CRAWLER, params=PARAMS)
    assert without["json_schema"]["properties"]["country"] == \
        with_params["json_schema"]["properties"]["country"]
    assert without["levels"]["country"] == with_params["levels"]["country"] == "task"


def test_a_params_only_function_toggle_is_levelled_as_a_function():
    params = {"task": {}, "squid": {"functions": {
        "fetch_business_images": {"default": False,
                                  "description": "Download each business's images"}}}}
    out = translate_input_schema({"input": []}, params=params)
    prop = out["json_schema"]["properties"]["fetch_business_images"]
    # no declared type: taken from the default rather than guessed as "string",
    # which would then reject `true`
    assert prop["type"] == "boolean"
    assert out["levels"]["fetch_business_images"] == "function"


def test_a_spec_that_describes_nothing_is_still_settable():
    out = translate_input_schema({"input": []},
                                 params={"squid": {"mystery": {"required": False}}})
    assert out["json_schema"]["properties"]["mystery"] == {}
    assert out["levels"]["mystery"] == "squid"
    # no type means nothing to check against, not a rejection
    assert validate_input({"mystery": 3}, out["json_schema"]) == []


def test_a_bare_value_instead_of_a_spec_is_left_alone():
    # older /params shapes answer {"squid": {"max_results": "int"}}
    out = translate_input_schema({"input": []}, params={"squid": {"max_results": "int"}})
    assert out["json_schema"]["properties"] == {}


# --- the route the value takes ----------------------------------------------


def run_and_capture(input_dict):
    """run_scraper against a squid with no saved tasks, returning every request
    body the client sent, keyed by (method, path)."""
    bodies: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if request.content:
            bodies[key] = json.loads(request.content)
        if key == ("GET", "/v1/tasks"):
            return httpx.Response(200, json={"total_results": 0, "page": 1,
                                             "total_pages": 1, "data": []})
        if key == ("GET", "/v1/crawlers/gm/params"):
            return httpx.Response(200, json=PARAMS)
        return httpx.Response(200, json={
            ("GET", "/v1/squids/sq1"): {"id": "sq1", "crawler": "gm", "name": "My GM"},
            ("GET", "/v1/crawlers/gm"): CRAWLER,
            ("GET", "/v1/user/balance"): {"available": 1000},
            ("POST", "/v1/squids/sq1"): {},
            ("POST", "/v1/tasks"): {"duplicated_count": 0, "tasks": [{"id": "t"}]},
            ("POST", "/v1/runs"): {"id": "run1", "status": "pending"},
        }[key])

    client = LobstrClient("https://api.lobstr.io/v1", "t",
                          transport=httpx.MockTransport(handler))
    out = run_scraper_impl(client, SETTINGS, IdempotencyStore(), input=input_dict,
                           confirm=True, squid_id="sq1")
    return out, bodies


def test_run_scraper_sends_the_params_only_key_as_a_squid_param():
    out, bodies = run_and_capture({"query": "dentists paris", "country": "France",
                                   "auto_verify_emails": True})
    assert out["run_id"] == "run1"
    # the API recognizes squid inputs only under "params"
    assert bodies[("POST", "/v1/squids/sq1")]["params"]["auto_verify_emails"] is True
    # and it must not ride along in the task row, where it would be dropped
    assert bodies[("POST", "/v1/tasks")]["tasks"] == [{"query": "dentists paris",
                                                       "country": "France"}]


def test_the_key_is_not_invented_for_a_crawler_that_does_not_advertise_it():
    # /params without it: validation lets the unknown key through (it always
    # has), but nothing in this client claims it is a squid setting.
    out = translate_input_schema(CRAWLER, params=PARAMS_WITHOUT)
    assert out["levels"].get("auto_verify_emails") is None
