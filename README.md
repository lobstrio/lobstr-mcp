<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset=".github/assets/logo-white.svg">
    <img src=".github/assets/logo-red.svg" alt="lobstr.io" width="72">
  </picture>
</p>

<h1 align="center">lobstr-mcp</h1>

<p align="center">Run lobstr.io scrapers from Claude, ChatGPT, and any MCP client — no CSV export/import.</p>

<p align="center">
  <a href="https://github.com/lobstrio/lobstr-mcp/actions/workflows/ci.yml"><img src="https://github.com/lobstrio/lobstr-mcp/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/lobstrio/lobstr-mcp/releases/latest"><img src="https://img.shields.io/github/v/release/lobstrio/lobstr-mcp" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/lobstrio/lobstr-mcp" alt="License"></a>
  <a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-server-000000" alt="MCP server"></a>
</p>

<p align="center"><b>Hosted at <a href="https://mcp.lobstr.io/mcp">https://mcp.lobstr.io/mcp</a></b> — add it to any MCP client by URL, no install required.</p>

> **Not the docs MCP.** `docs.lobstr.io/mcp` answers "how does lobstr.io
> work?". This one is "do this in my account": it authenticates as a user and
> spends real credits.

## Quick start

No directory listing or approval needed — this server implements **Dynamic
Client Registration**, so any MCP client can add it by URL and register
itself. Authenticating opens Lobstr's consent page in a browser; click
**Allow** and you're back in the client.

| Client | Setup |
|---|---|
| Claude Code | `claude mcp add --transport http lobstr https://mcp.lobstr.io/mcp` |
| Claude Desktop, Cursor, Windsurf | Add the block below to the client's MCP config |
| Claude.ai, ChatGPT | Settings → connectors → add custom connector → paste the URL |
| VS Code | `.vscode/mcp.json`, add a server entry pointing at the URL |
| Codex CLI | Register as a remote MCP server in `~/.codex/config.toml` |

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

Exact config keys vary by client and version — check your client's MCP docs
if the block above doesn't match. **HTTPS is mandatory**: the hosted server
only works over `https://`.

## Tools

20 tools. There is no per-scraper tool — new lobstr.io scrapers work with
**zero** code changes. Full behaviour, error codes and the intended call
sequence: [`docs/tools.md`](docs/tools.md), [`docs/errors.md`](docs/errors.md).

| Tool | Does |
|---|---|
| `search_scrapers` | Find scrapers matching a query |
| `get_scraper_details` | A scraper's input schema, outputs and pricing |
| `list_my_scrapers` | The user's saved scraper configurations |
| `get_my_scraper` | One saved configuration by id |
| `create_squid` | Save a new scraper configuration without running it |
| `add_tasks` | Add input rows to a saved configuration |
| `estimate_run` | Authoritative cost/time estimate before running |
| `run_scraper` | Configure and run a scraper in one call — **spends credits** |
| `attach_account` | Link a connected platform account to a scraper |
| `empty_scraper` | Clear a scraper's saved inputs, keep its config |
| `deactivate_scraper` | Free a scraper's concurrency slot without deleting it |
| `get_run` | Status and progress of a run |
| `list_runs` | Recent runs for a scraper |
| `get_results` | One page of a run's results |
| `get_results_url` | A signed download URL for a run's full results |
| `abort_run` | Stop a running job |
| `list_accounts` | The user's connected platform accounts |
| `get_account` | One connected account by id |
| `check_credits` | Credit balance and concurrency slots |
| `whoami` | The authenticated user and server version |

## Self-hosting

```bash
git clone https://github.com/lobstrio/lobstr-mcp.git
cd lobstr-mcp
cp .env.example .env    # then edit — see docs/configuration.md
docker compose up -d --build
```

Full runbook, production hardening and log retention:
[`DEPLOY.md`](DEPLOY.md). Environment variables:
[`docs/configuration.md`](docs/configuration.md).

## Development

```bash
uv sync --extra dev
uv run pytest
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full dev setup and PR flow.

## Learn more

- [`docs/architecture.md`](docs/architecture.md) — how auth works, module map, safeguards
- [`docs/tools.md`](docs/tools.md) — full tool reference
- [`docs/errors.md`](docs/errors.md) — the error-code catalogue
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — common failure modes
- [`CHANGELOG.md`](CHANGELOG.md) — what shipped, by version

lobstr.io: [website](https://lobstr.io) · [docs](https://docs.lobstr.io) ·
[scraper store](https://lobstr.io/store) ·
[Python SDK](https://github.com/lobstrio/lobstrio-sdk) ·
[CLI](https://github.com/lobstrio/lobstrio-cli)

## Contributing

Issues and pull requests welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md).
Security issues: [`SECURITY.md`](SECURITY.md), not a public issue. This
project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## License

[Apache License 2.0](LICENSE)
