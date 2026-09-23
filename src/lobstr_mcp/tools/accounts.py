"""Platform-account tools.

In Lobstr, an *account* is a third-party platform login a scraper authenticates
with — Facebook, LinkedIn, Instagram, LeBonCoin, … These tools let an agent see
which accounts are connected and whether they're healthy, so it can pick a
working one before running a scraper that needs a login — and `attach_account`
links one to a squid, which is required before a run can start (an
account-backed squid with none attached fails asynchronously with
done_reason "no_accounts").

SECURITY: `list_accounts`/`get_account` are read-only and every field returned
is whitelisted here — cookies/credentials are never exposed (the API model
doesn't carry them, and we select fields explicitly rather than echoing the
payload). `attach_account` is a write: it only ever posts account *ids* it
looked up itself, never a value the model could poison the payload with.
"""
from __future__ import annotations

from lobstr_mcp.account_linking import (
    AccountsShapeError,
    account_health_note,
    crawler_account_type,
    pick_or_explain,
    resolve_account,
    squid_account_ids,
)
from lobstr_mcp.auth.scopes import ACCOUNT_READ_SCOPES, EXECUTE_SCOPES
from lobstr_mcp.errors import structured
from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.render import toon_result

_LIST_KEYS = ("data", "results", "records")


def _rows(payload) -> list:
    if isinstance(payload, list):
        return payload
    for k in _LIST_KEYS:
        if isinstance(payload.get(k), list):
            return payload[k]
    return []


def _summary(a: dict) -> dict:
    """Whitelisted, non-sensitive view of a platform account."""
    return {
        "id": a.get("id"),
        "platform": a.get("type"),
        "username": a.get("username"),
        "status": a.get("status_code_description") or a.get("status_code_info"),
        "last_sync": a.get("last_synchronization_time"),
    }


@structured
def list_accounts_impl(client: LobstrClient, *, platform: str | None = None,
                       limit: int = 50, page: int = 1) -> dict:
    rows = _rows(client.list_accounts(limit=limit, page=page))
    if platform:
        p = platform.lower()
        rows = [a for a in rows if isinstance(a, dict) and p in (a.get("type") or "").lower()]
    return {"count": len(rows),
            "accounts": [_summary(a) for a in rows if isinstance(a, dict)]}


@structured
def get_account_impl(client: LobstrClient, account_id: str) -> dict:
    data = client.get_account(account_id)
    # the API may wrap a single account as {"data": [ {...} ]}
    if isinstance(data, dict) and isinstance(data.get("data"), list) and data["data"]:
        data = data["data"][0]
    if not isinstance(data, dict):
        data = {}
    out = _summary(data)
    # which of the user's scrapers use this account (ids/names only)
    squids = data.get("squids")
    if isinstance(squids, list):
        out["used_by"] = [{"id": s.get("id"), "name": s.get("name")}
                          for s in squids if isinstance(s, dict)]
    return out


@structured(verify_with="get_my_scraper(squid_id=...) and its `accounts`")
def attach_account_impl(client: LobstrClient, squid_id: str,
                        account_id: str | None = None) -> dict:
    """Link a connected platform account to a squid.

    Reads the squid's current accounts first (the API's `accounts` field is
    full-replace) and only ever sends the union of what's already attached
    plus the new one — an existing link is never dropped by this client. (The
    read and the write are two separate requests, not an atomic
    compare-and-set — the API has no such primitive — so a concurrent change
    to the squid between them can still race.) With no account_id, auto-picks
    only when the squid has no account yet and exactly one healthy, unlocked,
    right-type candidate exists; a locked candidate is reported separately
    (locks are routine/transient, not a reason to connect another account);
    otherwise returns the candidates (or the required platform, if none)
    instead of guessing.

    An explicit account_id is never refused for being unhealthy (that choice
    is the caller's) — the result carries `account_status` and a `warning`
    when it isn't fully healthy right now.
    """
    squid = client.get_squid(squid_id)
    crawler_id = squid.get("crawler") if isinstance(squid, dict) else None
    crawler = client.get_crawler(crawler_id) if crawler_id else {}
    account_type = crawler_account_type(crawler)

    if account_type is None:
        return {
            "error_code": "no_account_needed",
            "message": (f"{crawler.get('name') or 'This scraper'} does not use a "
                       "platform-account login — do not attach one; the API rejects "
                       "an `accounts` field on this squid."),
            "squid_id": squid_id,
        }

    try:
        current_ids = squid_account_ids(squid)
    except AccountsShapeError:
        return {
            "error_code": "accounts_unreadable",
            "message": ("This squid's current accounts couldn't be read in a shape "
                       "this client recognizes, so nothing was written — attaching now "
                       "could have silently dropped an existing link. Check it directly "
                       "(get_my_scraper) and try again."),
            "squid_id": squid_id,
        }

    if account_id is None:
        if current_ids:
            return {
                "error_code": "account_already_attached",
                "message": ("This squid already has account(s) attached; auto-pick "
                           "only runs on a squid with none, so an existing link is "
                           "never silently replaced. Pass account_id explicitly to "
                           "add another — it merges with what's already there."),
                "squid_id": squid_id, "current_accounts": current_ids,
            }
        account, error = pick_or_explain(client, account_type)
        if error:
            return {**error, "squid_id": squid_id}
        account_id = account["id"]
        auto_picked = True
        warning = None  # pick_or_explain only ever returns an unlocked, healthy account
    else:
        account, error = resolve_account(client, account_id, account_type)
        if error:
            return {**error, "squid_id": squid_id}
        auto_picked = False
        warning = account_health_note(account)

    result = {"squid_id": squid_id, "account_id": account_id,
              "account_status": account.get("status"), "auto_picked": auto_picked}
    if warning:
        result["warning"] = warning

    if account_id in current_ids:
        result["accounts"] = current_ids
        result["message"] = "That account was already attached; nothing changed."
        return result

    new_ids = [*current_ids, account_id]
    client.update_squid(squid_id, {"accounts": new_ids})
    result["accounts"] = new_ids
    result["previously_attached"] = current_ids
    result["message"] = "Account attached."
    return result


