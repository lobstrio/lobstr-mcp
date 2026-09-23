"""OAuth 2.1 Authorization Server for the Lobstr MCP (Approach A).

Delegates user authentication to Lobstr (via LobstrDelegationClient) and issues
its own MCP tokens, mapping each to the user's Lobstr identity in a TokenStore.
DCR + PKCE + metadata + routes are provided by FastMCP's OAuthProvider base.

The flow (frontend-consent model — the user approves on Lobstr's own frontend
where they are already logged in; no password ever reaches the MCP):
  authorize()        -> redirect the browser to the Lobstr frontend consent page
  complete_consent() -> (called by the /lobstr/consent handler after approval)
                        redeem the single-use grant for the user's identity +
                        token, create an auth code, redirect back to the client
  exchange_authorization_code() -> issue MCP access + refresh tokens, storing
                        the token->Lobstr-identity mapping in the TokenStore
  load_access_token()/revoke_token()/exchange_refresh_token() -> lifecycle

LIVE-VERIFICATION NOTE: the registry/exchange mechanics below are unit tested,
but the full browser round-trip (frontend consent page + grant redemption) and
the exact SDK error contracts must be verified on a deployed instance with real
secrets. Registries are in-memory — swap for a shared store (Redis/Postgres) in
deployment so tokens survive restarts and scale-out.
"""
from __future__ import annotations

import secrets
import time
from urllib.parse import urlparse

from fastmcp.server.auth.auth import ClientRegistrationOptions, OAuthProvider
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthToken

from lobstr_mcp.auth.scopes import ALL_SCOPES
from lobstr_mcp.auth.token_store import TokenLink, TokenStore

_PENDING_TTL = 600             # 10 min for the user to approve on the frontend
_AUTH_CODE_TTL = 600           # 10 min
_ACCESS_TTL = 3600             # 1 h
_REFRESH_TTL = 30 * 24 * 3600  # 30 d

# Documented for reviewers: the AS provider methods implemented below.
REQUIRED_PROVIDER_METHODS: tuple[str, ...] = (
    "register_client", "get_client", "authorize",
    "load_authorization_code", "exchange_authorization_code",
    "load_refresh_token", "exchange_refresh_token",
    "load_access_token", "revoke_token",
)


def build_client_registration_options() -> ClientRegistrationOptions:
    """DCR options: enable dynamic client registration, advertise our scopes."""
    return ClientRegistrationOptions(
        enabled=True, valid_scopes=list(ALL_SCOPES), default_scopes=list(ALL_SCOPES),
    )


class InvalidGrantError(Exception):
    pass


