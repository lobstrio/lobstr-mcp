"""Redis-backed stores: OAuth state must survive a restart.

The in-memory stores meant every container restart silently invalidated every
connected client's token — during P4 live testing four rebuilds each dropped a
working session. Idempotency keys vanishing is worse: a repeated run_scraper
call after a restart creates a second *paid* run.
"""
import time

import fakeredis
import pytest

from lobstr_mcp.auth.token_store import TokenLink
from lobstr_mcp.persistence import (
    RedisClientRegistry,
    RedisIdempotencyStore,
    RedisRefreshRegistry,
    RedisTokenStore,
    TokenCipher,
)

KEY = TokenCipher.generate_key()


def r():
    return fakeredis.FakeStrictRedis()


def link(expires_at=None):
    return TokenLink("u1", "LOBSTR_SECRET_TOKEN", ["crawlers:read"], expires_at)


# --- cipher ------------------------------------------------------------------

def test_cipher_roundtrips():
    c = TokenCipher(KEY)
    blob = c.encrypt("LOBSTR_SECRET_TOKEN")
    assert blob != "LOBSTR_SECRET_TOKEN"
    assert c.decrypt(blob) == "LOBSTR_SECRET_TOKEN"


def test_cipher_requires_a_key():
    with pytest.raises(ValueError):
        TokenCipher(None)


# --- token store -------------------------------------------------------------

def test_token_store_roundtrip_and_survives_a_new_instance():
    shared = r()
    RedisTokenStore(shared, KEY).put("mcp-tok", link())
    # a "restart": a brand-new store object over the same Redis
    got = RedisTokenStore(shared, KEY).get("mcp-tok")
    assert got is not None
    assert got.lobstr_user_id == "u1"
    assert got.lobstr_api_token == "LOBSTR_SECRET_TOKEN"
    assert got.scopes == ["crawlers:read"]


def test_lobstr_token_is_encrypted_at_rest():
    """Spec §8: Lobstr API tokens are encrypted at rest."""
    shared = r()
    RedisTokenStore(shared, KEY).put("mcp-tok", link())
    raw = b"".join(shared.get(k) or b"" for k in shared.keys("*"))
    assert b"LOBSTR_SECRET_TOKEN" not in raw, "plaintext Lobstr token found in Redis"


def test_token_store_get_missing_is_none():
    assert RedisTokenStore(r(), KEY).get("nope") is None


def test_token_store_revoke():
    store = RedisTokenStore(r(), KEY)
    store.put("t", link())
    store.revoke("t")
    assert store.get("t") is None


def test_token_store_honours_expiry():
    store = RedisTokenStore(r(), KEY)
    store.put("t", link(expires_at=time.time() - 1))
    assert store.get("t") is None


def test_token_store_sets_a_redis_ttl_so_dead_tokens_self_evict():
    shared = r()
    store = RedisTokenStore(shared, KEY)
    store.put("t", link(expires_at=time.time() + 3600))
    key = next(k for k in shared.keys("*"))
    assert 0 < shared.ttl(key) <= 3600


# --- idempotency -------------------------------------------------------------

def test_idempotency_store_roundtrip_across_instances():
    shared = r()
    RedisIdempotencyStore(shared).put("key1", "run1")
    assert RedisIdempotencyStore(shared).get("key1") == "run1"
    assert RedisIdempotencyStore(shared).get("other") is None


def test_idempotency_entries_expire():
    shared = r()
    RedisIdempotencyStore(shared, ttl=60).put("k", "run1")
    key = next(k for k in shared.keys("*"))
    assert 0 < shared.ttl(key) <= 60


def test_idempotency_put_ttl_overrides_the_store_default():
    # A derived (no explicit idempotency_key) run_scraper call passes a short
    # ttl per-call; it must win over the store's own (long) default.
    shared = r()
    RedisIdempotencyStore(shared, ttl=86400).put("k", "run1", ttl=120)
    key = next(k for k in shared.keys("*"))
    assert 0 < shared.ttl(key) <= 120


# --- DCR client registry -----------------------------------------------------

def test_client_registry_roundtrip():
    from mcp.shared.auth import OAuthClientInformationFull

    shared = r()
    client = OAuthClientInformationFull(
        client_id="c1", client_secret="s",
        redirect_uris=["https://client.example/cb"],
        grant_types=["authorization_code", "refresh_token"],
    )
    RedisClientRegistry(shared).put(client)
    got = RedisClientRegistry(shared).get("c1")
    assert got is not None and got.client_id == "c1"
    assert str(got.redirect_uris[0]) == "https://client.example/cb"
    assert RedisClientRegistry(shared).get("missing") is None


# --- refresh registry --------------------------------------------------------

def test_refresh_registry_roundtrip_and_pop():
    from mcp.server.auth.provider import RefreshToken

    shared = r()
    reg = RedisRefreshRegistry(shared, KEY)
    rt = RefreshToken(token="rt1", client_id="c1", scopes=["crawlers:read"],
                      expires_at=int(time.time()) + 3600)
    reg.put(rt, link())

    fresh = RedisRefreshRegistry(shared, KEY)
    got_rt, got_link = fresh.get("rt1")
    assert got_rt is not None and got_rt.client_id == "c1"
    assert got_link.lobstr_api_token == "LOBSTR_SECRET_TOKEN"

    popped_rt, popped_link = fresh.pop("rt1")
    assert popped_rt is not None and popped_link is not None
    assert fresh.get("rt1") == (None, None), "rotation must retire the old token"


