# Frontend task — Lobstr "Connect AI" consent page

Build the consent page that lets an **already-logged-in** Lobstr user authorize
an AI client (Claude, ChatGPT, …) to operate their Lobstr account through the
MCP. This is the browser step of the MCP's OAuth 2.1 flow. No password is
involved — the user is already signed in; they just click **Allow**.

## Where it lives
A route on the Lobstr frontend, e.g. **`https://app.lobstr.io/connect-ai`**
(the MCP is configured with this via `LOBSTR_CONSENT_URL`).

## The end-to-end flow (your page is step 2–4)

```
1. AI client → mcp.lobstr.io/authorize        (OAuth 2.1 + PKCE; handled by MCP)
2. MCP redirects the browser to:
     app.lobstr.io/connect-ai?request_id=<id>&scope=<space list>&client_id=<id>
3. YOUR PAGE (user already logged in):
     - show "Allow <client_id> to access your Lobstr account?" + the scopes
     - on Allow:  POST /v1/oauth/mcp-grant  (Authorization: Token <user token>)
                  body {"scopes": [<the scopes>]}  →  {"grant": "...", "expires_in": 60}
     - redirect the browser to:
                  https://mcp.lobstr.io/lobstr/consent?request_id=<id>&grant=<grant>
4. MCP redeems the grant server-to-side, mints its OAuth code, and redirects
   the browser back to the AI client. Done — you don't handle this step.
```

On **Deny**: don't call the grant endpoint; send the user back to the app (the
pending OAuth request simply expires). A dedicated deny-redirect can be added
to the MCP later if a cleaner "access denied" bounce-back is wanted.

## Inputs your page receives (query params on the redirect)
| Param | Meaning |
|---|---|
| `request_id` | Opaque id correlating this approval to the MCP's pending OAuth request. **Pass it back unchanged** to `/lobstr/consent`. |
| `scope` | Space-separated scopes being requested (display these to the user). |
| `client_id` | The AI client's id (display which app is asking). |
| `client_name` | The AI client's self-registered display name (e.g. `Claude`). **Self-asserted at registration — do not treat as proof of identity.** Optional. |
| `redirect_host` | Host of the client's OAuth redirect URI (e.g. `claude.ai`). **The trustworthy signal:** the authorization code is only ever delivered here, so a client claiming to be "Claude" that returns elsewhere is exposed. Optional. |

## Naming the client (who is asking)

Show *who* is requesting access rather than the opaque `client_id`. Derive the
label from `redirect_host`, because it is the only value you can trust — the
authorization code is delivered only to the client's registered redirect URI,
whereas `client_name` is free text the client chose at registration and any
client can set to "Claude".

```
KNOWN_HOSTS = {
  "claude.ai": "Claude",
  "chatgpt.com": "ChatGPT",
  "openai.com": "ChatGPT",
}

const friendlyName =
  KNOWN_HOSTS[redirect_host] || client_name || "This app";
```

Then show the verified host alongside it so nothing can spoof the destination:

> **Claude** wants access to your Lobstr account
> _will return to **claude.ai**_

If `redirect_host` is a stranger, say so plainly ("returns to `<host>`") instead
of dressing it up — that line is the user's one defence against a look-alike
client. Both params are optional; fall back to `client_name`, then a generic
"This app", and never render an empty name.

## API you call — mint the grant
`POST {API_BASE}/v1/oauth/mcp-grant`
- **Auth:** the logged-in user's own token — `Authorization: Token <user API token>`
  (the same token the app already uses for `/v1` calls).
- **Body:** `{ "scopes": ["crawlers:read", "runs:read", "results:read", "profile:read", "account:read", "runs:execute"] }`
  (echo the `scope` list you received).
- **Response:** `{ "grant": "<single-use, ~60s>", "expires_in": 60 }`

Then redirect the browser to
`{MCP_BASE}/lobstr/consent?request_id=<request_id>&grant=<grant>`.

The grant is **single-use and short-lived** — mint it only after the user
clicks Allow, then redirect immediately.

## Scopes (for display)
- `crawlers:read` — discover scrapers and read their settings
- `runs:read` — check run status
- `results:read` — read scraped results
- `profile:read` — read your Lobstr identity (name, email, plan) and credit balance
- `account:read` — read your connected platform accounts (LinkedIn, Facebook, …) and their status
- `runs:execute` — run scrapers (consumes credits)

## Status — shipped

Live in production. The page is served at `https://app.lobstr.io/connect-ai`
and the MCP at `https://mcp.lobstr.io`.

**The one thing that will break it again:** the redirect target comes from
`MCP_BASE` (`src/Utils/externals.js`), which is
`import.meta.env.VITE_MCP_BASE`. Vite inlines that at **build** time, so a
build without it in the environment ships `MCP_BASE === undefined`, and
``new URL(`${MCP_BASE}/lobstr/consent`)`` throws `TypeError: Invalid URL` on
every **Allow** click — caught and rendered as a generic error, with nothing
reaching the MCP. Keep `VITE_MCP_BASE=https://mcp.lobstr.io` in the Production
environment of every deploy target.

Worth hardening if the page is revisited: `handleAllow` mints the grant before
it builds the URL, so a failure there burns a valid single-use 60-second grant;
and a config error and a rejected grant render the same message.

For local development, point `LOBSTR_CONSENT_URL` (the MCP's redirect target)
at your dev page, and set `VITE_MCP_BASE` to whichever MCP you are running —
`http://localhost:8000` for a local one, over an SSH tunnel if it lives on a
remote box.

## Notes
- For real AI clients (Claude/ChatGPT) the MCP must be served over **HTTPS**;
  http is fine only for local frontend development.
- CORS: the grant call is same-origin if the app and API share the lobstr.io
  domain; for localhost dev you may need the API to allow your dev origin.
- Backend contract (for reference): the Lobstr API endpoints
  `POST /v1/oauth/mcp-grant` and `POST /v1/oauth/mcp-grant/redeem`.
