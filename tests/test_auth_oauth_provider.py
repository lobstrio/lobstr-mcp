from lobstr_mcp.auth.oauth_provider import (
    build_client_registration_options,
    REQUIRED_PROVIDER_METHODS,
)
from lobstr_mcp.auth.scopes import ALL_SCOPES


def test_dcr_options_enabled_with_scopes():
    opts = build_client_registration_options()
    assert opts.enabled is True
    assert set(opts.valid_scopes) == set(ALL_SCOPES)
    assert set(opts.default_scopes) == set(ALL_SCOPES)


def test_required_provider_methods_documented():
    for m in ("authorize", "exchange_authorization_code",
              "register_client", "load_access_token", "revoke_token"):
        assert m in REQUIRED_PROVIDER_METHODS
