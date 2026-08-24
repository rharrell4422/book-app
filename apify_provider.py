"""Apify integration -- Phase 1 (Check Now only).

Isolated in its own module (rather than folded into discovery_engine.py's
existing `_fetch_*` functions) because Apify's call shape is fundamentally
different from every other provider here: Google Books/OpenLibrary/
Hardcover are one HTTP request each, but Apify runs a hosted actor
(a scraper) per call, which is slower, costs money per run, and needs its
own budget/fan-out guardrails (see ApifyCallBudget below) that don't apply
to any other provider.

Exposes a single entry point, `fetch_apify_candidates`, called from
discovery_engine._fetch_apify_discovery as a sequential sub-flow attached
to the existing Serper+Anthropic web-search pass.

Single-actor design (as of 2026-08-24): one actor, APIFY_AMAZON_ACTOR_ID
("junglee/free-amazon-product-scraper"), handles both the "search by
free-text query" and "look up a known Amazon URL" jobs in exactly one
call, given either:
- a built Amazon search-results URL (https://www.amazon.com/s?k=<query>),
  when we don't already have an Amazon URL from Serper, or
- a direct Amazon product URL already surfaced by Serper.

This actor was verified experimentally (manual test calls against the
real actor, both a fuzzy series-name-only search query and an exact known
title) to return real, fully-structured product listings straight from a
search-results URL -- title/asin/url/author plus, notably, Amazon's own
series-position metadata (see _extract_series_hints_from_attributes) --
with zero `error`-typed dataset items in either test. That means no
separate "search actor -> take top result -> product actor" round trip
is needed at all, unlike the two-actor design this replaced.

That earlier two-actor design (APIFY_AMAZON_SEARCH_ACTOR_ID =
"epctex/amazon-scraper" for search, APIFY_AMAZON_PRODUCT_ACTOR_ID =
"apify/amazon-scraper" for product detail) was abandoned after debugging
a "Check Now finds nothing" report traced it to two independent, unrelated
problems: "apify/amazon-scraper" turned out to be a dead actor ID (404
straight from Apify's own API -- its store page silently redirects
browsers to junglee/free-amazon-product-scraper, but that redirect
doesn't exist at the API level), and "epctex/amazon-scraper" turned out to
be a $40/month-plus-usage rental actor that had never actually been
activated on this account (Apify's generic auth-failure error for calling
an unrented rental actor is indistinguishable from a genuinely bad API
token, which is what made this so slow to pin down).

Runs through Apify's synchronous "call and wait" API
(ApifyClient.actor(...).call(...)), which blocks until the run finishes (or
run_timeout/timeout elapses) and returns the run's dataset id, which we
then read via ApifyClient.dataset(...).list_items(). There's no separate
"run-sync-get-dataset-items" call needed in the Python client -- .call()
already does the run+wait, and reading the dataset back is one more
call.

Field names in the dataset items returned by third-party Apify Store
actors aren't part of any formally versioned contract, so
_normalize_product_item deliberately checks several plausible key
spellings for each field rather than assuming one exact shape -- see its
docstring. If a field genuinely isn't present under any of the checked
names, it's mapped to None (never left as a missing key or an accidental
empty string), matching every other provider's own normalization
convention in discovery_engine.py.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import timedelta
from typing import Any
from urllib.parse import quote_plus

from dotenv import load_dotenv

# Mirrors discovery_engine.py's own load_dotenv() call -- see that module's
# comment for why this is done here too rather than relying on import order.
load_dotenv()

APIFY_AMAZON_ACTOR_ID = "junglee/free-amazon-product-scraper"

# How many Amazon listings to request per categoryUrls entry, whether it's
# a direct product URL (which realistically only ever has 1 listing) or a
# built search-results URL (which can have many). Originally 5, based on
# an initial manual test that just confirmed the actor could return
# *multiple* real listings at all. Raised to 25 after a follow-up manual
# test (2026-08-24, "Jonathan Hunt Thriller Series Georgia Wagner" --
# single primary-author query, see primary_author_name/query_author in
# discovery_engine.py) showed 5 was leaving most of an 18-book series on
# the table: Amazon's own search log for that exact query reported
# productsCount=18 (i.e. every book in the series IS reachable from one
# search), but a cap of 5 only ever samples the top 5 of those per call.
# 25 comfortably covers a search that returns exactly one hit per book
# (18) plus some slack for the same title showing up as a second, separate
# ASIN (that test also surfaced ~10 foreign-language-edition duplicates of
# already-seen titles -- harmless noise fusion/dedup downstream should
# absorb, not a reason to keep the cap artificially low). Still one call
# per run either way (see APIFY_MAX_CALLS_PER_SERIES_RUN) -- cost scales
# with items actually returned, not the cap itself, and stays negligible
# at this actor's pay-per-event pricing (~$6.20/1,000 results: 25 items
# is well under $0.20).
APIFY_MAX_ITEMS_PER_START_URL = 25

# Apify actor runs are inherently slower than a plain HTTP call to a JSON
# API (a real scrape, not just a lookup) -- 30s gives a real run a fair
# chance to finish while still fitting comfortably inside a Check Now job's
# overall SERIES_CHECK_TIMEOUT_SECONDS budget (services/series_check_engine.py).
APIFY_REQUEST_TIMEOUT_SECONDS = 30

# Worst case per series-check run: exactly one Apify actor call. The
# single-actor design's whole point is that a search-or-direct-URL lookup
# and structured product detail come back together in that one call (see
# module docstring), unlike the two-actor design this replaced, which
# needed a separate search call plus a separate product call to
# accomplish the same round trip. This constant is still shared across
# BOTH the targeted pass and the author-fallback pass of one
# discover_candidates_for_series() call (see ApifyCallBudget), preserving
# its original per-*run* (not per-pass) intent from the two-actor design:
# at most one full Apify lookup attempt total, whichever pass reaches it
# first -- the two-actor design's budget of 2 covered exactly one round
# trip (1 search call + 1 product call) for that same reason, so 1 here
# is the equivalent value for a round trip that's now a single call.
APIFY_MAX_CALLS_PER_SERIES_RUN = 1


class ApifyCallBudget:
    """Thread-safe cap on total outbound Apify actor calls for one
    discover_candidates_for_series() run.

    A single instance is constructed once per run and threaded through
    _fetch_all_providers_parallel -> _fetch_web_search ->
    _fetch_apify_discovery -> fetch_apify_candidates, including both the
    targeted pass and the (possible) author-fallback pass, so the cap
    applies to the whole run rather than resetting per pass.

    try_consume() is the only mutating method, deliberately atomic (a
    single lock-guarded check-and-increment) rather than exposing separate
    is_exhausted()/increment() methods, which would let two callers each
    check "not exhausted yet" before either one increments -- a real risk
    here since _fetch_web_search (which calls into this) runs inside a
    worker thread spawned by _fetch_all_providers_parallel's own
    ThreadPoolExecutor, concurrently with the catalog-provider fetches.
    """

    def __init__(self, max_calls: int = APIFY_MAX_CALLS_PER_SERIES_RUN) -> None:
        self._max_calls = max_calls
        self._used = 0
        self._lock = threading.Lock()

    def try_consume(self) -> bool:
        """Atomically claim one call against the budget. Returns True if
        the call is allowed to proceed (and counts it immediately, before
        the actor call is even made -- a call that later fails/times out
        still spent real Apify compute), False if the budget is already
        exhausted, in which case the caller must not make the call."""
        with self._lock:
            if self._used >= self._max_calls:
                return False
            self._used += 1
            return True


def _log(message: str) -> None:
    print(f"[apify_provider] {message}", flush=True)


def _get_api_token() -> str:
    return os.environ.get("APIFY_API_TOKEN", "").strip()


def apify_enabled() -> bool:
    """Whether an Apify token is configured at all. Phase 1 does not call
    this to gate whether Apify's sub-flow *runs* (that's still tied to
    Serper+Anthropic both being enabled -- see discovery_engine._fetch_web_search),
    only used by fetch_apify_candidates itself as its own missing-key
    no-op guard, matching _fetch_hardcover's own pattern of returning []
    immediately when its key isn't configured."""
    return bool(_get_api_token())


