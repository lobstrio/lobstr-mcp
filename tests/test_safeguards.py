from lobstr_mcp.safeguards import (
    validate_input, estimate_cost, compute_idempotency_key, IdempotencyStore,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "pages": {"type": "integer"},
    },
    "required": ["query"],
}


def test_validate_missing_required():
    assert validate_input({}, SCHEMA) == ["query is required"]


def test_validate_wrong_type():
    errs = validate_input({"query": "x", "pages": "lots"}, SCHEMA)
    assert errs == ["pages must be integer"]


def test_validate_ok():
    assert validate_input({"query": "x", "pages": 3}, SCHEMA) == []


def test_estimate_cost_unknown_by_default():
    est = estimate_cost({"name": "GM"})
    assert est.credits is None
    assert "unknown" in est.basis


def test_estimate_cost_from_numeric_hint():
    est = estimate_cost({"credits_per_task": 5}, task_count=3)
    assert est.credits == 15.0


def test_idempotency_key_is_stable_and_order_insensitive():
    a = compute_idempotency_key("user1", "gm", {"query": "x", "pages": 2})
    b = compute_idempotency_key("user1", "gm", {"pages": 2, "query": "x"})
    c = compute_idempotency_key("user1", "gm", {"query": "y"})
    assert a == b and a != c


def test_idempotency_key_is_scoped_per_user():
    # Same scraper, same input, different callers: must NOT collide, or one
    # user's run comes back to another user as `already_submitted`.
    a = compute_idempotency_key("user1", "gm", {"query": "x"})
    b = compute_idempotency_key("user2", "gm", {"query": "x"})
    assert a != b


def test_idempotency_store():
    s = IdempotencyStore()
    assert s.get("k") is None
    s.put("k", "run1")
    assert s.get("k") == "run1"


def test_idempotency_store_entry_expires():
    s = IdempotencyStore()
    s.put("k", "run1", ttl=-1)  # already expired
    assert s.get("k") is None


def test_idempotency_store_default_ttl_is_not_forever():
    from lobstr_mcp.safeguards import DEFAULT_IDEMPOTENCY_TTL
    assert DEFAULT_IDEMPOTENCY_TTL is not None and DEFAULT_IDEMPOTENCY_TTL > 0


def test_estimate_cost_reports_the_per_row_rate():
    """Live crawlers price per row (credits_per_row); the total is unknowable
    before a run, but the rate is real information for the model."""
    est = estimate_cost({"name": "GM", "credits_per_row": 1}, task_count=2)
    assert est.credits is None, "total still depends on rows produced"
    assert est.rate == 1.0
    assert "per row" in est.basis


def test_estimate_cost_upper_bound_when_capped():
    """With a per-run result cap and a known per-row rate, we can give a real
    upper bound (cap x rate) instead of None."""
    est = estimate_cost({"credits_per_row": 1}, task_count=1, max_results_per_task=3)
    assert est.credits == 3.0
    assert est.rate == 1.0
    assert "up to" in est.basis


def test_estimate_cost_ignores_non_positive_cap():
    # empty-string / 0 caps (as the API sometimes stores) must not break it
    assert estimate_cost({"credits_per_row": 1}, max_results_per_task="").credits is None
    assert estimate_cost({"credits_per_row": 1}, max_results_per_task=0).credits is None


def test_validate_input_accepts_either_alternative_group():
    schema = {"type": "object", "properties": {}, "required": ["language"]}
    modes = {"either": [["url"], ["category", "country", "city"]], "always": ["language"]}
    assert validate_input({"url": "x", "language": "en"}, schema, input_modes=modes) == []
    assert validate_input({"category": "c", "country": "co", "city": "ci", "language": "en"},
                          schema, input_modes=modes) == []
    errs = validate_input({"category": "c"}, schema, input_modes=modes)
    assert any("input sets" in e for e in errs)
    assert any("language is required" in e for e in errs)


def test_estimate_cost_still_unknown_with_no_pricing_fields():
    est = estimate_cost({"name": "GM"})
    assert est.credits is None and est.rate is None
    assert "unknown" in est.basis


def test_credit_rate_resolves_the_live_legacy_current_dict():
    """Live crawlers expose credits_per_row as {"legacy": n, "current": m};
    the current rate is what a run is billed at."""
    from lobstr_mcp.safeguards import resolve_credit_rate
    assert resolve_credit_rate({"legacy": 1, "current": 2}) == 2.0
    assert resolve_credit_rate({"legacy": 5}) == 5.0
    assert resolve_credit_rate(3) == 3.0
    assert resolve_credit_rate(None) is None
    assert resolve_credit_rate({}) is None


def test_estimate_cost_reads_the_dict_shaped_rate():
    est = estimate_cost({"name": "GM", "credits_per_row": {"legacy": 1, "current": 2}})
    assert est.rate == 2.0
    assert "per row" in est.basis
