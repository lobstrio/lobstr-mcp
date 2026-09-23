from lobstr_mcp.errors import LobstrAPIError, to_error_dict


def test_known_upstream_type_maps_to_an_actionable_code():
    out = to_error_dict(LobstrAPIError(400, error_type="SquidNotReady",
                                       message="Squid not ready"))
    assert out["error_code"] == "scraper_not_ready"
    assert out["upstream_status"] == 400
    assert out["hint"]


def test_status_fallbacks():
    assert to_error_dict(LobstrAPIError(429))["error_code"] == "rate_limited"
    assert to_error_dict(LobstrAPIError(401))["error_code"] == "unauthorized"
    assert to_error_dict(LobstrAPIError(404))["error_code"] == "not_found"
    assert to_error_dict(LobstrAPIError(502))["error_code"] == "upstream_unavailable"


def test_message_is_always_present():
    assert to_error_dict(LobstrAPIError(500))["message"]


def test_slots_limit_exceeded_hints_deactivation():
    out = to_error_dict(LobstrAPIError(400, error_type="SlotsLimitExceeded",
                                       message="no free slots"))
    assert out["error_code"] == "slots_limit_exceeded"
    assert "deactivate_scraper" in out["hint"]
