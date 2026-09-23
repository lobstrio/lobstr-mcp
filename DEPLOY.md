# Deploying lobstr-mcp (test server → production)

Values in angle brackets (`<prod-host>`, `<app-user-home>`, `<api-worker-1>`,
...) are placeholders for your own infrastructure.

## 0. Prereqs
- Docker + docker compose on the host.
- A Lobstr API token for smoke testing (dev mode), and — for the OAuth server —
  the `LOBSTR_MCP_SERVICE_CREDENTIAL` shared with the Django
  `/v1/oauth/issue-token` endpoint.

## 1. Pull the code on the test server
```
git clone git@github.com:lobstrio/lobstr-mcp.git
cd lobstr-mcp
cp .env.example .env      # then edit .env
```

`.env` is edited in place from then on. If you need a backup before an edit,
put it outside the checkout (e.g. `~/env-backups/`), mode `0600`, owner-only —
never a copy sitting next to the live file, ignored or not.

## 2a. Full OAuth server (the default)
`docker compose up -d --build` runs the OAuth entrypoint
(`lobstr_mcp.server:create_app`) with `network_mode: host` — the container needs
the host network namespace because `LOBSTR_API_BASE` points at the Django
instance on `127.0.0.1:<api-port>`. Required in `.env`:
`LOBSTR_MCP_PUBLIC_URL`, `LOBSTR_API_BASE`, `LOBSTR_MCP_SERVICE_CREDENTIAL`,
`LOBSTR_CONSENT_URL`.

Connect an MCP client (Claude/ChatGPT) to `https://<host>/mcp`; it will
discover `/.well-known/oauth-*`, register via DCR, and send the user to the
Lobstr frontend consent page (`LOBSTR_CONSENT_URL`), which returns to
`/lobstr/consent?request_id=…&grant=…`.

Verify the discovery + authorize half of the flow with no account needed:
```
curl -s -i -X POST localhost:8000/mcp -H 'Accept: application/json, text/event-stream' \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# -> 401 + www-authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource/mcp"
```
(The bare `/.well-known/oauth-protected-resource` path 404s by design; RFC 9728
path-insertion means the advertised `…/oauth-protected-resource/mcp` is the live
one.)

## 2b. Smoke test (dev, no OAuth) — validate the tools against live api.lobstr.io
Set `LOBSTR_DEV_TOKEN` in `.env` to a real Lobstr API token and override the
command to the no-auth app:
```
docker compose run --rm --service-ports lobstr-mcp \
  uvicorn lobstr_mcp.server:app --host 0.0.0.0 --port 8000
python scripts/smoke_test.py http://localhost:8000/mcp
# optional (costs credits): actually run the top scraper
python scripts/smoke_test.py http://localhost:8000/mcp --run "dentists in Manchester"
```
This exercises search → details → (optional) run against the real Lobstr API.

> **Requires** the additive Django endpoints `POST /v1/oauth/mcp-grant` and
> `POST /v1/oauth/mcp-grant/redeem` to be live, the Lobstr frontend consent
> page to exist, plus TLS/DNS for `mcp.lobstr.io`. See
> `docs/frontend/CONSENT-FLOW.md` for the full contract.

## 2c. Production — `mcp.lobstr.io`

Deployed on the host that already serves `api.lobstr.io` (`<prod-host>`, which
the `mcp.lobstr.io` A record points at). Docker only — deliberately kept
separate from the API's own process manager.

