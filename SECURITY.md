# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for a security vulnerability.

Report it privately through
[GitHub Security Advisories](https://github.com/lobstrio/lobstr-mcp/security/advisories/new)
for this repository. Include what you found, the impact, and steps to
reproduce if you have them.

We'll acknowledge the report, work with you on a fix, and credit you in the
advisory unless you'd rather stay anonymous.

## Scope

This repo is the MCP server behind `mcp.lobstr.io` — the OAuth surface, tool
implementations, and the thin client over `api.lobstr.io/v1`. A vulnerability
in the Lobstr platform itself (the API, the dashboard) is out of scope here;
reach out through [lobstr.io](https://lobstr.io) instead.

## Supported versions

`main` is the only supported branch; `mcp.lobstr.io` runs from it. There is no
older maintained line.
