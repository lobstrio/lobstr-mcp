# Public distribution — OpenAI + Anthropic directory submission

Both directories list the **same** backend: `https://mcp.lobstr.io/mcp`. Do not
build separate execution stacks per client. Submit only after the OAuth server
is deployed, TLS-fronted, and tested end-to-end in Claude and ChatGPT (P4).

## Preconditions (gate)
- [ ] `mcp.lobstr.io` live over HTTPS (Cloudflare/DNS), OAuth server running (`create_app`).
- [ ] Additive Django `POST /v1/oauth/issue-token` deployed; `LOBSTR_MCP_SERVICE_CREDENTIAL` set.
- [ ] Full OAuth handshake verified from Claude and from ChatGPT (connect → Lobstr login → authorized).
- [ ] Failure matrix exercised (insufficient credits, invalid input, expired auth, duplicate run, scraper failure, timeout, empty results, huge datasets, ambiguous request, concurrent runs).
- [ ] Tool descriptions/annotations reviewed for model legibility (they drive tool selection).

## Metadata (reuse for both)
- **Name:** Lobstr
- **Endpoint:** `https://mcp.lobstr.io/mcp` (Streamable HTTP)
- **Auth:** OAuth 2.1 + PKCE, Dynamic Client Registration (metadata at
  `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource/mcp`).
- **Scopes:** `crawlers:read`, `runs:read`, `results:read`, `account:read`, `runs:execute`.
- **Tools (V1):** search_scrapers, get_scraper_details, run_scraper, get_run,
  get_results, list_my_scrapers, get_my_scraper.
- **Short description:** "Discover, configure, run, and retrieve results from
  Lobstr web scrapers directly inside your AI client."
- **Logo/branding:** TODO — supply Lobstr logo assets.
- **Privacy policy / terms URLs:** TODO — supply.

## OpenAI (Plugins / Apps Directory)
- [ ] Confirm current submission channel + requirements at the time of launch.
- [ ] Provide endpoint, OAuth config, metadata, logo, descriptions.
- [ ] Verify inside ChatGPT + Codex.

## Anthropic (Connectors Directory)
- [ ] Submit the same `mcp.lobstr.io/mcp` endpoint.
- [ ] Provide OAuth config + metadata + logo.
- [ ] Verify Claude users can connect and operate their account.

> Requirements for both directories evolve; re-check each program's current
> submission process at launch rather than assuming this list is complete.