`.env` (mode `0600`, owner read/write only — not group- or world-readable;
`chmod 600 .env` if it's ever looser; never committed):

```
LOBSTR_API_BASE=https://api.lobstr.io/v1
LOBSTR_MCP_PUBLIC_URL=https://mcp.lobstr.io
LOBSTR_MCP_SERVICE_CREDENTIAL=<shared with Django, see below>
LOBSTR_CONSENT_URL=https://app.lobstr.io/connect-ai
LOBSTR_MCP_REDIS_URL=redis://127.0.0.1:6379/<redis-db>   # a db nothing else on the host uses
LOBSTR_MCP_TOKEN_KEY=<Fernet key>
```

The container binds **127.0.0.1:8000**, not `0.0.0.0` — with
`network_mode: host` a wildcard bind publishes the plaintext app to the
internet next to the TLS vhost unless a host firewall blocks it. nginx
terminates TLS and proxies to it (`<nginx-vhost>`), with
`proxy_buffering off` and 3600s read/send timeouts so Streamable HTTP/SSE
responses are not buffered or cut. Certificate issued with
`certbot --nginx -d mcp.lobstr.io` (auto-renewing).

**Django side.** `MCP_SERVICE_CREDENTIAL` lives in `<app-user-home>/.env`
(loaded into the API processes' environment) and must match
`LOBSTR_MCP_SERVICE_CREDENTIAL` here. It is read at import time, so every API
instance needs restarting after it changes — one at a time, nginx fails over:

```
pm2 restart <api-worker-1> --update-env    # verify, then
pm2 restart <api-worker-2> --update-env
```

Verify the credential is accepted (400 `InvalidGrant` = accepted and only the
grant was bogus; 401 = mismatch):

```
curl -s -X POST https://api.lobstr.io/v1/oauth/mcp-grant/redeem \
  -H 'Content-Type: application/json' \
  -H "X-MCP-Service-Credential: $MCP_SERVICE_CREDENTIAL" -d '{"grant":"bogus"}'
```

Post-deploy smoke (no account needed): `GET /` → 200 HTML (the public landing
page) and `GET /lobstr/assets/lobstr-wordmark.svg` → 200 `image/svg+xml` (brand
assets; the same catch-all `location /` that proxies `/mcp` serves these — no
extra nginx block); `POST /mcp` → 401 + `WWW-Authenticate`;
`/.well-known/oauth-protected-resource/mcp` and
`/.well-known/oauth-authorization-server` → 200 with the `https://mcp.lobstr.io/`
issuer; `POST /register` → 201; `GET /authorize?…` → 302 to
`LOBSTR_CONSENT_URL` carrying `request_id`/`scope`/`client_id`;
`GET /lobstr/consent?request_id=nope&grant=bogus` → 400.

## 2d. Logs (surviving a redeploy)

`docker compose up -d --build` always recreates the container (new container
ID) and removes the old one. With the default `json-file` logging driver that
takes the previous container's log file with it — `max-size`/`max-file` only
cap disk usage, they do not change this. That has happened in practice: the
uvicorn lines from before a rebuild were gone, and the timeline had to be
rebuilt from `git reflog` and nginx access logs instead.

`docker-compose.yml` sets `logging: driver: journald` with a fixed
`tag: lobstr-mcp`. The host's systemd journal lives outside the container's
lifecycle, so a rebuild does not take it with it, and `docker logs` still
works against a journald-backed container (unlike syslog/fluentd). The tag is
fixed on purpose: the container ID and even the container name can change
across a rebuild, the tag does not, so the retrieval command below is the same
before and after `up -d --build`.

The host needs a *persistent* journal (`/var/log/journal/<machine-id>/`
exists). If a box only has `/run/log/journal` (volatile, wiped on reboot —
check with `ls /var/log/journal`), persistence needs enabling before this is
reliable:
```
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo systemctl restart systemd-journald
```

**Retention.** With `journald.conf` left at its defaults, `SystemMaxUse=` and
`SystemKeepFree=` are 10% / 15% of the filesystem holding `/var/log/journal`,
each capped at 4G, whichever is smaller, and `MaxRetentionSec=` is unset (0,
i.e. no time-based eviction). In practice that is a **host-wide ~4G ceiling
shared with every other journald-logged unit on the box**, not a per-container
budget, and eviction is by volume, not by age — how many days of `lobstr-mcp`
lines that buys depends on how chatty the rest of the host is on any given
day. Check current usage with `journalctl --disk-usage`; tighten with
`SystemMaxUse=`/`MaxRetentionSec=` in `/etc/systemd/journald.conf` if the app
ever gets loud enough to threaten that budget.

**Reading the logs after a redeploy.** Use `--timestamps` (`-t` also works but
collides with journald's own `-t`/`--identifier` flag, so spell it out) so
lines carry wall-clock time for a post-mortem:
```
docker compose logs --timestamps                # current container only
```
`docker compose logs` (and `docker logs`) only ever show the *current*
container's history. To see lines from before the last rebuild, go to the
journal directly, filtered by the fixed tag:
```
journalctl -t lobstr-mcp                        # everything under the tag, any container
journalctl -t lobstr-mcp --since "2026-09-08" --until "2026-09-10"
journalctl -t lobstr-mcp -f                      # follow, current + future
journalctl -t lobstr-mcp -o short-iso            # explicit ISO timestamps
```
These read across container recreations, which is the whole point of storing
logs this way instead of relying on `docker logs`/`json-file`.

## 3. Tests / CI
`uv run pytest -q` runs the full suite. GitHub Actions
(`.github/workflows/ci.yml`) runs it on every push.

## 4. Production hardening (before public launch)
- ~~Replace the in-memory `TokenStore` / `IdempotencyStore` / OAuth registries
  with Redis/Postgres.~~ **Done** — set `LOBSTR_MCP_REDIS_URL` and
  `LOBSTR_MCP_TOKEN_KEY` and the tokens, DCR clients, refresh tokens and
  idempotency keys move to Redis, with Lobstr API tokens encrypted at rest.
  Leaving `REDIS_URL` unset still works but logs a loud warning at
  boot. Auth codes and pending consent requests stay in-process by design —
  they are minutes-long in-flight browser state. Verified on the test server:
  an access token issued before `docker compose restart` still authenticated a
  tool call after it. Redis is required for more than one instance/worker
  regardless, since a grant minted on one must be redeemable on another.
- ~~Verify the full OAuth browser handshake (frontend consent → grant →
  redeem).~~ **Done on `mcp.lobstr.io`:** DCR (`POST /register` → 201),
  `GET /authorize` → 302 to `LOBSTR_CONSENT_URL` carrying
  `request_id`/`scope`/`client_id`, the 401 + `WWW-Authenticate` discovery hint,
  rejection of a bogus grant (400), and the service credential against
  `/v1/oauth/mcp-grant/redeem` (400 `InvalidGrant`, i.e. accepted).
  The consent page builds `https://mcp.lobstr.io/lobstr/consent` correctly —
  verified in-browser against the production bundle after `VITE_MCP_BASE` was
  provisioned. A first end-to-end run from a real AI client over HTTPS is the
  last confirmation.
- ~~Put `mcp.lobstr.io` behind TLS; provision secrets.~~ **Done** — see §2c.
  Secrets are generated on the host and live in `.env` / `<app-user-home>/.env`;
  moving them to a vault is still open.
- Then follow `docs/distribution/DIRECTORY-SUBMISSION.md`.