def _run_actor_sync(actor_id: str, run_input: dict[str, Any], budget: "ApifyCallBudget") -> list[dict] | None:
    """Runs one Apify actor to completion and returns its dataset items,
    or None if the call budget is exhausted, the actor errors, or it times
    out -- callers treat None exactly like an empty result (see
    fetch_apify_candidates), never letting an Apify-specific failure
    propagate out and break the rest of discovery, mirroring every other
    provider's own try/except-and-return-partial convention in
    discovery_engine.py.
    """
    if not budget.try_consume():
        _log(f"budget exhausted, skipping actor={actor_id}")
        return None

    try:
        from apify_client import ApifyClient
    except ImportError:
        _log("apify-client package not installed; skipping Apify call")
        return None

    token = _get_api_token()
    if not token:
        return None

    try:
        client = ApifyClient(token)
        run = client.actor(actor_id).call(
            run_input=run_input,
            run_timeout=timedelta(seconds=APIFY_REQUEST_TIMEOUT_SECONDS),
            timeout=timedelta(seconds=APIFY_REQUEST_TIMEOUT_SECONDS),
        )
        # apify-client 3.x's .call() returns a typed Run object (attribute
        # access, e.g. run.default_dataset_id), not a dict -- unlike almost
        # every other provider response in this codebase. A live regression
        # (2026-08-24) traced "Check Now finds nothing from Apify" all the
        # way through a real, successfully-billed actor run (confirmed via
        # Apify's own run logs showing real scraped products) to this one
        # line still using dict-style run.get("defaultDatasetId"), which
        # raised AttributeError on every single call, silently discarding
        # the entire run's results into this same except block below.
        dataset_id = getattr(run, "default_dataset_id", None) if run else None
        if not dataset_id:
            _log(f"actor={actor_id} run produced no dataset")
            return None
        dataset = client.dataset(dataset_id)
        items = list(dataset.list_items().items)
        _log(f"actor={actor_id} input={run_input} -> {len(items)} item(s)")
        return items
    except Exception as exc:  # Apify failures must never break the rest of discovery.
        _log(f"actor={actor_id} failed: {exc}")
        return None


