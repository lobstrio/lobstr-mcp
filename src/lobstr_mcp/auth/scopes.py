"""OAuth scopes for the Lobstr MCP and a scope-enforcement helper.

Read-only tools require read scopes; the (P3) execute tool `run_scraper`
will require `runs:execute`. Enforcement is always server-side.
"""
from __future__ import annotations

CRAWLERS_READ = "crawlers:read"
RUNS_READ = "runs:read"
RESULTS_READ = "results:read"
PROFILE_READ = "profile:read"
ACCOUNT_READ = "account:read"
RUNS_EXECUTE = "runs:execute"

ALL_SCOPES: list[str] = [
    CRAWLERS_READ, RUNS_READ, RESULTS_READ, PROFILE_READ, ACCOUNT_READ, RUNS_EXECUTE,
]

# Per-tool required-scope groups.
#
# `profile:read` and `account:read` are deliberately distinct: your Lobstr
# identity + credit balance is not your list of connected *platform* accounts
# (LinkedIn, Facebook, …). A client can ask for one without the other, so
# reading who you are never implies reading which third-party accounts you've
# linked.
READ_SCOPES: list[str] = [CRAWLERS_READ]          # crawler/squid discovery tools
EXECUTE_SCOPES: list[str] = [RUNS_EXECUTE]        # run_scraper, abort_run, empty_scraper
RUN_READ_SCOPES: list[str] = [RUNS_READ]          # get_run, list_runs
RESULTS_READ_SCOPES: list[str] = [RESULTS_READ]   # get_results, get_results_url
PROFILE_READ_SCOPES: list[str] = [PROFILE_READ]   # whoami, check_credits
ACCOUNT_READ_SCOPES: list[str] = [ACCOUNT_READ]   # list_accounts, get_account (connected platform accounts)

# Back-compat: before `profile:read` was split out, `account:read` also granted
# the profile reads (whoami, check_credits). Tokens minted then carry only
# `account:read`, so treat it as implying `profile:read`. This is ONE-directional
# — `profile:read` never implies `account:read` — so the least-privilege win
# holds: a profile-only grant still cannot read connected platform accounts.
_IMPLIED_SCOPES: dict[str, tuple[str, ...]] = {ACCOUNT_READ: (PROFILE_READ,)}


def expand_granted(granted) -> set[str]:
    """Granted scopes plus any legacy implications (see `_IMPLIED_SCOPES`)."""
    out = set(granted or [])
    for scope, implied in _IMPLIED_SCOPES.items():
        if scope in out:
            out.update(implied)
    return out


class ScopeError(Exception):
    def __init__(self, missing) -> None:
        self.missing = list(missing)
        super().__init__("missing required scope(s): " + ", ".join(self.missing))


def check_scopes(granted, required) -> None:
    """Raise ScopeError if any of `required` is absent from `granted`."""
    granted_set = expand_granted(granted)
    missing = [s for s in required if s not in granted_set]
    if missing:
        raise ScopeError(missing)
