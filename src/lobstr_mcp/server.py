from __future__ import annotations

import logging
from pathlib import Path

from fastmcp import FastMCP

from lobstr_mcp.auth.delegation import LobstrDelegationClient
from lobstr_mcp.auth.identity import current_user_client, make_authorizer
from lobstr_mcp.auth.oauth_provider import LobstrOAuthProvider
from lobstr_mcp.auth.token_store import TokenStore
from lobstr_mcp.auth.verifier import LobstrTokenVerifier
from lobstr_mcp.config import get_settings
from lobstr_mcp.lobstr_client import LobstrClient
from lobstr_mcp.observability import init_sentry
from lobstr_mcp.persistence import make_stores
from lobstr_mcp.safeguards import IdempotencyStore
from lobstr_mcp.tools.accounts import register_accounts_tools
from lobstr_mcp.tools.execution import register_execution_tools
from lobstr_mcp.tools.primitives import register_primitive_tools
from lobstr_mcp.tools.scrapers import register_scraper_tools
from lobstr_mcp.tools.user import register_user_tools

# Public marketing page + brand assets live at the repo root (…/lobstr-mcp/assets).
_ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
# Only these paths are servable — the membership check is the path-traversal guard.
_ASSET_WHITELIST = {
    "lobstr-wordmark.svg",
    "logo-red.svg",
    "fonts/segoeui.woff2",
    "fonts/segoeuisb.woff2",
    "fonts/segoeuib.woff2",
    "fonts/segoeuibl.woff2",
}
_ASSET_MEDIA = {".woff2": "font/woff2", ".svg": "image/svg+xml"}


def register_web_pages(mcp) -> None:
    """Serve the public landing page at / and its brand assets under
    /lobstr/assets. Additive to the MCP protocol (still at /mcp) and the OAuth
    routes — a browser hitting mcp.lobstr.io gets the marketing page, AI clients
    keep talking to /mcp."""
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, PlainTextResponse, Response

    try:
        landing_html = (_ASSETS_DIR / "landing.html").read_text(encoding="utf-8")
    except OSError:
        logging.getLogger(__name__).warning(
            "landing.html not found at %s; / will 404", _ASSETS_DIR)
        return

    @mcp.custom_route("/", methods=["GET"])
    async def landing(request: Request):  # noqa: ARG001
        return HTMLResponse(landing_html)

    @mcp.custom_route("/lobstr/assets/{path:path}", methods=["GET"])
    async def lobstr_asset(request: Request):
        rel = request.path_params["path"]
        if rel not in _ASSET_WHITELIST:
            return PlainTextResponse("Not found", status_code=404)
        f = _ASSETS_DIR / rel
        if not f.is_file():
            return PlainTextResponse("Not found", status_code=404)
        return Response(
            f.read_bytes(),
            media_type=_ASSET_MEDIA.get(f.suffix, "application/octet-stream"),
            headers={"Cache-Control": "public, max-age=86400"},
        )


def build_server(settings=None, client_factory=None) -> FastMCP:
    """Unauthenticated server for local dev — auth is a single dev token."""
    init_sentry()
    settings = settings or get_settings()

    if client_factory is None:
        def client_factory() -> LobstrClient:
            return LobstrClient(
                settings.lobstr_api_base,
                settings.dev_token or "",
                timeout=settings.request_timeout,
            )

    mcp = FastMCP(name="Lobstr")
    idem_store = IdempotencyStore()
    register_scraper_tools(mcp, client_factory)
    register_execution_tools(mcp, client_factory, settings, idem_store)
    register_primitive_tools(mcp, client_factory)
    register_user_tools(mcp, client_factory)
    register_accounts_tools(mcp, client_factory)
    register_web_pages(mcp)
    return mcp


def build_authenticated_server(settings=None, token_store=None):
    """Authenticated server: requires a valid MCP bearer token, resolved to a
    per-user Lobstr client, with per-tool scope enforcement. Returns
    (FastMCP, TokenStore).

    The interactive OAuth issuance flow (see auth.oauth_provider) populates the
    TokenStore; here we wire the resource-server verification, per-request
    identity, and read/execute scope gates.
    """
    settings = settings or get_settings()
    token_store = token_store if token_store is not None else TokenStore()
    verifier = LobstrTokenVerifier(token_store, base_url=settings.public_base_url)
    authorizer = make_authorizer()
    idem_store = IdempotencyStore()

    def client_factory() -> LobstrClient:
        return current_user_client(settings)

    mcp = FastMCP(name="Lobstr", auth=verifier)
    register_scraper_tools(mcp, client_factory, authorizer=authorizer)
    register_execution_tools(mcp, client_factory, settings, idem_store,
                             authorizer=authorizer)
    register_primitive_tools(mcp, client_factory, authorizer=authorizer)
    register_user_tools(mcp, client_factory, authorizer=authorizer)
    register_accounts_tools(mcp, client_factory, authorizer=authorizer)
    return mcp, token_store


