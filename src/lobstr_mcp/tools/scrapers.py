from __future__ import annotations

import math
import re

from lobstr_mcp.account_linking import crawler_account_type
from lobstr_mcp.auth.scopes import EXECUTE_SCOPES, READ_SCOPES
from lobstr_mcp.errors import structured
from lobstr_mcp.render import toon_result
from lobstr_mcp.safeguards import resolve_credit_rate
from lobstr_mcp.lobstr_client import LobstrClient, resolve_crawler_id
from lobstr_mcp.schema_translator import translate_input_schema


def _summary(crawler: dict) -> dict:
    """Field names match a live GET /crawlers item — there is no "pricing" or
    "platform" key, pricing is exposed per row / per enriched email."""
    return {
        "id": crawler.get("id"),
        "name": crawler.get("name"),
        "slug": crawler.get("slug"),
        "description": crawler.get("description", ""),
        "credits_per_row": resolve_credit_rate(crawler.get("credits_per_row")),
        "credits_per_email": resolve_credit_rate(crawler.get("credits_per_email")),
        "is_premium": crawler.get("is_premium"),
        "is_available": crawler.get("is_available"),
    }


# A search that dumps the whole catalog (~180 crawlers, ~50KB) costs the model
# tens of KB per call. Default to the relevant matches only; full=True restores
# the complete ranked list for callers that really want to browse everything.
_SEARCH_CAP = 10

# Cap on the `similar` list — partial matches are a pointer to check, not a
# second results page, so we keep the response small even when many crawlers
# share one common word with the query.
_SIMILAR_CAP = 5

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Function words only. Domain-generic words ("scraper", "search", "data") are
# NOT listed here on purpose — BM25's idf already down-weights whatever is
# common across the catalog, so a hardcoded list would drift as crawlers are
# added/renamed. See _bm25_scores. Reused verbatim from the shelved ranking
# spike — only `similar` uses it, `results`
# below is untouched strict AND matching.
_STOPWORDS = frozenset({
    "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "by",
    "from", "at", "as", "is", "are", "be", "this", "that", "it", "the",
})

_BM25_K1 = 1.5
_BM25_B = 0.75


def _tokenize(text: str) -> list[str]:
    """Lowercase and split on runs of non-alphanumerics, so hyphenated slugs
    ("google-maps") and names split into the same words as free text, then
    drop function-word stopwords and single characters."""
    return [t for t in _TOKEN_RE.findall((text or "").lower())
            if t not in _STOPWORDS and len(t) > 1]


def _crawler_tokens(c: dict) -> list[str]:
    blob = f"{c.get('name','')} {c.get('description','')} {c.get('slug','')}"
    return _tokenize(blob)


def _bm25_scores(query_tokens: list[str], docs_tokens: list[list[str]]) -> list[float]:
    """Rank each doc by summed BM25 score over the query terms it contains —
    a doc needs only ONE matching term to score above 0 (OR, not AND), and
    terms common across the catalog (low idf) contribute little regardless of
    English-stopword status. Deterministic, no external index or embeddings:
    the catalog is ~200 rows, small enough to score in memory on every call."""
    n_docs = len(docs_tokens)
    if n_docs == 0 or not query_tokens:
        return [0.0] * n_docs
    doc_freq: dict[str, int] = {}
    for tokens in docs_tokens:
        for term in set(tokens):
            doc_freq[term] = doc_freq.get(term, 0) + 1
    avg_len = (sum(len(t) for t in docs_tokens) / n_docs) or 1.0
    scores = []
    for tokens in docs_tokens:
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        dl = len(tokens) or 1
        score = 0.0
        for term in query_tokens:
            f = tf.get(term, 0)
            if f == 0:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
            denom = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / avg_len)
            score += idf * (f * (_BM25_K1 + 1)) / denom
        scores.append(score)
    return scores


