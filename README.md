# lobstr-mcp

**Operational** MCP server for Lobstr. It lets an AI client authenticate as a
Lobstr user and **discover → configure → run → monitor → retrieve** scrapers
in-conversation, with no manual CSV export/import.

A thin translation layer over `api.lobstr.io/v1` — it holds no scraping logic
and no matrix DB access. Streamable HTTP on a single `/mcp` endpoint.

> **Not the docs MCP.** `https://docs.lobstr.io/mcp` is a separate, public,
> unauthenticated server that answers "how does Lobstr work?". This one is
> "do this in my account": it authenticates as a user and spends real credits.

**Live at `https://mcp.lobstr.io/mcp`.** Add it to any MCP client by URL — see
[Connecting an AI client](#connecting-an-ai-client).

---

## Contents

- [Connecting an AI client](#connecting-an-ai-client)
- [How authentication works](#how-authentication-works)
- [Tool reference](#tool-reference)
- [Error contract](#error-contract)
- [Safeguards](#safeguards)
- [Configuration](#configuration)
- [Running locally](#running-locally)
- [Deployment](#deployment)
- [Architecture](#architecture)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)

---

## Connecting an AI client

No directory listing or marketplace approval is needed to use this server —
those only affect discoverability. Any MCP client can add it by URL.

The server implements **Dynamic Client Registration**, so clients register
themselves: you never paste a client ID or secret.

**Claude Code**

```bash
claude mcp add --transport http lobstr https://mcp.lobstr.io/mcp
```

Then run `/mcp` in Claude Code and authenticate — the browser opens Lobstr's
consent page, you click **Allow**, and you are returned to the client. On a
remote/headless box the authorize URL is printed for you to open locally.

**Claude Desktop** — add to its MCP config:

```json
{
  "mcpServers": {
    "lobstr": {
      "type": "http",
      "url": "https://mcp.lobstr.io/mcp"
    }
  }
}
```

**Cursor** (`.cursor/mcp.json`) — same shape as Claude Desktop.

**Claude.ai / ChatGPT** — add it as a **custom connector** in settings, using
`https://mcp.lobstr.io/mcp`. Both drive the OAuth flow for you.

**Codex CLI** — register it as a remote MCP server in `~/.codex/config.toml`,
using `https://mcp.lobstr.io/mcp`. Check the Codex docs for the exact key names
for your version; they have changed between releases and are not pinned here.

> **HTTPS is mandatory** for real clients. The MCP SDK rejects a non-HTTPS
> OAuth issuer on a public host, so `LOBSTR_MCP_PUBLIC_URL` must be an
> `https://` URL. Plain `http://localhost` works only for local development —
> see [Troubleshooting](#troubleshooting).

---

## How authentication works

**Approach A — delegation.** Lobstr is the identity provider. The MCP presents a
spec-compliant OAuth 2.1 surface to clients and delegates login and consent to
Lobstr's own frontend. **No password ever reaches the MCP or the AI client.**

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

### Endpoints

| Path | Purpose |
|---|---|
| `/mcp` | Streamable HTTP MCP endpoint |
| `/.well-known/oauth-protected-resource/mcp` | Protected Resource Metadata (RFC 9728) |
| `/.well-known/oauth-authorization-server` | Authorization Server Metadata (RFC 8414) |
| `/register` | Dynamic Client Registration |
| `/authorize` | Authorization endpoint (PKCE S256 required) |
| `/token` | Token endpoint (authorization_code, refresh_token) |
| `/lobstr/consent` | Return target for the Lobstr consent page |

The bare `/.well-known/oauth-protected-resource` path returns 404 **by design** —
RFC 9728 path-insertion means the advertised `…/oauth-protected-resource/mcp` is
the live one.

### Scopes

| Scope | Grants |
|---|---|
| `crawlers:read` | Discover scrapers and read their settings |
| `runs:read` | Check run status |
| `results:read` | Read scraped results |
| `account:read` | Read account / balance |
| `runs:execute` | Run scrapers (**consumes credits**), including the squid writes that requires (settings, tasks, and — since v0.3.0 — attaching a platform account) |

Enforcement is always server-side, whatever the model requests. The grant is
authoritative: if the user approves fewer scopes than the client asked for, the
issued token carries only the intersection, and an approval overlapping nothing
is rejected outright.

`attach_account` additionally requires `account:read`, since (unlike `run_scraper`)
it can enumerate the user's connected accounts by id/username — that's what
`account:read`, not `runs:execute`, is meant to gate. No new scope was added for
either tool.

Refresh tokens are **rotated** on every exchange (OAuth 2.1); replaying a
retired one returns 401.

---

## Tool reference

Eight tools. Squid/Task/Run internals are hidden. Each declares MCP annotations
so clients know which calls have side effects or cost credits.

### `search_scrapers(query: str)`
`readOnly`, `openWorld` · scope `crawlers:read`

Find the most relevant Lobstr scrapers. Walks every page of `GET /crawlers`.

Returns `{count, results: [{id, name, slug, description, credits_per_row, credits_per_email, is_premium, is_available}]}`.

### `get_scraper_details(scraper: str)`
`readOnly` · scope `crawlers:read`

The heart of dynamic discovery — new scrapers work with **zero** code changes.

Returns `{id, name, description, input_schema, param_levels, required_account_type, output_fields, credits_per_row, credits_per_email, is_available, max_concurrency}`.

- `input_schema` — JSON Schema translated from the crawler's live `input[]`, with
  `required`, types, defaults and examples. Read this before calling `run_scraper`.
- `param_levels` — where each input goes: `task`, `squid`, or `function`. `run_scraper`
  routes its `input` by this map for you automatically. Building a squid by hand with
  `create_squid`'s `config` does **not** route anything — `config` is sent to the API as-is,
  so squid-level keys go in directly and function-level ones must be nested under a
  `functions` key yourself; task-level fields never belong in `config` at all (use
  `add_tasks`). Act on this map before calling `create_squid`, not after — a
  function-level param sent flat there is rejected, and the squid it half-created is
  awkward to recover.
- `required_account_type` — `null` when the crawler needs no platform account, else the
  account-type slug (e.g. `"linkedin-sync"`) a squid built from it needs attached before a
  run can succeed (`attach_account` or `run_scraper`'s `account_id`) — otherwise the run is
  created but fails asynchronously with `done_reason: "no_accounts"`.

### `list_my_scrapers()`
`readOnly` · scope `crawlers:read`

The user's saved configurations ("run my Rightmove scraper again").
Returns `{count, scrapers: [{id, name}]}`.

### `get_my_scraper(squid_id: str)`
`readOnly` · scope `crawlers:read`

Inspect one saved configuration.

### `run_scraper(scraper: str, input: dict, confirm: bool = False, idempotency_key: str | None = None, squid_id: str | None = None, account_id: str | None = None, replace_tasks: bool = False)`
**`destructive`, non-idempotent, `openWorld`** · scope `runs:execute` · **spends credits**

Validates `input` against the translated schema, estimates cost, then
orchestrates create squid → save settings → add task(s) → start run.

- Returns `{needs_confirmation: true, estimate, message, hint}` when the cost is
  above `LOBSTR_RUN_CONFIRM_THRESHOLD` **or unknown**. Call again with
  `confirm=true` to execute. Because Lobstr prices per result row, the total is
  genuinely unknowable up front, so this gate fires for most runs — by design.
- **A squid's saved inputs are never replaced unless you ask.** A squid holds a
  list of task rows and *every* run scrapes all of them, so applying one new
  task-level input to a squid that already has rows would mean either deleting
  what its owner saved (the old behaviour, silent and destructive) or running
  the whole list at that many times the cost. With `squid_id`:
  - no `input` → runs the saved rows as they are;
  - task-level `input`, squid has rows → `{error_code: "squid_has_tasks",
    existing_task_count, new_task, message}`, nothing written, no run started;
    the message names the four ways forward (run them as they are, a separate
    new scraper, `add_tasks` + re-run, or `replace_tasks=true`);
  - task-level `input`, squid empty → added and run, no `/empty` call;
  - squid-level-only `input` → merged into the saved settings (the API merges
    `params`), rows untouched and all of them run;
  - rows unreadable → `tasks_unreadable` for a task-level input; a
    settings-only input still runs with the count reported as unknown.

  `replace_tasks=true` restores the old behaviour deliberately: it empties the
  squid and leaves only your input. The config is saved first, the rows are
  snapshotted and restored if emptying or adding then fails, and an
  unrecoverable loss comes back as `tasks_lost` + `lost_tasks` verbatim (there
  is no atomic task swap upstream, and adding before emptying is refused on a
  squid at its crawler's max task count).
- Account-backed crawlers (LinkedIn Leads, Sales Navigator Leads, …) need a
  connected platform account attached before a run can start. `run_scraper`
  resolves/attaches this automatically when it's unambiguous (squid has none
  yet, exactly one healthy, unlocked, right-type account connected); pass
  `account_id` to pick one explicitly — it's never refused for being
  unhealthy (that's your call), the response just carries an
  `account_warning` when it isn't fully healthy (locked, cookies_expired, …).
  With several such candidates it returns
  `{error_code: "multiple_accounts_available", required_type, message}` — call
  `list_accounts` to see them, then retry with `account_id`. With none
  connected, `{error_code: "no_account_available", required_type}` — there's
  nothing to list, connect one first (a locked-but-otherwise-fine candidate is
  reported separately, `account_locked`, since LinkedIn/Sales Navigator locks
  are routine and clear on their own — attach it explicitly by id instead of
  connecting a new account). Never touches an account already attached to the
  squid. See `attach_account` below for attaching one outside a run.
- On success: `{run_id, squid_id, scraper, status, estimate, submitted_input,
  account_attached, tasks_action, task_count, tasks_note}`, plus
  `replaced_task_count` on the replace path and `account_warning` when
  relevant. `tasks_action` is `created` / `added` / `replaced` / `unchanged`
  and `task_count` is how many rows this run actually scrapes (`null` when
  this client didn't read them) — a caller that only reads `run_id` cannot
  tell "your input is all this run scrapes" from "your input plus 50 rows
  somebody else saved", which is the difference in the bill. The estimate is
  still computed for a single input; `tasks_note` says so when the run covers
  more. `account_attached`
  reports whether the squid has an account attached *now* — true whether this
  call attached it, an earlier call did, or it was reused on a squid that
  already had one — not whether this specific call was the one that attached
  it. It's the account id when you passed `account_id` yourself (even if it
  was already attached and nothing was written); otherwise a bare
  `true`/`false` of that current state — an auto-picked or pre-existing
  account's id is never exposed to a `runs:execute`-only
  token (see the scope note above).

### `attach_account(squid_id: str, account_id: str | None = None)`
**non-destructive, idempotent, `openWorld`** · scope `runs:execute` + `account:read`

Links a connected platform account to a squid — the standalone version of the
auto-attach `run_scraper` does inline, for pre-configuring a squid or fixing
one that's missing an account. **Only ever adds**: the underlying API field
(`POST /squids/{hash}` `accounts`) is full-replace, so this reads the squid's
current accounts first and re-sends that set plus the new one — nothing
already attached is silently detached by this call (the read and the write
are two separate requests, not an atomic compare-and-set, so a concurrent
change to the same squid can still race). With no `account_id`, auto-picks
only when the squid has no account yet and exactly one healthy, unlocked,
exact-type-match account is connected; a locked candidate is reported
separately (`account_locked`) rather than as "none available," since a lock
is routine and transient and the account can still be attached explicitly by
id; otherwise returns the candidates (or the required platform) instead of
guessing. An explicit `account_id` is never refused for being unhealthy — the
result carries `account_status` and a `warning` instead. A wrong-platform
account, an unknown account id, and a crawler needing no account each come
back as a distinct `error_code` with a next-step message, not the raw
upstream error.

### `get_run(run_id: str)`
`readOnly` · scope `runs:read`

Returns `{run_id, status, is_done, progress, tasks_total, tasks_done, total_results, credits_consumed, done_reason, started_at, ended_at, duration, stats}`.

`status` is the authoritative value from the run detail endpoint:
`pending` | `running` | `done` | `error` | `paused` | `aborted`.

### `get_results(run_id=None, squid_id=None, page=1, page_size=None, fields=None)`
`readOnly` · scope `results:read`

One page of results, **capped at 25 rows** by default so a large dataset never
floods the model's context. Pass exactly one of `run_id` or `squid_id`.

Returns `{total_results, page, total_pages, returned, available_fields, next, results}`.
Use `fields` to select columns.

### The intended flow

```
search_scrapers → get_scraper_details → (model reads the schema)
               → run_scraper → get_run → get_results
```

There is never a per-scraper tool. New Lobstr scrapers appear automatically.

**Deferred (post-V1):** `create_scraper`, `update_scraper`, `list_runs`,
`cancel_run`, `export_results`, `get_account_balance`, `get_usage` — kept out to
keep tool selection reliable.

---

## Error contract

Tools never surface a raw upstream 4xx/5xx. Every failure is a stable
`error_code` plus a human message, and a remediation `hint` where one exists —
so the model can self-correct instead of guessing.

| `error_code` | Meaning |
|---|---|
| `validation_error` | Input failed schema validation. `errors` lists per-field messages (e.g. `["url is required"]`). |
| `invalid_request` | Bad tool arguments (e.g. neither `run_id` nor `squid_id`). |
| `insufficient_credits` | Balance below the estimate. Includes `required` / `available`. |
| `unauthorized` | Token invalid or expired — re-authorize. |
| `forbidden` | Authenticated but not permitted. |
| `not_found` | No such run / squid / scraper. |
| `scraper_not_found` | Unknown scraper id. |
| `no_account_needed` | `account_id` was passed for a crawler that doesn't use a platform account. |
| `account_not_found` | The given `account_id` doesn't exist, or belongs to another user. |
| `account_type_mismatch` | The given account's platform doesn't match what the scraper needs. Includes `required_type` / `given_type`. |
| `account_already_attached` | `attach_account` called with no `account_id` on a squid that already has one — auto-pick never runs there; pass one explicitly. |
| `no_account_available` | No account of the required platform (unlocked and healthy) is connected. Includes `required_type`. |
| `account_locked` | Only a locked account of the required platform is connected — routine/transient on LinkedIn/Sales Navigator, not "none available." Attach it explicitly by id if you want to use it anyway. `attach_account` includes `candidates`; `run_scraper` doesn't. |
| `multiple_accounts_available` | Several healthy candidates exist; auto-pick won't guess. `attach_account` includes `candidates`; `run_scraper` doesn't (see its scope note above) — call `list_accounts`. |
| `accounts_unreadable` | The squid's current `accounts` field wasn't in a shape this client recognizes; nothing was written, to avoid silently dropping an existing link. |
| `squid_has_tasks` | `run_scraper` was given a task-level `input` for a squid that already has saved rows; nothing was written, since applying it means deleting them or running all of them. Includes `existing_task_count` and `new_task`; pass `replace_tasks=true` to delete them on purpose. |
| `tasks_unreadable` | The squid's saved task rows couldn't be listed, so `run_scraper` can't tell whether applying the input would discard them; nothing was written. |
| `scraper_not_ready` | Settings were not saved before starting the run. |
| `conflict` | Upstream 409. |
| `rate_limited` | Throttled. |
| `upstream_rejected` | Upstream 400 with a reason (carries `upstream_type`). |
| `upstream_unavailable` | Upstream 5xx. |
| `upstream_error` | Any other upstream failure. |

Every one also carries `upstream_status`, and `upstream_type` when Lobstr
supplied one. `needs_confirmation` is a **flag, not an error** — see
`run_scraper`.

---

## Safeguards

- **Cost estimate + confirmation gate.** Estimated from the crawler's real
  `credits_per_row` / `credits_per_email` (which the API returns as
  `{legacy, current}` dicts — the current rate wins). Above
  `LOBSTR_RUN_CONFIRM_THRESHOLD`, or when unknowable, `run_scraper` refuses
  until `confirm=true`. It never invents a number.
- **Idempotency.** Key derived from `(scraper, normalized input)`, or supply
  `idempotency_key`. A repeat within the TTL returns
  `{status: "already_submitted", run_id, idempotent: true}` instead of creating
  a second **paid** run. Protects against model retries and agent loops.
- **Balance check.** `GET /user/balance` before executing; a shortfall returns
  `insufficient_credits`, never a raw upstream error.
- **Scope gates.** Read vs execute, enforced server-side.
- **Row cap.** `get_results` defaults to 25 rows per call.
- **Encryption at rest.** Lobstr API tokens are Fernet-encrypted in Redis.

---

## Configuration

All via environment (`.env` for Docker — see `.env.example`). Never commit secrets.

### Upstream

| Variable | Default | Purpose |
|---|---|---|
| `LOBSTR_API_BASE` | `https://api.lobstr.io/v1` | Lobstr REST API base |
| `LOBSTR_REQUEST_TIMEOUT` | `30` | HTTP timeout (seconds) |
| `LOBSTR_RUN_CONFIRM_THRESHOLD` | `100` | Credit ceiling before `run_scraper` demands `confirm=true` |

### OAuth server

| Variable | Required | Purpose |
|---|---|---|
| `LOBSTR_MCP_PUBLIC_URL` | yes | Public base URL; becomes the OAuth issuer. **Must be `https://`** for real clients. |
| `LOBSTR_MCP_SERVICE_CREDENTIAL` | yes | Shared secret for the server-to-server grant redemption |
| `LOBSTR_CONSENT_URL` | `https://app.lobstr.io/connect-ai` | Lobstr frontend consent page |

### Persistence

| Variable | Required | Purpose |
|---|---|---|
| `LOBSTR_MCP_REDIS_URL` | production | Persists tokens, DCR clients, refresh tokens, idempotency keys |
| `LOBSTR_MCP_TOKEN_KEY` | with Redis | Fernet key encrypting Lobstr API tokens at rest |

Without `LOBSTR_MCP_REDIS_URL` everything is in-process and the server **warns
loudly at boot**: a restart then invalidates every issued token, drops every
registered OAuth client, and forgets idempotency keys — which can charge a user
twice for one request. Redis is also **required** for more than one instance or
worker, since a consent grant minted by one process must be redeemable by
another. Give it a database of its own; Django's cache uses db 0.

Generate a key:

```bash
python -c "from lobstr_mcp.persistence import TokenCipher; print(TokenCipher.generate_key())"
```

### Dev-only

| Variable | Purpose |
|---|---|
| `LOBSTR_DEV_TOKEN` | A real Lobstr API token; makes the no-auth dev app act as that single user |

---

## Running locally

```bash
uv sync --extra dev
```

**Dev mode (no OAuth)** — quickest way to exercise the tools against the live API:

```bash
export LOBSTR_DEV_TOKEN=<your Lobstr API token>
uv run uvicorn lobstr_mcp.server:app --port 8000
python scripts/smoke_test.py http://localhost:8000/mcp
# optional, COSTS CREDITS:
python scripts/smoke_test.py http://localhost:8000/mcp --run "dentists in Manchester"
```

**Full OAuth server** — the production shape:

```bash
uv run uvicorn lobstr_mcp.server:create_app --factory --port 8000
```

Check the discovery half without an account:

```bash
curl -s -i -X POST localhost:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# -> 401 + www-authenticate: Bearer resource_metadata="…"
```

---

## Deployment

```bash
git clone -b feat/operational-mcp git@github.com:lobstrio/lobstr-mcp.git
cd lobstr-mcp
cp .env.example .env    # then edit
docker compose up -d --build
```

Compose runs the **OAuth entrypoint** by default with `network_mode: host` —
required when `LOBSTR_API_BASE` points at a Django instance on `127.0.0.1`,
because the container must share the host's network namespace. Port 8000 binds
directly (no `ports:` mapping).

For the dev no-auth app instead:

```bash
docker compose run --rm --service-ports lobstr-mcp \
  uvicorn lobstr_mcp.server:app --host 0.0.0.0 --port 8000
```

See `DEPLOY.md` for the full runbook and the pre-launch checklist.

### Depends on

All three are live in production:

- The additive Lobstr API endpoints `POST /v1/oauth/mcp-grant` and
  `POST /v1/oauth/mcp-grant/redeem`, gated by `MCP_SERVICE_CREDENTIAL`.
- The Lobstr frontend `/connect-ai` consent page, built with
  `VITE_MCP_BASE=https://mcp.lobstr.io`. Vite inlines that at build time, so a
  rebuild without it silently ships an `undefined` base and every **Allow**
  click dies on `new URL()` — see `docs/frontend/CONSENT-FLOW.md`.
- TLS/DNS for `mcp.lobstr.io`, provisioned secrets and Redis — see `DEPLOY.md`.

Deployed as its own container, separate from the rest of the platform, to
isolate blast radius.

---

## Architecture

```
Claude / ChatGPT / Codex
        │  Streamable HTTP (OAuth 2.1 bearer)
        ▼
  mcp.lobstr.io ── lobstr-mcp (FastMCP) ──────┐
        │  Authorization: Token <per-user>    │  Redis:
        ▼                                      │   - OAuth clients / tokens / refresh
  api.lobstr.io/v1/*  (Django + DRF)           │   - mcp_token -> {user, lobstr_token, scopes}
        ▲                                      │   - idempotency keys
        └── + POST /v1/oauth/mcp-grant{,/redeem}
```

| Module | Responsibility |
|---|---|
| `server.py` | FastMCP app, tool registration, `/lobstr/consent`, store selection |
| `auth/oauth_provider.py` | OAuth 2.1 AS: DCR, PKCE, codes, tokens, rotation |
| `auth/delegation.py` | Server-to-server grant redemption |
| `auth/identity.py`, `auth/verifier.py`, `auth/scopes.py` | Per-request user, scope enforcement |
| `auth/token_store.py`, `persistence.py` | Token map, idempotency, registries (memory or Redis) |
| `lobstr_client.py` | The only thing that talks to `/v1`. Pagination, timeouts, error normalization |
| `schema_translator.py` | Crawler `input[]` + `/params` → JSON Schema, and input placement |
| `execution.py` | `run_scraper` orchestration, `get_run`, `get_results` |
| `safeguards.py` | Validation, cost estimation, idempotency store |
| `errors.py` | `LobstrAPIError` → the error contract |

Two entrypoints: `server:app` (dev, single dev token) and
`server:create_app` (OAuth, per-user).

### Related components

| Component | Part |
|---|---|
| `lobstrio/lobstr-mcp` | this server |
| Lobstr API | grant endpoints (`/v1/oauth/mcp-grant{,/redeem}`) |
| Lobstr dashboard | `/connect-ai` consent page |

---

## Testing

```bash
uv run pytest          # full suite, no network or broker needed
```

Unit-tested against recorded `/v1` shapes: schema translation, pagination
envelopes, cost estimation, idempotency, error mapping, the full OAuth handshake
(DCR + PKCE + rotation), scope enforcement, and Redis persistence via
`fakeredis`.

`scripts/smoke_test.py` exercises the live API in dev mode.

> Fixtures must reflect **recorded** responses. The first implementation was
> written from the design spec instead, and encoded field names the API does not
> use — nine defects that only live testing surfaced. If you add a fixture,
> capture a real response first.

---

## Versioning and changelog

The version lives once, in `pyproject.toml`, and is read back through
`lobstr_mcp.__version__` (package metadata). It appears in the
`User-Agent: lobstr-mcp/<version>` sent to the API, in the Sentry `release`, and
in the `server_version` field of the `whoami` tool, so a deploy is identifiable
from the API logs, from Sentry and from an AI client.

Bump it in the same PR as the change, and add the entry to `CHANGELOG.md`
(Keep a Changelog headings). There is no PyPI package: a deploy is
`git pull` + `docker compose up -d --build` on the box (see `DEPLOY.md`), and
the container installs the package, which is where the metadata comes from.

## Troubleshooting

**Client says the server is unauthorized / OAuth fails to start.**
`LOBSTR_MCP_PUBLIC_URL` must be `https://` on any public host — the MCP SDK
rejects a non-HTTPS issuer. For local frontend work, tunnel instead:
`ssh -L 8000:localhost:8000 -L 8012:localhost:8012 <host>`.

**Everyone gets logged out after a deploy.**
`LOBSTR_MCP_REDIS_URL` is unset, so state is in-process. The boot log warns
about this.

**Consent fails with "Consent failed or request expired."**
The grant is single-use and lives ~60s. Also check
`LOBSTR_MCP_SERVICE_CREDENTIAL` matches Django's: redemption returning 401
means credential mismatch, whereas 400 `InvalidGrant` means the credential was
accepted and only the grant was stale.

**`run_scraper` returns `upstream_rejected` / `InvalidParam`.**
Some squid inputs are **function toggles** the API accepts only nested under
`params.functions` (`extract_emails_from_website`, `collect_business_details`,
`fetch_business_images`). `GET /crawlers/{hash}/params` is authoritative and the
translator uses it. If a new toggle appears, confirm it is listed under
`squid.functions` there.

**`run_scraper` always asks for confirmation.**
Expected. Lobstr prices per result row, so the total is unknowable before the
run. The estimate still reports the real per-row rate.

**A run sits in `pending` forever, or flips to `error` immediately** *(platform side, not this server)*
The MCP server only starts the run; executing it happens on the Lobstr
platform. If `get_run` keeps reporting `pending`, or the run fails straight
away with no results, the run failed on the platform side. Contact Lobstr
support with the run id from `get_run` (and the squid id) so the team can look
into it.

**`get_scraper_details` on a bad id returns `upstream_unavailable`.**
Upstream quirk: `GET /crawlers/{unknown}` answers 500 rather than 404, so it
cannot be mapped to `not_found`. Verify the id from `search_scrapers`.
