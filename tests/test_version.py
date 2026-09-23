"""The version is read once from the package metadata and used everywhere."""
import re
import tomllib
from pathlib import Path

from lobstr_mcp import __version__
from lobstr_mcp.lobstr_client import USER_AGENT
from lobstr_mcp.observability import SENTRY_RELEASE


def test_version_matches_pyproject():
    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert __version__ == data["project"]["version"]


def test_version_is_a_release_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__


def test_user_agent_and_sentry_release_carry_the_version():
    assert USER_AGENT == f"lobstr-mcp/{__version__}"
    assert SENTRY_RELEASE == f"lobstr-mcp@{__version__}"