def _similar_matches(query: str, crawlers: list[dict]) -> list[dict]:
    """Rank crawlers that share at least one non-stopword query token.

    Only called when `results` is empty (see search_scrapers_impl), so there
    is nothing to exclude — every partial match is fair game.

    Ranking:

    1. the same BM25 score `results` matching would use if it existed (see
       _bm25_scores) — its idf already weights a rare, distinctive word like
       "reddit" far above a common one like "posts", which is what puts
       Reddit Scraper first for "Reddit search posts comments" without a
       separate word-count rule (an earlier version of this ranked by matched
       word count first; that put a crawler matching 3 generic words above
       one matching 2, including the distinctive one, so it's gone);
    2. `total_runs` (lifetime run count) descending, read tolerantly —
       `crawler.get("total_runs") or 0` — since the field may be absent
       (older catalog snapshot) or genuinely 0; absent-everywhere behaves
       identically to today, all rows tying at 0 and falling through to (3);
    3. name (or id) ascending, so the order is deterministic even when the
       above all tie.

    Each row carries `matched`/`missing`: the query words found / not found
    in that crawler's name, description and slug, checked as whole tokens
    (not substrings), so a short query word can't falsely "match" by
    appearing inside an unrelated word. These are informational only — they
    do not affect the ranking above."""
    query_terms = list(dict.fromkeys(_tokenize(query)))  # dedup, keep order
    if not query_terms or not crawlers:
        return []
    candidates = crawlers
    docs_tokens = [_crawler_tokens(c) for c in candidates]
    doc_term_sets = [set(tokens) for tokens in docs_tokens]
    scores = _bm25_scores(query_terms, docs_tokens)

    def sort_key(i: int):
        name = candidates[i].get("name") or candidates[i].get("id") or ""
        total_runs = candidates[i].get("total_runs") or 0
        return (-scores[i], -total_runs, name)

    ranked = sorted(range(len(candidates)), key=sort_key)
    out = []
    for i in ranked:
        if scores[i] <= 0:
            break  # sorted by score first — nothing after this scores higher than 0
        doc_terms = doc_term_sets[i]
        # score[i] > 0 implies at least one query term is in this doc's token
        # multiset, so `matched` below is never empty here.
        matched = [t for t in query_terms if t in doc_terms]
        missing = [t for t in query_terms if t not in doc_terms]
        row = _summary(candidates[i])
        row["matched"] = matched
        row["missing"] = missing
        out.append(row)
        if len(out) >= _SIMILAR_CAP:
            break
    return out


def _no_exact_match_hint(query: str, similar: list[dict]) -> str:
    if not similar:
        return f"No scraper matches '{query}' — nothing in the catalog shares a word with it."
    top = similar[0]
    matched = ", ".join(top["matched"])
    return (f"No scraper matches all of '{query}'. Closest: {top['name']} "
            f"(matches {matched}) — not an exact match; check get_scraper_details "
            "before assuming it covers the task.")


@structured
def search_scrapers_impl(client: LobstrClient, query: str, full: bool = False) -> dict:
    crawlers = client.list_crawlers()
    tokens = (query or "").strip().lower().split()

    def matches(c: dict) -> bool:
        blob = f"{c.get('name','')} {c.get('description','')} {c.get('slug','')}".lower()
        # no query -> everything matches (a browse); otherwise every word must appear
        return all(tok in blob for tok in tokens)

    if full:
        ranked = sorted(crawlers, key=lambda c: (0 if tokens and matches(c) else 1))
        return {"count": len(ranked), "total_available": len(crawlers),
                "results": [_summary(c) for c in ranked]}

    hits = [c for c in crawlers if matches(c)]
    capped = hits[:_SEARCH_CAP]
    out = {"count": len(capped), "total_available": len(crawlers),
           "results": [_summary(c) for c in capped]}

    if capped:
        # An exact match exists — `similar` is a pointer for when there is
        # none, not a second results page, so it stays empty here even if a
        # partial match also exists.
        out["similar"] = []
        if len(hits) > len(capped):
            out["truncated"] = True
            out["hint"] = (f"showing the first {len(capped)} of {len(hits)} matches — "
                           "narrow the query, or call again with full=true for all")
    else:
        out["similar"] = _similar_matches(query, crawlers)
        out["hint"] = _no_exact_match_hint(query, out["similar"])
    return out


def _strip_examples(schema: dict) -> dict:
    """Drop per-property `examples` from a JSON Schema — they roughly double its
    size and each property's description already conveys intent."""
    props = schema.get("properties")
    if not isinstance(props, dict):
        return schema
    trimmed = {
        name: ({k: v for k, v in spec.items() if k != "examples"}
               if isinstance(spec, dict) else spec)
        for name, spec in props.items()
    }
    return {**schema, "properties": trimmed}


