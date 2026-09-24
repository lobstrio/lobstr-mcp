from __future__ import annotations

_TYPE_MAP = {
    "int": "integer", "integer": "integer",
    "float": "number", "number": "number",
    "bool": "boolean", "boolean": "boolean",
    "string": "string", "str": "string",
    "list": "array", "array": "array",
    "dict": "object", "object": "object",
}

# "cluster" is the API's older internal name for a squid-level input; the two
# mean the same placement and execution.py routes both the same way.
_SQUID_LEVEL_NAMES = ("squid", "cluster")


def _normalized_level(level: str | None) -> str:
    return "squid" if level in _SQUID_LEVEL_NAMES else (level or "task")


def _input_modes(items: list[dict]) -> dict | None:
    """Derive alternative input modes from the inputs' authoritative `group`.

    Some crawlers (e.g. Google Maps) split required inputs into groups — `url`
    in group "url", `category`/`country`/`city` in group "location" — where you
    supply the fields of ONE group, not all. Ungrouped required fields are
    always required — unless they also carry a `default`, in which case, same
    as the main schema's `required`, the default fills them when omitted and
    they don't belong in `always` either. Only kicks in when 2+ groups each
    carry a required field; otherwise there are no alternatives to express.
    """
    groups: dict[str, list[str]] = {}
    always: list[str] = []
    for it in items:
        if not it.get("required"):
            continue
        name, group = it["name"], it.get("group")
        if not group and "default" in it:
            continue
        if group:
            groups.setdefault(group, []).append(name)
        else:
            always.append(name)

    either = [fields for fields in groups.values() if fields]
    if len(either) < 2:
        return None

    desc = "Provide the fields of ONE of these groups: " + " OR ".join(
        "(" + " + ".join(g) + ")" for g in either)
    if always:
        desc += ". Always also provide: " + ", ".join(always) + "."
    return {"either": either, "always": always, "description": desc}


def _levels_from_params(params: dict | None) -> dict[str, set[str]]:
    """Map input name -> every level GET /crawlers/{hash}/params places it at.

    build.py writes `public_params[level][name]` straight from lobstr.json with
    no dedup across levels, so a crawler that declares one name twice on purpose
    lands in BOTH sections. The live Google Maps crawler does exactly that with
    `country`: the task-level one goes into the search URL path, the squid-level
    one sets Google's `gl=` region. Everything that has to tell two same-named
    inputs apart starts from this map.
    """
    if not isinstance(params, dict):
        return {}
    at: dict[str, set[str]] = {}
    task = params.get("task")
    if isinstance(task, dict):
        for name in task:
            at.setdefault(name, set()).add("task")
    squid = params.get("squid")
    if isinstance(squid, dict):
        for name, spec in squid.items():
            if name == "functions":
                if isinstance(spec, dict):
                    for fname in spec:
                        at.setdefault(fname, set()).add("function")
                continue
            at.setdefault(name, set()).add("squid")
    return at


def _placement_from_params(params: dict | None) -> dict[str, str]:
    """Map input name -> "task" | "squid" | "function" using the authoritative
    GET /crawlers/{hash}/params payload.

    Its squid.functions dict names the toggles the API accepts ONLY nested
    under params.functions; sending one as a top-level squid param is rejected
    outright with InvalidParam, which fails the whole run. input[] marks them
    all as plain level=squid, so /params is the only reliable discriminator.

    A name listed in more than one section is a collision, not a contradiction,
    and the task claim wins. The API builds a crawler's required-group check
    from `public_params["task"]` alone and nothing in that path consults the
    squid, so a required task field routed to the squid instead comes back as
    "Missing required parameters". Letting the squid loop overwrite here is
    what did that to `country`. The shadowed entry is not lost:
    translate_input_schema publishes it under an alias.
    """
    placement: dict[str, str] = {}
    for name, at in _levels_from_params(params).items():
        for level in ("task", "function", "squid"):
            if level in at:
                placement[name] = level
                break
    return placement


_DEFAULT_TYPES = ((bool, "boolean"), (int, "integer"), (float, "number"),
                  (str, "string"), (list, "array"), (dict, "object"))


def _params_type(spec: dict) -> str | None:
    """The JSON-schema type of a /params spec: its own `type` when it states
    one, else the type of its `default`, else none at all.

    A property with no `type` is still valid JSON Schema and still settable —
    validate_input simply has nothing to check it against. That beats guessing
    "string" for a toggle whose default is False and then rejecting `true`.
    """
    declared = spec.get("type")
    if isinstance(declared, str) and declared in _TYPE_MAP:
        return _TYPE_MAP[declared]
    default = spec.get("default")
    if default is not None:
        for py_type, json_type in _DEFAULT_TYPES:
            if isinstance(default, py_type):
                return json_type
    return None


