"""User tools: the authenticated user's credit balance and identity.

NB: "accounts" in Lobstr means the platform logins a scraper authenticates with
(Facebook, LinkedIn, …) — the SDK's `accounts` resource — not this. These tools
are about the Lobstr *user*, so they live here, not under an "account" name.

One number, one tool. `GET /user/balance` and `GET /me` both report something
called "consumed" and they are not the same quantity: balance's is the current
period (today, on a daily-reset account), `/me`'s is lifetime minus bonus
credits. Reading both put "16382.6" and "0" in front of the same agent seconds
apart. So `check_credits` is the only tool here that reports
credits, and it says which period its figures cover; `whoami` carries identity
and the plan name and no credit figure at all.
"""
from __future__ import annotations

from lobstr_mcp._version import __version__
from lobstr_mcp.auth.scopes import PROFILE_READ_SCOPES
from lobstr_mcp.errors import structured
from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.render import toon_result

# What the model is told about the period the figures cover. `interval` comes
# straight from the API (`user.reset_interval`); only "daily" takes the
# per-day branch in the balance view, every other value is the billing period.
_DAILY_NOTE = (
    "interval=daily: 'available' is TODAY's allowance (the period's credits "
    "minus everything spent in it, divided by the days left) and 'consumed' is "
    "what has been spent TODAY. 'available' does NOT have 'consumed' taken off "
    "it: what can still be spent today is 'remaining', and budget a run against "
    "that. Neither figure is the account balance, and 'consumed' reads 0 for "
    "the rest of a day on an account that has spent a great deal in the period "
    "(measured: consumed 0 beside 125,000 spent in the period). reset_time is "
    "when TODAY's counter rolls over; the period's own reset is not reported on "
    "this interval."
)
_PERIOD_NOTE = (
    "interval={interval}: 'available' is the current billing period's whole "
    "allowance and 'consumed' is what has been spent in it — 'available' does "
    "NOT have 'consumed' taken off it, so what can still be spent is "
    "'remaining'. The period resets at reset_time."
)
_UNKNOWN_INTERVAL_NOTE = (
    "The API reported no interval for this account, so what period 'available' "
    "and 'consumed' cover is unknown; do not present them as the account "
    "balance. 'available' is an allowance with nothing taken off it, so what "
    "can still be spent is 'remaining'."
)
_REMAINING_NOTE = (
    "remaining = available - consumed, the figure to budget a run against; 0 "
    "means nothing is left, and it can go negative (the account is over its "
    "period's allowance, not merely at zero)."
)

_SLOTS_NOTE = (
    "used_slots is the concurrency reserved by this account's ACTIVE scrapers "
    "(the sum of their concurrency, recomputed on every call); total_slots is "
    "what the plan allows. The cap is checked only when a scraper is created, "
    "activated or has its concurrency raised — never when a run starts."
)
_SLOTS_OVER_NOTE = (
    " used_slots is above total_slots here, which happens after a plan change "
    "and on staff accounts, which skip the check; on its own it is not an error "
    "and it does not stop the scrapers that already exist from running."
)


def _credits_note(interval: str | None) -> str:
    if interval is None:
        note = _UNKNOWN_INTERVAL_NOTE
    elif str(interval).lower() == "daily":
        note = _DAILY_NOTE
    else:
        note = _PERIOD_NOTE.format(interval=interval)
    return f"{note} {_REMAINING_NOTE}"


def _remaining(available, consumed):
    """What can still be spent, which is not what `available` reports.

    `GET /user/balance` builds `available` from the plan's credits plus every
    bonus, and takes the period's spend off it only on the daily branch, to
    size one day's slice (`UserBalanceView`: daily `available =
    (credits_with_bonus - total_consumed) / days_left`, every other interval
    `available = credits_with_bonus`). `consumed` is then the period's spend on
    a monthly account and the day's spend on a daily one — in both cases a
    figure `available` has NOT had taken off it. Measured on a monthly test
    account: available 3,000,000 beside consumed 125,000, and the same account
    flipped to daily reports available 143,750 = (3,000,000 - 125,000) / 20
    days left, which is only arithmetic if the monthly 3,000,000 is gross.

    So a model reading `available` as "what I can spend" over-budgets by
    `consumed` every time. The API's own pre-run gate does this subtraction
    (`ClusterEstimationView`: `available_credits = available - consumed`); this
    reports the same quantity instead of leaving it to be inferred. Null when
    either figure is missing, rather than guessing one to be zero.

    Reported as-is, never clamped to 0: a negative figure is real (an
    overspent period) and clamping it hid that from the model.
    """
    if not isinstance(available, (int, float)) or isinstance(available, bool):
        return None
    if not isinstance(consumed, (int, float)) or isinstance(consumed, bool):
        return None
    return available - consumed


