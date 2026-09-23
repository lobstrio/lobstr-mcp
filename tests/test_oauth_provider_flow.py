import asyncio
from types import SimpleNamespace
from urllib.parse import urlparse, parse_qs

from mcp.server.auth.provider import AuthorizationParams
from lobstr_mcp.auth.oauth_provider import InvalidGrantError, LobstrOAuthProvider
from lobstr_mcp.auth.token_store import TokenStore, TokenLink

CONSENT_URL = "https://app.lobstr.io/connect-ai"


class FakeDelegation:
    def __init__(self):
        self.grants = []

    def redeem_grant(self, grant):
        self.grants.append(grant)
        return TokenLink("u1", "LOBSTR_TOK", ["crawlers:read"])


def make_provider():
    return LobstrOAuthProvider(
        base_url="https://mcp.lobstr.io",
        delegation_client=FakeDelegation(),
        token_store=TokenStore(),
        consent_url=CONSENT_URL,
    )


def qs(url, key):
    return parse_qs(urlparse(url).query)[key][0]


def params():
    return AuthorizationParams(
        state="st8", scopes=["crawlers:read"], code_challenge="chal",
        redirect_uri="https://client.example/cb",
        redirect_uri_provided_explicitly=True, resource=None,
    )


def test_register_and_get_client():
    p = make_provider()
    client = SimpleNamespace(client_id="c1")
    asyncio.run(p.register_client(client))
    assert asyncio.run(p.get_client("c1")) is client
    assert asyncio.run(p.get_client("nope")) is None


def test_authorize_redirects_to_frontend_consent():
    p = make_provider()
    client = SimpleNamespace(client_id="c1")
    url = asyncio.run(p.authorize(client, params()))
    assert url.startswith(CONSENT_URL)
    assert qs(url, "request_id")
    assert qs(url, "scope") == "crawlers:read"
    assert qs(url, "client_id") == "c1"


