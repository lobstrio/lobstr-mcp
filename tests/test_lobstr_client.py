import httpx
from lobstr_mcp.lobstr_client import LobstrClient, USER_AGENT


def make_client(handler):
    transport = httpx.MockTransport(handler)
    return LobstrClient("https://api.lobstr.io/v1", "tok123", transport=transport)


def test_requests_carry_the_mcp_user_agent():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["ua"] = request.headers.get("User-Agent")
        return httpx.Response(200, json=[])

    make_client(handler).list_crawlers()
    assert captured["ua"] == USER_AGENT
    assert captured["ua"].startswith("lobstr-mcp/")


def test_list_crawlers_sends_token_and_parses():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        captured["url"] = str(request.url)
        return httpx.Response(200, json={
            "total_results": 1, "limit": 50, "page": 1, "total_pages": 1,
            "data": [{"id": "abc", "name": "Google Maps"}],
            "next": None, "previous": None})

    client = make_client(handler)
    result = client.list_crawlers()
    assert captured["auth"] == "Token tok123"
    assert captured["url"].startswith("https://api.lobstr.io/v1/crawlers")
    assert result == [{"id": "abc", "name": "Google Maps"}]


def test_get_crawler_params_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.lobstr.io/v1/crawlers/xyz/params"
        return httpx.Response(200, json={"account": {"type": "cookies"}})

    client = make_client(handler)
    assert client.get_crawler_params("xyz") == {"account": {"type": "cookies"}}


def test_get_squid_path():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.lobstr.io/v1/squids/sq1"
        return httpx.Response(200, json={"id": "sq1", "name": "My scraper"})

    client = make_client(handler)
    assert client.get_squid("sq1") == {"id": "sq1", "name": "My scraper"}


def test_raises_on_http_error():
    from lobstr_mcp.errors import LobstrAPIError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not found"})

    client = make_client(handler)
    try:
        client.get_crawler("missing")
        assert False, "expected an error"
    except LobstrAPIError as exc:
        assert exc.status == 404
        assert exc.message == "Not found"


# --- real /v1 pagination envelope (found in P4 live testing) -------------------
# api.lobstr.io returns {total_results, limit, page, total_pages, data, next,
# previous} for collection endpoints — not a bare list. Earlier fixtures here
# assumed a bare list, so search_scrapers/list_my_scrapers broke against the
# live API and only ever saw the first page.

def _envelope(items, page, total_pages, total):
    return {"total_results": total, "limit": 50, "page": page,
            "total_pages": total_pages, "result_from": 1, "result_to": len(items),
            "data": items, "next": None, "previous": None}


def _paged_handler(pages, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(dict(request.url.params).get("page", 1))
        seen.append((request.url.path, page))
        items = pages[page - 1]
        return httpx.Response(200, json=_envelope(
            items, page, len(pages), sum(len(p) for p in pages)))
    return handler


def test_list_crawlers_unwraps_envelope_and_follows_all_pages():
    seen = []
    pages = [[{"id": "a"}, {"id": "b"}], [{"id": "c"}], [{"id": "d"}]]
    client = make_client(_paged_handler(pages, seen))
    result = client.list_crawlers()
    assert [c["id"] for c in result] == ["a", "b", "c", "d"]
    assert seen == [("/v1/crawlers", 1), ("/v1/crawlers", 2), ("/v1/crawlers", 3)]


def test_list_squids_unwraps_envelope_and_follows_all_pages():
    seen = []
    pages = [[{"id": "s1"}], [{"id": "s2"}]]
    client = make_client(_paged_handler(pages, seen))
    assert [s["id"] for s in client.list_squids()] == ["s1", "s2"]
    assert seen == [("/v1/squids", 1), ("/v1/squids", 2)]


def test_list_endpoints_tolerate_a_bare_list():
    """Legacy/loose shapes must not break the client (spec §13)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "abc"}])

    client = make_client(handler)
    assert client.list_crawlers() == [{"id": "abc"}]
    assert client.list_squids() == [{"id": "abc"}]


def test_pagination_is_capped_against_a_runaway_total_pages():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(dict(request.url.params).get("page", 1))
        calls.append(page)
        return httpx.Response(200, json=_envelope([{"id": page}], page, 10_000, 10_000))

    client = make_client(handler)
    client.list_crawlers()
    assert len(calls) <= 50, f"followed {len(calls)} pages; must be capped"


# --- structured upstream errors (spec §7) ------------------------------------

def test_http_error_becomes_a_parsed_lobstr_api_error():
    """A raw httpx error reaches the model as an opaque "Client error '400 Bad
    Request'"; the client normalizes Lobstr's {"errors": {...}} envelope."""
    from lobstr_mcp.errors import LobstrAPIError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errors": {
            "message": "Squid not ready, please update the settings first.",
            "type": "SquidNotReady", "code": 400}})

    client = make_client(handler)
    try:
        client.start_run("sq1")
    except LobstrAPIError as exc:
        assert exc.status == 400
        assert exc.error_type == "SquidNotReady"
        assert "not ready" in exc.message
    else:
        raise AssertionError("expected LobstrAPIError")


def test_http_error_without_an_envelope_still_normalizes():
    from lobstr_mcp.errors import LobstrAPIError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    client = make_client(handler)
    try:
        client.list_crawlers()
    except LobstrAPIError as exc:
        assert exc.status == 503
    else:
        raise AssertionError("expected LobstrAPIError")
