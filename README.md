<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/logo-white.svg">
    <img src=".github/assets/logo-red.svg" alt="lobstr.io" width="72">
  </picture>
</p>

<h1 align="center">lobstr.io MCP server</h1>

<p align="center">Structured data from websites for Claude, ChatGPT, Cursor and any MCP client: the whole lobstr.io API in five tools.</p>

<p align="center">
  <a href="https://docs.lobstr.io/agents"><strong>Docs</strong></a> ·
  <a href="https://api.lobstr.io/v2/catalog.md"><strong>Catalog</strong></a> ·
  <a href="https://app.lobstr.io/dashboard/api"><strong>API key</strong></a> ·
  <a href="https://www.lobstr.io/pricing"><strong>Pricing</strong></a>
</p>

---

The server is hosted: there is nothing to install. It lives at `https://api.lobstr.io/v2/mcp` and takes your API key, from the [app](https://app.lobstr.io/dashboard/api), with 100 free credits a month.

## Connect

**Claude Code**

```bash
claude mcp add --transport http lobstr https://api.lobstr.io/v2/mcp \
  --header "Authorization: Bearer YOUR_API_KEY"
```

**Cursor, Windsurf and any client that reads `mcpServers`**

```json
{
  "mcpServers": {
    "lobstr": {
      "url": "https://api.lobstr.io/v2/mcp",
      "headers": { "Authorization": "Bearer YOUR_API_KEY" }
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`, the key asked once and kept secret)

```json
{
  "servers": {
    "lobstr": {
      "type": "http",
      "url": "https://api.lobstr.io/v2/mcp",
      "headers": { "Authorization": "Bearer ${input:lobstr-api-key}" }
    }
  },
  "inputs": [
    { "id": "lobstr-api-key", "type": "promptString", "description": "lobstr.io API key", "password": true }
  ]
}
```

**Claude, ChatGPT and any client that takes a URL only** (a custom connector): the key goes in the URL.

```text
https://api.lobstr.io/v2/mcp?api_key=YOUR_API_KEY
```

Or paste this to your agent:

```text
Connect the lobstr.io MCP server (https://api.lobstr.io/v2/mcp)
with my API key. Then find the operation that fits,
tell me what it costs, and get every bakery in Lyon
from Google Maps with its phone number.
```

## Five tools

| Tool | What it gives |
| --- | --- |
| `find_operations` | The operations that fit what you need, with their price. |
| `get_operation` | An operation's options, the fields of its records, its errors. |
| `run_operation` | One page of records, what it cost, and the call for the next page. |
| `get_balance` | The credits left, and what one credit costs on your plan. |
| `send_feedback` | What went wrong or what you need, to lobstr.io's team. Free. |

They go through the API itself: an agent gets the same answers, errors and prices as your code. Listing the tools needs no key.

## For agents that read

- [lobstr.io/llms.txt](https://www.lobstr.io/llms.txt): lobstr.io in one page, every operation with its price.
- [api.lobstr.io/v2/catalog.md](https://api.lobstr.io/v2/catalog.md): every operation; each one's whole contract at `/v2/catalog/{source}/{resource}/{verb}.md`.
- [api.lobstr.io/v2/openapi.json](https://api.lobstr.io/v2/openapi.json): the API, for a client generator.

## The v1 server

The server for the batches (v1), squids, tasks and runs at `mcp.lobstr.io`, lives on the [`v1`](https://github.com/lobstrio/lobstr-mcp/tree/v1) branch, its last state tagged [`v1-final`](https://github.com/lobstrio/lobstr-mcp/releases/tag/v1-final).

## License

[Apache 2.0](LICENSE)
