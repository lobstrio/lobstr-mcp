"""The MCP answers without a session id (card MLhj4ohs): Claude's connector
calls from many hosts, and with sessions those requests got 400 "Missing
session ID" / 404 "Session not found", which clients showed as 503s."""
import os

from starlette.testclient import TestClient

HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def test_tools_list_works_without_and_with_an_unknown_session_id():
    os.environ.setdefault("LOBSTR_DEV_TOKEN", "dummy")
    from lobstr_mcp.server import app

    with TestClient(app) as client:
        r1 = client.post("/mcp", json=LIST, headers=HEADERS)
        r2 = client.post("/mcp", json=LIST, headers={**HEADERS, "Mcp-Session-Id": "0" * 32})
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert "run_scraper" in r1.text
