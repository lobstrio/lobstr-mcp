# lobstr.io plugin for Claude

Structured data from websites for Claude: Google Maps places and reviews, realtor.com listings, X posts and more, one call each, as JSON records with the same fields every time. The plugin connects lobstr.io's hosted MCP server, `https://api.lobstr.io/v2/mcp`: nothing runs on your machine.

## Install

```text
/plugin install lobstr --marketplace lobstrio/lobstr-mcp
```

Claude Code asks your lobstr.io API key once and keeps it secret. The key is in the app, under [API](https://app.lobstr.io/dashboard/api); the free plan has 100 credits a month.

## Tools

- `find_operations`: the operations that fit what you need, with their price.
- `get_operation`: an operation's options, the fields of its records, its errors.
- `run_operation`: one page of records, what it cost, and the call for the next page.
- `get_balance`: the credits left, and what one credit costs on your plan.
- `send_feedback`: what went wrong or what you need, to lobstr.io's team. Free.

Docs: [docs.lobstr.io/agents](https://docs.lobstr.io/agents)
