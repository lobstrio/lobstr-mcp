# Architecture

```
Claude / ChatGPT / Cursor / …
        │  Streamable HTTP (OAuth 2.1 bearer)
        ▼
  mcp.lobstr.io ── lobstr-mcp (FastMCP) ──────┐
        │  Authorization: Token <per-user>    │  Redis:
        ▼                                      │   - OAuth clients / tokens / refresh
  api.lobstr.io/v1/*  (REST API)               │   - mcp_token -> {user, lobstr_token, scopes}
        ▲                                      │   - idempotency keys
        └── + POST /v1/oauth/mcp-grant{,/redeem}
```

A thin translation layer over `api.lobstr.io/v1` — it holds no scraping logic
and no direct database access.

| Module | Responsibility |
|---|---|
| `server.py` | FastMCP app, tool registration, `/lobstr/consent`, store selection |
| `auth/oauth_provider.py` | OAuth 2.1 authorization server: DCR, PKCE, codes, tokens, rotation |
| `auth/delegation.py` | Server-to-server grant redemption |
| `auth/identity.py`, `auth/verifier.py`, `auth/scopes.py` | Per-request user, scope enforcement |
| `auth/token_store.py`, `persistence.py` | Token map, idempotency, registries (memory or Redis) |
| `lobstr_client.py` | The only thing that talks to `/v1`. Pagination, timeouts, error normalisation |
| `schema_translator.py` | Crawler `input[]` + `/params` → JSON Schema, and input placement |
| `execution.py` | `run_scraper` orchestration, `get_run`, `get_results`, run listing |
| `safeguards.py` | Validation, cost estimation, idempotency store |
| `errors.py` | `LobstrAPIError` → the [error contract](errors.md) |

Two entrypoints: `server:app` (dev, single dev token) and
`server:create_app` (OAuth, per-user) — see [`DEPLOY.md`](../DEPLOY.md).

## How authentication works

Lobstr is the identity provider. The MCP presents a spec-compliant OAuth 2.1
surface to clients and delegates login and consent to Lobstr's own frontend —
no password ever reaches the MCP or the AI client.

```
1. Client hits /mcp unauthenticated
     -> 401 + WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource/mcp"
2. Client self-registers                       POST /register            (DCR)
3. Client starts authorization-code + PKCE     GET  /authorize
4. MCP redirects the browser to Lobstr:
     app.lobstr.io/connect-ai?request_id=…&scope=…&client_id=…
5. User (already logged in) clicks Allow. The page mints a single-use grant:
     POST {API_BASE}/v1/oauth/mcp-grant   -> {"grant": "…", "expires_in": 60}
   and redirects to:
     {MCP_BASE}/lobstr/consent?request_id=…&grant=…
6. MCP redeems the grant server-to-server, gated by a shared service credential:
     POST {API_BASE}/v1/oauth/mcp-grant/redeem
   and redirects the browser back to the client with ?code=…
7. Client exchanges the code                   POST /token              (+ PKCE verifier)
     -> MCP access token + refresh token
8. Every tool call carries the MCP bearer; the server resolves it to that
   user's Lobstr API token and calls /v1 as them.
```

Refresh tokens are **rotated** on every exchange (OAuth 2.1); replaying a
retired one returns 401.

### Endpoints

| Path | Purpose |
|---|---|
| `/mcp` | Streamable HTTP MCP endpoint |
| `/.well-known/oauth-protected-resource/mcp` | Protected Resource Metadata (RFC 9728) |
| `/.well-known/oauth-authorization-server` | Authorization Server Metadata (RFC 8414) |
| `/register` | Dynamic Client Registration |
| `/authorize` | Authorization endpoint (PKCE S256 required) |
| `/token` | Token endpoint (`authorization_code`, `refresh_token`) |
| `/lobstr/consent` | Return target for the Lobstr consent page |

The bare `/.well-known/oauth-protected-resource` path returns 404 **by
design** — RFC 9728 path-insertion means the advertised
`…/oauth-protected-resource/mcp` is the live one.

### Scopes

| Scope | Grants |
|---|---|
| `crawlers:read` | Discover scrapers and read their settings |
| `runs:read` | Check run status |
| `results:read` | Read scraped results |
| `profile:read` | Read identity (`whoami`) and credit balance (`check_credits`) |
| `account:read` | Read connected platform accounts; implies `profile:read` |
| `runs:execute` | Run scrapers and write squid state (**consumes credits**) |

Enforcement is always server-side, whatever the model requests. The grant is
authoritative: if the user approves fewer scopes than the client asked for,
the issued token carries only the intersection.

## Safeguards

- **Cost estimate + confirmation gate.** Estimated from the crawler's real
  per-row/per-email rate. Above the confirmation threshold, or when unknown,
  `run_scraper` refuses until `confirm=true`.
- **Idempotency.** Key derived from `(scraper, normalised input)`, or supply
  `idempotency_key`. A repeat within the TTL returns `already_submitted`
  instead of creating a second paid run.
- **Balance check** happens on the API side; the client no longer
  second-guesses it (see `CHANGELOG.md` 0.4.2).
- **Scope gates.** Read vs execute, enforced server-side.
- **Row cap.** `get_results` defaults to 25 rows per call.
- **Encryption at rest.** Lobstr API tokens are Fernet-encrypted in Redis.

## Related components

| Component | Part |
|---|---|
| `lobstrio/lobstr-mcp` (this repo) | The MCP server |
| Lobstr API | Grant endpoints (`/v1/oauth/mcp-grant{,/redeem}`) |
| Lobstr dashboard | `/connect-ai` consent page |
