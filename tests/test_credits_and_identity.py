"""check_credits and whoami must not report two different numbers for one word.

The bug: the same account, seconds apart, was told `consumed: 16382.6`
by `whoami` (from `/me`'s plan list: lifetime credits minus bonus) and
`consumed: 0` by `check_credits` (from `/user/balance`, which on a daily-reset
account reports *today*). Neither endpoint is wrong; both were reported under
the same word with nothing saying which period they covered.
"""
import json

import httpx

from lobstr_mcp._version import __version__
from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.tools.user import check_credits_impl, whoami_impl


def client_for(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=routes.get(request.url.path, {}))
    return LobstrClient("https://api.lobstr.io/v1", "t",
                        transport=httpx.MockTransport(handler))


# The account from the report: daily reset, nothing spent yet today, 16382.6
# credits consumed over its lifetime, 608 slots held against a plan of 1.
DAILY_BALANCE = {
    "available": 137, "consumed": 0, "interval": "daily",
    "reset_time": "2026-09-23T00:00:00Z",
    "used_slots": 608, "total_available_slots": 1,
}
ME = {
    "first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com",
    "is_staff": True, "credit_interval": "daily",
    "plan": [
        {"type": "current", "name": "Premium", "status": "active",
         "total_credits": 100000, "total_consumed": 16382.6, "is_legacy": False},
        {"type": "upcoming", "name": "Business", "total_consumed": 0},
    ],
}


# --- check_credits: which period are these numbers about? -------------------


def test_check_credits_surfaces_the_interval_and_its_reset():
    out = check_credits_impl(client_for({"/v1/user/balance": DAILY_BALANCE}))
    assert out["interval"] == "daily"
    assert out["reset_time"] == "2026-09-23T00:00:00Z"


def test_a_daily_account_is_told_its_figures_are_todays_only():
    out = check_credits_impl(client_for({"/v1/user/balance": DAILY_BALANCE}))
    note = out["credits_note"]
    assert "TODAY" in note
    assert "Neither figure is the account balance" in note
    # reset_time on this interval is today's counter rolling over, not the
    # billing period's reset, which the payload does not carry at all
    assert "period's own reset is not reported" in note


def test_a_monthly_account_is_told_its_figures_cover_the_period():
    balance = {**DAILY_BALANCE, "interval": "monthly"}
    out = check_credits_impl(client_for({"/v1/user/balance": balance}))
    assert out["interval"] == "monthly"
    assert "billing period" in out["credits_note"]
    assert "TODAY" not in out["credits_note"]


def test_a_balance_without_an_interval_does_not_claim_a_period():
    balance = {k: v for k, v in DAILY_BALANCE.items() if k != "interval"}
    out = check_credits_impl(client_for({"/v1/user/balance": balance}))
    assert out["interval"] is None
    assert "unknown" in out["credits_note"]


def test_check_credits_still_reports_the_same_four_numbers():
    out = check_credits_impl(client_for({"/v1/user/balance": DAILY_BALANCE}))
    assert (out["available"], out["consumed"]) == (137, 0)
    assert (out["used_slots"], out["total_slots"]) == (608, 1)


# --- `available` is an allowance, not what is left --------------------------
# Measured on the test account: monthly available 3,000,000
# beside consumed 125,000, and the same account on daily reports available
# 143,750 = (3,000,000 - 125,000) / 20 days left. The monthly 3,000,000 is
# therefore gross: `available` never has `consumed` taken off it, on either
# interval, and a model budgeting against it over-budgets by `consumed`.


def test_remaining_is_what_is_actually_left_on_a_monthly_account():
    balance = {"available": 3000000, "consumed": 125000, "interval": "monthly",
               "reset_time": "2026-10-12T09:18:39",
               "used_slots": 0, "total_available_slots": 20}
    out = check_credits_impl(client_for({"/v1/user/balance": balance}))
    assert out["remaining"] == 2875000
    assert "does NOT have 'consumed' taken off it" in out["credits_note"]
    assert "remaining = available - consumed" in out["credits_note"]


def test_remaining_is_todays_headroom_on_a_daily_account():
    balance = {"available": 143750, "consumed": 12000, "interval": "daily",
               "reset_time": "2026-09-23T00:00:00",
               "used_slots": 0, "total_available_slots": 20}
    out = check_credits_impl(client_for({"/v1/user/balance": balance}))
    assert out["remaining"] == 131750
    assert "budget a run against" in out["credits_note"]