def _params_only_specs(params: dict | None) -> dict[str, dict]:
    """Every input the /params squid section describes, name -> its spec.

    A crawler's `input[]` is also what the dashboard renders as a form, so a
    setting the dashboard already draws its own control for is deliberately
    kept out of it — `auto_verify_emails`, advertised for crawlers whose module
    has email verification, is the first of them and stays out on purpose.
    /params is authoritative about what the API accepts, so a key it lists that
    `input[]` omits is still a real, settable squid input. Without a property
    for it there is no type to validate, run_scraper cannot classify it as
    squid-level, and it lands in the task row instead — where the setting is
    silently dropped and the run still bills.

    Name-agnostic by design: the API will advertise more of these, and a check
    on a specific field name would have to be edited every time one lands. The
    payload carries the type, default and description already.

    Function toggles (nested under squid.functions) are included: they are
    squid inputs too, and _placement_from_params levels them correctly.
    """
    if not isinstance(params, dict):
        return {}
    squid = params.get("squid")
    if not isinstance(squid, dict):
        return {}
    specs: dict[str, dict] = {}
    for name, spec in squid.items():
        if name == "functions":
            if isinstance(spec, dict):
                for fname, fspec in spec.items():
                    if isinstance(fspec, dict):
                        specs[fname] = fspec
            continue
        # A bare value instead of a spec dict describes nothing we could
        # validate or explain, so it is left alone rather than guessed at.
        if isinstance(spec, dict):
            specs[name] = spec
    return specs


def _property_from_item(item: dict) -> dict:
    """The JSON-schema property for one crawler input[] entry."""
    prop: dict = {"type": _TYPE_MAP.get(item.get("type", "string"), "string")}
    if "description" in item:
        prop["description"] = item["description"]
    if "default" in item:
        prop["default"] = item["default"]
    if "example" in item:
        prop["examples"] = [item["example"]]
    return prop


def _alias_name(level: str, name: str, taken) -> str:
    """The MCP-side name for an input whose plain name a same-named input at
    another level kept: its level, an underscore, then the name the API knows —
    `squid_country` for the squid-level `country`.

    Mechanical on purpose. It matches no particular field, so the next crawler
    that declares a name twice is handled with no code change, and it invents
    no meaning: the prefix is the level the caller already reads out of
    param_levels, the rest is the API's own name for the field. The `_2`, `_3`
    tail only ever fires if a crawler itself publishes an input already called
    `<level>_<name>`.
    """
    base = f"{_normalized_level(level)}_{name}"
    candidate, n = base, 1
    while candidate in taken:
        n += 1
        candidate = f"{base}_{n}"
    return candidate


_LEVEL_BLURB = {
    "task": "one value per input row you scrape",
    "squid": "one value for the whole scraper",
    "function": "one value for the whole scraper, an optional extra step",
}


def _alias_description(alias: str, name: str, level: str, item: dict,
                       other_level: str, other_item: dict) -> str:
    """Say which of the two same-named inputs this one is, in the schema
    itself. A model reading the properties is given no other clue that the two
    are different fields, and picking the wrong one is not a validation error:
    it is a run that searches for, or is served from, somewhere else entirely.
    """
    own = (item.get("description") or "").strip().rstrip(".")
    other = (other_item.get("description") or "").strip().rstrip(".")
    mine, theirs = _normalized_level(level), _normalized_level(other_level)
    text = (f'This scraper has two different inputs both named "{name}". '
            f'This is the {mine}-level one ({_LEVEL_BLURB.get(mine, "")})')
    text += f": {own}. " if own else ". "
    text += (f'The other one is {theirs}-level '
             f'({_LEVEL_BLURB.get(theirs, "")})')
    text += f": {other}. " if other else ". "
    text += (f'That one keeps the plain name "{name}"; set this one as '
             f'"{alias}" instead. run_scraper sends it to the API under its '
             f'real name "{name}" — the alias exists only here.')
    return text


def _shadowed_level(name: str, shadowed_item: dict, kept_item: dict,
                    params_levels: dict[str, set[str]]) -> str:
    """Where the shadowed entry goes. /params first: the sections it lists the
    name in, minus the one the kept entry took, leave exactly one answer for a
    two-way collision — and /params is the only source that knows a squid input
    is really a function toggle. Otherwise the entry's own declared level.
    """
    candidates = params_levels.get(name, set()) - {
        _normalized_level(kept_item.get("level"))}
    if len(candidates) == 1:
        return next(iter(candidates))
    return shadowed_item.get("level") or "squid"