# Crawlers whose task-level `city` accepts a ZIP/postal code and returns
# results from the surrounding area, not city limits.
_ZIP_CODE_SEARCHES_NEARBY_SLUGS = {"google-maps-leads-scraper"}


@structured
def get_scraper_details_impl(client: LobstrClient, scraper: str, full: bool = False) -> dict:
    scraper = resolve_crawler_id(client, scraper)
    crawler = client.get_crawler(scraper)
    # /params tells us which squid inputs are function toggles (nested under
    # params.functions); the returned param_levels must match what run_scraper
    # will actually do with them. It also carries squid inputs the crawler's
    # input[] leaves out on purpose (auto_verify_emails, which the dashboard
    # draws its own control for), so the schema below is input[] plus whatever
    # /params describes that input[] does not.
    crawler_params = client.get_crawler_params(scraper)
    translated = translate_input_schema(crawler, params=crawler_params)
    schema = translated["json_schema"]
    if not full:
        schema = _strip_examples(schema)
    result = {
        "id": crawler.get("id", scraper),
        "name": crawler.get("name"),
        "description": crawler.get("description", ""),
        "input_schema": schema,
        # Which of the inputs above are "task" (add_tasks/per-row), "squid"
        # (create_squid's config / a squid-level run_scraper input), or
        # "function" (nested under params.functions — never send these flat to
        # create_squid's config, that's what orphaned a squid in production).
        # run_scraper splits by this map itself; create_squid
        # does not, so check it before building `config` by hand.
        "param_levels": translated["levels"],
        "output_fields": crawler.get("result", []),
        # (param_wire_names is added below, only when this crawler has one.)
        "credits_per_row": resolve_credit_rate(crawler.get("credits_per_row")),
        "credits_per_email": resolve_credit_rate(crawler.get("credits_per_email")),
        "is_available": crawler.get("is_available"),
        "max_concurrency": crawler.get("max_concurrency"),
        # None when this crawler needs no platform account at all; otherwise
        # the account-type slug (e.g. "linkedin-sync") a squid built from it
        # needs attached before a run can succeed — without one the run is
        # created but fails asynchronously with done_reason "no_accounts".
        # Previously only discoverable by hitting that failure.
        #
        # Reuses crawler_account_type() unchanged. Verified live against the
        # real serializer: a no-account crawler's `account` field is `null`
        # (never an absent key with a value, and never a `{"type": "none"}`
        # sentinel — the serializer makes that shape structurally impossible,
        # a module with no account type serialises to null by construction),
        # which crawler_account_type() already reads as None; an account-
        # backed crawler returns the object with the real slug. Both this
        # field and run_scraper's/attach_account's account resolution share
        # that read and are both correct against the live shape.
        "required_account_type": crawler_account_type(crawler),
    }
    # e.g. Google Maps: supply `url` OR `category`+`country`+`city`, not both.
    if translated.get("input_modes"):
        result["input_modes"] = translated["input_modes"]
    # A crawler can declare two different inputs under one name at two levels
    # (Google Maps' `country`: task-level, part of the search URL; squid-level,
    # Google's region). Only one can keep the plain name, so the other is
    # published under an alias — this maps each alias to the name the API
    # knows. run_scraper translates them itself; create_squid's `config` and
    # add_tasks go straight to the API, so use the name on the right there.
    # Absent entirely for a crawler with no such collision.
    if translated.get("wire_names"):
        result["param_wire_names"] = translated["wire_names"]
    if crawler.get("slug") in _ZIP_CODE_SEARCHES_NEARBY_SLUGS:
        result["note"] = ("A ZIP/postal code in `city` returns nearby towns too, not just "
                          "that one — filter on the `city` output field for city limits only.")
    return result


_SQUID_LIST_KEYS = ("data", "results", "records")


def _squid_rows(payload) -> list:
    """Pull the row list out of a /squids response (envelope or bare list)."""
    if isinstance(payload, list):
        return payload
    for k in _SQUID_LIST_KEYS:
        if isinstance(payload.get(k), list):
            return payload[k]
    return []


def _squid_summary(s: dict) -> dict:
    """Compact, model-facing view of a squid. Surfaces the crawler it was built
    from and its last-run status so the model can tell squids apart; null extras
    are dropped to keep the listing light."""
    out = {"id": s.get("id"), "name": s.get("name")}
    crawler = s.get("crawler_name") or s.get("crawler")
    if crawler:
        out["crawler"] = crawler
    if s.get("last_run_status"):
        out["last_run_status"] = s.get("last_run_status")
    if s.get("last_run_at"):
        out["last_run_at"] = s.get("last_run_at")
    if s.get("created_via"):
        out["created_via"] = s.get("created_via")  # webapp/api/sdk/cli/mcp
    return out