def _slots_note(used, total) -> str:
    note = _SLOTS_NOTE
    if isinstance(used, (int, float)) and isinstance(total, (int, float)) and used > total:
        note += _SLOTS_OVER_NOTE
    return note


@structured
def check_credits_impl(client: LobstrClient) -> dict:
    b = client.get_balance()
    interval = b.get("interval")
    used_slots = b.get("used_slots")
    total_slots = b.get("total_available_slots")
    available, consumed = b.get("available"), b.get("consumed")
    return {
        "available": available,
        "consumed": consumed,
        "remaining": _remaining(available, consumed),
        "interval": interval,
        "reset_time": b.get("reset_time"),
        "credits_note": _credits_note(interval),
        "used_slots": used_slots,
        "total_slots": total_slots,
        "slots_note": _slots_note(used_slots, total_slots),
    }


def _current_plan(plans) -> dict:
    """The one entry of `/me`'s `plan` list that describes the plan in force.

    `/me` returns a list: a "current" entry when a subscription is active, plus
    an "upcoming" one when a schedule changes it later. Anything else (a bare
    string from an older shape, no plan at all) yields no plan name.
    """
    if not isinstance(plans, list):
        return {}
    entries = [p for p in plans if isinstance(p, dict)]
    for p in entries:
        if p.get("type") == "current":
            return p
    return entries[0] if entries else {}


@structured
def whoami_impl(client: LobstrClient) -> dict:
    u = client.whoami()
    # `/me` has no id and no name field: it returns first_name and last_name.
    name = " ".join(
        part for part in (u.get("first_name"), u.get("last_name")) if part
    ).strip() or None
    plan = _current_plan(u.get("plan"))
    return {
        "email": u.get("email"),
        "name": name,
        "is_staff": u.get("is_staff"),
        "plan": plan.get("name"),
        "plan_status": plan.get("status"),
        "credit_interval": u.get("credit_interval"),
        "credits_note": (
            "No credit figure is reported here; check_credits is the only tool "
            "that reports credits and slots. credit_interval says which period "
            "those figures will cover ('daily' = today only)."
        ),
        "server_version": __version__,
    }


def register_user_tools(mcp, client_factory, authorizer=None) -> None:
    def authz(scopes):
        if authorizer:
            authorizer(scopes)

    @mcp.tool(annotations={"title": "Check Credits", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def check_credits(toon: bool = False) -> dict:
        """Credits and concurrency slots for the authenticated Lobstr account.
        The only tool that reports credits; nothing else returns a credit figure.

        **Budget a run against `remaining`, not `available`.** `available` is an
        allowance with nothing taken off it; `remaining` is
        `available - consumed`, which is what can still be spent. Not floored
        at 0: a negative `remaining` means the account is over its period's
        allowance, not merely "nothing left".

        What period the three figures cover depends on `interval`, and
        `reset_time` is when that counter rolls over:

        - `interval="daily"`: `available` is TODAY's allowance (the period's
          credits minus everything spent in it, divided by the days left) and
          `consumed` is what has been spent TODAY, so `remaining` is today's
          headroom. Neither `available` nor `consumed` is the account balance:
          an account that has spent 125,000 in its period still reads
          `consumed: 0` for the rest of a day on which it has spent nothing.
          Say "today" when reporting any of the three. `reset_time` here is
          when today's counter rolls over; the billing period's own reset is
          not reported on this interval.
        - any other interval: all three are the current billing period's
          figures, and `reset_time` is when the period rolls over.

        No lifetime total is reported. `credits_note` in the response repeats
        which case applies.

        `used_slots` is the concurrency reserved by this account's ACTIVE
        scrapers (the sum of their concurrency), `total_slots` what the plan
        allows. The cap is checked when a scraper is created, activated or has
        its concurrency raised — never when a run starts. So the pair can read
        over itself (`used_slots: 608` against `total_slots: 1`) after a plan
        change or on a staff account, which skips the check; that on its own is
        not an error, and the scrapers that already exist keep running. When the
        check does apply and the cap is reached, it is the create or activate
        call that fails and says so.

        Returns JSON; pass toon=true for compact TOON.
        """
        authz(PROFILE_READ_SCOPES)
        out = check_credits_impl(client_factory())
        return toon_result(out) if toon else out

    @mcp.tool(annotations={"title": "Who Am I", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def whoami() -> dict:
        """Who the authenticated Lobstr user is, and the version of this MCP
        server: `email`, `name` (built from the account's first and last name,
        null when neither is set), `is_staff`, the plan in force (`plan`,
        `plan_status`), `credit_interval` and `server_version`.

        Carries no credit figure. Credits and concurrency slots come from
        `check_credits` alone, so two tools cannot report different numbers for
        the same word; `credit_interval` says which period those figures will
        cover ("daily" = today only, anything else = the billing period).
        """
        authz(PROFILE_READ_SCOPES)
        return whoami_impl(client_factory())
