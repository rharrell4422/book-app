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
to the existing Serper+Anthropic web-search pass -- see that function's
docstring for how the two actors below (search vs. product) get chosen.

Two actors are used:
- APIFY_AMAZON_SEARCH_ACTOR_ID: query -> a list of Amazon product
  URLs/ASINs (used when we don't already have an Amazon URL from Serper).
- APIFY_AMAZON_PRODUCT_ACTOR_ID: a single Amazon product URL/ASIN ->
  structured product metadata.

Both run through Apify's synchronous "call and wait" API
(ApifyClient.actor(...).call(...)), which blocks until the run finishes (or
run_timeout/timeout elapses) and returns the run's dataset id, which we
then read via ApifyClient.dataset(...).list_items(). There's no separate
"run-sync-get-dataset-items" call needed in the Python client -- .call()
already does the run+wait, and reading the dataset back is one more
call.

Field names in the dataset items returned by third-party Apify Store
actors aren't part of any formally versioned contract, so
_normalize_product_item/_normalize_search_item deliberately check several
plausible key spellings for each field rather than assuming one exact
shape -- see their docstrings. If a field genuinely isn't present under
any of the checked names, it's mapped to None (never left as a missing
key or an accidental empty string), matching every other provider's own
normalization convention in discovery_engine.py.
"""
from __future__ import annotations

import os
import threading
from datetime import timedelta
from typing import Any

from dotenv import load_dotenv

# Mirrors discovery_engine.py's own load_dotenv() call -- see that module's
# comment for why this is done here too rather than relying on import order.
load_dotenv()

APIFY_AMAZON_PRODUCT_ACTOR_ID = "apify/amazon-scraper"
APIFY_AMAZON_SEARCH_ACTOR_ID = "epctex/amazon-scraper"

# Apify actor runs are inherently slower than a plain HTTP call to a JSON
# API (a real scrape, not just a lookup) -- 30s gives real runs a fair
# chance to finish while still fitting comfortably inside a Check Now job's
# overall SERIES_CHECK_TIMEOUT_SECONDS budget (services/series_check_engine.py),
# even in the worst case of one search call + one product call per pass.
APIFY_REQUEST_TIMEOUT_SECONDS = 30

# Worst case per series-check run: 1 search-or-direct-URL call + 1
# product-extraction call (see ApifyCallBudget and
# discovery_engine._fetch_apify_discovery's top-1 fan-out cap on both the
# Apify-search-ASIN branch and the direct-Amazon-URL-from-Serper branch).
# Shared across BOTH the targeted pass and the author-fallback pass of one
# discover_candidates_for_series() call -- see that function -- so this is
# a true per-run cap, not a per-pass one.
APIFY_MAX_CALLS_PER_SERIES_RUN = 2


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
        if not run or not run.get("defaultDatasetId"):
            _log(f"actor={actor_id} run produced no dataset")
            return None
        dataset = client.dataset(run["defaultDatasetId"])
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


def _normalize_search_item(item: dict) -> dict | None:
    """Maps one Apify Amazon-search dataset item to {"asin", "url"}.

    Field names checked here (asin/ASIN, url/link/productUrl) cover the
    common spellings seen across Apify Store Amazon-search actors; unknown
    additional fields are simply ignored, not an error.
    """
    asin = _first_of(item, "asin", "ASIN")
    url = _first_of(item, "url", "link", "productUrl", "detailPageURL")
    if not asin and not url:
        return None
    return {"asin": str(asin).strip() if asin else None, "url": str(url).strip() if url else None}


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
    isbn13 = _first_of(item, "isbn13", "isbn", "ISBN")
    asin = _first_of(item, "asin", "ASIN")
    url = _first_of(item, "url", "link", "productUrl", "detailPageURL")
    cover_image = _first_of(item, "thumbnailImage", "image", "mainImage", "coverImage")
    if isinstance(cover_image, list):
        cover_image = cover_image[0] if cover_image else None
    if isinstance(cover_image, dict):
        cover_image = cover_image.get("url") or cover_image.get("src")

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
        "series_number_hint": None,
        "upcoming_hint": None,
        "series_name_hint": None,
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

    Two paths, per Diff 2/3/4's agreed design:
    - amazon_urls is non-empty: skip Apify's own search actor entirely and
      go straight to the product actor on the single highest-priority URL
      (top 1 -- see discovery_engine._fetch_apify_discovery for why only
      one, and why it's the caller's job, not this function's, to decide
      which URL that is).
    - amazon_urls is empty/None: run the search actor on `query` first,
      take its top 1 ASIN/URL result, then run the product actor on that.

    Returns [] (never raises) if APIFY_API_TOKEN isn't configured, the
    call budget is exhausted, or either actor call fails/times out --
    exactly like every other provider's own missing-key/failure behavior
    in discovery_engine.py, so a caller can always treat this the same way
    it treats _fetch_hardcover returning [].
    """
    if not apify_enabled():
        return []

    target_url: str | None = None
    if amazon_urls:
        target_url = amazon_urls[0]
    else:
        search_items = _run_actor_sync(
            APIFY_AMAZON_SEARCH_ACTOR_ID,
            {"search": query, "maxItems": 1},
            budget,
        )
        if not search_items:
            return []
        for raw in search_items:
            normalized = _normalize_search_item(raw)
            if normalized and (normalized.get("url") or normalized.get("asin")):
                target_url = normalized.get("url") or normalized.get("asin")
                break
        if not target_url:
            return []

    product_items = _run_actor_sync(
        APIFY_AMAZON_PRODUCT_ACTOR_ID,
        {"urls": [target_url]} if target_url.startswith("http") else {"asins": [target_url]},
        budget,
    )
    if not product_items:
        return []

    candidates = []
    for raw in product_items:
        normalized = _normalize_product_item(raw)
        if normalized:
            candidates.append(normalized)
    return candidates