def translate_input_schema(crawler: dict, params: dict | None = None) -> dict:
    properties: dict = {}
    required: list[str] = []
    levels: dict[str, str] = {}
    wire_names: dict[str, str] = {}
    # required fields with a default: optional for the model, but the API
    # won't apply the default, so callers send it
    defaults_to_fill: dict = {}

    # A crawler's input[] can list the same name twice at different levels —
    # the live Google Maps scraper has a required task-level `country` and an
    # optional squid-level `country`. Blind assignment let the later entry win,
    # which sent a required task field to the squid level. Keep the entry that
    # is required (task level breaking the tie), otherwise the first seen.
    def _outranks(new_item: dict, old_item: dict) -> bool:
        new_req, old_req = bool(new_item.get("required")), bool(old_item.get("required"))
        if new_req != old_req:
            return new_req
        return False

    placement = _placement_from_params(params)
    params_levels = _levels_from_params(params)
    seen: dict[str, dict] = {}
    # name -> the entry the tie-break did NOT keep, when the two sit at
    # different levels and are therefore two different inputs that happen to
    # share a name (Google Maps' `country`). The same level twice is one input
    # written twice: keep the first, as before, and publish nothing extra.
    shadowed: dict[str, dict] = {}
    for item in crawler.get("input", []):
        name = item["name"]
        previous = seen.get(name)
        if previous is None:
            seen[name] = item
            continue
        kept, dropped = ((item, previous) if _outranks(item, previous)
                         else (previous, item))
        seen[name] = kept
        if _normalized_level(dropped.get("level")) != _normalized_level(kept.get("level")):
            shadowed[name] = dropped

    for name, item in seen.items():
        properties[name] = _property_from_item(item)
        # grouped (input_modes) fields keep their group semantics
        if item.get("required"):
            if "default" in item and not item.get("group"):
                defaults_to_fill[name] = item["default"]
            else:
                required.append(name)
        if name in shadowed:
            # /params lists this name in two sections at once, so it cannot say
            # where THIS entry goes; the tie-break above already decided, and
            # its answer stands. (Without this the squid section overwrote the
            # task placement and a required task field went out as a squid
            # setting, which the API answers with "Missing required
            # parameters".)
            levels[name] = item.get("level", "task")
        else:
            levels[name] = placement.get(name) or item.get("level", "task")

    # Then the inputs only /params knows about. Strictly additive: a name
    # input[] already defines is skipped entirely, so neither its property nor
    # its level is touched here. The tie-break above still decides which
    # duplicate `country` entry becomes the property, and _placement_from_params
    # still decides every level, exactly as they did before this loop existed.
    for name, spec in _params_only_specs(params).items():
        if name in properties:
            continue
        prop: dict = {}
        json_type = _params_type(spec)
        if json_type:
            prop["type"] = json_type
        description = spec.get("description")
        if isinstance(description, str) and description:
            prop["description"] = description
        if "default" in spec:
            prop["default"] = spec["default"]
        properties[name] = prop
        # placement has an entry for every key in this section; the fallback is
        # only for a shape we did not anticipate.
        levels[name] = placement.get(name) or "squid"
        # Deliberately not added to `required`: these describe settings the
        # crawler's own input list does not ask for, and making a run refuse
        # without one would be this client inventing a rule. A key the API
        # genuinely needs is still refused upstream, with its own message.

    # Last, the entries a same-named input shadowed above. Dropping one used to
    # be the only outcome and it costs a real setting — the squid-level
    # `country` is Google's region, and losing it falls back to gl=US silently,
    # which changes which businesses come back. Each gets a distinct MCP-side
    # name here; wire_names maps it to the name the API knows, which is the
    # only name that ever goes out (see execution.py's run_scraper). Placed
    # after both loops so every already-published name is taken.
    for name, item in shadowed.items():
        level = _shadowed_level(name, item, seen[name], params_levels)
        alias = _alias_name(level, name, properties)
        prop = _property_from_item(item)
        prop["description"] = _alias_description(
            alias, name, level, item, levels[name], seen[name])
        properties[alias] = prop
        levels[alias] = level
        wire_names[alias] = name
        # Never added to `required`, nor to input_modes: the requirement
        # belongs to the entry that kept the plain name, and asking for both
        # would make the caller supply the same field twice.

    # Inputs split across 2+ `group`s (e.g. url vs location) are alternative
    # modes: supply one group, not all. Surface that as input_modes and drop the
    # grouped alternatives from the schema's hard-required set (keep only the
    # always-required ungrouped ones), so the model isn't told to supply all.
    modes = _input_modes(list(seen.values()))
    schema_required = list(modes["always"]) if modes else required

    out = {
        "json_schema": {"type": "object", "properties": properties,
                        "required": schema_required},
        "levels": levels,
        "input_modes": modes,
    }
    # Only present when there is one, so a crawler with no colliding names
    # answers exactly what it answered before. Read it as
    # `.get("wire_names") or {}`.
    if wire_names:
        out["wire_names"] = wire_names
    if defaults_to_fill:
        out["defaults_to_fill"] = defaults_to_fill
    return out