def test_refresh_registry_missing():
    assert RedisRefreshRegistry(r(), KEY).get("nope") == (None, None)


# --- provider wiring: state survives a restart -------------------------------

def _provider(shared=None):
    from types import SimpleNamespace
    from lobstr_mcp.auth.oauth_provider import LobstrOAuthProvider
    from lobstr_mcp.persistence import (
        MemoryClientRegistry, MemoryRefreshRegistry,
    )

    class Delegation:
        def redeem_grant(self, grant):
            return link()

    if shared is None:
        clients, refreshes = MemoryClientRegistry(), MemoryRefreshRegistry()
        tokens = __import__("lobstr_mcp.auth.token_store", fromlist=["TokenStore"]).TokenStore()
    else:
        clients = RedisClientRegistry(shared)
        refreshes = RedisRefreshRegistry(shared, KEY)
        tokens = RedisTokenStore(shared, KEY)
    return LobstrOAuthProvider(
        base_url="https://mcp.lobstr.io", delegation_client=Delegation(),
        token_store=tokens, consent_url="https://app.lobstr.io/connect-ai",
        client_registry=clients, refresh_registry=refreshes,
    ), SimpleNamespace(client_id="c1")


def test_registered_client_and_tokens_survive_a_restart():
    """A restart used to drop DCR clients and every issued token, so a connected
    AI client got 401s with a client_id the server no longer recognized."""
    import asyncio
    from urllib.parse import parse_qs, urlparse
    from mcp.server.auth.provider import AuthorizationParams
    from mcp.shared.auth import OAuthClientInformationFull

    shared = r()
    p1, _ = _provider(shared)
    client = OAuthClientInformationFull(
        client_id="c1", client_secret="s",
        redirect_uris=["https://client.example/cb"],
        grant_types=["authorization_code", "refresh_token"])
    asyncio.run(p1.register_client(client))

    params = AuthorizationParams(
        state="s1", scopes=["crawlers:read"], code_challenge="ch",
        redirect_uri="https://client.example/cb",
        redirect_uri_provided_explicitly=True, resource=None)
    consent = asyncio.run(p1.authorize(client, params))
    rid = parse_qs(urlparse(consent).query)["request_id"][0]
    code = parse_qs(urlparse(p1.complete_consent(rid, "g")).query)["code"][0]
    authcode = asyncio.run(p1.load_authorization_code(client, code))
    tok = asyncio.run(p1.exchange_authorization_code(client, authcode))

    # restart: a brand-new provider over the same Redis
    p2, _ = _provider(shared)
    assert asyncio.run(p2.get_client("c1")) is not None, "DCR client lost"
    at = asyncio.run(p2.load_access_token(tok.access_token))
    assert at is not None, "issued access token lost"
    assert at.claims["lobstr_token"] == "LOBSTR_SECRET_TOKEN"

    rt = asyncio.run(p2.load_refresh_token(client, tok.refresh_token))
    assert rt is not None, "refresh token lost"
    new = asyncio.run(p2.exchange_refresh_token(client, rt, []))
    assert new.access_token and new.refresh_token != tok.refresh_token


def test_memory_registries_still_work_by_default():
    import asyncio
    from mcp.shared.auth import OAuthClientInformationFull

    p, _ = _provider()
    client = OAuthClientInformationFull(
        client_id="c1", client_secret="s",
        redirect_uris=["https://client.example/cb"],
        grant_types=["authorization_code"])
    asyncio.run(p.register_client(client))
    assert asyncio.run(p.get_client("c1")) is not None


# --- store selection ---------------------------------------------------------

def _settings(**over):
    from lobstr_mcp.config import Settings
    base = dict(lobstr_api_base="https://api.lobstr.io/v1", dev_token=None,
                request_timeout=30.0, run_confirm_threshold=100,
                public_base_url="https://mcp.lobstr.io", service_credential=None,
                consent_url="https://app.lobstr.io/connect-ai",
                redis_url=None, token_key=None)
    base.update(over)
    return Settings(**base)


def test_make_stores_defaults_to_memory():
    from lobstr_mcp.persistence import make_stores, MemoryClientRegistry
    from lobstr_mcp.auth.token_store import TokenStore

    stores = make_stores(_settings())
    assert isinstance(stores.tokens, TokenStore)
    assert isinstance(stores.clients, MemoryClientRegistry)
    assert stores.backend == "memory"


def test_make_stores_uses_redis_when_configured():
    from lobstr_mcp.persistence import make_stores

    stores = make_stores(
        _settings(redis_url="redis://ignored/0", token_key=KEY),
        redis_factory=lambda url: fakeredis.FakeStrictRedis())
    assert isinstance(stores.tokens, RedisTokenStore)
    assert isinstance(stores.clients, RedisClientRegistry)
    assert isinstance(stores.refreshes, RedisRefreshRegistry)
    assert isinstance(stores.idempotency, RedisIdempotencyStore)
    assert stores.backend == "redis"


def test_redis_without_a_token_key_is_refused():
    """Better to fail at boot than to write user API tokens to Redis in clear."""
    from lobstr_mcp.persistence import make_stores

    with pytest.raises(ValueError):
        make_stores(_settings(redis_url="redis://ignored/0", token_key=None),
                    redis_factory=lambda url: fakeredis.FakeStrictRedis())