def _first_of(item: dict, *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", []):
            return value
    return None


# Matches "Book 2 of 18" as a whole attribute *key* -- junglee/free-
# amazon-product-scraper (and Amazon generally) encodes series position
# this way: the total book count (18 here) varies per book and is part of
# the key itself, not a separate field, and there's no fixed key name to
# look up (unlike every other field this module reads via _first_of), so
# this needs its own regex scan -- see _extract_series_hints_from_attributes.
# Both numbers are captured: group 1 is this book's position, group 2 is
# the series-wide total, which feeds series_total_hint below (the same
# field Hardcover already populates from its own API -- see
# discovery_engine.py's series_total_hint handling) so a series' known
# length surfaces in the UI even when Hardcover has no data for it at all.
_SERIES_POSITION_ATTRIBUTE_PATTERN = re.compile(r"^Book\s+(\d+)\s+of\s+(\d+)$", re.IGNORECASE)


def _extract_from_attributes(item: dict, *want_keys: str) -> str | None:
    """Looks up a value inside this actor's `attributes` field -- a list
    of {"key": ..., "value": ...} pairs (e.g. {"key": "Publication date",
    "value": "January 1, 2026"}) -- rather than a flat top-level field,
    which is why this needs its own helper distinct from _first_of.
    Case-insensitive exact match against each candidate key in want_keys.
    """
    attributes = item.get("attributes")
    if not isinstance(attributes, list):
        return None
    want_keys_lower = {key.lower() for key in want_keys}
    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        key = attribute.get("key")
        if isinstance(key, str) and key.strip().lower() in want_keys_lower:
            value = attribute.get("value")
            if value not in (None, ""):
                return str(value).strip()
    return None


def _extract_series_hints_from_attributes(item: dict) -> tuple[str | None, str | None, int | None]:
    """Returns (series_number_hint, series_name_hint, series_total_hint)
    from this actor's `attributes` field, or (None, None, None) if this
    listing has no series-position attribute at all (standalone books, or
    series info Amazon simply didn't attach to this particular ASIN) --
    see _SERIES_POSITION_ATTRIBUTE_PATTERN for why this can't be a plain
    _extract_from_attributes lookup.
    """
    attributes = item.get("attributes")
    if not isinstance(attributes, list):
        return None, None, None
    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        key = attribute.get("key")
        if not isinstance(key, str):
            continue
        match = _SERIES_POSITION_ATTRIBUTE_PATTERN.match(key.strip())
        if match:
            value = attribute.get("value")
            series_name_hint = str(value).strip() if value else None
            return match.group(1), series_name_hint, int(match.group(2))
    return None, None, None


def _normalize_product_item(item: dict) -> dict | None:
    """Maps one Apify Amazon-product dataset item to the canonical
    snake_case provider shape shared by every discovery_engine.py provider
    (see that module's _fetch_google_books/_fetch_openlibrary/
    _fetch_hardcover for the same shape). Missing fields are always
    explicitly None, never an absent key or empty string, so downstream
    fusion/filtering code (which does `candidate.get("field")`) behaves
    identically whether a field is "not present in this Amazon listing" or
    "not returned by this particular actor version."
    """
    title = _first_of(item, "title", "name")
    if not title:
        return None

    author_value = _first_of(item, "author", "authors", "byLine", "brand")
    if isinstance(author_value, list):
        authors = [str(a).strip() for a in author_value if str(a).strip()]
    elif author_value:
        authors = [str(author_value).strip()]
    else:
        authors = []

    published_date = _first_of(item, "publicationDate", "releaseDate", "firstAvailable", "publishDate")
    if not published_date:
        published_date = _extract_from_attributes(item, "Publication date", "Release date")
    isbn13 = _first_of(item, "isbn13", "isbn", "ISBN")
    asin = _first_of(item, "asin", "ASIN")
    url = _first_of(item, "url", "link", "productUrl", "detailPageURL")
    cover_image = _first_of(item, "thumbnailImage", "image", "mainImage", "coverImage")
    if isinstance(cover_image, list):
        cover_image = cover_image[0] if cover_image else None
    if isinstance(cover_image, dict):
        cover_image = cover_image.get("url") or cover_image.get("src")

    series_number_hint, series_name_hint, series_total_hint = _extract_series_hints_from_attributes(item)

    return {
        "source": "apify",
        "source_id": str(asin).strip() if asin else (str(url).strip() if url else None),
        "title": str(title).strip(),
        "authors": authors,
        "published_date": str(published_date).strip() if published_date else None,
        "description": _first_of(item, "description", "productDescription"),
        "isbn13": str(isbn13).strip() if isbn13 else None,
        "source_url": str(url).strip() if url else None,
        "language": "",
        "series_number_hint": series_number_hint,
        "upcoming_hint": None,
        "series_name_hint": series_name_hint,
        "series_total_hint": series_total_hint,
        "asin": str(asin).strip() if asin else None,
        "cover_image": str(cover_image).strip() if cover_image else None,
        "confidence": "medium",
    }


def fetch_apify_candidates(
    query: str,
    amazon_urls: list[str] | None,
    budget: "ApifyCallBudget",
) -> list[dict]:
    """Returns Apify-sourced candidates in the standard discovery_engine.py
    provider dict shape (see _normalize_product_item).

    Single-actor design (see module docstring for how this replaced an
    earlier two-actor search-then-product design):
    - amazon_urls is non-empty: run the actor directly on the single
      highest-priority URL (top 1 -- see discovery_engine._fetch_apify_discovery
      for why only one, and why it's the caller's job, not this
      function's, to decide which URL that is).
    - amazon_urls is empty/None: build an Amazon search-results URL from
      `query` and run the actor on that instead. Verified experimentally
      to return multiple real, fully-structured listings directly from a
      search-results URL, not just a bare list of URLs needing a second
      lookup -- see module docstring.

    Either way this is exactly one Apify actor call, returning up to
    APIFY_MAX_ITEMS_PER_START_URL structured listings from that one call.

    Returns [] (never raises) if APIFY_API_TOKEN isn't configured, the
    call budget is exhausted, or the actor call fails/times out --
    exactly like every other provider's own missing-key/failure behavior
    in discovery_engine.py, so a caller can always treat this the same way
    it treats _fetch_hardcover returning [].
    """
    if not apify_enabled():
        return []

    target_url = amazon_urls[0] if amazon_urls else f"https://www.amazon.com/s?k={quote_plus(query)}"

    product_items = _run_actor_sync(
        APIFY_AMAZON_ACTOR_ID,
        {
            "categoryUrls": [{"url": target_url}],
            "maxItemsPerStartUrl": APIFY_MAX_ITEMS_PER_START_URL,
            "scrapeProductDetails": True,
        },
        budget,
    )
    if not product_items:
        return []

    candidates = []
    for raw in product_items:
        # Error items (bad URL, product not found, no results, etc. --
        # see this actor's documented error-item shape) are the actor's
        # way of reporting a failed lookup inline in the dataset rather
        # than failing the whole run; they have no title/asin/product
        # fields worth normalizing, so skip them explicitly rather than
        # relying on _normalize_product_item's missing-title check alone.
        if raw.get("error"):
            continue
        normalized = _normalize_product_item(raw)
        if normalized:
            candidates.append(normalized)
    return candidates
