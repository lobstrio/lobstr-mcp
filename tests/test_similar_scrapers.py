"""A natural, descriptive query like "Reddit search posts
comments" returned count:0 under strict AND matching — every query word had
to appear in a crawler's name/slug/description, and the Reddit Scraper's
listing never says "search" or "comments". An agent seeing count:0 reasonably
concluded no such scraper exists and started building a workaround, instead
of running the one that was actually right there.

`results` keeps today's strict AND matching untouched (owner's decision:
replace nothing there). This adds `similar`: a small, ranked list of partial
matches (BM25 over name/slug/description, reused from the shelved ranking
spike) so the caller can see "we have the Reddit
Scraper, close but not exact" instead of a bare, misleading zero.

Tests run the real code (search_scrapers_impl) over a fixture built from the
live crawler catalog (production GET /v1/crawlers, fetched read-only, id/
name/slug/description/credits_per_row/is_available only — see
tests/fixtures/crawlers_sample.json), not synthetic data.
"""
import json
from pathlib import Path

from lobstr_mcp.tools.scrapers import search_scrapers_impl

_FIXTURE = Path(__file__).parent / "fixtures" / "crawlers_sample.json"


class _Client:
    def __init__(self, crawlers):
        self._crawlers = crawlers

    def list_crawlers(self):
        return self._crawlers


def _client():
    with open(_FIXTURE, encoding="utf-8") as f:
        return _Client(json.load(f))


def _names(rows):
    return [r["name"] for r in rows]


# --- the four repro queries ---------------------------------------------

def test_reddit_descriptive_query_has_no_exact_result_but_a_similar_one():
    out = search_scrapers_impl(_client(), "Reddit search posts comments")
    assert out["results"] == []
    assert out["count"] == 0

    assert out["similar"], "similar must not be empty — Reddit Scraper exists"
    # BM25 score is the primary key (not matched-word count — an earlier
    # version ranked by count first, which put a 3-generic-word match above
    # Reddit Scraper's 2 words including the distinctive one; reverted). idf
    # weights "reddit" far above "posts", which is what puts Reddit Scraper
    # first here without a separate word-count rule.
    top = out["similar"][0]
    assert top["name"] == "Reddit Scraper"
    assert set(top["matched"]) >= {"reddit", "posts"}
    assert set(top["missing"]) >= {"search", "comments"}

    assert "Reddit Scraper" in out["hint"]
    assert "no" in out["hint"].lower() or "closest" in out["hint"].lower()


def test_software_reviews_query_has_trustpilot_in_similar_top_3():
    out = search_scrapers_impl(_client(), "software reviews Capterra G2 Trustpilot")
    assert out["results"] == []
    assert out["count"] == 0
    assert "Trustpilot Reviews Scraper" in _names(out["similar"])[:3]


def test_reddit_single_word_query_matches_exactly_as_before():
    out = search_scrapers_impl(_client(), "reddit")
    assert out["count"] >= 1
    assert _names(out["results"])[0] == "Reddit Scraper"
    # similar is only populated when results is empty — an exact match means
    # there is nothing partial to show, not just that this one is excluded
    assert out["similar"] == []


def test_trustpilot_reviews_query_matches_exactly_as_before():
    out = search_scrapers_impl(_client(), "Trustpilot reviews")
    assert out["count"] >= 1
    assert _names(out["results"])[0] == "Trustpilot Reviews Scraper"
    assert out["similar"] == []


# --- shape and edge cases -------------------------------------------------

def test_similar_key_always_present_even_with_no_query():
    out = search_scrapers_impl(_client(), "")
    assert "similar" in out
    assert out["similar"] == []


def test_similar_capped_at_five():
    # "scraper" appears in almost every fixture row's name/description —
    # make sure the partial-match list never exceeds the cap even so.
    out = search_scrapers_impl(_client(), "scraper data extraction tool automation")
    assert len(out["similar"]) <= 5


def test_similar_entries_share_results_field_set_plus_matched_missing():
    out = search_scrapers_impl(_client(), "Reddit search posts comments")
    row = out["similar"][0]
    for key in ("id", "name", "slug", "description", "credits_per_row",
                "credits_per_email", "is_premium", "is_available"):
        assert key in row
    assert "matched" in row and "missing" in row


