from lobstr_mcp.schema_translator import translate_input_schema


CRAWLER = {
    "input": [
        {"name": "days_back", "type": "int", "level": "cluster",
         "default": 7, "required": False, "description": "How many days"},
        {"name": "url", "type": "string", "level": "task",
         "required": True, "example": "https://x", "description": "Target URL"},
        {"name": "verified", "type": "bool", "level": "task", "required": False},
        {"name": "weird", "type": "mystery", "level": "task", "required": False},
    ]
}


def test_maps_types_and_required():
    out = translate_input_schema(CRAWLER)
    schema = out["json_schema"]
    assert schema["type"] == "object"
    assert schema["properties"]["days_back"]["type"] == "integer"
    assert schema["properties"]["url"]["type"] == "string"
    assert schema["properties"]["verified"]["type"] == "boolean"
    assert schema["properties"]["weird"]["type"] == "string"
    assert schema["required"] == ["url"]


def test_required_field_with_a_default_is_not_required():
    # A field the crawler marks required AND gives a default for isn't
    # actually mandatory: the default fills it when omitted. Google Maps'
    # `language` is exactly this shape — required=True, default="en" — and
    # was listed in the JSON schema's `required`, which told a model to
    # always pass it even though the crawler runs fine without it.
    crawler = {"input": [
        {"name": "url", "type": "string", "level": "task", "required": True},
        {"name": "language", "type": "string", "level": "squid",
         "required": True, "default": "en"},
    ]}
    out = translate_input_schema(crawler)
    schema = out["json_schema"]
    assert "language" not in schema["required"]
    assert schema["required"] == ["url"]
    # still published, with its default, so a model reading the schema can
    # see and override it
    assert schema["properties"]["language"]["default"] == "en"


GM_GROUPED = {"input": [
    {"name": "url", "type": "string", "level": "task", "required": True, "group": "url"},
    {"name": "category", "type": "string", "level": "task", "required": True, "group": "location"},
    {"name": "country", "type": "string", "level": "task", "required": True, "group": "location"},
    {"name": "city", "type": "string", "level": "task", "required": True, "group": "location"},
    {"name": "region", "type": "string", "level": "task", "required": False, "group": "location"},
    {"name": "language", "type": "string", "level": "squid", "required": True},
]}


def test_grouped_inputs_become_alternative_modes():
    out = translate_input_schema(GM_GROUPED)
    m = out["input_modes"]
    assert m is not None
    assert ["url"] in m["either"]
    assert ["category", "country", "city"] in m["either"]  # required ones only (region excluded)
    assert m["always"] == ["language"]
    # schema hard-requires only the always fields, not the grouped alternatives
    assert out["json_schema"]["required"] == ["language"]


def test_no_modes_without_multiple_required_groups():
    assert translate_input_schema(CRAWLER)["input_modes"] is None


def test_carries_description_default_example_and_levels():
    out = translate_input_schema(CRAWLER)
    props = out["json_schema"]["properties"]
    assert props["days_back"]["default"] == 7
    assert props["days_back"]["description"] == "How many days"
    assert props["url"]["examples"] == ["https://x"]
    assert out["levels"] == {
        "days_back": "cluster", "url": "task",
        "verified": "task", "weird": "task",
    }


def test_empty_input_is_valid_empty_schema():
    out = translate_input_schema({"input": []})
    assert out["json_schema"] == {"type": "object", "properties": {}, "required": []}
    assert out["levels"] == {}


# --- live input[] quirks (found in P4 live testing) ---------------------------

def test_duplicate_name_across_levels_keeps_the_required_task_param():
    """The live Google Maps crawler lists `country` twice: a required task-level
    param and an optional squid-level setting with a default. Last-wins made
    param_levels['country'] == 'squid', so run_scraper would have sent a
    required task field as a squid setting."""
    crawler = {"input": [
        {"name": "url", "type": "string", "level": "task", "required": True},
        {"name": "country", "type": "string", "level": "task", "required": True},
        {"name": "country", "type": "string", "level": "squid", "required": False,
         "default": "United States"},
    ]}
    out = translate_input_schema(crawler)
    assert out["levels"]["country"] == "task"
    assert "country" in out["json_schema"]["required"]
    assert out["json_schema"]["required"].count("country") == 1, "no duplicates"
    assert "default" not in out["json_schema"]["properties"]["country"]


def test_duplicate_name_same_level_keeps_first():
    crawler = {"input": [
        {"name": "x", "type": "string", "level": "squid", "required": False,
         "description": "first"},
        {"name": "x", "type": "int", "level": "squid", "required": False,
         "description": "second"},
    ]}
    out = translate_input_schema(crawler)
    assert out["json_schema"]["properties"]["x"]["description"] == "first"


# --- function toggles live under params.functions (found in P4 live testing) --
# GET /crawlers/{h}/params is authoritative about where a squid input goes: its
# squid.functions dict names the toggles the API only accepts nested. The
# translator previously ignored /params entirely and trusted input[]'s
# level=squid, so run_scraper sent extract_emails_from_website as a top-level
# squid param and the API rejected the whole run with
# InvalidParam "The specified parameter extract_emails_from_website is invalid."

GM_INPUT = {"input": [
    {"name": "url", "type": "string", "level": "task", "required": True},
    {"name": "city", "type": "string", "level": "task", "required": True},
    {"name": "max_results", "type": "int", "level": "squid", "default": 200},
    {"name": "geo_match", "type": "boolean", "level": "squid", "default": True},
    {"name": "extract_emails_from_website", "type": "boolean", "level": "squid",
     "default": True},
    {"name": "collect_business_details", "type": "boolean", "level": "squid",
     "default": False},
]}

GM_PARAMS = {
    "task": {"url": "string", "city": "string"},
    "squid": {
        "max_results": "int",
        "geo_match": "boolean",
        "functions": {
            "extract_emails_from_website": {"default": True},
            "collect_business_details": {"default": False},
            "fetch_business_images": {"default": False},
        },
    },
}


def test_function_toggles_are_marked_as_function_level():
    out = translate_input_schema(GM_INPUT, params=GM_PARAMS)
    lv = out["levels"]
    assert lv["extract_emails_from_website"] == "function"
    assert lv["collect_business_details"] == "function"
    assert lv["max_results"] == "squid"
    assert lv["geo_match"] == "squid"
    assert lv["url"] == "task"
    assert lv["city"] == "task"


def test_function_toggles_still_appear_in_the_schema():
    """The model must still be able to set them — only the placement changes."""
    out = translate_input_schema(GM_INPUT, params=GM_PARAMS)
    props = out["json_schema"]["properties"]
    assert props["extract_emails_from_website"]["type"] == "boolean"


def test_params_are_optional_and_input_levels_are_the_fallback():
    out = translate_input_schema(GM_INPUT)
    assert out["levels"]["extract_emails_from_website"] == "squid"
    assert out["levels"]["url"] == "task"


def test_params_override_a_disagreeing_input_level():
    """/params wins: it is what the API validates against."""
    crawler = {"input": [
        {"name": "max_results", "type": "int", "level": "task", "default": 5},
    ]}
    out = translate_input_schema(crawler, params={"task": {}, "squid": {"max_results": "int"}})
    assert out["levels"]["max_results"] == "squid"