class LobstrOAuthProvider(OAuthProvider):
    def __init__(self, *, base_url, delegation_client, token_store: TokenStore,
                 consent_url: str, now=time.time,
                 client_registry=None, refresh_registry=None) -> None:
        super().__init__(
            base_url=base_url,
            client_registration_options=build_client_registration_options(),
        )
        self._base_url = str(base_url).rstrip("/")
        self._delegation = delegation_client
        self._tokens = token_store
        self._consent_url = consent_url
        self._now = now
        # Registries are injectable so deployment can back them with Redis;
        # the defaults keep everything in this process.
        from lobstr_mcp.persistence import (
            MemoryClientRegistry, MemoryRefreshRegistry,
        )
        self._clients = client_registry or MemoryClientRegistry()
        self._refresh = refresh_registry or MemoryRefreshRegistry()
        # Auth codes and pending requests are in-flight browser state (minutes),
        # so they stay in memory by design — see persistence.py.
        self._codes: dict[str, AuthorizationCode] = {}
        self._code_identity: dict[str, TokenLink] = {}
        # request_id -> (client_id, AuthorizationParams, expires_at)
        self._pending: dict = {}

    # --- pending-request bookkeeping ---
    def _prune_pending(self) -> None:
        now = int(self._now())
        for rid in [r for r, (_, _, exp) in self._pending.items() if exp < now]:
            del self._pending[rid]

    def pending_count(self) -> int:
        """Live count of authorize requests awaiting consent (for tests/metrics)."""
        self._prune_pending()
        return len(self._pending)

    @staticmethod
    def _effective_scopes(requested, granted) -> list[str]:
        """The grant is authoritative. The user may approve fewer scopes than the
        AI client asked for; never issue more than the grant carries."""
        requested = list(requested or [])
        granted = list(granted or [])
        if not granted:
            return requested
        if not requested:
            return granted
        allowed = set(granted)
        scopes = [s for s in requested if s in allowed]
        if not scopes:
            raise InvalidGrantError(
                "none of the requested scopes were granted by the user"
            )
        return scopes

    # --- DCR client registry ---
    async def register_client(self, client_info) -> None:
        self._clients.put(client_info)

    async def get_client(self, client_id):
        return self._clients.get(client_id)

    @staticmethod
    def _redirect_host(client) -> str | None:
        """Host of the client's OAuth redirect_uri — the *verified* identity
        signal for the consent page. Unlike client_name (self-asserted at DCR
        and freely spoofable), the authorization code is only ever delivered
        here, so a client claiming to be "Claude" that returns somewhere else
        is exposed. The frontend shows this verbatim and derives the friendly
        name from it."""
        uris = getattr(client, "redirect_uris", None) or []
        if not uris:
            return None
        host = urlparse(str(uris[0])).hostname
        return host or None

    # --- authorize: send the browser to Lobstr's frontend consent page ---
    async def authorize(self, client, params) -> str:
        self._prune_pending()
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = (client.client_id, params,
                                     int(self._now()) + _PENDING_TTL)
        # Pass the client's DCR display name (self-asserted) and the verified
        # redirect host so the consent page can name who is asking instead of
        # showing an opaque DCR client_id. Both are optional; omit rather than
        # send an empty value so the frontend's fallbacks stay clean.
        extra = {}
        client_name = getattr(client, "client_name", None)
        if client_name:
            extra["client_name"] = client_name
        redirect_host = self._redirect_host(client)
        if redirect_host:
            extra["redirect_host"] = redirect_host
        return construct_redirect_uri(
            self._consent_url,
            request_id=request_id,
            scope=" ".join(params.scopes or []),
            client_id=client.client_id,
            **extra,
        )

    def complete_consent(self, request_id: str, grant: str) -> str:
        """Called by the /lobstr/consent handler after the user approves on the
        Lobstr frontend. Redeems the single-use grant for the user's identity +
        token, creates an auth code, and returns the client redirect URL."""
        self._prune_pending()
        entry = self._pending.get(request_id)
        if entry is None:
            raise InvalidGrantError("unknown or expired consent request")
        client_id, params, _ = entry
        # Redeem before consuming the pending request: a transient failure
        # talking to Lobstr must leave the flow retryable, not dead.
        link = self._delegation.redeem_grant(grant)
        self._pending.pop(request_id, None)  # single-use, only once redeemed
        scopes = self._effective_scopes(params.scopes, link.scopes)
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=scopes,
            expires_at=int(self._now()) + _AUTH_CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=link.lobstr_user_id,
        )
        self._code_identity[code] = link
        return construct_redirect_uri(str(params.redirect_uri),
                                      code=code, state=params.state)

    async def load_authorization_code(self, client, authorization_code):
        code = self._codes.get(authorization_code)
        if code and code.expires_at >= int(self._now()):
            return code
        return None

    async def exchange_authorization_code(self, client, authorization_code):
        code = authorization_code.code
        link = self._code_identity.pop(code, None)
        self._codes.pop(code, None)
        if link is None:
            raise InvalidGrantError("unknown or already-used authorization code")
        scopes = list(authorization_code.scopes)
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self._tokens.put(access, TokenLink(link.lobstr_user_id, link.lobstr_api_token,
                                           scopes, int(self._now()) + _ACCESS_TTL))
        self._refresh.put(
            RefreshToken(token=refresh, client_id=client.client_id, scopes=scopes,
                         expires_at=int(self._now()) + _REFRESH_TTL),
            link)
        return OAuthToken(access_token=access, token_type="Bearer",
                          expires_in=_ACCESS_TTL, scope=" ".join(scopes),
                          refresh_token=refresh)

    async def load_refresh_token(self, client, refresh_token):
        rt, _ = self._refresh.get(refresh_token)
        if rt is None:
            return None
        if rt.expires_at is not None and rt.expires_at < int(self._now()):
            self._refresh.pop(refresh_token)
            return None
        return rt

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        # Rotate: the presented refresh token is retired here, so a replay of a
        # leaked token fails (OAuth 2.1 requirement for public clients).
        _, link = self._refresh.pop(refresh_token.token)
        if link is None:
            raise InvalidGrantError("unknown refresh token")
        new_scopes = self._effective_scopes(
            list(scopes) if scopes else list(refresh_token.scopes), link.scopes
        )
        access = secrets.token_urlsafe(32)
        rotated = secrets.token_urlsafe(32)
        self._tokens.put(access, TokenLink(link.lobstr_user_id, link.lobstr_api_token,
                                           new_scopes, int(self._now()) + _ACCESS_TTL))
        self._refresh.put(
            RefreshToken(token=rotated, client_id=client.client_id, scopes=new_scopes,
                         expires_at=int(self._now()) + _REFRESH_TTL),
            link)
        return OAuthToken(access_token=access, token_type="Bearer",
                          expires_in=_ACCESS_TTL, scope=" ".join(new_scopes),
                          refresh_token=rotated)

    async def load_access_token(self, token):
        link = self._tokens.get(token)
        if link is None:
            return None
        return AccessToken(
            token=token, client_id=link.lobstr_user_id, scopes=list(link.scopes),
            expires_at=int(link.expires_at) if link.expires_at is not None else None,
            subject=link.lobstr_user_id,
            claims={"lobstr_token": link.lobstr_api_token,
                    "lobstr_user_id": link.lobstr_user_id},
        )

    async def revoke_token(self, token) -> None:
        t = getattr(token, "token", None)
        if t:
            self._tokens.revoke(t)
            self._refresh.pop(t)
