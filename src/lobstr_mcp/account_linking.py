"""Shared logic for linking a connected platform account to a squid.

The only attach path on the Lobstr API is ``POST /v1/squids/{hash}`` with
``{"accounts": [...]}`` — and that field is **full-replace**: the API deletes
every existing link before writing the new list. Every helper here reads the
squid's current accounts first and works from that, so a caller can never be
silently unlinked from an account it already had. That said, the read and the
write are two separate requests, not a compare-and-set — a concurrent change
to the squid between them (another call, the dashboard) can still race. See
each tool's docstring.

Auto-pick (choosing an account without being told which one) only fires when
the choice is unambiguous, mirroring the condition the run worker itself uses
to decide an account is usable: exact
account-type slug match, ``status == "200"``, not ``cookies_expired``, and not
presently locked — and only when exactly one such account exists. A locked
account is reported separately from there being none at all (see
``find_candidates``): locks on LinkedIn/Sales Navigator are routine and
transient, so "connect another account" is the wrong remedy for one.
"""
from __future__ import annotations

from datetime import datetime

from lobstr_mcp.errors import LobstrAPIError

_LIST_KEYS = ("data", "results", "records")


def _rows(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in _LIST_KEYS:
            if isinstance(payload.get(k), list):
                return payload[k]
    return []


def crawler_account_type(crawler: dict) -> str | None:
    """The account platform slug a crawler needs (e.g. "linkedin-sync",
    "sales-nav-sync"), or None when the crawler needs no account at all."""
    account = crawler.get("account") if isinstance(crawler, dict) else None
    return account.get("type") if isinstance(account, dict) else None


class AccountsShapeError(Exception):
    """A squid's `accounts` field is a non-empty list this client can't read
    ids from. Raised instead of returning [], because the caller then unions
    that "current" list with a new id and writes the result — silently
    treating an unrecognized shape as empty would narrow (or wipe) whatever
    was actually attached."""


def squid_account_ids(squid: dict) -> list[str]:
    """The account ids already attached to a squid.

    Two shapes are both real API responses for the same `accounts` field:
    `GET /squids/{hash}` returns a list of dicts (`{"id": ..., "status": ...}`);
    the `POST /squids/{hash}` update echoes back what it just wrote as a plain
    list of account hash strings. Both are accepted. Anything else non-empty
    raises AccountsShapeError rather than being read as "no accounts."
    """
    accounts = squid.get("accounts") if isinstance(squid, dict) else None
    if accounts is None:
        return []
    if not isinstance(accounts, list):
        raise AccountsShapeError(
            f"squid.accounts is a {type(accounts).__name__}, not a list")
    if not accounts:
        return []
    if all(isinstance(a, str) for a in accounts):
        return list(accounts)
    if all(isinstance(a, dict) for a in accounts):
        ids = [a.get("id") for a in accounts if isinstance(a.get("id"), str)]
        if len(ids) == len(accounts):
            return ids
        raise AccountsShapeError("squid.accounts has a dict entry with no string 'id'")
    raise AccountsShapeError("squid.accounts mixes shapes this client doesn't recognize")


def _is_locked(account: dict) -> bool:
    lock_time = account.get("lock_time")
    if not lock_time:
        return False
    try:
        lt = datetime.fromisoformat(str(lock_time).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        # Unparseable lock_time: don't let a formatting quirk block a pick.
        return False
    now = datetime.now(lt.tzinfo) if lt.tzinfo else datetime.utcnow()
    return lt > now


def account_health_note(account: dict) -> str | None:
    """A short, human description of why `account` isn't fully usable right
    now, or None when it is fine. Never a reason to refuse an explicit
    account_id — only something to warn the caller about."""
    status = str(account.get("status") or "")
    if status != "200":
        return f'status is "{status or "unknown"}", not the healthy "200"'
    if (account.get("status_code_info") or "") == "cookies_expired":
        return "its cookies have expired; it needs re-syncing before a run can use it"
    if _is_locked(account):
        lock_time = account.get("lock_time")
        return (f"it is currently locked{f' until {lock_time}' if lock_time else ''} — "
               "routine on LinkedIn/Sales Navigator and usually clears on its own")
    return None


def is_healthy(account: dict) -> bool:
    """The exact condition the run worker uses to treat an attached account as
    usable right now: status "200" (live), not cookies_expired, not locked."""
    return account_health_note(account) is None


def _account_summary(a: dict) -> dict:
    out = {"id": a.get("id"), "username": a.get("username"),
           "type": a.get("type"), "status": a.get("status"),
           "status_code_info": a.get("status_code_info")}
    if a.get("lock_time"):
        out["lock_time"] = a.get("lock_time")
    return out


def _type_status_ok(account, account_type: str) -> bool:
    """Type-matches and is live/not cookies_expired — everything auto-pick
    could ever use, before splitting on whether it's presently locked."""
    return (isinstance(account, dict) and account.get("type") == account_type
            and str(account.get("status") or "") == "200"
            and (account.get("status_code_info") or "") != "cookies_expired")


def find_candidates(client, account_type: str) -> tuple[list[dict], list[dict]]:
    """Exact-type-match, live, not-cookies_expired accounts, split into
    (unlocked, locked). Filters server-side (type/status) as a first pass,
    then re-checks precisely — the server-side filter is a convenience, not
    the source of truth for what auto-pick is allowed to choose.
    """
    rows = _rows(client.list_accounts(limit=100, page=1, type=account_type, status=1))
    candidates = [a for a in rows if _type_status_ok(a, account_type)]
    unlocked = [a for a in candidates if not _is_locked(a)]
    locked = [a for a in candidates if _is_locked(a)]
    return unlocked, locked


def resolve_account(client, account_id: str, account_type: str):
    """Look up an explicitly-given account id and check it matches the
    crawler's required platform. Returns (account, None) on success or
    (None, error_dict) with a message that says what to do next.

    Does NOT check health/lock status — an explicit account_id is the
    caller's choice to make. Check `account_health_note(account)` on the
    returned account and surface it as a warning, not a refusal.
    """
    try:
        account = client.get_account(account_id)
    except LobstrAPIError:
        return None, {
            "error_code": "account_not_found",
            "message": (f"No account found for id '{account_id}'. It may be "
                       "wrong, deleted, or belong to another user. Call "
                       f'list_accounts(platform="{account_type}") to find a valid id.'),
            "account_id": account_id,
        }
    if isinstance(account, dict) and isinstance(account.get("data"), list) and account["data"]:
        account = account["data"][0]
    if not isinstance(account, dict) or not account.get("id"):
        return None, {
            "error_code": "account_not_found",
            "message": (f"No account found for id '{account_id}'. Call "
                       f'list_accounts(platform="{account_type}") to find a valid id.'),
            "account_id": account_id,
        }
    if account.get("type") != account_type:
        return None, {
            "error_code": "account_type_mismatch",
            "message": (f"Account '{account_id}' is a '{account.get('type')}' account; "
                       f"this scraper needs a '{account_type}' account — they are not "
                       f'interchangeable. Call list_accounts(platform="{account_type}") '
                       "to find a matching one."),
            "required_type": account_type, "given_type": account.get("type"),
        }
    return account, None


def pick_or_explain(client, account_type: str):
    """No account id given: auto-pick only when exactly one unlocked, healthy,
    right-type account exists. Otherwise return an actionable error instead of
    guessing:

    - several unlocked candidates -> name them, ask the caller to choose.
    - none unlocked but some locked -> name the locked one(s) and say they can
      still be attached explicitly by id (a lock is routine and transient;
      "connect another account" would be the wrong remedy).
    - none at all -> name the required platform.
    """
    unlocked, locked = find_candidates(client, account_type)
    if len(unlocked) == 1:
        return unlocked[0], None
    if len(unlocked) > 1:
        return None, {
            "error_code": "multiple_accounts_available",
            "message": (f"{len(unlocked)} healthy '{account_type}' accounts are connected; "
                       "auto-pick won't guess between them. Pick one and pass its id as "
                       "account_id."),
            "required_type": account_type,
            "candidates": [_account_summary(a) for a in unlocked],
        }
    if locked:
        return None, {
            "error_code": "account_locked",
            "message": (f"{len(locked)} '{account_type}' account(s) are connected but "
                       "currently locked — a routine, transient rate-limit on "
                       "LinkedIn/Sales Navigator, not a broken account. Auto-pick skips a "
                       "locked account, but you can still attach one explicitly: pass its "
                       "id as account_id."),
            "required_type": account_type,
            "candidates": [_account_summary(a) for a in locked],
        }
    return None, {
        "error_code": "no_account_available",
        "message": (f"No '{account_type}' account is connected and ready to use. Connect "
                   "one from your Lobstr dashboard, or call list_accounts to check for one "
                   "that just needs re-syncing, then retry."),
        "required_type": account_type,
    }