def test_authorize_forwards_client_name_and_verified_redirect_host():
    """The consent page must be able to name the client instead of showing the
    opaque DCR client_id: forward the self-asserted client_name and, as the
    trustworthy signal, the host of the redirect_uri the code is delivered to."""
    p = make_provider()
    client = SimpleNamespace(
        client_id="c1", client_name="Claude",
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"])
    url = asyncio.run(p.authorize(client, params()))
    assert qs(url, "client_name") == "Claude"
    assert qs(url, "redirect_host") == "claude.ai"


def test_authorize_omits_identity_hints_when_absent():
    """A minimally-registered client (no name, no redirect_uris) must not emit
    empty client_name/redirect_host params — the frontend falls back cleanly."""
    p = make_provider()
    client = SimpleNamespace(client_id="c1")
    url = asyncio.run(p.authorize(client, params()))
    assert "client_name=" not in url
    assert "redirect_host=" not in url


def test_redirect_host_ignores_spoofed_client_name():
    """A client can self-assert client_name='Claude' but its code is only ever
    delivered to its real redirect_uri — redirect_host exposes the mismatch."""
    p = make_provider()
    client = SimpleNamespace(
        client_id="c1", client_name="Claude",
        redirect_uris=["https://evil.example/cb"])
    url = asyncio.run(p.authorize(client, params()))
    assert qs(url, "client_name") == "Claude"       # what it claims
    assert qs(url, "redirect_host") == "evil.example"  # where it actually goes


def test_full_consent_flow_to_access_token():
    p = make_provider()
    client = SimpleNamespace(client_id="c1")

    consent_url = asyncio.run(p.authorize(client, params()))
    request_id = qs(consent_url, "request_id")

    # frontend approves and hands back a single-use grant
    cb = p.complete_consent(request_id, "grant-xyz")
    assert cb.startswith("https://client.example/cb")
    assert qs(cb, "state") == "st8"
    code_str = qs(cb, "code")

    authcode = asyncio.run(p.load_authorization_code(client, code_str))
    assert authcode is not None and authcode.client_id == "c1"
    token = asyncio.run(p.exchange_authorization_code(client, authcode))
    assert token.token_type == "Bearer" and token.access_token
    assert token.refresh_token and token.scope == "crawlers:read"

    at = asyncio.run(p.load_access_token(token.access_token))
    assert at is not None
    assert at.claims["lobstr_token"] == "LOBSTR_TOK"
    assert at.scopes == ["crawlers:read"]

    # code is single-use
    assert asyncio.run(p.load_authorization_code(client, code_str)) is None


def test_refresh_rotates_access_token():
    p = make_provider()
    client = SimpleNamespace(client_id="c1")
    request_id = qs(asyncio.run(p.authorize(client, params())), "request_id")
    code_str = qs(p.complete_consent(request_id, "g1"), "code")
    authcode = asyncio.run(p.load_authorization_code(client, code_str))
    token = asyncio.run(p.exchange_authorization_code(client, authcode))

    rt = asyncio.run(p.load_refresh_token(client, token.refresh_token))
    assert rt is not None
    new = asyncio.run(p.exchange_refresh_token(client, rt, []))
    assert new.access_token and new.access_token != token.access_token
    at = asyncio.run(p.load_access_token(new.access_token))
    assert at.claims["lobstr_token"] == "LOBSTR_TOK"


def test_revoke_access_token():
    p = make_provider()
    client = SimpleNamespace(client_id="c1")
    request_id = qs(asyncio.run(p.authorize(client, params())), "request_id")
    code_str = qs(p.complete_consent(request_id, "g1"), "code")
    authcode = asyncio.run(p.load_authorization_code(client, code_str))
    token = asyncio.run(p.exchange_authorization_code(client, authcode))

    at = asyncio.run(p.load_access_token(token.access_token))
    asyncio.run(p.revoke_token(at))
    assert asyncio.run(p.load_access_token(token.access_token)) is None


# --- live-integration hardening (P4) ------------------------------------------

class FlakyDelegation:
    """Fails the first N redemptions, then succeeds — models a transient
    network/5xx blip between the MCP and Django."""

    def __init__(self, failures=1, scopes=None):
        self.failures = failures
        self.calls = 0
        self.scopes = scopes if scopes is not None else ["crawlers:read"]

    def redeem_grant(self, grant):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("upstream unavailable")
        return TokenLink("u1", "LOBSTR_TOK", list(self.scopes))


def make_provider_with(delegation, now=None):
    kwargs = {}
    if now is not None:
        kwargs["now"] = now
    return LobstrOAuthProvider(
        base_url="https://mcp.lobstr.io",
        delegation_client=delegation,
        token_store=TokenStore(),
        consent_url=CONSENT_URL,
        **kwargs,
    )


def params_with(scopes):
    return AuthorizationParams(
        state="st8", scopes=list(scopes), code_challenge="chal",
        redirect_uri="https://client.example/cb",
        redirect_uri_provided_explicitly=True, resource=None,
    )


def test_failed_redemption_leaves_request_retryable():
    """A transient redeem failure must not destroy the pending OAuth request —
    the user re-clicking Allow has to be able to finish the flow."""
    delegation = FlakyDelegation(failures=1)
    p = make_provider_with(delegation)
    client = SimpleNamespace(client_id="c1")
    request_id = qs(asyncio.run(p.authorize(client, params())), "request_id")

    try:
        p.complete_consent(request_id, "grant-1")
    except Exception:
        pass
    else:
        raise AssertionError("expected the first redemption to fail")

    cb = p.complete_consent(request_id, "grant-2")
    assert qs(cb, "code")
    assert delegation.calls == 2


def test_unknown_request_id_raises_invalid_grant():
    p = make_provider()
    try:
        p.complete_consent("no-such-request", "g1")
    except InvalidGrantError:
        pass
    except KeyError as exc:
        raise AssertionError(f"leaked KeyError instead of InvalidGrantError: {exc}")
    else:
        raise AssertionError("expected InvalidGrantError")


def test_request_id_is_single_use():
    p = make_provider()
    client = SimpleNamespace(client_id="c1")
    request_id = qs(asyncio.run(p.authorize(client, params())), "request_id")
    p.complete_consent(request_id, "g1")
    try:
        p.complete_consent(request_id, "g1")
    except InvalidGrantError:
        pass
    else:
        raise AssertionError("a consumed request_id must not be replayable")


def test_granted_scopes_narrow_requested_scopes():
    """The grant is authoritative: the user may approve fewer scopes than the
    AI client asked for, and the issued token must never exceed the grant."""
    delegation = FlakyDelegation(failures=0, scopes=["crawlers:read"])
    p = make_provider_with(delegation)
    client = SimpleNamespace(client_id="c1")
    request_id = qs(
        asyncio.run(p.authorize(client, params_with(["crawlers:read", "runs:execute"]))),
        "request_id",
    )
    code_str = qs(p.complete_consent(request_id, "g1"), "code")
    authcode = asyncio.run(p.load_authorization_code(client, code_str))
    assert authcode.scopes == ["crawlers:read"]
    token = asyncio.run(p.exchange_authorization_code(client, authcode))
    assert token.scope == "crawlers:read"
    at = asyncio.run(p.load_access_token(token.access_token))
    assert at.scopes == ["crawlers:read"]


def test_pending_authorize_requests_expire():
    """/authorize is public and unauthenticated — abandoned requests must not
    accumulate in memory forever."""
    clock = {"t": 1000.0}
    p = make_provider_with(FlakyDelegation(failures=0), now=lambda: clock["t"])
    client = SimpleNamespace(client_id="c1")
    request_id = qs(asyncio.run(p.authorize(client, params())), "request_id")

    clock["t"] += 10_000  # well past any sane consent window
    try:
        p.complete_consent(request_id, "g1")
    except InvalidGrantError:
        pass
    else:
        raise AssertionError("an abandoned authorize request must expire")
    assert p.pending_count() == 0


def test_refresh_token_rotates_and_old_one_is_rejected():
    p = make_provider()
    client = SimpleNamespace(client_id="c1")
    request_id = qs(asyncio.run(p.authorize(client, params())), "request_id")
    code_str = qs(p.complete_consent(request_id, "g1"), "code")
    authcode = asyncio.run(p.load_authorization_code(client, code_str))
    token = asyncio.run(p.exchange_authorization_code(client, authcode))

    rt = asyncio.run(p.load_refresh_token(client, token.refresh_token))
    new = asyncio.run(p.exchange_refresh_token(client, rt, []))
    assert new.refresh_token != token.refresh_token, "OAuth 2.1 requires rotation"
    assert asyncio.run(p.load_refresh_token(client, token.refresh_token)) is None


def test_load_refresh_token_rejects_expired():
    clock = {"t": 1000.0}
    p = make_provider_with(FlakyDelegation(failures=0), now=lambda: clock["t"])
    client = SimpleNamespace(client_id="c1")
    request_id = qs(asyncio.run(p.authorize(client, params())), "request_id")
    code_str = qs(p.complete_consent(request_id, "g1"), "code")
    authcode = asyncio.run(p.load_authorization_code(client, code_str))
    token = asyncio.run(p.exchange_authorization_code(client, authcode))

    clock["t"] += 31 * 24 * 3600
    assert asyncio.run(p.load_refresh_token(client, token.refresh_token)) is None


def test_no_overlapping_scope_is_rejected():
    delegation = FlakyDelegation(failures=0, scopes=["crawlers:read"])
    p = make_provider_with(delegation)
    client = SimpleNamespace(client_id="c1")
    request_id = qs(asyncio.run(p.authorize(client, params_with(["runs:execute"]))),
                    "request_id")
    try:
        p.complete_consent(request_id, "g1")
    except InvalidGrantError:
        pass
    else:
        raise AssertionError("expected InvalidGrantError when nothing was granted")
