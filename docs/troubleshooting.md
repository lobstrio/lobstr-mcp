# Troubleshooting

**Client says the server is unauthorized / OAuth fails to start.**
`LOBSTR_MCP_PUBLIC_URL` must be `https://` on any public host — the MCP SDK
rejects a non-HTTPS issuer. For local development against a remote host,
tunnel instead: `ssh -L 8000:localhost:8000 <host>`.

**Everyone gets logged out after a deploy.**
`LOBSTR_MCP_REDIS_URL` is unset, so state is in-process. The boot log warns
about this — see [`docs/configuration.md`](configuration.md).

**Consent fails with "Consent failed or request expired."**
The grant is single-use and lives ~60s. Also check
`LOBSTR_MCP_SERVICE_CREDENTIAL` matches the value configured on the Lobstr API
side: redemption returning 401 means credential mismatch, whereas 400
`InvalidGrant` means the credential was accepted and only the grant was stale.

**`run_scraper` returns `upstream_rejected` / `InvalidParam`.**
Some squid inputs are **function toggles** the API accepts only nested under
`params.functions` (e.g. `extract_emails_from_website`,
`collect_business_details`, `fetch_business_images`). `GET
/crawlers/{hash}/params` is authoritative and the schema translator uses it.

**`run_scraper` always asks for confirmation.**
Expected. Lobstr prices per result row, so the total is unknowable before the
run. The estimate still reports the real per-row rate — see `estimate_run` in
[`docs/tools.md`](tools.md).

**A run sits in `pending` forever, or flips to `error` immediately** *(platform side, not this server).*
The MCP server only starts the run; executing it happens on the Lobstr
platform. If `get_run` keeps reporting `pending`, or the run fails straight
away with no results, contact [lobstr.io support](https://lobstr.io/contact)
with the run id from `get_run` (and the squid id).

**`get_scraper_details` on a bad id returns `upstream_unavailable`.**
Upstream quirk: `GET /crawlers/{unknown}` answers 500 rather than 404, so it
cannot be mapped to `not_found`. Verify the id from `search_scrapers` first.
