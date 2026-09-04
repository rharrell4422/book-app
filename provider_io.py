"""Provider I/O for book discovery: the raw catalog-API/web-search fetches,
plus the Anthropic LLM calls that structure and reconcile their results, and
the small amount of per-provider bookkeeping (publication-date backfill,
web-search health diagnostics) built on top of them.

Split out of discovery_engine.py (RT-1a/RT-2). This is the "talks to the
outside world" half of what used to be one file -- HTTP calls to Google
Books/OpenLibrary/Hardcover/Serper, and Anthropic calls to turn unstructured
web-search snippets or a messy multi-provider candidate list into
structured data. deterministic_fusion.py is the pure, no-I/O counterpart
that consumes this module's output; see that module's docstring.

discovery_engine.py re-exports everything below, so existing external
callers (agents/series_agent.py, provider_protocol.py's lazy adapter
imports, tests, etc.) are unaffected by this split.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import trafilatura
from dotenv import load_dotenv

from apify_provider import ApifyCallBudget, apify_enabled, fetch_apify_candidates
# PP-2/PP-3: _fetch_all_providers_parallel calls providers through these
# adapters (never-raising, uniform ProviderFetchResult contract) instead of
# calling _fetch_google_books/_fetch_openlibrary/_fetch_hardcover/
# _fetch_web_search directly -- see provider_protocol.py's module docstring.
from provider_protocol import (
    GoogleBooksProvider,
    OpenLibraryProvider,
    HardcoverProvider,
    WebDiscoveryProvider,
)
from services.discovery_cache import CACHE_MISS, DiscoveryCache
from services.discovery_telemetry import DiscoveryTelemetry, maybe_pass_scope
from services.identity import _normalize_series_name_for_identity
from llm_client import call_llm
# HTA Orchestrator Step 5: Tier A/B prompt assembly now lives in
# prompts.py -- these two names are re-exported here unchanged so
# discovery_engine.py's existing `from provider_io import
# _WEB_SEARCH_STRUCTURING_PROMPT, _LLM_RECONCILIATION_PROMPT` keeps
# working with no import changes needed there.
from prompts import (
    _WEB_SEARCH_STRUCTURING_PROMPT,
    _LLM_RECONCILIATION_PROMPT,
    build_extraction_prompt,
    build_canonical_page_extraction_prompt,
    build_reconciliation_prompt,
)

from discovery_text import (
    _author_matches,
    _log,
    _series_names_compatible,
    _to_int_or_none,
    core_title_key,
    infer_number_from_title,
    normalize_series_name_for_query,
    normalize_text,
)
from deterministic_fusion import (
    UnifiedCandidate,
    _METADATA_COMPLETENESS_FIELDS,
    _fuse_and_score_candidates,
    _reconciled_completeness_score,
    _resolve_candidate_number,
    catalog_providers_are_sufficient,
)
from diagnostics import _record_drop_diagnostic

# Loaded here too (in addition to discovery_engine.py) so this module reads
# the right API keys from the environment even if it's ever imported
# directly, without going through discovery_engine.py first.
load_dotenv()

GOOGLE_BOOKS_ENDPOINT = "https://www.googleapis.com/books/v1/volumes"
OPENLIBRARY_ENDPOINT = "https://openlibrary.org/search.json"
HARDCOVER_ENDPOINT = "https://api.hardcover.app/v1/graphql"
# Brave Search is no longer a viable provider: its only sub-enterprise tier
# caps out at 1000 queries/month, which this app hit during personal-use
# testing alone. Serper is its replacement -- see
# discovery_agentic_migration_decision_log.md. Serper's coverage of the
# indie/LitRPG/web-serial sources Brave used to surface is unverified and
# may differ; see _web_search_enabled/_llm_structuring_enabled below and
# discover_candidates_for_series' diagnostic-only mode for how to check
# that coverage by hand before relying on it.
SERPER_SEARCH_ENDPOINT = "https://google.serper.dev/search"
REQUEST_TIMEOUT_SECONDS = 12.0
WEB_SEARCH_TIMEOUT_SECONDS = 20.0

# Structured APIs (Google Books/OpenLibrary/Hardcover) index catalog metadata,
# which lags behind for indie/self-published titles and pure announcements
# that only exist as blog posts, retailer pre-order pages, or author social
# posts. This provider fills that gap with a live web search whose raw,
# unstructured results are then normalized into the same candidate shape by
# a small LLM call, rather than trying to hand-write regexes/heuristics for
# every possible way a book announcement can be phrased across the web.
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
WEB_SEARCH_MAX_RESULTS = 8

# A generic "<series> <author>" search's relevance ranking skews heavily
# towards whichever entry has the most existing links/reviews -- almost
# always book 1 -- so a brand new release can fail to place in the top
# results even with a generous count (observed live: book 8 of a series
# appeared in a plain "<series> <author>" search, but book 9, an
# announced-but-not-yet-released preorder, did not, even at count=20).
# Explicitly searching "<series> book <N>" for the next few sequential
# numbers reliably surfaces it instead, so both query styles are combined.
#
# Raised from 3 to 10 (see discovery_catchup_architecture_spec.md #2.1):
# long/under-indexed series (e.g. the Jonathan Hunt case that motivated this
# spec) can sit many volumes ahead of the highest owned book, and 3 wasn't
# wide enough to catch them in one pass. Still batched into a single LLM
# structuring call regardless of width -- see _fetch_web_search -- so this
# only adds web-search-provider calls (and a larger prompt on that one
# call), not LLM call count.
WEB_SEARCH_LOOKAHEAD_BOOKS = 10

# When a candidate's first-pass query snippet doesn't include a release date,
# a second, title-specific "<title> release date" query surfaces
# date-focused pages (Goodreads, author sites, retailer detail pages, "new
# releases this week" roundups, etc.) far more often than the broader
# "<series> book N" query does -- observed live: a just-released book's
# generic listing had no date in its snippet and got wrongly defaulted to
# "upcoming" until this second look ran. Capped since it costs one extra
# web-search + Anthropic call per undated candidate.
WEB_SEARCH_DATE_REFINEMENT_MAX = 3

# Bounds concurrent web-search-provider requests within one _fetch_web_search
# call (the targeted pass alone can have WEB_SEARCH_LOOKAHEAD_BOOKS + 1 = 11
# distinct queries) -- a small fixed pool, not one thread per query, so a
# wide lookahead window doesn't fire a burst of ~11 simultaneous requests and
# risk provider rate-limiting that never happened when these were
# sequential. Purely a latency optimization: does not change web-search call
# count, LLM call count, or which URLs get fetched.
WEB_SEARCH_MAX_PARALLEL_WORKERS = 5

# Hardcover's own search index tags each hit with its position within a
# series (when it has one), which is a far more reliable source of a book's
# number than trying to parse it out of free-text title formatting -- so a
# result from this provider carries that as an explicit hint rather than
# leaving it to title-text inference.
_HARDCOVER_SEARCH_QUERY = """
query Search($query: String!, $perPage: Int!) {
  search(query: $query, query_type: "Book", per_page: $perPage) {
    results
  }
}
"""

# OpenLibrary (and, less aggressively, Google Books) apply basic
# bot-mitigation heuristics that can reject requests using generic HTTP
# client default user agents. A descriptive User-Agent identifying this
# app, per OpenLibrary's own guidance, avoids spurious 403s.
REQUEST_HEADERS = {"User-Agent": "BookAppSeriesTracker/1.0 (personal series-tracking tool)"}


def _fetch_google_books(query: str, max_results: int = 40) -> list[dict]:
    params: dict = {"q": query, "maxResults": max_results}
    api_key = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()
    if api_key:
        params["key"] = api_key
    response = httpx.get(GOOGLE_BOOKS_ENDPOINT, params=params, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    items = (response.json() or {}).get("items") or []
    results: list[dict] = []
    for item in items:
        info = item.get("volumeInfo") or {}
        title = str(info.get("title") or "").strip()
        if not title:
            continue
        subtitle = str(info.get("subtitle") or "").strip()
        full_title = f"{title}: {subtitle}" if subtitle else title
        identifiers = info.get("industryIdentifiers") or []
        isbn13 = next((i.get("identifier") for i in identifiers if i.get("type") == "ISBN_13"), None)
        results.append(
            {
                "source": "google_books",
                "source_id": item.get("id"),
                "title": full_title,
                "authors": info.get("authors") or [],
                "published_date": str(info.get("publishedDate") or "").strip(),
                "description": info.get("description"),
                "isbn13": str(isbn13 or "").strip() or None,
                "source_url": str(info.get("infoLink") or "").strip() or None,
                "language": str(info.get("language") or "").strip(),
            }
        )
    return results


def _fetch_openlibrary(query: str, max_results: int = 40) -> list[dict]:
    params = {"q": query, "limit": max_results}
    response = httpx.get(OPENLIBRARY_ENDPOINT, params=params, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    docs = (response.json() or {}).get("docs") or []
    results: list[dict] = []
    for doc in docs:
        title = str(doc.get("title") or "").strip()
        if not title:
            continue
        year = doc.get("first_publish_year")
        isbn_list = doc.get("isbn") or []
        languages = doc.get("language") or []
        results.append(
            {
                "source": "openlibrary",
                "source_id": doc.get("key"),
                "title": title,
                "authors": doc.get("author_name") or [],
                "published_date": str(year) if year else "",
                "description": None,
                "isbn13": next((i for i in isbn_list if len(str(i)) == 13), None),
                "source_url": f"https://openlibrary.org{doc.get('key')}" if doc.get("key") else None,
                "language": str(languages[0]) if languages else "",
            }
        )
    return results


def _fetch_hardcover(query: str, max_results: int = 25) -> list[dict]:
    api_key = os.environ.get("HARDCOVER_API_KEY", "").strip()
    if not api_key:
        return []

    headers = {**REQUEST_HEADERS, "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"query": _HARDCOVER_SEARCH_QUERY, "variables": {"query": query, "perPage": max_results}}
    response = httpx.post(HARDCOVER_ENDPOINT, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    body = response.json() or {}
    if body.get("errors"):
        raise RuntimeError(str(body["errors"])[:300])

    hits = (((body.get("data") or {}).get("search") or {}).get("results") or {}).get("hits") or []
    results: list[dict] = []
    for hit in hits:
        doc = hit.get("document") or {}
        title = str(doc.get("title") or "").strip()
        if not title:
            continue

        isbns = doc.get("isbns") or []
        isbn13 = next((i for i in isbns if len(str(i)) == 13), None)

        featured_series = doc.get("featured_series") or {}
        series_position = None
        raw_position = featured_series.get("position")
        if raw_position is not None:
            try:
                # Rounding to the nearest int used to collapse Hardcover's
                # fractional positions (0.5-style companion novellas/side
                # stories, e.g. "Threshing Day" at position 3.5 in The
                # Empyrean) into a whole number -- and Python's round-half-
                # to-even rounds X.5 *up* whenever the next integer is even,
                # so position 3.5 became "Book 4", making a side story
                # indistinguishable from (and blocking/confusing detection
                # of) the real next numbered entry. Keep the float as-is so
                # downstream code can tell a companion book (non-integer
                # position) apart from a genuine next-in-sequence book.
                series_position = float(raw_position)
            except (TypeError, ValueError):
                series_position = None

        # Hardcover's own crowd-sourced series length -- a real answer to
        # "how many books are in this series" that doesn't depend on our own
        # search coverage. primary_books_count (numbered main entries) is
        # preferred over books_count (which also counts 0.5-style novellas/
        # side content) since it's the more intuitive "book N of M" figure;
        # falls back to books_count when that's the only one present.
        series_info = featured_series.get("series") or {}
        series_total_hint = series_info.get("primary_books_count") or series_info.get("books_count")
        try:
            series_total_hint = int(series_total_hint) if series_total_hint else None
        except (TypeError, ValueError):
            series_total_hint = None

        results.append(
            {
                "source": "hardcover",
                "source_id": doc.get("id"),
                "title": title,
                "authors": doc.get("author_names") or [],
                "published_date": str(doc.get("release_date") or "").strip(),
                "description": doc.get("description"),
                "isbn13": str(isbn13 or "").strip() or None,
                "source_url": f"https://hardcover.app/books/{doc.get('slug')}" if doc.get("slug") else None,
                "language": "",
                "series_number_hint": series_position,
                "upcoming_hint": bool(featured_series.get("unreleased")),
                # Only populated when Hardcover's own index actually ties
                # this book to a series -- used as a per-candidate series
                # name signal by discover_candidates_for_author, which (unlike
                # discover_candidates_for_series) has no single fixed series
                # name of its own to compare candidates against. The name
                # itself lives one level deeper than `position`/`unreleased`
                # (verified against a live API response) -- featured_series
                # is `{position, unreleased, series: {id, name, slug, ...}}`,
                # not a flat object with its own "name" key.
                "series_name_hint": str((featured_series.get("series") or {}).get("name") or "").strip() or None,
                "series_total_hint": series_total_hint,
            }
        )
    return results


# Bounds how many extra, dedicated Hardcover lookups
# backfill_missing_publication_dates will issue in one call -- an unusually
# large batch of undated candidates (e.g. a big author-fallback sweep)
# shouldn't turn into a dozen-plus extra live API calls on top of everything
# else a Check Now run already does. Anything beyond the cap is left with
# no published_date, still covered by classify_upcoming's existing
# conservative "unconfirmed" default.


MAX_PUBLICATION_DATE_BACKFILL_LOOKUPS = 8

# Bounds verify_missing_volume_recovery_dates's own lookups, separately from
# MAX_PUBLICATION_DATE_BACKFILL_LOOKUPS above -- deliberately small (this
# path is meant for the rare 1-3 candidates a single Check Now run recovers
# via the missing-volume lookahead, not a large sweep) and also caps its own
# Apify fallback calls via a dedicated ApifyCallBudget, entirely separate
# from discover_candidates_for_series' own per-run Apify budget (see that
# function's ApifyCallBudget docstring) since this verification pass runs
# afterward, in series_agent.py, with no access to that budget instance.
MAX_MISSING_VOLUME_DATE_VERIFICATION_LOOKUPS = 3


def _find_matching_provider_date(
    hits: list[dict], isbn13: str, title_key: str, author: str
) -> tuple[str | None, str | None]:
    """NS-6: the shared "does this provider hit match this candidate, and
    if so what's its date/isbn13" loop body used by both
    backfill_missing_publication_dates and
    verify_missing_volume_recovery_dates below (the latter reuses it once
    for its Hardcover lookup and once more, with isbn13 forced blank, for
    its Apify fallback -- Apify hits are always matched by title+author
    only, never by ISBN, regardless of whether the candidate itself has
    one).

    Returns (published_date, isbn13) for the first hit that matches by
    ISBN (when isbn13 is given) or by core_title_key, and whose authors
    match via _author_matches -- or (None, None) if nothing matches. Pure
    lookup only; callers decide what to do with a match (fill only if
    blank, always override, etc.).
    """
    for hit in hits:
        if isbn13:
            if str(hit.get("isbn13") or "").strip() != isbn13:
                continue
        elif core_title_key(str(hit.get("title") or "")) != title_key:
            continue
        if not _author_matches(hit.get("authors") or [], author):
            continue
        hit_date = str(hit.get("published_date") or "").strip()
        if not hit_date:
            continue
        return hit_date, (str(hit.get("isbn13") or "").strip() or None)
    return None, None


def backfill_missing_publication_dates(candidates: list[dict], author: str) -> None:
    """Fills in a real published_date for candidates that don't have one,
    by issuing a dedicated Hardcover lookup per candidate (by ISBN when
    known, else by title) -- mutates each candidate dict in place, and only
    ever fills a blank date, never overwrites a real one.

    Candidates with no published_date at all are almost always ones the
    web-search+LLM pass surfaced without a confirmed date in its result
    snippets -- classify_upcoming's own conservative default then treats
    those as "not confirmed available yet" (see its docstring), which is
    often wrong for an already-released indie/KU title that the web-search
    provider just didn't happen to state a date for. The same real book is
    frequently already in Hardcover's structured catalog with a real release date --
    it just didn't get chosen as this fused candidate's representative hit
    because the broader "<series> <author>" search that ran earlier either
    didn't surface it prominently enough, or Hardcover's own search index
    doesn't have this specific title tagged under the series at all (see the
    live regression this was written for: Georgia Wagner's "Jonathan Hunt
    Thriller Series" -- Hardcover's series-scoped search never surfaced
    several already-released titles, but a direct ISBN/title lookup for
    each one did, immediately, with a real past release date).

    A bare title lookup can collide with a same-titled, unrelated real book
    by a different author (regression while building this: "The Desert
    Reckoning" and "The Winter Siege" are both real, unrelated non-Wagner
    books already in Hardcover's catalog) -- guarded against by requiring
    the hit's own title to resolve to the same core_title_key AND its
    author(s) to match via _author_matches before trusting its date. An
    ISBN lookup is trusted on ISBN equality alone since ISBNs don't collide.
    """
    if not os.environ.get("HARDCOVER_API_KEY", "").strip():
        return

    lookups_done = 0
    for raw in candidates:
        if lookups_done >= MAX_PUBLICATION_DATE_BACKFILL_LOOKUPS:
            break
        if str(raw.get("published_date") or "").strip():
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue

        isbn13 = str(raw.get("isbn13") or "").strip()
        try:
            hits = _fetch_hardcover(isbn13 or title)
        except Exception:
            continue
        lookups_done += 1

        title_key = core_title_key(title)
        hit_date, hit_isbn13 = _find_matching_provider_date(hits, isbn13, title_key, author)
        if hit_date:
            raw["published_date"] = hit_date
            if not isbn13 and hit_isbn13:
                raw["isbn13"] = hit_isbn13


def verify_missing_volume_recovery_dates(candidates: list[dict], author: str) -> None:
    """Re-verifies published_date for every candidate tagged
    confidence=="missing_volume_recovery" against Hardcover, falling back to
    a dedicated Apify product lookup when Hardcover has nothing usable --
    and, unlike backfill_missing_publication_dates above, OVERRIDES an
    already-present date rather than only filling a blank one.

    missing_volume_recovery candidates come from _reconstruct_series_
    skeleton's lookahead pass: an LLM reading raw web-search snippets for a
    single targeted query ("<series> <author> book <N>"), the least
    reliable source this pipeline has for a hard fact like a release date --
    unlike Hardcover's/Apify's own structured catalog data, it can state a
    confident-looking date that's simply wrong, which classify_upcoming then
    trusts at face value. Live regression (2026-08-24): "Jonathan Hunt
    Thriller Series" Book 9 ("The Terror Plot") was recovered this way with
    published_date parsed as 2027-03-06, a full year after its real release
    (2026-06-11) -- wrongly classifying an already-available book as
    upcoming. backfill_missing_publication_dates couldn't have caught this:
    it explicitly skips any candidate that already has *a* date, blank or
    not being the only thing it checks.

    Deliberately narrow-scoped to missing_volume_recovery candidates only
    (never the broader candidate pool backfill_missing_publication_dates
    covers) -- every other source (Hardcover, Google Books, OpenLibrary,
    Apify's own structured product data, even a plain "targeted" web-search
    hit) already carries enough of its own structure/corroboration to be
    trusted at face value; re-verifying every candidate's date on every run
    would multiply this function's live API calls for no benefit on sources
    that aren't the problem.

    Mutates each matching candidate dict in place. Silently does nothing
    (never raises) if HARDCOVER_API_KEY is unset AND Apify isn't enabled --
    same fail-open convention as every other optional-provider function in
    this module.
    """
    targets = [raw for raw in candidates if raw.get("confidence") == "missing_volume_recovery"]
    if not targets:
        return

    hardcover_key = bool(os.environ.get("HARDCOVER_API_KEY", "").strip())
    apify_budget = ApifyCallBudget(max_calls=MAX_MISSING_VOLUME_DATE_VERIFICATION_LOOKUPS) if apify_enabled() else None
    if not hardcover_key and apify_budget is None:
        return

    lookups_done = 0
    for raw in targets:
        if lookups_done >= MAX_MISSING_VOLUME_DATE_VERIFICATION_LOOKUPS:
            break
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        lookups_done += 1

        isbn13 = str(raw.get("isbn13") or "").strip()
        title_key = core_title_key(title)
        existing_date = str(raw.get("published_date") or "").strip()
        verified_date: str | None = None
        verified_isbn13: str | None = None

        if hardcover_key:
            try:
                hits = _fetch_hardcover(isbn13 or title)
            except Exception:
                hits = []
            hardcover_date, hardcover_isbn13 = _find_matching_provider_date(hits, isbn13, title_key, author)
            if hardcover_date:
                verified_date = hardcover_date
                if not isbn13:
                    verified_isbn13 = hardcover_isbn13

        if verified_date is None and apify_budget is not None:
            try:
                apify_hits = fetch_apify_candidates(f"{title} {author}".strip(), None, apify_budget)
            except Exception:
                apify_hits = []
            # Apify hits are matched by title+author only, never by ISBN --
            # forcing isbn13="" here reuses the shared helper's title-match
            # branch regardless of whether this candidate itself has one.
            apify_date, apify_isbn13 = _find_matching_provider_date(apify_hits, "", title_key, author)
            if apify_date:
                verified_date = apify_date
                if not isbn13 and not verified_isbn13:
                    verified_isbn13 = apify_isbn13

        if verified_date and verified_date != existing_date:
            _log(
                f"missing_volume_recovery date override for {title!r}: "
                f"{existing_date or 'unset'!r} -> {verified_date!r}"
            )
            raw["published_date"] = verified_date
        if verified_isbn13 and not isbn13:
            raw["isbn13"] = verified_isbn13




def _web_search_enabled() -> bool:
    """Frontier web-search coverage (Serper), independent of whether an LLM
    is available to structure it into candidates -- see
    _llm_structuring_enabled and discover_candidates_for_series' diagnostic-
    only mode, which is exactly "web search enabled, LLM structuring not"
    made explicit and useful instead of just falling through to the
    original combined-gate behavior of running nothing at all.
    """
    return bool(os.environ.get("SERPER_API_KEY", "").strip())


def _llm_structuring_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _catalog_sufficiency_gate_enabled() -> bool:
    """Kill switch for catalog_providers_are_sufficient (see that
    function's docstring in deterministic_fusion.py) -- defaults on. Set
    CATALOG_SUFFICIENCY_GATE_ENABLED=false to fall back to the previous,
    always-run-web-search-when-configured behavior without a code change,
    e.g. if the gate is ever suspected of skipping web search/Apify on a
    series that genuinely needed it.
    """
    return os.environ.get("CATALOG_SUFFICIENCY_GATE_ENABLED", "true").strip().lower() != "false"


def _fetch_serper_web_search(
    query: str, count: int = WEB_SEARCH_MAX_RESULTS, *, telemetry: "DiscoveryTelemetry | None" = None
) -> list[dict]:
    """Brave Search's replacement (see SERPER_SEARCH_ENDPOINT's comment). Same
    return shape as the function this replaced (list of {title,
    description, url}), so every downstream consumer -- LLM structuring,
    caching, dedup -- is unaffected by the provider swap.
    """
    api_key = os.environ.get("SERPER_API_KEY", "").strip()
    if not api_key:
        return []

    headers = {**REQUEST_HEADERS, "X-API-KEY": api_key, "Content-Type": "application/json"}
    payload = {"q": query, "num": count}
    started = time.monotonic()
    try:
        response = httpx.post(
            SERPER_SEARCH_ENDPOINT, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
    finally:
        if telemetry is not None:
            telemetry.record_web_search_call(query=query, duration_s=time.monotonic() - started)

    hits = (response.json() or {}).get("organic") or []
    results: list[dict] = []
    for hit in hits:
        title = str(hit.get("title") or "").strip()
        url = str(hit.get("link") or "").strip()
        if not title or not url:
            continue
        results.append(
            {
                "title": title,
                "description": str(hit.get("snippet") or "").strip(),
                "url": url,
            }
        )
    return results


def _structure_web_results_with_llm(
    series_name: str | None,
    author: str,
    raw_results: list[dict],
    *,
    diagnostics: list[dict] | None = None,
    telemetry: "DiscoveryTelemetry | None" = None,
) -> list[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or not raw_results:
        return []

    snippets = "\n\n".join(
        f"[{i}] Title: {r['title']}\nSnippet: {r['description']}\nURL: {r['url']}"
        for i, r in enumerate(raw_results)
    )
    series_name = str(series_name or "").strip()
    if series_name:
        # Scoped to one specific series (the normal Check Now case) -- other
        # series by the same author are noise here, so explicitly excluded.
        scope_line = f'Target series: "{series_name}"'
        skip_other_series = " unrelated books by other authors, other series by the same author,"
        title_scope = "this series by this author"
    else:
        # Author-wide discovery has no single series to scope to -- every
        # series and standalone by this author is in scope, so the "other
        # series by the same author" exclusion from the scoped case would be
        # wrong here and is dropped.
        scope_line = "Target: ANY book by this author, across all of their series and standalone works."
        skip_other_series = " unrelated books by other authors,"
        title_scope = "this author"

    # HTA Orchestrator Step 5: Tier A prompt builder -- see prompts.py's
    # build_extraction_prompt docstring; zero behavior change from the
    # inline .format() call this replaces.
    prompt = build_extraction_prompt(
        scope_line=scope_line,
        author=author,
        count=len(raw_results),
        snippets=snippets,
        skip_other_series=skip_other_series,
        title_scope=title_scope,
    )

    started = time.monotonic()
    llm_response = None
    try:
        # HTA Orchestrator Step 7: dispatches via tier="A" now instead of a
        # positional ANTHROPIC_MODEL -- TIER_MODEL_MAP["A"] still resolves
        # to the same Haiku model, so this is a zero-behavior-change
        # migration off the positional call shape onto tier-based dispatch.
        llm_response = call_llm(
            tier="A",
            prompt=prompt,
            max_tokens=2000,
            # Deterministic extraction task (pull book_number/title/etc out of
            # unambiguous snippet text), not generative writing -- temperature=0
            # avoids run-to-run variance that otherwise intermittently drops a
            # correctly-worded candidate (see discovery_catchup_architecture_spec.md
            # recall-gap diagnostic).
            temperature=0,
            timeout=WEB_SEARCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # CR-2: a failed structuring call must degrade to
        # "no candidates from this pass" like a JSON-parse failure below,
        # not raise AttributeError from `llm_response` staying None on the
        # next line -- see _reconcile_candidates_with_llm for the same
        # pattern.
        _log(f"LLM web-search structuring call failed: {exc}")
        _record_drop_diagnostic(
            "web_structuring",
            {"title": None, "isbn13": None, "series_number": None},
            "llm_call_failure",
            diagnostics,
        )
        return []
    finally:
        if telemetry is not None:
            telemetry.record_llm_call(
                duration_s=time.monotonic() - started,
                tokens_in=llm_response.tokens_in if llm_response is not None else 0,
                tokens_out=llm_response.tokens_out if llm_response is not None else 0,
                model_id=ANTHROPIC_MODEL,
            )
    text = llm_response.text

    # The prompt asks for raw JSON, but strip markdown fences defensively in
    # case the model wraps its answer in one anyway.
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        _record_drop_diagnostic(
            "web_structuring",
            {"title": None, "isbn13": None, "series_number": None},
            "json_parse_failure",
            diagnostics,
        )
        return []
    return parsed if isinstance(parsed, list) else []


def _structure_canonical_page_with_llm(
    series_name: str | None,
    author: str,
    page_text: str,
    source_label: str,
    *,
    telemetry: "DiscoveryTelemetry | None" = None,
) -> list[dict]:
    """Canonical-page variant of _structure_web_results_with_llm above
    (Guided Discovery, Option A fix, 2026-09-03 Goodreads/Jonathan Hunt
    validation test) -- see build_canonical_page_extraction_prompt's own
    docstring for why this needs a dedicated prompt rather than reusing
    the snippet-oriented one. Otherwise identical machinery to
    _structure_web_results_with_llm (same call_llm tier="A" dispatch,
    same temperature=0 determinism, same markdown-fence stripping, same
    JSON-array parsing, same fail-soft [] on any missing-key/call/parse
    failure) -- deliberately NOT sharing that function's body directly
    since it also builds a {count}/{snippets} block for a list of
    raw_results that doesn't apply here (always exactly one page, never
    a list to enumerate), and it calls build_extraction_prompt, not this
    call site's build_canonical_page_extraction_prompt.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or not str(page_text or "").strip():
        return []

    series_name = str(series_name or "").strip()
    if series_name:
        scope_line = f'Target series: "{series_name}"'
    else:
        scope_line = "Target: ANY book by this author, across all of their series and standalone works."

    prompt = build_canonical_page_extraction_prompt(
        scope_line=scope_line,
        author=author,
        source_label=source_label,
        page_text=page_text,
    )

    started = time.monotonic()
    llm_response = None
    try:
        llm_response = call_llm(
            tier="A",
            prompt=prompt,
            max_tokens=2000,
            temperature=0,
            timeout=WEB_SEARCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # a failed structuring call must degrade to
        # "no candidates from this canonical page" like a JSON-parse
        # failure below, never raise out to fetch_canonical_page_candidates.
        _log(f"LLM canonical-page structuring call failed: {exc}")
        return []
    finally:
        if telemetry is not None:
            telemetry.record_llm_call(
                duration_s=time.monotonic() - started,
                tokens_in=llm_response.tokens_in if llm_response is not None else 0,
                tokens_out=llm_response.tokens_out if llm_response is not None else 0,
                model_id=ANTHROPIC_MODEL,
            )
    text = llm_response.text

    # The prompt asks for raw JSON, but strip markdown fences defensively in
    # case the model wraps its answer in one anyway.
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # 2026-09-03 observability fix (see fetch_canonical_page_
        # candidates's own new logging for the incident this addresses):
        # previously silent here too.
        _log(f"canonical page LLM structuring response was not valid JSON: {text[:200]!r}")
        return []
    return parsed if isinstance(parsed, list) else []


# Revised from 2500 (Guided Discovery iteration 5, item 19: "~2,000-3,000
# characters, matching WEB_SEARCH_MAX_RESULTS * a typical Serper snippet's
# size") after a live Jonathan Hunt/Goodreads re-test (2026-09-03, post-
# canonical-page-observability-logging fix) proved that budget wrong for
# this specific use case: the log line "canonical page fetch ... extracted
# 2500 chars of text" followed by "yielded 4 candidate(s)" showed the cap
# truncating the series' own book list after only its first 4 volumes
# (roughly 625 extracted chars/book on this page) -- out of 19 total. The
# iteration-5 rationale (matching the multi-*snippet* pipeline's own total
# budget) never actually applies here: that budget caps several short,
# independent snippets so no single one dominates a shared prompt, but a
# canonical series page is deliberately the OPPOSITE shape on purpose (see
# iteration 3, item 11's "one URL, one fetch" design and fetch_canonical_
# page_candidates's own docstring on why one page is expected to describe
# MANY books) -- capping it at snippet scale directly defeats that intent
# once a series has more than a handful of volumes. 15000 leaves headroom
# for a ~20-24 volume series at this page's observed density while keeping
# the fetch bounded (still one single one-time LLM call per Check Now
# round, not per-query, so the added token cost is trivial -- see
# fetch_canonical_page_candidates's own per-call cost in telemetry).
#
# Revised again to 20000 (live "Escape Velocity"/Backyard Starship
# investigation, 2026-09-03, alongside fetch_canonical_page_text's own
# fast=True fix below): a longer-running, higher-review-count series page
# is denser than the Jonathan Hunt page this budget was originally sized
# against -- a direct fetch+extract test against the real Backyard
# Starship page (35 primary works) produced 15,851 chars with fast=True,
# just over the old 15000 cap, which would have silently clipped the tail
# of the list (its own newest/most-wanted volumes) on every sufficiently
# long series from here on. 20000 leaves comparable headroom past that
# measurement to the old cap's headroom past its own Jonathan Hunt
# measurement (~19 volumes).
CANONICAL_PAGE_TEXT_MAX_CHARS = 20000


def fetch_canonical_page_text(url: str) -> str | None:
    """Guided Discovery (locked 2026-09-03, iterations 3-5): best-effort
    direct fetch of a user-supplied canonical series URL, extracted to
    bounded, readable plain text via trafilatura.

    Deliberately narrow and best-effort: a plain httpx GET using this
    module's existing REQUEST_HEADERS (the same honest, non-browser
    User-Agent every other fetch here already uses) has no chance against
    a JS-rendered SPA (e.g. Kobo/Google Play Books) and may be blocked
    outright by some retail sites -- exactly the situation Amazon already
    needed a dedicated Apify actor for (see apify_provider.py). This is an
    optional upgrade path, never a hard dependency: returns None (never
    raises) on any failure -- non-200, timeout, connection error, or
    trafilatura finding no extractable main content -- so the only caller
    (fetch_canonical_page_candidates) falls back to "no canonical
    candidates this round" rather than ever sinking discovery.

    The extracted text is capped at CANONICAL_PAGE_TEXT_MAX_CHARS -- see
    that constant's own docstring for the current value and the 2026-09-03
    revision (up from the original iteration-5 snippet-scale budget, after
    a live test proved it too small to cover a full ~19-volume series'
    listing) so a long series' book list isn't truncated before every
    volume on the page has a chance to be described.
    """
    cleaned_url = str(url or "").strip()
    if not cleaned_url:
        return None

    try:
        response = httpx.get(
            cleaned_url, headers=REQUEST_HEADERS, timeout=WEB_SEARCH_TIMEOUT_SECONDS, follow_redirects=True
        )
        response.raise_for_status()
    except Exception as exc:
        _log(f"canonical page fetch failed for {cleaned_url!r}: {exc}")
        return None

    html = response.text
    if not html:
        _log(f"canonical page fetch for {cleaned_url!r} returned an empty body (status={response.status_code})")
        return None

    try:
        # fast=True (live "Escape Velocity"/Backyard Starship investigation,
        # 2026-09-03): trafilatura's default mode runs a secondary
        # "which block is the real main content" scoring pass on top of its
        # primary extraction, designed for blog/news/article pages with one
        # dominant prose block competing against sidebar/nav noise. A
        # Goodreads series-listing page is the opposite shape -- dozens of
        # short, near-identical "Book N / title / ratings / reviews" blocks,
        # no single dominant prose block -- and that secondary pass can
        # misjudge the entire book list as boilerplate, keeping only one
        # stray long paragraph (a single book's blurb) instead. Confirmed
        # directly against the real page: default mode returned 353 chars
        # (just Book 1's blurb, zero structured list) for Backyard Starship,
        # while fast=True (skips that secondary pass, keeping the primary
        # extraction's fuller output) returned 15,851 chars covering every
        # book 0.5 through 35 on the same page. Verified fast=True doesn't
        # regress the Jonathan Hunt page this pipeline was originally built
        # and tested against -- byte-identical output to default mode there
        # (7,185 chars, books 1-18), since that page's book list apparently
        # scored high enough under the secondary pass to survive it anyway.
        # This is NOT a "different Goodreads page layout" fix (both pages
        # share the exact same template/markup, confirmed by direct diff of
        # the raw HTML) -- it's a heuristic false-negative in trafilatura's
        # own content-scoring, triggered by this page's much higher
        # ratings/reviews-per-book text density (35 long-running, heavily-
        # reviewed volumes vs. Jonathan Hunt's newer, sparser 18).
        extracted = trafilatura.extract(html, url=cleaned_url, fast=True)
    except Exception as exc:
        _log(f"canonical page extraction failed for {cleaned_url!r}: {exc}")
        return None

    cleaned_text = str(extracted or "").strip()
    if not cleaned_text:
        # Silent before this log line (2026-09-03 observability fix, added
        # after a live Jonathan Hunt/Goodreads re-test went from "LLM call
        # failed" (logged) on one run to zero canonical-related log lines
        # at all on the very next run, nine minutes later, same URL --
        # impossible to tell from logs alone whether that meant "no
        # canonical attempt happened" or "attempt happened but trafilatura
        # silently found nothing" (e.g. the site's own bot-blocking/rate-
        # limiting returning a 200 with an unusable interstitial/CAPTCHA
        # page body instead of an error status httpx would have raised
        # on). This is that missing signal.
        _log(
            f"canonical page fetch for {cleaned_url!r} succeeded (status={response.status_code}, "
            f"{len(html)} bytes of HTML) but trafilatura extracted no usable main-content text -- "
            "page may be a bot-block/interstitial/JS-shell rather than the real content"
        )
        return None
    return cleaned_text[:CANONICAL_PAGE_TEXT_MAX_CHARS]


def fetch_canonical_page_candidates(
    url: str,
    canonical_source: str,
    series_name: str | None,
    author: str,
    *,
    telemetry: "DiscoveryTelemetry | None" = None,
) -> list[dict]:
    """Guided Discovery (locked 2026-09-03, iterations 1-5; structuring
    prompt fixed 2026-09-03 -- Option A of the Goodreads/Jonathan Hunt
    validation test): structures a fetched canonical page's content into
    candidate dicts via _structure_canonical_page_with_llm's dedicated
    canonical-page prompt/schema -- NOT _structure_web_results_with_llm's
    snippet-oriented one.

    Originally reused _structure_web_results_with_llm unmodified (per
    Iteration 3/5's "reuse the current structuring pattern" requirement),
    but a live validation test against a real canonical page (Jonathan
    Hunt Thriller / Goodreads, 2026-09-03) produced zero extracted
    candidates: that prompt explicitly tells the model to SKIP "fan wiki
    summaries of a whole series"/"retailer category pages" -- exactly
    what a canonical series-listing page structurally looks like -- and
    frames its schema as one-book-per-input-result, never instructing the
    model that one input may describe many books. See
    build_canonical_page_extraction_prompt's own docstring for the full
    root-cause writeup. _structure_canonical_page_with_llm's prompt
    inverts both of those.

    Deliberately does NOT route through _structure_with_verdict_cache/
    _structure_and_pair_web_search_hits the way _fetch_web_search's normal
    queries do -- that pairing keys its output by source URL (see
    fresh_by_url there), which assumes at most ONE structured candidate
    per source URL. That assumption is wrong here on purpose: a canonical
    series page is expected to describe MANY books under one single URL
    (that's the entire point -- see the Guided Discovery design chat), so
    every structured item the LLM returns for this one page must survive,
    not just the last one. Every item is instead paired directly with the
    same single source dict below and run through
    _parse_web_search_structured_items exactly as _fetch_web_search's own
    pass_label="missing_volume" lookahead already does downstream of its
    own _fetch_web_search call.

    Returns [] (never raises) whenever the page fetch/extraction fails,
    ANTHROPIC_API_KEY isn't configured, or the LLM structures zero real
    book entries out of the page -- callers fall back to "no canonical
    candidates this round", never a hard failure.

    Tags every returned candidate "source": "canonical_page" (via
    _parse_web_search_structured_items's source_label override), not
    "web_search" -- a live validation test (Jonathan Hunt/Goodreads,
    2026-09-03) found the prompt fix above alone wasn't sufficient: every
    extracted candidate still got auto-rejected by confidence_engine
    because "web_search" grades "low", and title_confidence=="unverified"
    (true for any brand-new number) + any other dimension=="low" forces
    _overall_confidence to "low". "canonical_page" grades "medium" instead
    (Option A of that fix -- see confidence_engine._PROVIDER_CONFIDENCE's
    "canonical_page" entry for the full reasoning), which is enough to
    clear that bar without touching title_confidence/number_confidence/
    series_alignment_confidence at all.
    """
    page_text = fetch_canonical_page_text(url)
    if not page_text:
        # fetch_canonical_page_text already logs the specific reason
        # (fetch failure / empty body / trafilatura found nothing) --
        # nothing to add here.
        return []

    _log(f"canonical page fetch for {url!r} extracted {len(page_text)} chars of text -- structuring with LLM")

    raw_result = {
        "title": f"Canonical {canonical_source or 'source'} page",
        "description": page_text,
        "url": str(url or "").strip(),
    }
    try:
        parsed = _structure_canonical_page_with_llm(
            series_name, author, page_text, raw_result["title"], telemetry=telemetry
        )
    except Exception as exc:  # a structuring failure here must never sink discovery
        _log(f"canonical page structuring failed for {url!r}: {exc}")
        return []
    if not parsed:
        # 2026-09-03 observability fix (same incident as fetch_canonical_
        # page_text's own new log line above): previously silent, so a
        # canonical page that fetched fine but the LLM judged to describe
        # zero real books (or a JSON-parse failure inside
        # _structure_canonical_page_with_llm -- see that function's own
        # logging) was indistinguishable in the logs from this function
        # never having been called at all.
        _log(f"canonical page structuring for {url!r} returned zero book candidates")
        return []

    structured_with_source = [(item, raw_result) for item in parsed if isinstance(item, dict)]
    results = _parse_web_search_structured_items(structured_with_source, author, source_label="canonical_page")
    # Author Bibliography Discovery (2026-09-04): tag every candidate with
    # the specific canonical_source it came from (KU/Nook/Kobo/GooglePlay/
    # PublisherSite/Goodreads/Other), not just the generic "canonical_page"
    # source label already set above. Nothing in confidence_engine/
    # deterministic_fusion reads this key -- it rides along on the dict
    # unchanged the same way confidence_score/metadata_completeness_score
    # already do (see _unified_candidate_to_raw_dict) -- but agents/
    # series_agent.py's routing block reads it to force a "PublisherSite"
    # candidate (an author's own personal site/blog) to needs_review even
    # when it otherwise passes belongs_to_series cleanly: unlike Goodreads/
    # retailer canonical pages, a personal site's own announcement of an
    # upcoming book is often a tentative/self-reported date, not a vetted
    # catalog listing.
    cleaned_canonical_source = str(canonical_source or "").strip() or None
    for item in results:
        item["canonical_source"] = cleaned_canonical_source
    _log(f"canonical page for {url!r} yielded {len(results)} candidate(s): {[r['title'] for r in results]}")
    return results


_SERIES_OVERVIEW_PROMPT = """You are writing a short, spoiler-light overview of a book series for a reader deciding whether to start it.

Series: "{series_name}" by {author}

Below are the descriptions of books discovered in this series so far. Use ONLY this information -- do not invent plot details, characters, or facts not present in the text below.

{book_descriptions}

Write 2-4 sentences covering: the general premise/setting/genre, and what kind of reader would enjoy it. Do not describe it book-by-book or mention book numbers. Do not add a title or heading. Respond with ONLY the overview text, no prose before or after it."""


def generate_series_overview(series_name: str, author: str, books: list[dict]) -> str | None:
    """On-demand only (called from a "Series Overview" button click, never
    during discovery itself) -- synthesizes a short premise summary for a
    series the user doesn't own yet, from descriptions already fetched
    during discovery. Reuses the same Anthropic client/model as the
    web-search structuring pass rather than adding new LLM infrastructure.

    Deliberately does not re-fetch anything: the caller passes in whatever
    descriptions the discovery pass already returned, keeping this to a
    single LLM call with no additional catalog API cost.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None

    usable_books = [b for b in books if str(b.get("description") or "").strip()]
    if not usable_books:
        return None

    book_descriptions = "\n\n".join(
        f"- {str(b.get('title') or 'Untitled').strip()}: {str(b.get('description') or '').strip()}"
        for b in usable_books
    )
    prompt = _SERIES_OVERVIEW_PROMPT.format(
        series_name=series_name or "this series",
        author=author or "this author",
        book_descriptions=book_descriptions,
    )

    try:
        # Deliberately no `temperature` (this call wants the SDK's default,
        # more-generative behavior for readable prose) and no `telemetry`
        # (this on-demand call has never been tracked by DiscoveryTelemetry)
        # -- both asymmetries vs. the other two call sites are intentional,
        # not oversights; see llm-client-wrapper-evaluation canvas.
        #
        # HTA Orchestrator Step 7: stays outside the tier system entirely
        # (no `tier=`, no `pass_scope`) -- this call was and remains a
        # deliberate exception, not something Step 7 folds into Tier A/B.
        # `provider="anthropic"` is now required alongside the explicit
        # `model_id` (llm_client.call_llm no longer infers provider from
        # the model_id string).
        llm_response = call_llm(
            provider="anthropic",
            model_id=ANTHROPIC_MODEL,
            prompt=prompt,
            max_tokens=400,
            timeout=WEB_SEARCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # CR-2: on-demand, best-effort call -- a failure
        # here must surface as "no overview available" (this function's own
        # documented "not during discovery" contract), not a 500 from the
        # "Series Overview" button.
        _log(f"LLM series overview call failed: {exc}")
        return None
    return llm_response.text or None


def _refine_undated_web_search_results_batch(
    entries_to_refine: list[tuple[int, dict]],
    series_name: str,
    author: str,
    *,
    telemetry: "DiscoveryTelemetry | None" = None,
    cache: "DiscoveryCache | None" = None,
    scope_type: str = "series",
) -> dict[int, dict]:
    """Best-effort second look for candidates the first pass couldn't date:
    fires one dedicated "<title> release date" web-search query per candidate
    (query text must stay per-candidate -- a shared/merged query text would
    blur which hits belong to which title), then structures ALL of those
    queries' combined raw results in a single LLM call rather than one call
    per candidate -- mirrors the targeted pass's own multi-query-then-
    single-structure shape and cuts refinement's LLM-call count from one
    per undated candidate down to one per batch (bounded by
    WEB_SEARCH_DATE_REFINEMENT_MAX candidates either way).

    Both cache layers are shared with _fetch_web_search via
    _structure_with_verdict_cache: this query text is often repeated
    verbatim across rounds/passes for the same still-undated candidate
    (e.g. re-checking an upcoming book that hasn't been dated yet), so
    leaving it uncached meant paying a fresh web-search+LLM call for the
    exact same "<title> release date" search every single time it recurred.

    Correlation guardrail: a structured item is only ever applied to the
    one candidate whose OWN query actually returned that item's URL, AND
    whose title matches via core_title_key -- never "closest title in the
    whole batch". Handing the LLM one large, mixed-title batch in a single
    call is exactly the shape of prompt that caused the missing-volume
    recall-gap bug (architecture spec #8: a big noisy batch can misclassify
    or drop an individual item); requiring both source-query provenance and
    a title match keeps one candidate's resolved date from ever being
    misattributed to a different candidate. Any candidate this can't
    cleanly resolve a date for is simply absent from the returned dict --
    callers must leave that candidate's original entry untouched, not
    guess.

    Returns {result_index: refined_entry}, where result_index is each
    input tuple's own first element (the caller's index into its own
    results list) -- not related to the LLM's own per-call result_index.
    """
    refined: dict[int, dict] = {}
    per_candidate_urls: dict[int, set[str]] = {}
    all_raw: list[dict] = []
    seen_urls: set[str] = set()

    # Same cache-first-then-bounded-parallel shape as _fetch_web_search's
    # own query loop: resolve cache hits synchronously, fire only genuine
    # misses concurrently (capped -- WEB_SEARCH_DATE_REFINEMENT_MAX is small
    # already, but no reason to serialize what doesn't need to be).
    candidate_queries: dict[int, str] = {}
    raw_by_index: dict[int, list[dict]] = {}
    queries_to_fetch: list[tuple[int, str]] = []
    for index, entry in entries_to_refine:
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        # Live regression: quoting just the bare title as an exact phrase
        # (an earlier version of this query) gets swamped for a common
        # title -- "Here We Go Again" is also a Demi Lovato song/album, a
        # movie, a TV series, etc., none of which are this book. Adding the
        # series name and author as unquoted extra terms (soft ranking
        # signals, not exact-phrase requirements) reliably surfaced the
        # actual author's release-announcement blog post instead.
        query = " ".join(part for part in (title, series_name, author, "release date") if part)
        candidate_queries[index] = query

        cache_hit = cache.get_provider_fetch("web_search", query) if cache is not None else CACHE_MISS
        if cache_hit is not CACHE_MISS:
            raw_by_index[index] = cache_hit
        else:
            queries_to_fetch.append((index, query))

    if queries_to_fetch:
        max_workers = min(len(queries_to_fetch), WEB_SEARCH_MAX_PARALLEL_WORKERS)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_index = {
                executor.submit(_fetch_serper_web_search, query, telemetry=telemetry): index
                for index, query in queries_to_fetch
            }
            for future, index in future_to_index.items():
                try:
                    raw = future.result()
                except Exception:
                    raw = []
                raw_by_index[index] = raw
                if cache is not None:
                    cache.set_provider_fetch("web_search", candidate_queries[index], raw)

    for index, entry in entries_to_refine:
        raw = raw_by_index.get(index, [])
        per_candidate_urls[index] = {item["url"] for item in raw}
        for item in raw:
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            all_raw.append(item)

    if not all_raw:
        return refined

    try:
        verdict_by_url = _structure_with_verdict_cache(
            all_raw, series_name, author, telemetry=telemetry, cache=cache, scope_type=scope_type
        )
    except Exception:
        return refined

    for index, entry in entries_to_refine:
        candidate_key = core_title_key(str(entry.get("title") or "")) or normalize_text(str(entry.get("title") or ""))
        if not candidate_key:
            continue
        for url in per_candidate_urls.get(index, ()):
            item = verdict_by_url.get(url)
            if not item:
                continue
            item_key = core_title_key(str(item.get("title") or "")) or normalize_text(str(item.get("title") or ""))
            if item_key != candidate_key:
                continue
            published_date = str(item.get("published_date") or "").strip()
            if not published_date:
                continue
            refined_entry = dict(entry)
            refined_entry["published_date"] = published_date
            refined_entry["upcoming_hint"] = bool(item.get("is_upcoming"))
            refined[index] = refined_entry
            break

    return refined


def _structure_with_verdict_cache(
    raw_results: list[dict],
    series_name: str | None,
    author: str,
    *,
    diagnostics: list[dict] | None = None,
    telemetry: "DiscoveryTelemetry | None" = None,
    cache: "DiscoveryCache | None" = None,
    scope_type: str = "series",
    bypass_cached_rejection: bool = False,
) -> dict[str, dict]:
    """Layer B LLM-verdict cache (see services/discovery_cache.py): splits
    raw_results into what's already been sent to the LLM this job
    (cached_by_url, accepted-or-None-for-rejected) vs. what still needs a
    fresh call (uncached_raw). result_index from the LLM is resolved against
    uncached_raw -- the exact subset actually sent -- then immediately
    converted to a URL-keyed map so nothing downstream relies on positional
    indices into raw_results at all. Returns only *accepted* verdicts, keyed
    by URL -- a rejected/no-verdict URL is simply absent from the result.

    `bypass_cached_rejection` (used by the missing-volume interior-gap pass,
    see _fetch_web_search): when True, a cached *rejection* is treated as a
    miss and re-sent to the LLM, while a cached *acceptance* is still
    trusted. See discovery_catchup_architecture_spec.md's recall-gap
    diagnostic for why: the broad targeted/lookahead pass's large batch can
    wrongly reject a book-number-bearing URL it would correctly accept in
    isolation, and that wrong rejection getting cached then silently
    poisons a later, more focused pass's dedicated retry of the same URL.

    Shared by _fetch_web_search (targeted/lookahead/missing-volume passes)
    and _refine_undated_web_search_results_batch (date-refinement queries)
    so both get identical caching semantics instead of refinement bypassing
    the cache entirely.
    """
    if not raw_results:
        return {}

    # FIX-LB-KEY: this cache key used to be built with _normalize_query_text
    # (the same normalizer Layer A's provider-fetch cache uses for raw query
    # text), which doesn't match the normalizer used everywhere else a
    # series' identity is compared/deduped (services/identity.py's
    # _normalize_series_name_for_identity -- e.g. it strips a trailing
    # "series"/"book series" suffix, which _normalize_query_text does not).
    # Two spellings of the same series that only differ in a way
    # _normalize_series_name_for_identity treats as identical but
    # _normalize_query_text does not would silently miss each other's
    # cached verdicts within the same job. Low-risk to change:
    # DiscoveryCache is created fresh per Check Now job and discarded at job
    # end (see services/discovery_cache.py's own docstring), so this only
    # affects within-job cache hit/miss consistency, not any persisted key.
    series_name_key = _normalize_series_name_for_identity(series_name) if series_name else ""
    cached_by_url: dict[str, dict | None] = {}
    uncached_raw: list[dict] = []
    for item in raw_results:
        if cache is None:
            uncached_raw.append(item)
            continue
        verdict = cache.get_llm_verdict(scope_type, series_name_key, item["url"])
        if verdict is CACHE_MISS or (bypass_cached_rejection and verdict is None):
            uncached_raw.append(item)
        else:
            cached_by_url[item["url"]] = verdict

    fresh_structured = (
        _structure_web_results_with_llm(series_name, author, uncached_raw, diagnostics=diagnostics, telemetry=telemetry)
        if uncached_raw
        else []
    )

    fresh_by_url: dict[str, dict] = {}
    accepted_urls: set[str] = set()
    for item in fresh_structured:
        if not isinstance(item, dict):
            continue
        try:
            source = uncached_raw[int(item.get("result_index"))]
        except (TypeError, ValueError, IndexError):
            continue
        fresh_by_url[source["url"]] = item
        accepted_urls.add(source["url"])
        if cache is not None:
            cache.set_llm_verdict(scope_type, series_name_key, source["url"], item)

    if cache is not None:
        for item in uncached_raw:
            if item["url"] not in accepted_urls:
                # Negative sentinel: this URL was checked and excluded --
                # never re-sent to the LLM again within this job (unless a
                # later bypass_cached_rejection=True caller overrides it).
                cache.set_llm_verdict(scope_type, series_name_key, item["url"], None)

    return {**{u: v for u, v in cached_by_url.items() if v is not None}, **fresh_by_url}


# Domains checked to recognize a structured web-search result as an Amazon
# product page -- see _fetch_apify_discovery. Kept intentionally small/
# literal (substring match, not a full URL parser) since these are already-
# fetched, already-LLM-accepted source_url values, not untrusted input.
_AMAZON_URL_DOMAINS = ("amazon.com", "amazon.co.uk", "amazon.ca", "amazon.de", "amazon.co.jp", "amazon.in")


def _fetch_apify_discovery(
    query: str,
    structured_web_results: list[dict],
    budget: "ApifyCallBudget | None",
) -> list[dict]:
    """Sequential Apify sub-flow attached to the Serper+Anthropic web-search
    pass (see _fetch_web_search) -- NOT one of the parallel providers in
    _fetch_all_providers_parallel. Phase 1 scope (Check Now only, per the
    Apify integration design chat's consensus):

    - If the already-LLM-structured web-search results include an Amazon
      product URL, run Apify's single actor directly on that URL -- top 1
      only (see the module docstring on symmetric fan-out caps).
    - Otherwise, run the same actor on a search-results URL built from
      `query` instead -- see apify_provider.py's module docstring for why
      one actor call now covers both cases (single-actor design, replacing
      an earlier two-actor search-then-product design).

    Returns [] with no error whenever Apify isn't configured/budget is
    exhausted/the actor call fails -- see fetch_apify_candidates.
    """
    if budget is None or not apify_enabled():
        return []

    amazon_urls = [
        str(result["source_url"]).strip()
        for result in structured_web_results
        if result.get("source_url")
        and any(domain in str(result["source_url"]).lower() for domain in _AMAZON_URL_DOMAINS)
    ]
    # Top 1 only, even when multiple Amazon URLs are already present in the
    # structured web-search results -- worst-case Apify usage per pass
    # stays at exactly one actor call either way (direct-URL lookup or
    # built search-results URL), per apify_provider.py's single-actor
    # design.
    top_amazon_urls = amazon_urls[:1]

    try:
        return fetch_apify_candidates(query, top_amazon_urls or None, budget)
    except Exception as exc:  # Apify failures must never break web-search's own results.
        _log(f"apify sub-flow failed: {exc}")
        return []


def _promote_web_search_health_diagnostics(
    diagnostics_list: list[dict],
    provider_failures_list: list[dict],
    pass_label: str,
    provider_name: str,
) -> None:
    """Promotes _fetch_web_search's "web_search_provider_unhealthy" markers
    (recorded when Serper's HTTP layer failed or returned zero raw hits,
    even though Apify's fallback may have let the pass still return
    candidates -- see _fetch_web_search) into a real provider_failures
    entry, called right after each pass's fetch in
    discover_candidates_for_series.

    This is the one place Serper's health becomes visible again: once
    _fetch_web_search stops raising in this scenario (the whole point of
    the fallback), _fetch_all_providers_parallel's own
    `except Exception as exc: failures[provider] = exc` bookkeeping never
    fires, so nothing would otherwise land in provider_failures even
    though Serper genuinely failed. provider_failures is what actually
    surfaces in the Check Now debug summary (see
    services/discovery_logging.py) and the API response, so recording the
    marker via a plain _log() line alone would not be enough.

    Removes the promoted markers from diagnostics_list in place -- they're
    a provider-health signal, not an actual dropped candidate, so they
    must not also surface as a fake entry in compute_drop_explanations's
    per-candidate drop list.
    """
    remaining: list[dict] = []
    for entry in diagnostics_list:
        if entry.get("type") == "web_search_provider_unhealthy" and entry.get("pass_label") == pass_label:
            provider_failures_list.append(
                {
                    "provider": provider_name,
                    "error": entry.get("error"),
                    "apify_fallback_used": True,
                }
            )
        else:
            remaining.append(entry)
    diagnostics_list[:] = remaining


def _fetch_raw_web_search_hits(
    queries: list[str],
    *,
    telemetry: "DiscoveryTelemetry | None",
    cache: "DiscoveryCache | None",
) -> tuple[list[dict], list[Exception]]:
    """RT-2 sub-step of _fetch_web_search: resolve every query to its raw
    (unstructured) search hits, deduped by URL across queries.

    Cache lookups are cheap and local -- resolved synchronously first so
    only genuine cache misses ever reach the network. Only those misses are
    fired concurrently, bounded by a small fixed worker count (not one
    thread per query): an unbounded pool would fire every lookahead query
    (up to WEB_SEARCH_LOOKAHEAD_BOOKS + 1 of them) in the same instant,
    risking provider rate-limiting that never happened when these were
    sequential.

    Returns (raw_results, query_errors): raw_results are reassembled in
    original query order (not completion order) so URL dedup's "first query
    wins" behavior is unaffected by which concurrent fetch happened to
    finish first; query_errors collects one entry per query whose fetch
    raised, for the caller's empty-result diagnostics.
    """
    items_by_position: dict[int, list[dict]] = {}
    queries_to_fetch: list[tuple[int, str]] = []
    query_errors: list[Exception] = []
    for position, query in enumerate(queries):
        cache_hit = cache.get_provider_fetch("web_search", query) if cache is not None else CACHE_MISS
        if cache_hit is not CACHE_MISS:
            items_by_position[position] = cache_hit
        else:
            queries_to_fetch.append((position, query))

    if queries_to_fetch:
        max_workers = min(len(queries_to_fetch), WEB_SEARCH_MAX_PARALLEL_WORKERS)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_position = {
                executor.submit(_fetch_serper_web_search, query, telemetry=telemetry): position
                for position, query in queries_to_fetch
            }
            for future, position in future_to_position.items():
                try:
                    items = future.result()
                except Exception as exc:  # one query's transient failure shouldn't sink the others
                    query_errors.append(exc)
                    continue
                items_by_position[position] = items
                if cache is not None:
                    cache.set_provider_fetch("web_search", queries[position], items)

    raw_results: list[dict] = []
    seen_urls: set[str] = set()
    for position in range(len(queries)):
        for item in items_by_position.get(position, []):
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            raw_results.append(item)

    return raw_results, query_errors


def _web_search_empty_result_fallback(
    queries: list[str],
    query_errors: list[Exception],
    *,
    diagnostics: list[dict] | None,
    pass_label: str,
    apify_budget: "ApifyCallBudget | None",
) -> list[dict]:
    """RT-2 sub-step of _fetch_web_search: what happens when every query in
    a web-search pass came back with zero raw hits (all queries errored,
    e.g. a Serper 403, or all queries succeeded but found zero organic
    hits).

    Previously this either re-raised (the all-errored case) or returned []
    silently (the all-empty case) -- in both cases execution never reached
    the Apify sub-flow at the bottom of _fetch_web_search (a raise skips the
    rest of the function entirely; so does an early return), so a Serper
    outage/empty-result pass could never be substituted by Apify even
    though Apify was independently configured and reachable. Per the Apify
    integration design chat's consensus, both cases now instead fall
    through to the Apify fallback below -- recorded into `diagnostics`
    rather than raised, so discover_candidates_for_series can still surface
    Serper's unhealthy status in provider_failures even when Apify
    successfully substitutes (see the promotion logic there).

    Deliberately NOT extended to "raw hits came back but the LLM structured
    zero real candidates out of them" -- that's a softer, different failure
    mode (the LLM deciding nothing here is a real book), where an
    independent Amazon search is more likely to add noise than signal; that
    case is handled separately, unchanged, by the caller returning []
    itself once `results` ends up empty after LLM structuring.
    """
    error_text = (
        str(query_errors[0]) if query_errors and len(query_errors) == len(queries) else "no results"
    )
    if diagnostics is not None:
        diagnostics.append(
            {
                "type": "web_search_provider_unhealthy",
                "pass_label": pass_label,
                "error": error_text,
            }
        )
    return _fetch_apify_discovery(queries[0] if queries else "", [], apify_budget)


def _structure_and_pair_web_search_hits(
    raw_results: list[dict],
    series_name: str | None,
    author: str,
    *,
    diagnostics: list[dict] | None,
    telemetry: "DiscoveryTelemetry | None",
    cache: "DiscoveryCache | None",
    scope_type: str,
    pass_label: str,
) -> list[tuple[dict, dict]]:
    """RT-2 sub-step of _fetch_web_search: run raw hits through the
    LLM-structuring verdict cache and pair each accepted item back up with
    its source raw-hit dict.

    See _structure_with_verdict_cache's own docstring for the Layer B
    cache-splicing mechanics. bypass_cached_rejection is scoped to the
    missing-volume interior-gap pass -- see that function's docstring for
    the recall-gap rationale.

    Reassembled in raw_results' original order (cached-accepted + fresh-
    accepted), never "fresh then cached" -- keeps _first_present_field-
    style precedence logic identical whether a given item came from cache
    or a fresh LLM call this round.
    """
    verdict_by_url = _structure_with_verdict_cache(
        raw_results,
        series_name,
        author,
        diagnostics=diagnostics,
        telemetry=telemetry,
        cache=cache,
        scope_type=scope_type,
        bypass_cached_rejection=(pass_label == "missing_volume"),
    )
    return [(verdict_by_url[source["url"]], source) for source in raw_results if source["url"] in verdict_by_url]


def _parse_web_search_structured_items(
    structured_with_source: list[tuple[dict, dict]],
    author: str,
    *,
    source_label: str = "web_search",
) -> list[dict]:
    """RT-2 sub-step of _fetch_web_search: turn LLM-structured (item,
    source) pairs into the provider's normal candidate-dict shape.

    source_label defaults to "web_search" for every normal caller (Serper
    snippet pipeline), but fetch_canonical_page_candidates overrides it to
    "canonical_page" (Guided Discovery, 2026-09-03 Option A confidence
    fix) so candidates extracted from a user-designated canonical URL are
    distinguishable downstream in confidence_engine/deterministic_fusion
    from candidates extracted from an arbitrary keyword-search snippet --
    see confidence_engine._PROVIDER_CONFIDENCE's "canonical_page" entry
    for why that distinction matters (it's the only lever separating the
    two once fed into _overall_confidence)."""
    results: list[dict] = []
    for item, source in structured_with_source:
        title = str(item.get("title") or "").strip()
        if not title:
            continue

        book_number = item.get("book_number")
        try:
            # CR-3: float, not int -- a fractional position (0.5/0.7-style
            # companion/novella entries) from LLM-structured web results
            # used to get silently truncated to an int (or to None, if
            # truncation produced 0 where the caller only checked truthiness
            # elsewhere), while Hardcover's own hint keeps the float (see
            # _fetch_hardcover's series_position above for the identical
            # rationale) -- an asymmetric loss of legitimate fractional
            # entries depending on which provider happened to surface them.
            book_number = float(book_number) if book_number is not None else None
        except (TypeError, ValueError):
            book_number = None

        series_name_hint = str(item.get("series_name") or "").strip() or None
        # Belt-and-suspenders on top of the prompt instruction above: some
        # series (progression-fantasy/LitRPG especially) title every entry
        # with nothing but "<Series Name> <N>" -- no distinct subtitle at
        # all. If the LLM still stripped that down to just the bare series
        # name (title now carries no number of its own -- see
        # infer_number_from_title), the resulting title collapses onto the
        # SAME core_title_key as every other bare-series-titled candidate
        # (most commonly book 1, which very often really is just titled
        # the bare series name with no number). That broke two different
        # ways in production ("Defiance of the Fall 17" investigation,
        # 2026-08-30/31): fusion couldn't recognize this as the same book
        # as a same-numbered candidate from another provider (no shared
        # title_key to merge on), leaving two title-distinguishable-looking
        # candidates that share a number and no ISBN -- which
        # delta_engine's duplicate_number check then flags as a real
        # conflict instead of a missed merge, tanking confidence and
        # dropping it outright; and/or persistence's own bare-title
        # fallback matched it straight onto owned book 1 instead of ever
        # inserting the real new book. Restoring the number to the title
        # text fixes both by letting core_title_key fold it back in.
        # Excluded for book_number == 1: that's exactly the case where a
        # bare "<Series Name>" title (no number) is legitimately correct
        # (book 1 is the eponymous volume), so reconstructing a "<Series
        # Name> 1" title there would be actively wrong, not a fix.
        if (
            book_number is not None
            and book_number != 1
            and series_name_hint
            and infer_number_from_title(title, series_name_hint) is None
            and core_title_key(title) == core_title_key(series_name_hint)
        ):
            number_text = int(book_number) if book_number == int(book_number) else book_number
            title = f"{series_name_hint} {number_text}"

        author_names = item.get("author_names")
        if not isinstance(author_names, list) or not author_names:
            author_names = [author]

        published_date = str(item.get("published_date") or "").strip()
        # Belt-and-suspenders on top of the prompt's own instruction: a
        # retailer/store listing existing is not proof a book is actually
        # out yet (pre-orders look identical), so if the model didn't
        # extract an explicit date, treat it as not-yet-confirmed-available
        # regardless of what it set "is_upcoming" to -- safer to under- than
        # over-claim availability here.
        upcoming_hint = bool(item.get("is_upcoming")) or not published_date

        results.append(
            {
                "source": source_label,
                "source_id": source["url"],
                "title": title,
                "authors": [str(a) for a in author_names if str(a).strip()],
                "published_date": published_date,
                "description": source.get("description"),
                "isbn13": str(item.get("isbn13") or "").strip() or None,
                "source_url": source["url"],
                "language": "",
                "series_number_hint": book_number,
                "upcoming_hint": upcoming_hint,
                # The LLM's own guess at which series (if any) this result
                # belongs to -- used as a per-candidate series-name signal by
                # discover_candidates_for_author (see series_name_hint on the
                # Hardcover provider for the same purpose).
                "series_name_hint": series_name_hint,
            }
        )
    return results


def _refine_web_search_result_dates(
    results: list[dict],
    series_name: str | None,
    author: str,
    *,
    telemetry: "DiscoveryTelemetry | None",
    cache: "DiscoveryCache | None",
    scope_type: str,
) -> list[dict]:
    """RT-2 sub-step of _fetch_web_search: run a bounded second targeted
    query for any still-undated entries, splicing refined entries back into
    `results` in place (and returning it) exactly as the inline code used
    to."""
    entries_to_refine: list[tuple[int, dict]] = []
    for index, entry in enumerate(results):
        if entry.get("published_date"):
            continue
        if len(entries_to_refine) >= WEB_SEARCH_DATE_REFINEMENT_MAX:
            break
        entries_to_refine.append((index, entry))

    refined_by_index = _refine_undated_web_search_results_batch(
        entries_to_refine, series_name, author, telemetry=telemetry, cache=cache, scope_type=scope_type
    )
    for index, refined_entry in refined_by_index.items():
        results[index] = refined_entry
    return results


def _prepend_apify_candidates_to_web_results(
    queries: list[str], results: list[dict], apify_budget: "ApifyCallBudget | None"
) -> list[dict]:
    """RT-2 sub-step of _fetch_web_search: the Apify sub-flow, sequential
    and attached to this web-search pass rather than run as its own
    parallel provider (see _fetch_apify_discovery).

    Prepended -- not appended -- to `results` so Apify candidates sort
    ahead of web_search's own LLM-parsed-snippet candidates inside
    _fuse_and_score_candidates' ordered_raw list, which picks members[0] as
    a duplicate group's primary/backfill-source record by position, not by
    _PROVIDER_CONFIDENCE_WEIGHT -- Apify's structured Amazon-page extraction
    is trusted over an LLM's guess at unstructured search-snippet text, and
    that trust only takes effect here if it appears first.

    The caller only invokes this when `results` is non-empty -- raw hits DID
    come back (otherwise the empty-result fallback would have fired
    instead), but skipping it entirely when the LLM structured zero real
    candidates out of them. That's the LLM legitimately deciding nothing
    here is a real book, a softer/different failure mode than the raw fetch
    itself producing nothing -- an independent Apify search on the same
    query is more likely to introduce noise than signal here, so this
    deliberately does NOT fall back the way an empty/failed raw fetch does
    (see the Apify integration design chat's consensus).
    """
    apify_candidates = _fetch_apify_discovery(queries[0] if queries else "", results, apify_budget)
    return apify_candidates + results


def _fetch_web_search(
    queries: list[str],
    series_name: str | None,
    author: str,
    *,
    diagnostics: list[dict] | None = None,
    telemetry: "DiscoveryTelemetry | None" = None,
    cache: "DiscoveryCache | None" = None,
    scope_type: str = "series",
    pass_label: str = "web_search",
    apify_budget: "ApifyCallBudget | None" = None,
) -> list[dict]:
    # HTA Orchestrator Step 3: tier="A" -- structuring's static tier binding.
    # This scope also covers the raw Serper fetch above the structuring call
    # (record_web_search_call doesn't read tier, so that's harmless); the
    # only record_llm_call() made inside this scope is the structuring pass'.
    with maybe_pass_scope(telemetry, pass_label, tier="A"):
        raw_results, query_errors = _fetch_raw_web_search_hits(queries, telemetry=telemetry, cache=cache)

        if not raw_results:
            return _web_search_empty_result_fallback(
                queries, query_errors, diagnostics=diagnostics, pass_label=pass_label, apify_budget=apify_budget
            )

        structured_with_source = _structure_and_pair_web_search_hits(
            raw_results,
            series_name,
            author,
            diagnostics=diagnostics,
            telemetry=telemetry,
            cache=cache,
            scope_type=scope_type,
            pass_label=pass_label,
        )

    results = _parse_web_search_structured_items(structured_with_source, author)

    # Same tier="A" binding -- date refinement is still a structuring call
    # (_refine_undated_web_search_results_batch -> _structure_with_verdict_
    # cache -> _structure_web_results_with_llm), just a separate pass_scope()
    # invocation/correlation_id from the one above.
    with maybe_pass_scope(telemetry, f"{pass_label}_refinement", tier="A"):
        results = _refine_web_search_result_dates(
            results, series_name, author, telemetry=telemetry, cache=cache, scope_type=scope_type
        )

    # Skipped entirely when `results` is empty -- see
    # _prepend_apify_candidates_to_web_results's docstring for why.
    if not results:
        return results
    return _prepend_apify_candidates_to_web_results(queries, results, apify_budget)




def _fetch_all_providers_parallel(
    query_author: str,
    series_name: str | None,
    targeted_query_text: str,
    highest_owned_book_number: int | None,
    *,
    author: str,
    openlibrary_query: str | None = None,
    web_search_queries: list[str] | None = None,
    enable_web_search: bool = True,
    diagnostics: list[dict] | None = None,
    telemetry: "DiscoveryTelemetry | None" = None,
    cache: "DiscoveryCache | None" = None,
    pass_label: str = "targeted",
    apify_budget: "ApifyCallBudget | None" = None,
) -> dict:
    """Fetch Google Books, OpenLibrary, Hardcover, and (optionally) the
    web-search+LLM provider concurrently instead of one after another.

    This only changes *how* the four provider calls are issued (threaded
    instead of sequential) -- it does not change what gets sent to each
    provider, nor how each provider's own errors/timeouts are handled.
    Every _fetch_* function keeps its own REQUEST_TIMEOUT_SECONDS/
    WEB_SEARCH_TIMEOUT_SECONDS and lets its own exceptions propagate exactly
    as before; this function just catches whichever one propagates from
    each thread so a single slow/failing provider can't block -- or crash --
    the others, mirroring the previous per-provider try/except blocks.

    Default query construction below reproduces
    discover_candidates_for_series's targeted (primary) pass exactly:
    Google gets `"<series>" inauthor:"<author>"` (or plain `inauthor:` with
    no series name), OpenLibrary/Hardcover both get the bare
    `targeted_query_text`, and the web-search query list is the
    lookahead-aware "<series> book <N>" set gated on highest_owned_book_number.
    Callers whose query shape genuinely differs pass openlibrary_query/
    web_search_queries explicitly to reproduce their own existing query
    text unchanged: discover_candidates_for_author's plain author-wide
    sweep uses OpenLibrary's `author:"<name>"` field query plus its own
    "<author> new books" web-search query, while the series-scoped
    author-fallback pass (_fetch_fallback_series_providers) stays
    series-scoped even here -- its OpenLibrary query is `"<series>"
    "<author>"` (quoted free-text, not the `author:` field form) and it
    also passes enable_web_search=False by default since it never queries
    web search unless a caller opts in.

    Returns {"google": [...], "openlibrary": [...], "hardcover": [...],
    "web": [...]} exactly like the four raw lists the sequential calls used
    to produce, plus "_failures": {provider_key: exception} for any provider
    whose call failed -- callers use that to build the same
    provider_failures/any_provider_succeeded bookkeeping they always have,
    unchanged.

    PP-2/PP-3: each provider is now called through a `provider_protocol.py`
    adapter (`GoogleBooksProvider`/`OpenLibraryProvider`/`HardcoverProvider`/
    `WebDiscoveryProvider`), so none of the four `_fetch_*` calls below can
    raise out of this function any more -- every adapter's `.fetch()` always
    returns a `ProviderFetchResult(items, ok, error)`; failure is signaled by
    `ok=False`, not by a propagated exception. This function still builds
    the same `results`/`failures` dicts as before (`results[provider]` stays
    a plain `list[dict]` via `.to_legacy_dict()`, `failures[provider]` stays
    an object `str()`-able into the same message), so every existing
    downstream consumer (`_fuse_and_score_candidates`,
    `discover_candidates_for_series`'s provider_failures bookkeeping) is
    unaffected by this internal change. The outer `try/except` around
    `future.result()` is kept anyway as a belt-and-suspenders guard against a
    bug *inside* an adapter itself, not because a provider is expected to
    raise through it.
    """
    # query_series_name is series_name with any trailing LitRPG-style
    # genre-marketing subtitle stripped -- used ONLY for the two outgoing
    # query strings built directly from series_name below (google_query,
    # the missing-volume lookahead queries). series_name itself is left
    # untouched everywhere else in this function (catalog-sufficiency gate,
    # fusion, WebDiscoveryProvider's LLM structuring context, logging) --
    # see normalize_series_name_for_query's own docstring for why those
    # must keep the original string.
    query_series_name = normalize_series_name_for_query(series_name)
    google_query = (
        f'"{query_series_name}" inauthor:"{query_author}"' if query_series_name else f'inauthor:"{query_author}"'
    )
    resolved_openlibrary_query = openlibrary_query if openlibrary_query is not None else targeted_query_text
    hardcover_query = targeted_query_text

    if web_search_queries is not None:
        resolved_web_queries = web_search_queries
    else:
        resolved_web_queries = [targeted_query_text] if targeted_query_text else []
        if series_name and highest_owned_book_number:
            lookahead_author = f" {query_author}" if query_author else ""
            resolved_web_queries += [
                f'"{query_series_name}"{lookahead_author} book {number}'
                for number in range(
                    highest_owned_book_number + 1, highest_owned_book_number + 1 + WEB_SEARCH_LOOKAHEAD_BOOKS
                )
            ]

    run_web_search = bool(
        enable_web_search and resolved_web_queries and _web_search_enabled() and _llm_structuring_enabled()
    )

    # Observability only -- logs the real, final query string each provider
    # is about to receive (not the caller's targeted_query_text, which
    # Google Books never actually uses). Tagged by pass_label so a single
    # discovery run that triggers multiple passes (targeted, author_
    # fallback, precheck) gets one accurate line per pass instead of one
    # potentially-misleading line per run. Purely additive: reuses the
    # already-computed query_series_name/google_query/hardcover_query/
    # resolved_openlibrary_query/resolved_web_queries values below without
    # touching identity, fusion, or sufficiency logic.
    _log(
        f"DiscoveryQuery[{pass_label}]: raw_series_name={series_name!r} "
        f"normalized_series_name={query_series_name!r} google_query={google_query!r} "
        f"hardcover_query={hardcover_query!r} openlibrary_query={resolved_openlibrary_query!r} "
        f"web_search_queries={resolved_web_queries!r}"
    )

    # Layer A provider-fetch cache (see services/discovery_cache.py):
    # Google/OpenLibrary/Hardcover's query text here doesn't depend on
    # highest_owned_book_number at all, so it's byte-identical on every
    # round of the same job -- a live measurement (discovery_catchup_
    # architecture_spec.md #6) showed this exact repeated call being paid
    # fresh on every round. A cache hit short-circuits the fetch entirely
    # (that provider is left out of `tasks` below and never submitted to
    # the executor).
    #
    # NS-2: below, this cache is keyed by the internal fetch_results
    # vocabulary ("google"/"openlibrary"/"hardcover" -- see catalog_
    # queries), while the web-search fetch functions cache under the
    # literal "web_search" (see _fetch_brave_web_search and its structured
    # variant) rather than "web". Not a collision or a bug -- web search's
    # own cache calls are self-contained, keyed by query text within their
    # own function and never cross-referenced against this dict's "web"
    # key -- but it's the same google/openlibrary/hardcover-vs-web_search/
    # web split documented on _PROVIDER_SORT_RANK in deterministic_
    # fusion.py (NS-1), so don't expect the two cache namespaces to line
    # up string-for-string with each other or with `results` below.
    catalog_queries = {
        "google": google_query,
        "openlibrary": resolved_openlibrary_query,
        "hardcover": hardcover_query,
    }
    results: dict[str, list[dict]] = {"google": [], "openlibrary": [], "hardcover": [], "web": []}
    failures: dict[str, Exception] = {}

    def _run_tasks(tasks: dict[str, tuple]) -> None:
        """Submits `tasks` to a fresh, per-call ThreadPoolExecutor and
        populates the outer `results`/`failures` dicts -- shared by both
        the catalog-only batch and the (conditional) web-search batch
        below, so the catalog-sufficiency gate in between can see completed
        catalog results before deciding whether a second batch is needed
        at all.
        """
        if not tasks:
            return
        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            future_to_provider = {
                executor.submit(func, *args, **kwargs): provider for provider, (func, args, kwargs) in tasks.items()
            }
            for future, provider in future_to_provider.items():
                try:
                    fetch_result = future.result()
                except Exception as exc:  # belt-and-suspenders only -- see docstring
                    failures[provider] = exc
                    continue
                if fetch_result.ok:
                    results[provider] = [item.to_legacy_dict() for item in fetch_result.items]
                    if cache is not None and provider in catalog_queries:
                        cache.set_provider_fetch(provider, catalog_queries[provider], results[provider])
                else:
                    failures[provider] = RuntimeError(fetch_result.error or f"{provider} provider failed")

    catalog_tasks: dict[str, tuple] = {}
    catalog_fetchers = {
        "google": GoogleBooksProvider(),
        "openlibrary": OpenLibraryProvider(),
        "hardcover": HardcoverProvider(),
    }
    for provider, query in catalog_queries.items():
        cache_hit = cache.get_provider_fetch(provider, query) if cache is not None else CACHE_MISS
        if cache_hit is not CACHE_MISS:
            results[provider] = cache_hit
        else:
            catalog_tasks[provider] = (catalog_fetchers[provider].fetch, (query,), {"telemetry": telemetry})

    # Catalog providers must be fetched (or served from cache) and awaited
    # BEFORE web search's own task is even built -- the catalog-sufficiency
    # gate right below needs their completed results to decide whether
    # web search/Apify are worth running at all. This trades away the
    # previous all-four-providers-in-one-batch latency win on the
    # "web search still needed" path (now two sequential batches instead
    # of one) in exchange for being able to skip the second batch
    # (web search, and by extension its own Apify sub-flow) entirely on
    # the "catalogs alone are already sufficient" path.
    _run_tasks(catalog_tasks)

    # PB-10 diagnostic pass (Percy Jackson re-run investigation): the gate
    # below is only ever *reached* when `run_web_search` is already True
    # here -- i.e. this pass would have run web search anyway (enough
    # queries, Serper+Anthropic both configured, enable_web_search=True for
    # this pass). Most _fetch_all_providers_parallel callers never reach
    # this line at all: precheck passes `enable_web_search=False` outright,
    # and the author-fallback pass defaults `enable_fallback_web_search` to
    # False in the real Check Now flow -- for both, `run_web_search` is
    # already False before this point, so nothing about the gate is even
    # applicable and nothing is logged here (there was never going to be a
    # web-search call for the gate to skip). Logged verbosely and
    # unconditionally (not just on the "sufficient" branch, unlike the
    # original version of this gate) precisely because the last diagnosis
    # of this gate had no visibility into *why* it did or didn't fire.
    if run_web_search:
        if _catalog_sufficiency_gate_enabled():
            contributing_provider_count = sum(
                1
                for provider in ("google", "openlibrary", "hardcover")
                if provider not in failures and results.get(provider)
            )
            _log(
                f"Catalog-sufficiency gate [{pass_label}] BEFORE: series={series_name!r} author={author!r} "
                f"google_hits={len(results.get('google') or [])} "
                f"openlibrary_hits={len(results.get('openlibrary') or [])} "
                f"hardcover_hits={len(results.get('hardcover') or [])} "
                f"contributing_providers={contributing_provider_count}"
            )
            for provider_key in ("hardcover", "google", "openlibrary"):
                for raw in results.get(provider_key) or []:
                    _log(
                        f"  [{pass_label}] catalog input ({provider_key}): "
                        f"title={raw.get('title')!r} authors={raw.get('authors')!r} "
                        f"number={raw.get('series_number_hint')!r} isbn13={raw.get('isbn13')!r}"
                    )

            fused_catalog_candidates = _fuse_and_score_candidates(
                {
                    "google": results["google"],
                    "openlibrary": results["openlibrary"],
                    "hardcover": results["hardcover"],
                    "web": [],
                },
                author,
                series_name,
            )
            sufficient = catalog_providers_are_sufficient(
                fused_catalog_candidates,
                series_name,
                highest_owned_book_number,
                contributing_provider_count=contributing_provider_count,
                pass_label=pass_label,
            )
            _log(
                f"Catalog-sufficiency gate [{pass_label}] AFTER: "
                f"sufficient={sufficient} -> {'SKIP' if sufficient else 'RUN'} web search "
                f"(highest_owned_book_number={highest_owned_book_number})"
            )
            outcome = "PASSED" if sufficient else "FAILED"
            if sufficient:
                run_web_search = False
            if telemetry is not None:
                telemetry.record_gate_outcome("catalog_sufficiency", outcome)
        else:
            _log(f"Catalog-sufficiency gate [{pass_label}]: DISABLED via CATALOG_SUFFICIENCY_GATE_ENABLED -- running web search unconditionally")
            if telemetry is not None:
                telemetry.record_gate_outcome("catalog_sufficiency", "SKIPPED")

    if run_web_search:
        _run_tasks(
            {
                "web": (
                    WebDiscoveryProvider().fetch,
                    (resolved_web_queries, series_name, author),
                    {
                        "diagnostics": diagnostics,
                        "telemetry": telemetry,
                        "cache": cache,
                        "pass_label": pass_label,
                        "apify_budget": apify_budget,
                    },
                )
            }
        )

    results["_failures"] = failures
    return results




# Thresholds gating _reconcile_candidates_with_llm -- deliberately
# conservative so the (comparatively expensive, latency-adding) LLM pass
# only runs when the cheap, deterministic fusion above actually left
# something messy behind, not on every Check Now run.
RECONCILIATION_SERIES_COMPLETENESS_THRESHOLD = 0.8
RECONCILIATION_METADATA_COMPLETENESS_THRESHOLD = 0.5
RECONCILIATION_DISAGREEMENT_RATIO_THRESHOLD = 0.2

# Bounds how many candidates get sent into one reconciliation prompt --
# keeps the call's cost/latency bounded for an unusually noisy fetch.
# Anything beyond this is passed through untouched rather than dropped.
RECONCILIATION_MAX_CANDIDATES = 40


def _candidate_has_provenance_disagreement(candidate: "UnifiedCandidate") -> bool:
    """True if the raw members _fuse_and_score_candidates grouped into this
    one candidate don't actually agree with each other on book number,
    ISBN, or series name -- i.e. fusion picked *a* value (the first
    non-null one it found), but the providers disagreed about what that
    value should be. That's exactly the kind of conflict a deterministic
    "first non-null wins" backfill can't adjudicate well, and is one of the
    signals _needs_llm_reconciliation uses to decide the fused set is worth
    a second, LLM-driven look.
    """
    if len(candidate.source_provenance) < 2:
        return False
    numbers = {
        round(float(member["series_number_hint"]), 2)
        for member in candidate.source_provenance
        if member.get("series_number_hint") is not None
    }
    if len(numbers) > 1:
        return True
    isbns = {
        str(member["isbn13"]).strip() for member in candidate.source_provenance if str(member.get("isbn13") or "").strip()
    }
    if len(isbns) > 1:
        return True
    # Providers disagreeing on which series a book belongs to (e.g. one
    # source tagging a candidate under a different Wagner thriller series
    # than another) is just as real a fusion conflict as disagreeing on
    # number/ISBN, and the same "first non-null wins" backfill can't
    # adjudicate it either. Compared via _series_names_compatible rather
    # than strict equality, so two members merely branding the SAME series
    # differently (e.g. "Jonathan Hunt" vs. "Jonathan Hunt Thriller
    # Series") don't register as a disagreement -- only genuinely
    # different series names do.
    series_names = [
        str(member.get("series_name_hint") or "")
        for member in candidate.source_provenance
        if member.get("series_name_hint")
    ]
    return any(
        not _series_names_compatible(a, b)
        for i, a in enumerate(series_names)
        for b in series_names[i + 1 :]
    )


def _needs_llm_reconciliation(unified_candidates: list["UnifiedCandidate"], series_name: str | None) -> bool:
    """Gates _reconcile_candidates_with_llm. Only worth the extra LLM call
    when the deterministic fusion pass actually left something messy: the
    series looks incomplete (few distinct book numbers relative to the
    highest one seen), providers actively disagreed with each other on some
    candidates, or the fused metadata is thin across the board. A clean,
    complete, well-agreed-upon result skips this entirely -- fusion alone
    is enough.
    """
    if len(unified_candidates) < 2:
        return False

    numbers = sorted(
        {
            int(number)
            for candidate in unified_candidates
            for number in (_resolve_candidate_number(candidate, series_name),)
            if number is not None and float(number).is_integer()
        }
    )
    if numbers:
        expected_total = numbers[-1]
        series_completeness = len(numbers) / expected_total
        if series_completeness < RECONCILIATION_SERIES_COMPLETENESS_THRESHOLD:
            _log(f"LLM reconciliation triggered: series completeness {series_completeness:.0%} below threshold")
            return True

    disagreement_count = sum(1 for candidate in unified_candidates if _candidate_has_provenance_disagreement(candidate))
    if disagreement_count / len(unified_candidates) > RECONCILIATION_DISAGREEMENT_RATIO_THRESHOLD:
        _log(
            f"LLM reconciliation triggered: provider disagreement on "
            f"{disagreement_count}/{len(unified_candidates)} candidates"
        )
        return True

    avg_completeness = sum(candidate.metadata_completeness_score for candidate in unified_candidates) / len(
        unified_candidates
    )
    if avg_completeness < RECONCILIATION_METADATA_COMPLETENESS_THRESHOLD:
        _log(f"LLM reconciliation triggered: average metadata completeness {avg_completeness:.0%} below threshold")
        return True

    return False


def _format_candidate_for_reconciliation(index: int, candidate: "UnifiedCandidate") -> str:
    sources = list(dict.fromkeys(str(member.get("source") or "unknown") for member in candidate.source_provenance))
    return (
        f"[{index}] title={candidate.title!r} authors={candidate.authors!r} "
        f"series_name={candidate.series_name!r} "
        f"series_number={candidate.series_number} isbn13={candidate.isbn13 or 'null'} "
        f"published_date={candidate.published_date or 'null'!r} sources={sources} "
        f"metadata_completeness={candidate.metadata_completeness_score}"
    )


def _coerce_reconciled_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _coerce_reconciled_str(value) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None




def _apply_reconciliation_entry(entry: dict, members: list["UnifiedCandidate"]) -> "UnifiedCandidate":
    """Turns one entry of _reconcile_candidates_with_llm's response (plus
    the UnifiedCandidate(s) it references) into a single resolved
    UnifiedCandidate.

    A single-member entry (the model found nothing to merge it with) only
    ever *backfills* a field that candidate was missing -- it never
    overwrites a value the candidate already had, so one LLM misread can't
    quietly corrupt a candidate that didn't actually need reconciling. A
    multi-member entry is a real merge: the model's own resolved values are
    trusted first (that's the point of asking it to pick the best value
    across disagreeing sources), falling back to whichever merged member
    has a non-null value if the model left a field null.
    """
    primary = members[0]

    if len(members) == 1:
        title = primary.title
        authors = primary.authors
        series_name = primary.series_name
        series_number = primary.series_number
        isbn13 = primary.isbn13
        published_date = primary.published_date

        if series_number is None:
            series_number = _coerce_reconciled_float(entry.get("series_number"))
        if series_name is None:
            series_name = _coerce_reconciled_str(entry.get("series_name"))
        if not isbn13:
            candidate_isbn = _coerce_reconciled_str(entry.get("isbn13"))
            if candidate_isbn and len(candidate_isbn) == 13 and candidate_isbn.isdigit():
                isbn13 = candidate_isbn
        if not published_date:
            published_date = _coerce_reconciled_str(entry.get("published_date"))

        provenance = [dict(item) for item in primary.source_provenance] or [{}]
        provenance[0] = {
            **provenance[0],
            "series_number_hint": series_number if series_number is not None else provenance[0].get("series_number_hint"),
            "series_name_hint": series_name or provenance[0].get("series_name_hint"),
            "isbn13": isbn13 or provenance[0].get("isbn13"),
            "published_date": published_date or provenance[0].get("published_date"),
        }

        completeness = _reconciled_completeness_score(
            title, authors, series_name, series_number, isbn13, published_date, provenance[0].get("description")
        )

        return UnifiedCandidate(
            title=title,
            authors=authors,
            series_name=series_name,
            series_number=series_number,
            isbn13=isbn13,
            edition_type="bundle" if entry.get("is_bundle") else primary.edition_type,
            published_date=published_date,
            source_provenance=provenance,
            confidence_score=primary.confidence_score,
            metadata_completeness_score=max(primary.metadata_completeness_score, completeness),
            upcoming_hint=primary.upcoming_hint,
        )

    title = _coerce_reconciled_str(entry.get("title")) or primary.title
    author_names = entry.get("author_names")
    authors = list(author_names) if isinstance(author_names, list) and author_names else None
    if not authors:
        authors = next((member.authors for member in members if member.authors), [])
    series_name = _coerce_reconciled_str(entry.get("series_name")) or next(
        (member.series_name for member in members if member.series_name), None
    )
    series_number = _coerce_reconciled_float(entry.get("series_number"))
    if series_number is None:
        series_number = next((member.series_number for member in members if member.series_number is not None), None)
    isbn13_candidate = _coerce_reconciled_str(entry.get("isbn13"))
    if isbn13_candidate and len(isbn13_candidate) == 13 and isbn13_candidate.isdigit():
        isbn13 = isbn13_candidate
    else:
        isbn13 = next((member.isbn13 for member in members if member.isbn13), None)
    published_date = _coerce_reconciled_str(entry.get("published_date")) or next(
        (member.published_date for member in members if member.published_date), None
    )

    provenance: list[dict] = []
    for member in members:
        provenance.extend(dict(item) for item in member.source_provenance)
    if not provenance:
        provenance = [{}]
    provenance[0] = {
        **provenance[0],
        "authors": authors,
        "isbn13": isbn13,
        "published_date": published_date,
        "series_name_hint": series_name,
        "series_number_hint": series_number,
    }

    unique_sources = list(dict.fromkeys(str(item.get("source") or "unknown") for item in provenance))
    confidence_score = round(
        min(max(member.confidence_score for member in members) + (0.1 if len(unique_sources) > 1 else 0.0), 1.0), 4
    )
    completeness = _reconciled_completeness_score(
        title, authors, series_name, series_number, isbn13, published_date, provenance[0].get("description")
    )

    return UnifiedCandidate(
        title=title,
        authors=list(authors or []),
        series_name=series_name,
        series_number=series_number,
        isbn13=isbn13,
        edition_type="bundle" if entry.get("is_bundle") else "unknown",
        published_date=published_date,
        source_provenance=provenance,
        confidence_score=confidence_score,
        metadata_completeness_score=max(completeness, max(member.metadata_completeness_score for member in members)),
        upcoming_hint=next((member.upcoming_hint for member in members if member.upcoming_hint is not None), None),
    )


def _reconcile_candidates_with_llm(
    unified_candidates: list["UnifiedCandidate"],
    series_name: str | None,
    *,
    diagnostics: list[dict] | None = None,
    telemetry: "DiscoveryTelemetry | None" = None,
) -> list["UnifiedCandidate"]:
    """Single conditional LLM pass over an already-fused candidate set for
    one series -- normalizes series names/book numbers, merges candidates
    that plain identity-key fusion couldn't recognize as the same book
    (different title formatting, one has an ISBN the other lacks, etc.),
    excludes candidates it judges to actually belong to a different series
    by the same author, flags suspected bundles, and surfaces (but does not
    itself act on -- see _reconstruct_series_skeleton for actually
    searching for them) suspected missing volume numbers. Gated by
    _needs_llm_reconciliation; only called when fusion alone left the set
    looking incomplete, internally disagreeing, or thin on metadata.

    Deliberately conservative on failure: a missing API key, empty input,
    a network/parse error, or a response that doesn't cleanly partition
    every input candidate exactly once between a resolved entry's
    source_indices and excluded_indices (no gaps, no overlaps, nothing
    claimed by both) all fall back to returning unified_candidates
    completely unchanged -- this is an enrichment step, never allowed to
    lose or corrupt a candidate fusion already produced. An excluded
    candidate is simply dropped from the returned list -- there is no
    separate "other books by this author" output for it to land in
    instead.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or len(unified_candidates) < 2:
        return unified_candidates

    # Anything beyond the cap was never sent to the model and is passed
    # through untouched below, rather than silently dropped.
    candidates = unified_candidates[:RECONCILIATION_MAX_CANDIDATES]

    candidate_listing = "\n".join(
        _format_candidate_for_reconciliation(index, candidate) for index, candidate in enumerate(candidates)
    )
    # HTA Orchestrator Step 5: Tier B prompt builder -- see prompts.py's
    # build_reconciliation_prompt docstring; zero behavior change from the
    # inline .format() call this replaces.
    prompt = build_reconciliation_prompt(
        series_name=series_name or "unknown",
        count=len(candidates),
        candidate_listing=candidate_listing,
        max_index=len(candidates) - 1,
    )

    # HTA Orchestrator Step 3: tier="B" -- reconciliation's static tier
    # binding. Behavior is unchanged (still ANTHROPIC_MODEL/Haiku); this
    # only labels telemetry so per-tier cost can eventually be measured
    # before any routing change.
    with maybe_pass_scope(telemetry, "reconciliation", tier="B"):
        started = time.monotonic()
        llm_response = None
        try:
            # HTA Orchestrator Step 7: dispatches via tier="B" now instead
            # of a positional ANTHROPIC_MODEL -- TIER_MODEL_MAP["B"] still
            # resolves to the same Haiku model, so this is a zero-
            # behavior-change migration onto tier-based dispatch.
            llm_response = call_llm(
                tier="B",
                prompt=prompt,
                max_tokens=3000,
                # Deterministic normalize/merge/flag task, not generative writing --
                # see _structure_web_results_with_llm's temperature=0 for the same
                # rationale.
                temperature=0,
                timeout=WEB_SEARCH_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # a reconciliation failure should never sink the candidates fusion already found
            _log(f"LLM reconciliation call failed: {exc}")
            return unified_candidates
        finally:
            if telemetry is not None:
                telemetry.record_llm_call(
                    duration_s=time.monotonic() - started,
                    tokens_in=llm_response.tokens_in if llm_response is not None else 0,
                    tokens_out=llm_response.tokens_out if llm_response is not None else 0,
                    model_id=ANTHROPIC_MODEL,
                )

    text = llm_response.text
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return unified_candidates

    if not isinstance(parsed, dict):
        return unified_candidates

    resolved_entries = parsed.get("resolved_candidates")
    if not isinstance(resolved_entries, list) or not resolved_entries:
        return unified_candidates

    missing_volume_suggestions = parsed.get("missing_volume_suggestions")
    if isinstance(missing_volume_suggestions, list) and missing_volume_suggestions:
        _log(f"LLM reconciliation suspects missing volume(s): {missing_volume_suggestions}")

    # Every original index must be claimed by exactly one entry, OR listed
    # in excluded_indices, and never both -- anything else means the model
    # didn't cleanly partition the input (overlap, gap, garbage index, or
    # an index silently missing from both), and trusting a
    # partial/overlapping response risks silently dropping or duplicating a
    # candidate fusion already handled correctly. Excluding a candidate
    # (because it belongs to a different series) is the one allowed way for
    # an index to leave "resolved_candidates" -- it must still be accounted
    # for, just via excluded_indices instead of a resolved entry.
    all_indices = set(range(len(candidates)))
    seen_indices: set[int] = set()
    valid_entries: list[dict] = []
    for entry in resolved_entries:
        if not isinstance(entry, dict):
            return unified_candidates
        indices = entry.get("source_indices")
        if not isinstance(indices, list) or not indices:
            return unified_candidates
        try:
            indices = [int(index) for index in indices]
        except (TypeError, ValueError):
            return unified_candidates
        if any(index < 0 or index >= len(candidates) or index in seen_indices for index in indices):
            return unified_candidates
        seen_indices.update(indices)
        valid_entries.append({**entry, "source_indices": indices})

    raw_excluded = parsed.get("excluded_indices", [])
    if raw_excluded and not isinstance(raw_excluded, list):
        return unified_candidates
    try:
        excluded_indices = {int(index) for index in (raw_excluded or [])}
    except (TypeError, ValueError):
        return unified_candidates
    if not excluded_indices.issubset(all_indices):
        return unified_candidates
    if seen_indices & excluded_indices:
        return unified_candidates
    if excluded_indices:
        _log(f"LLM reconciliation excluded {len(excluded_indices)} candidate(s) as belonging to a different series")
        for index in excluded_indices:
            excluded_candidate = candidates[index]
            _record_drop_diagnostic(
                "llm_reconciliation",
                {
                    "title": excluded_candidate.title,
                    "isbn13": excluded_candidate.isbn13,
                    "series_number": _to_int_or_none(excluded_candidate.series_number),
                },
                "excluded_by_llm",
                diagnostics,
            )

    if seen_indices | excluded_indices != all_indices:
        return unified_candidates

    reconciled = [_apply_reconciliation_entry(entry, [candidates[i] for i in entry["source_indices"]]) for entry in valid_entries]

    return reconciled + unified_candidates[len(candidates):]


# Complements _normalize_title_for_identity (services/identity.py), which
# *strips* these same bracketed/trailing format qualifiers when comparing
# titles for identity -- this is the extractive counterpart, used only to
# rank same-book candidates by _edition_priority once _finalize_candidates
# has already decided two of them are the same underlying book. Order
# matters: the more specific "mass market paperback" is checked before the
# more general "paperback" it contains.