def test_remaining_is_reported_negative_not_clamped_to_zero():
    # An overspent period is real; clamping to 0 hid that from the model.
    spent = {**DAILY_BALANCE, "available": 10, "consumed": 40}
    assert check_credits_impl(client_for({"/v1/user/balance": spent}))["remaining"] == -30
    missing = {k: v for k, v in DAILY_BALANCE.items() if k != "consumed"}
    assert check_credits_impl(
        client_for({"/v1/user/balance": missing}))["remaining"] is None


def test_the_unknown_interval_note_still_warns_about_the_allowance():
    balance = {k: v for k, v in DAILY_BALANCE.items() if k != "interval"}
    note = check_credits_impl(client_for({"/v1/user/balance": balance}))["credits_note"]
    assert "unknown" in note
    assert "allowance with nothing taken off it" in note


# --- the slots pair, described as it behaves today --------------------------


def test_slots_are_described_as_reserved_by_active_scrapers_not_as_running_runs():
    out = check_credits_impl(client_for({"/v1/user/balance": DAILY_BALANCE}))
    note = out["slots_note"]
    assert "ACTIVE scrapers" in note
    assert "never when a run starts" in note


def test_a_slots_pair_over_its_own_cap_is_called_out_as_not_an_error():
    out = check_credits_impl(client_for({"/v1/user/balance": DAILY_BALANCE}))
    assert "above total_slots" in out["slots_note"]
    assert "not an error" in out["slots_note"]


def test_a_slots_pair_within_the_cap_says_nothing_about_being_over():
    balance = {**DAILY_BALANCE, "used_slots": 1, "total_available_slots": 4}
    out = check_credits_impl(client_for({"/v1/user/balance": balance}))
    assert "above total_slots" not in out["slots_note"]


# --- whoami: identity only --------------------------------------------------


def test_whoami_reports_no_credit_figure_at_all():
    out = whoami_impl(client_for({"/v1/me": ME}))
    # The lifetime figure that contradicted check_credits must not be anywhere
    # in the payload, under any key.
    assert 16382.6 not in out.values()
    assert not any(k in out for k in ("consumed", "total_consumed", "credits",
                                      "total_credits", "available"))
    # No credits_note either: it only ever repeated "check_credits reports
    # credits", not something whoami's own fields need explaining.
    assert "credits_note" not in out


def test_whoami_builds_the_name_the_api_actually_returns():
    out = whoami_impl(client_for({"/v1/me": ME}))
    assert out["name"] == "Ada Lovelace"


def test_whoami_ships_no_key_that_is_always_null():
    # `/me` returns no id and no `name`/`full_name`; keys mapped from them were
    # null for every user, which a model cannot tell from "this user set none".
    out = whoami_impl(client_for({"/v1/me": ME}))
    assert "id" not in out
    assert set(out) == {"email", "name", "is_staff", "plan", "plan_status",
                        "credit_interval", "server_version"}


def test_whoami_name_is_null_only_when_the_account_has_neither_name():
    me = {k: v for k, v in ME.items() if k not in ("first_name", "last_name")}
    out = whoami_impl(client_for({"/v1/me": me}))
    assert out["name"] is None
    me_first_only = {**me, "first_name": "Ada"}
    assert whoami_impl(client_for({"/v1/me": me_first_only}))["name"] == "Ada"


def test_whoami_reports_the_plan_in_force_not_the_whole_list():
    out = whoami_impl(client_for({"/v1/me": ME}))
    assert out["plan"] == "Premium"
    assert out["plan_status"] == "active"


def test_whoami_with_no_subscription_reports_no_plan():
    out = whoami_impl(client_for({"/v1/me": {**ME, "plan": []}}))
    assert out["plan"] is None and out["plan_status"] is None
    assert out["email"] == "ada@example.com"


def test_whoami_keeps_identity_and_the_server_version():
    out = whoami_impl(client_for({"/v1/me": ME}))
    assert out["email"] == "ada@example.com"
    assert out["is_staff"] is True
    assert out["credit_interval"] == "daily"
    assert out["server_version"] == __version__


def test_the_two_tools_no_longer_disagree_about_consumed():
    c = client_for({"/v1/user/balance": DAILY_BALANCE, "/v1/me": ME})
    credits = check_credits_impl(c)
    identity = whoami_impl(c)
    # The lifetime figure used to travel nested inside whoami's `plan` list,
    # which is how the reporter saw 16382.6 and 0 seconds apart.
    assert "16382.6" not in json.dumps(identity)
    assert [v for k, v in {**credits, **identity}.items() if k == "consumed"] == [0]
