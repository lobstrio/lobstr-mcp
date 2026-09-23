"""The one place the server's version is read.

`pyproject.toml` holds the number; the installed package metadata exposes it
(`uv pip install .` in the Dockerfile, an editable install under `uv run`).
Everything that needs it imports `__version__` from here: the User-Agent sent
to the Lobstr API (so `created_via` logs can tell MCP builds apart), the Sentry
release, and the `whoami` tool.
"""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("lobstr-mcp")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"
