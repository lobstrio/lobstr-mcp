# Contributing to lobstr-mcp

Thanks for taking the time to contribute.

## Dev setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/lobstrio/lobstr-mcp.git
cd lobstr-mcp
uv sync --extra dev
```

Run the server locally in dev mode (no OAuth, a single dev token) — see the
[README](README.md#quick-start) and [`DEPLOY.md`](DEPLOY.md) for the full
OAuth shape.

```bash
export LOBSTR_DEV_TOKEN=<your Lobstr API token>
uv run uvicorn lobstr_mcp.server:app --port 8000
```

## Tests

```bash
uv run pytest          # full suite, no network or broker needed
```

CI (`.github/workflows/ci.yml`) runs the same command on every push and pull
request. A PR needs a green run before it can merge.

Fixtures must reflect **recorded** `/v1` responses, not the design spec — see
[`docs/`](docs/) and the test files for the pattern. If you add a fixture,
capture a real response first rather than guessing field names.

## Making a change

1. Fork the repo and create a branch off `main`.
2. Make your change, with tests for new behaviour.
3. Update `CHANGELOG.md` (Keep a Changelog headings) and bump `pyproject.toml`
   `version` when the tool surface or its behaviour changes.
4. If a tool's behaviour changes, update its docstring in the same change —
   that docstring is what the AI client reads to decide how to call it.
5. Open a pull request against `main`. Direct pushes to `main` are disabled;
   every change lands through a PR with CI green.

## Reporting a bug or requesting a feature

Use the issue templates. For a security vulnerability, see
[`SECURITY.md`](SECURITY.md) instead of a public issue.

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md).