def test_similar_stays_empty_when_results_is_non_empty_even_with_a_partial_match():
    # "reddit" is an exact match (Reddit Scraper); other crawlers only share
    # "posts" with a wider query — similar must stay [] rather than surface
    # them alongside an already-successful exact match.
    out = search_scrapers_impl(_client(), "reddit posts")
    assert out["count"] >= 1
    assert _names(out["results"])[0] == "Reddit Scraper"
    assert out["similar"] == []
    assert "hint" not in out or "showing the first" in out.get("hint", "")


def test_query_matching_nothing_leaves_both_empty_with_honest_hint():
    out = search_scrapers_impl(_client(), "zzqxnonexistentscraperterm")
    assert out["results"] == []
    assert out["similar"] == []
    assert out["count"] == 0
    assert out["total_available"] > 0
    assert "hint" in out
    assert "no" in out["hint"].lower()


def test_ordering_is_deterministic():
    out1 = search_scrapers_impl(_client(), "Reddit search posts comments")
    out2 = search_scrapers_impl(_client(), "Reddit search posts comments")
    assert _names(out1["similar"]) == _names(out2["similar"])


def test_full_mode_has_no_similar_key():
    out = search_scrapers_impl(_client(), "reddit", full=True)
    assert "similar" not in out


# --- similar ordering: BM25 score first, total_runs second ----------------------

def test_equal_relevance_breaks_the_tie_on_total_runs_descending():
    crawlers = [
        {"id": "a", "name": "Alpha Reddit Tool", "slug": "alpha-reddit-tool",
         "description": "reddit posts", "total_runs": 5},
        {"id": "b", "name": "Beta Reddit Tool", "slug": "beta-reddit-tool",
         "description": "reddit posts", "total_runs": 500},
    ]
    out = search_scrapers_impl(_Client(crawlers), "reddit posts extra")
    names = _names(out["similar"])
    assert names.index("Beta Reddit Tool") < names.index("Alpha Reddit Tool")


def test_a_higher_bm25_score_still_ranks_above_total_runs_however_lopsided():
    crawlers = [
        {"id": "a", "name": "Reddit Scraper", "slug": "reddit-scraper",
         "description": "reddit posts", "total_runs": 1},
        {"id": "b", "name": "Facebook Posts Scraper", "slug": "facebook-posts",
         "description": "facebook posts", "total_runs": 100000},
    ]
    out = search_scrapers_impl(_Client(crawlers), "reddit posts extra")
    names = _names(out["similar"])
    # Reddit Scraper matches "reddit" and "posts"; Facebook Posts Scraper
    # matches only "posts" — the extra matched term gives Reddit Scraper the
    # higher BM25 score, and score is the primary key, so it ranks first
    # however far behind it is on total_runs (the tie-break, not summed in).
    assert names.index("Reddit Scraper") < names.index("Facebook Posts Scraper")


def test_ordering_unchanged_when_total_runs_missing_everywhere():
    crawlers = [
        {"id": "a", "name": "Reddit Scraper", "slug": "reddit-scraper",
         "description": "reddit posts"},
        {"id": "b", "name": "Facebook Posts Scraper", "slug": "facebook-posts",
         "description": "facebook posts"},
    ]
    out_before = search_scrapers_impl(_Client(crawlers), "reddit posts extra")
    for c in crawlers:
        c["total_runs"] = 0  # explicit 0, same as "absent" per `or 0`
    out_after = search_scrapers_impl(_Client(crawlers), "reddit posts extra")
    assert _names(out_before["similar"]) == _names(out_after["similar"])


def test_docstring_explains_results_vs_similar():
    from lobstr_mcp.tools import scrapers as scrapers_module
    import inspect

    src = inspect.getsource(scrapers_module)
    idx = src.index("def search_scrapers(query")
    doc = src[idx:idx + 1400]
    assert "similar" in doc
    assert "exact" in doc.lower()
    assert "does" in doc.lower() and "not mean" in doc.lower() or "doesn't mean" in doc.lower() \
        or "not mean" in doc.lower()
