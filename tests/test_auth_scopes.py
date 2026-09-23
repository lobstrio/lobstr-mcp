import pytest
from lobstr_mcp.auth.scopes import (
    check_scopes,
    expand_granted,
    ScopeError,
    ACCOUNT_READ,
    CRAWLERS_READ,
    PROFILE_READ,
    RUNS_EXECUTE,
)


def test_passes_when_all_required_granted():
    check_scopes([CRAWLERS_READ, RUNS_EXECUTE], [CRAWLERS_READ])  # no raise


def test_raises_with_missing_list():
    with pytest.raises(ScopeError) as exc:
        check_scopes([CRAWLERS_READ], [CRAWLERS_READ, RUNS_EXECUTE])
    assert exc.value.missing == [RUNS_EXECUTE]


def test_empty_granted_raises():
    with pytest.raises(ScopeError):
        check_scopes(None, [CRAWLERS_READ])


def test_legacy_account_read_implies_profile_read():
    # Tokens minted before profile:read was split out carry only account:read,
    # which used to also grant whoami / check_credits — keep them working.
    check_scopes([ACCOUNT_READ], [PROFILE_READ])  # no raise
    assert PROFILE_READ in expand_granted([ACCOUNT_READ])


def test_profile_read_does_not_imply_account_read():
    # One-directional: reading who you are must never grant reading which
    # connected platform accounts you have.
    with pytest.raises(ScopeError):
        check_scopes([PROFILE_READ], [ACCOUNT_READ])
    assert ACCOUNT_READ not in expand_granted([PROFILE_READ])
