"""A client registered before profile:read existed can still authorize.

Claude Code, 23/11/2026: its client, registered on 03/09 with account:read but
no profile:read (split out later), got "invalid_scope: Client was not registered
with scope profile:read" on its next sign-in. 17 Claude and 2 ChatGPT clients
were registered the same way.
"""
import asyncio

import pytest
from mcp.shared.auth import InvalidScopeError, OAuthClientInformationFull

from lobstr_mcp.auth.oauth_provider import LobstrOAuthProvider
from lobstr_mcp.auth.scopes import ALL_SCOPES
from lobstr_mcp.auth.token_store import TokenStore

LEGACY = "account:read crawlers:read results:read runs:execute runs:read"


def provider():
    return LobstrOAuthProvider(base_url="https://mcp.lobstr.io", delegation_client=object(),
                               token_store=TokenStore(), consent_url="https://app.lobstr.io/connect-ai")


def client(scope):
    return OAuthClientInformationFull(client_id="c1", client_name="Claude Code (lobstr)", scope=scope,
                                      redirect_uris=["http://localhost:3118/callback"])


def get(p, scope):
    asyncio.run(p.register_client(client(scope)))
    return asyncio.run(p.get_client("c1"))


def test_a_legacy_client_may_ask_for_profile_read():
    c = get(provider(), LEGACY)
    assert c.validate_scope(" ".join(ALL_SCOPES)) == ALL_SCOPES


def test_without_account_read_nothing_is_added():
    c = get(provider(), "crawlers:read")
    assert c.scope == "crawlers:read"
    with pytest.raises(InvalidScopeError):
        c.validate_scope("profile:read")


def test_a_current_client_is_unchanged():
    c = get(provider(), " ".join(ALL_SCOPES))
    assert c.scope == " ".join(ALL_SCOPES)


def test_unknown_client_is_none():
    assert asyncio.run(provider().get_client("nope")) is None
