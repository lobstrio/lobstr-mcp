# Configuration reference

All configuration is via environment variables (`.env` for Docker — see
[`.env.example`](../.env.example)). Never commit secrets. See
[`DEPLOY.md`](../DEPLOY.md) for the deployment runbook these variables feed
into.

## Upstream

| Variable | Default | Purpose |
|---|---|---|
| `LOBSTR_API_BASE` | `https://api.lobstr.io/v1` | Lobstr REST API base |
| `LOBSTR_REQUEST_TIMEOUT` | `30` | HTTP timeout (seconds) |
| `LOBSTR_RUN_CONFIRM_THRESHOLD` | `100` | Credit ceiling before `run_scraper` demands `confirm=true` |

## OAuth server

| Variable | Required | Purpose |
|---|---|---|
| `LOBSTR_MCP_PUBLIC_URL` | yes | Public base URL; becomes the OAuth issuer. **Must be `https://`** for real clients. |
| `LOBSTR_MCP_SERVICE_CREDENTIAL` | yes | Shared secret for the server-to-server grant redemption |
| `LOBSTR_CONSENT_URL` | `https://app.lobstr.io/connect-ai` | Lobstr frontend consent page |

## Persistence

| Variable | Required | Purpose |
|---|---|---|
| `LOBSTR_MCP_REDIS_URL` | production | Persists tokens, DCR clients, refresh tokens, idempotency keys |
| `LOBSTR_MCP_TOKEN_KEY` | with Redis | Fernet key encrypting Lobstr API tokens at rest |

Without `LOBSTR_MCP_REDIS_URL` everything is in-process and the server **warns
loudly at boot**: a restart then invalidates every issued token, drops every
registered OAuth client, and forgets idempotency keys — which can charge a
user twice for one request. Redis is also **required** for more than one
instance or worker, since a consent grant minted by one process must be
redeemable by another. Give it a database of its own — the Lobstr Django
app's cache uses db 0.

Generate a token-encryption key:

```bash
python -c "from lobstr_mcp.persistence import TokenCipher; print(TokenCipher.generate_key())"
```

## Dev-only

| Variable | Purpose |
|---|---|
| `LOBSTR_DEV_TOKEN` | A real Lobstr API token; makes the no-auth dev app act as that single user |

## Versioning

The version lives once, in `pyproject.toml`, read back through
`lobstr_mcp.__version__`. It appears in the `User-Agent: lobstr-mcp/<version>`
sent to the API, in the Sentry `release`, and in `whoami`'s `server_version` —
so a deploy is identifiable from the API logs, from Sentry, and from an AI
client. Bump it in the same PR as the change, alongside a `CHANGELOG.md`
entry (Keep a Changelog headings).