@structured
def list_my_scrapers_impl(client: LobstrClient, *, name: str | None = None,
                          crawler: str | None = None, page: int = 1,
                          page_size: int = 50) -> dict:
    # A user can have hundreds of squids, so this pages rather than dumping them
    # all into the model's context. `name` filters server-side; `crawler`
    # (slug/name/id) has no server filter, so when it's set we walk the squids
    # (capped) and filter + paginate in memory.
    if crawler:
        crawler_id = resolve_crawler_id(client, crawler)
        needle = crawler.lower()
        name_l = name.lower() if name else None

        def keep(s: dict) -> bool:
            hit = (s.get("crawler") == crawler_id
                   or needle in (s.get("crawler_name") or "").lower())
            return hit and (name_l is None or name_l in (s.get("name") or "").lower())

        matched = [s for s in client.list_squids() if keep(s)]
        total = len(matched)
        total_pages = max(1, (total + page_size - 1) // page_size)
        start = (page - 1) * page_size
        rows = matched[start:start + page_size]
        nxt = page + 1 if page < total_pages else None
    else:
        env = client.page_squids(name=name, page=page, page_size=page_size)
        rows = _squid_rows(env)
        if isinstance(env, dict):
            total, total_pages, page, nxt = (env.get("total_results"),
                                             env.get("total_pages"),
                                             env.get("page", page), env.get("next"))
        else:  # bare list — a single page
            total, total_pages, nxt = len(rows), 1, None

    return {
        "count": len(rows),
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "next": nxt,
        "scrapers": [_squid_summary(s) for s in rows if isinstance(s, dict)],
    }


# Heavy / non-actionable squid fields not worth handing to an LLM. `icon` is a
# ~4KB base64 SVG; `ui_state` is dashboard UI scratch state.
_SQUID_DROP_FIELDS = ("icon", "ui_state")


@structured
def get_my_scraper_impl(client: LobstrClient, squid_id: str) -> dict:
    squid = client.get_squid(squid_id)
    if isinstance(squid, dict):
        return {k: v for k, v in squid.items() if k not in _SQUID_DROP_FIELDS}
    return squid


@structured(verify_with="estimate_run(squid_id=...), whose `tasks.count` is how many input "
                        "rows the scraper still holds")
def empty_scraper_impl(client: LobstrClient, squid_id: str) -> dict:
    """Remove all tasks (inputs) from a squid without deleting the squid itself —
    lets the caller reuse a configured scraper with fresh inputs."""
    client.empty_squid(squid_id)
    return {"squid_id": squid_id, "status": "emptied",
            "message": "All tasks removed. The squid's configuration is kept; add new tasks and run again."}


@structured(verify_with="get_my_scraper(squid_id=...) and its `is_active`")
def deactivate_scraper_impl(client: LobstrClient, squid_id: str) -> dict:
    """Deactivate a squid to free its concurrency slot, keeping its config +
    results (unlike deleting). Reversible via the dashboard."""
    client.set_squid_active(squid_id, False)
    return {"squid_id": squid_id, "is_active": False, "status": "deactivated",
            "message": "Scraper deactivated — its concurrency slot is now free, and it keeps all "
                       "its configuration and results (unlike deleting). Any in-progress run was "
                       "stopped. Reactivate it from your lobstr.io dashboard to run it again."}


def register_scraper_tools(mcp, client_factory, authorizer=None) -> None:
    def authz(scopes):
        if authorizer:
            authorizer(scopes)

    @mcp.tool(annotations={"title": "Search Scrapers", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def search_scrapers(query: str, full: bool = False, toon: bool = False) -> dict:
        """Find the most relevant Lobstr scrapers for a request. These are
        *crawlers* — the scraper templates you run (e.g. "Google Maps"); running
        one creates a *squid*, your saved, configured instance of it.

        `results` are exact matches — every word in `query` appears in that
        crawler's name, description or slug. `similar` only appears when
        `results` is empty: up to 5 ranked crawlers that share SOME of the
        words (each row also carries `matched`/`missing`) — check
        `get_scraper_details` before assuming one covers the task. An empty
        `results` with a non-empty `similar` does NOT mean the scraper
        doesn't exist: read `similar` and `hint` before concluding nothing
        matches. Only when both are empty has nothing in the catalog matched
        any word of the query.

        Returns the top matches as JSON; pass toon=true for compact TOON
        (fewer tokens), or full=true to get the entire catalog (large; no
        `similar` in that mode since it already returns everything). Concepts
        & full reference: https://docs.lobstr.io/mcp"""
        authz(READ_SCOPES)
        out = search_scrapers_impl(client_factory(), query, full=full)
        return toon_result(out) if toon else out

    @mcp.tool(annotations={"title": "Get Scraper Details", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def get_scraper_details(scraper: str, full: bool = False) -> dict:
        """Get a scraper (crawler) template's input schema, output fields, and
        pricing. `param_levels` marks each input "task" (add_tasks), "squid"
        (create_squid's config), or "function" (nested under params.functions —
        never pass these flat to create_squid's config). When a crawler names
        two different inputs the same at two levels, one is published under a
        prefixed alias (e.g. `squid_country`) and `param_wire_names` maps that
        alias to the name the API knows — run_scraper's `input` takes the
        alias, create_squid's `config` and add_tasks take the API name.
        `required_account_type`
        is null when no platform account is needed, else the account type a
        squid built from this crawler must have attached (via attach_account or
        run_scraper's account_id) before a run can succeed. `note`, when
        present, is a crawler-specific heads-up (e.g. Google Maps: a
        ZIP/postal code in `city` returns nearby towns too — filter on the
        `city` output field for city limits only). Pass full=true to
        also include per-input examples."""
        authz(READ_SCOPES)
        return get_scraper_details_impl(client_factory(), scraper, full=full)

    @mcp.tool(annotations={"title": "List My Scrapers", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def list_my_scrapers(name: str | None = None, crawler: str | None = None,
                         page: int = 1, page_size: int = 50,
                         toon: bool = False) -> dict:
        """List the current user's saved scrapers (squids) — instances they've
        configured from a crawler template. Paged (page/page_size) so large
        accounts don't overflow; the reply carries total/total_pages/next.
        Filter with name (matches the squid's name) and/or crawler (the template's
        slug, name, or id). Pass toon=true for compact TOON."""
        authz(READ_SCOPES)
        out = list_my_scrapers_impl(client_factory(), name=name, crawler=crawler,
                                    page=page, page_size=page_size)
        return toon_result(out) if toon else out

    @mcp.tool(annotations={"title": "Get My Scraper", "readOnlyHint": True,
                           "destructiveHint": False, "openWorldHint": True})
    def get_my_scraper(squid_id: str) -> dict:
        """Get one of the user's saved scrapers (a squid) by id — its full
        configuration."""
        authz(READ_SCOPES)
        return get_my_scraper_impl(client_factory(), squid_id)

    @mcp.tool(annotations={"title": "Empty My Scraper",
                           "readOnlyHint": False,
                           # Clears the squid's tasks but keeps the squid and its
                           # config; not a delete of the scraper itself.
                           "destructiveHint": True,
                           "idempotentHint": True, "openWorldHint": True})
    def empty_scraper(squid_id: str) -> dict:
        """Remove all tasks (inputs) from one of the user's scrapers (a squid),
        keeping the squid and its configuration so it can be reused with new
        inputs. Does not delete the scraper."""
        authz(EXECUTE_SCOPES)
        return empty_scraper_impl(client_factory(), squid_id)

    @mcp.tool(annotations={"title": "Deactivate My Scraper",
                           "readOnlyHint": False,
                           # Frees a slot and stops any in-flight run, but keeps
                           # the squid + its data — reversible, not a delete.
                           "destructiveHint": False,
                           "idempotentHint": True, "openWorldHint": True})
    def deactivate_scraper(squid_id: str) -> dict:
        """Deactivate one of the user's scrapers (a squid) to free its concurrency
        slot, WITHOUT deleting it — the squid keeps its configuration and results
        and can be reactivated later from the dashboard. Prefer this over deleting
        a scraper you might reuse (deleting loses its data). Note: deactivating
        stops any run currently in progress."""
        authz(EXECUTE_SCOPES)
        return deactivate_scraper_impl(client_factory(), squid_id)
