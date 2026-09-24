"""Redis-backed OAuth/idempotency state (spec §8).

The in-memory stores are fine for a single throwaway process, but they make a
restart destructive: every issued MCP token, every registered DCR client and
every idempotency key disappears, so connected AI clients silently lose
authorization and a repeated `run_scraper` call can create a second *paid* run.

Everything here keeps the same tiny interface as its in-memory counterpart, so
`build_oauth_server()` swaps implementations without callers changing.

Lobstr API tokens are encrypted at rest (spec §8) with Fernet; the key comes
from `LOBSTR_MCP_TOKEN_KEY`. There is deliberately no plaintext fallback — a
missing key raises rather than quietly writing user API tokens to Redis in the
clear.

Auth codes and pending consent requests stay in memory on purpose: they live
for minutes inside a single browser handshake, and a restart mid-handshake just
means the user clicks Allow again.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet

from lobstr_mcp.auth.token_store import TokenLink

_TOKEN_PREFIX = "lobstr_mcp:token:"
_IDEM_PREFIX = "lobstr_mcp:idem:"
_CLIENT_PREFIX = "lobstr_mcp:client:"
_REFRESH_PREFIX = "lobstr_mcp:refresh:"

_DEFAULT_IDEM_TTL = 24 * 3600
_CLIENT_TTL = 90 * 24 * 3600


class TokenCipher:
    """Symmetric encryption for Lobstr API tokens held at rest."""

    def __init__(self, key: str | bytes | None) -> None:
        if not key:
            raise ValueError(
                "a token-encryption key is required to persist Lobstr API "
                "tokens; set LOBSTR_MCP_TOKEN_KEY (see TokenCipher.generate_key)"
            )
        self._f = Fernet(key if isinstance(key, bytes) else key.encode())

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode()

    def encrypt(self, plaintext: str) -> str:
        return self._f.encrypt(plaintext.encode()).decode()

    def decrypt(self, blob: str) -> str:
        return self._f.decrypt(blob.encode()).decode()


def _ttl_from(expires_at: float | None, *, now: float | None = None) -> int | None:
    if expires_at is None:
        return None
    remaining = int(expires_at - (now if now is not None else time.time()))
    return remaining if remaining > 0 else None


def _link_to_json(link: TokenLink, cipher: TokenCipher) -> str:
    return json.dumps({
        "lobstr_user_id": link.lobstr_user_id,
        "lobstr_api_token_enc": cipher.encrypt(link.lobstr_api_token),
        "scopes": list(link.scopes),
        "expires_at": link.expires_at,
    })


def _link_from_json(raw: Any, cipher: TokenCipher) -> TokenLink:
    d = json.loads(raw)
    return TokenLink(
        lobstr_user_id=d["lobstr_user_id"],
        lobstr_api_token=cipher.decrypt(d["lobstr_api_token_enc"]),
        scopes=list(d.get("scopes") or []),
        expires_at=d.get("expires_at"),
    )


class RedisTokenStore:
    """Drop-in replacement for TokenStore, backed by Redis."""

    def __init__(self, redis, key: str | bytes | None) -> None:
        self._r = redis
        self._cipher = TokenCipher(key)

    def put(self, mcp_token: str, link: TokenLink) -> None:
        payload = _link_to_json(link, self._cipher)
        ttl = _ttl_from(link.expires_at)
        self._r.set(_TOKEN_PREFIX + mcp_token, payload, ex=ttl)

    def get(self, mcp_token: str, now: float | None = None) -> TokenLink | None:
        raw = self._r.get(_TOKEN_PREFIX + mcp_token)
        if raw is None:
            return None
        link = _link_from_json(raw, self._cipher)
        if link.is_expired(now):
            self.revoke(mcp_token)
            return None
        return link

    def revoke(self, mcp_token: str) -> None:
        self._r.delete(_TOKEN_PREFIX + mcp_token)


class RedisIdempotencyStore:
    """Drop-in replacement for IdempotencyStore, backed by Redis.

    Losing these entries risks charging the user twice for one request, so they
    outlive the process and carry a generous TTL.
    """

    def __init__(self, redis, ttl: int = _DEFAULT_IDEM_TTL) -> None:
        self._r = redis
        self._ttl = ttl

    def get(self, key: str) -> str | None:
        raw = self._r.get(_IDEM_PREFIX + key)
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    def put(self, key: str, run_id: str, ttl: int | None = None) -> None:
        self._r.set(_IDEM_PREFIX + key, run_id, ex=(self._ttl if ttl is None else ttl))


class RedisClientRegistry:
    """Dynamically-registered OAuth clients. AI clients cache the client_id they
    were issued, so dropping these forces a re-registration they handle poorly.
    """

    def __init__(self, redis, ttl: int = _CLIENT_TTL) -> None:
        self._r = redis
        self._ttl = ttl

    def put(self, client_info) -> None:
        self._r.set(_CLIENT_PREFIX + client_info.client_id,
                    client_info.model_dump_json(), ex=self._ttl)

    def get(self, client_id: str):
        raw = self._r.get(_CLIENT_PREFIX + client_id)
        if raw is None:
            return None
        from mcp.shared.auth import OAuthClientInformationFull
        return OAuthClientInformationFull.model_validate_json(raw)


class RedisRefreshRegistry:
    """Refresh tokens plus the Lobstr identity each one maps to, stored together
    so a rotation is a single atomic-enough pop/put pair.
    """

    def __init__(self, redis, key: str | bytes | None) -> None:
        self._r = redis
        self._cipher = TokenCipher(key)

    def put(self, refresh_token, link: TokenLink) -> None:
        payload = json.dumps({
            "refresh": refresh_token.model_dump_json(),
            "link": _link_to_json(link, self._cipher),
        })
        ttl = _ttl_from(getattr(refresh_token, "expires_at", None))
        self._r.set(_REFRESH_PREFIX + refresh_token.token, payload, ex=ttl)

    def _load(self, raw):
        from mcp.server.auth.provider import RefreshToken
        d = json.loads(raw)
        return (RefreshToken.model_validate_json(d["refresh"]),
                _link_from_json(d["link"], self._cipher))

    def get(self, token: str):
        raw = self._r.get(_REFRESH_PREFIX + token)
        if raw is None:
            return (None, None)
        return self._load(raw)

    def pop(self, token: str):
        name = _REFRESH_PREFIX + token
        raw = self._r.get(name)
        if raw is None:
            return (None, None)
        self._r.delete(name)
        return self._load(raw)


class MemoryClientRegistry:
    """Default in-process client registry — same interface as the Redis one."""

    def __init__(self) -> None:
        self._d: dict = {}

    def put(self, client_info) -> None:
        self._d[client_info.client_id] = client_info

    def get(self, client_id: str):
        return self._d.get(client_id)


class MemoryRefreshRegistry:
    def __init__(self) -> None:
        self._d: dict = {}

    def put(self, refresh_token, link: TokenLink) -> None:
        self._d[refresh_token.token] = (refresh_token, link)

    def get(self, token: str):
        return self._d.get(token, (None, None))

    def pop(self, token: str):
        return self._d.pop(token, (None, None))


@dataclass
class Stores:
    """The four pieces of state the OAuth server keeps, plus which backend
    supplied them (surfaced at boot so an operator can see it)."""
    tokens: Any
    idempotency: Any
    clients: Any
    refreshes: Any
    backend: str


def make_stores(settings, redis_factory=None) -> Stores:
    """Pick the persistence backend from settings.

    No redis_url -> in-process stores (fine for a single throwaway instance).
    redis_url -> Redis, which also requires token_key: persisting a user's
    Lobstr API token without encryption is refused rather than done quietly.
    """
    from lobstr_mcp.auth.token_store import TokenStore
    from lobstr_mcp.safeguards import IdempotencyStore

    if not settings.redis_url:
        return Stores(TokenStore(), IdempotencyStore(), MemoryClientRegistry(),
                      MemoryRefreshRegistry(), "memory")

    if not settings.token_key:
        raise ValueError(
            "LOBSTR_MCP_REDIS_URL is set but LOBSTR_MCP_TOKEN_KEY is not; "
            "Lobstr API tokens must be encrypted at rest. Generate one with "
            "python -c \"from lobstr_mcp.persistence import TokenCipher; "
            "print(TokenCipher.generate_key())\""
        )

    if redis_factory is None:
        def redis_factory(url):
            import redis
            return redis.Redis.from_url(url)

    client = redis_factory(settings.redis_url)
    return Stores(
        RedisTokenStore(client, settings.token_key),
        RedisIdempotencyStore(client),
        RedisClientRegistry(client),
        RedisRefreshRegistry(client, settings.token_key),
        "redis",
    )