def build_oauth_server(settings=None):
    """Production entrypoint: full OAuth 2.1 AS (Approach A) + hosted Lobstr
    login route. Returns (FastMCP, TokenStore, LobstrOAuthProvider).

    The interactive login route below is the live-integration boundary — it
    needs the additive Django /v1/oauth/issue-token endpoint, real secrets
    (LOBSTR_MCP_SERVICE_CREDENTIAL), a shared token store, CSRF protection, and
    styling before it is production-trusted.
    """
    init_sentry()
    settings = settings or get_settings()
    stores = make_stores(settings)
    if stores.backend == "memory":
        logging.getLogger(__name__).warning(
            "lobstr-mcp state is in-process: a restart will invalidate every "
            "issued token, drop registered OAuth clients, and forget "
            "idempotency keys (a repeated run could be charged twice). Set "
            "LOBSTR_MCP_REDIS_URL + LOBSTR_MCP_TOKEN_KEY for a real deployment."
        )
    else:
        logging.getLogger(__name__).info("lobstr-mcp state backend: %s",
                                         stores.backend)
    token_store = stores.tokens
    delegation = LobstrDelegationClient(settings.lobstr_api_base,
                                        settings.service_credential or "")
    provider = LobstrOAuthProvider(base_url=settings.public_base_url,
                                   delegation_client=delegation,
                                   token_store=token_store,
                                   consent_url=settings.consent_url,
                                   client_registry=stores.clients,
                                   refresh_registry=stores.refreshes)
    authorizer = make_authorizer()
    idem_store = stores.idempotency

    def client_factory() -> LobstrClient:
        return current_user_client(settings)

    mcp = FastMCP(name="Lobstr", auth=provider)
    register_scraper_tools(mcp, client_factory, authorizer=authorizer)
    register_execution_tools(mcp, client_factory, settings, idem_store,
                             authorizer=authorizer)
    register_primitive_tools(mcp, client_factory, authorizer=authorizer)
    register_user_tools(mcp, client_factory, authorizer=authorizer)
    register_accounts_tools(mcp, client_factory, authorizer=authorizer)

    import anyio.to_thread
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse, RedirectResponse

    @mcp.custom_route("/lobstr/consent", methods=["GET"])
    async def lobstr_consent(request: Request):
        """The Lobstr frontend consent page redirects the browser here (after
        the already-logged-in user approves) with the single-use grant. We
        redeem it and hand the OAuth code back to the AI client."""
        request_id = request.query_params.get("request_id", "")
        grant = request.query_params.get("grant", "")
        if not request_id or not grant:
            return PlainTextResponse("Missing request_id or grant.", status_code=400)
        try:
            # complete_consent() redeems the grant over blocking httpx; keep it
            # off the event loop so one slow Django call can't stall the server.
            redirect_url = await anyio.to_thread.run_sync(
                provider.complete_consent, request_id, grant
            )
        except Exception:
            return PlainTextResponse("Consent failed or request expired.", status_code=400)
        return RedirectResponse(redirect_url, status_code=302)

    register_web_pages(mcp)
    return mcp, token_store, provider


def create_app():
    """ASGI factory for the production OAuth server. Run with:
    uvicorn lobstr_mcp.server:create_app --factory"""
    from starlette.middleware import Middleware

    from lobstr_mcp.auth.http_guard import AuthEndpointGuard

    oauth_mcp, _, _ = build_oauth_server()
    # The SDK's /register, /token and /revoke routes 500 on a bare OPTIONS or a
    # non-JSON registration body (Sentry MCP-4); the guard answers those itself.
    return oauth_mcp.http_app(middleware=[Middleware(AuthEndpointGuard)])


# Dev (no-auth) app for local smoke testing with LOBSTR_DEV_TOKEN.
mcp = build_server()
app = mcp.http_app()
