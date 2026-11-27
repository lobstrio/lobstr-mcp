# lobstr.io plugin for Claude

lobstr.io is a web data API for your code and your AI agents. Each operation is one thing a site does: search a marketplace, get a listing, check it is still online. One call, JSON back in seconds, the same fields every time, read on the site at the moment you call. Sites from local businesses and real estate to travel, marketplaces, news and social networks, more each week: every operation is in [api.lobstr.io/v2/catalog.md](https://api.lobstr.io/v2/catalog.md).

The plugin connects lobstr.io's hosted MCP server, `https://api.lobstr.io/v2/mcp`: nothing runs on your machine.

## Install

```text
/plugin install lobstr --marketplace lobstrio/lobstr-mcp
```

The first time a tool needs your account, Claude asks you to sign in to lobstr.io and allow it: no key to copy. The free plan has 100 credits a month.

## Tools

- `find_operations`: the operations that fit what you need, with their price.
- `get_operation`: an operation's options, the fields of its records, its errors.
- `run_operation`: one page of records, what it cost, and the call for the next page.
- `get_balance`: the credits left, and what one credit costs on your plan.
- `send_feedback`: what went wrong or what you need, to lobstr.io's team. Free.

Docs: [docs.lobstr.io/agents](https://docs.lobstr.io/agents)