def register_accounts_tools(mcp, client_factory, authorizer=None) -> None:
    def authz(scopes):
        if authorizer:
            authorizer(scopes)

    @mcp.tool(annotations={"title": "List Accounts", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def list_accounts(platform: str | None = None, limit: int = 50,
                      page: int = 1, toon: bool = False) -> dict:
        """List the user's connected platform accounts (the logins scrapers
        authenticate with — Facebook, LinkedIn, Instagram, LeBonCoin, …), with
        each one's status/health. Filter by platform (e.g. "linkedin"). Returns
        metadata only — never credentials. Pass toon=true for compact TOON."""
        authz(ACCOUNT_READ_SCOPES)
        out = list_accounts_impl(client_factory(), platform=platform,
                                 limit=limit, page=page)
        return toon_result(out) if toon else out

    @mcp.tool(annotations={"title": "Get Account", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def get_account(account_id: str) -> dict:
        """Get one connected platform account by id — its platform, status, and
        which of the user's scrapers use it. Metadata only; no credentials."""
        authz(ACCOUNT_READ_SCOPES)
        return get_account_impl(client_factory(), account_id)

    @mcp.tool(annotations={"title": "Attach Account",
                           "readOnlyHint": False,
                           # Adds a link; never removes one, so it destroys no
                           # existing data.
                           "destructiveHint": False,
                           "idempotentHint": True, "openWorldHint": True})
    def attach_account(squid_id: str, account_id: str | None = None) -> dict:
        """Link a connected platform account to a saved scraper (squid) so it
        can launch a run. Required before running any scraper whose crawler
        needs a synced account (e.g. LinkedIn Leads, Sales Navigator Leads) —
        without one, the run is created but fails asynchronously with
        done_reason "no_accounts".

        This only ADDS a link; it never removes one. (The underlying API field
        is full-replace, so this reads the squid's current accounts first and
        re-sends that set plus the new account — nothing already attached is
        silently detached by this call. That read and that write are two
        separate requests, not an atomic compare-and-set — the API offers no
        such primitive — so a concurrent change to the same squid between
        them can still race.)

        Pass account_id to attach a specific account — its platform must match
        the scraper's required account type exactly (e.g. "linkedin-sync" vs
        "sales-nav-sync"; they are not interchangeable, use list_accounts to
        check). An explicit account_id is never refused for being unhealthy —
        the response carries account_status and a warning when it isn't fully
        healthy (e.g. locked or cookies_expired) rather than blocking you.

        Omit account_id to auto-pick: this only succeeds when the squid has NO
        account yet and exactly one healthy, unlocked, right-type account is
        connected. A locked candidate is reported separately from there being
        none at all (LinkedIn/Sales Navigator locks are routine and
        transient) — you can still attach it explicitly by id. With several
        unlocked candidates or none at all, this returns them (or the
        required platform) instead of guessing.

        A scraper that needs no account rejects this call outright — check
        get_scraper_details first if unsure."""
        authz(ACCOUNT_READ_SCOPES + EXECUTE_SCOPES)
        return attach_account_impl(client_factory(), squid_id, account_id=account_id)
