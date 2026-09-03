"""Apify integration -- retail-search supplemental actor (Phase 2).

Second, independent Apify actor added on top of apify_provider.py's
existing single-actor design, per the "Second Apify Actor" architecture
review (2026-09-02 chat): the primary actor
(apify_provider.APIFY_AMAZON_ACTOR_ID, a product-detail scraper) already
covers "search Amazon and/or read a known product URL," but that review
found it structurally cannot look past the highest series-book-number any
provider has already found (there is no server-side "how many books does
this series really have" signal it can act on) -- and the checked-in
architecture decisions from that review call for a genuinely different
Amazon retail-search actor to run only as a last-resort, narrow recovery
attempt for a series' confirmed interior numbering gaps, not as a
general-purpose parallel provider.

Isolated in its own module rather than folded into apify_provider.py
(explicit architecture decision) because this actor's input/output shape
is fundamentally different from the primary one: `igview-owner/amazon-
search-scraper` takes a plain free-text `query` + `maxPages` (no
`categoryUrls`/`scrapeProductDetails`), and returns flat search-result-page
fields (asin/product_title/product_url/price/rating/badges) with NO
author, series-position, ISBN, or publication-date data at all --
confirmed by a live manual validation run against the real actor
(2026-09-02) before any of this module was written, per that same review's
explicit "validate before implementing" decision. That means a hit from
this actor is a weaker signal than the primary actor's product-detail
extraction (see confidence_engine._PROVIDER_CONFIDENCE and
deterministic_fusion._PROVIDER_CONFIDENCE_WEIGHT/_PROVIDER_SORT_RANK,
which all grade "apify_retail_search" below "apify") -- its whole value is
a real ASIN + title Amazon's own relevance ranking surfaced for a query
none of the other providers could resolve, not a fully-verified match; any
missing author/series-number signal is backfilled the same way any other
under-specified provider hit already is (title-text inference,
corroboration during fusion, and normal belongs_to_series review routing
downstream -- nothing here bypasses that).

Exposes a single entry point, `fetch_retail_search_candidates`, called
from discovery_engine._attempt_retail_search_recovery -- a narrow, dedicated
call site at the tail of discovery_engine._reconstruct_series_skeleton, run
only when a series' interior numbering gaps survive the targeted pass, the
author-fallback pass, AND the existing missing-volume web-search lookahead
pass. This is a deliberately different attachment point from the primary
actor's (which is a sequential sub-flow of every web-search pass, targeted/
fallback/lookahead alike) -- see that call site's own docstring for why.

Reuses apify_provider.py's ApifyClient plumbing (_run_actor_sync,
apify_enabled, _first_of) rather than duplicating it -- both actors share
the same APIFY_API_TOKEN, the same synchronous call-and-wait shape, and
the same "never raise, treat failure like an empty result" contract, so
only the actor id, run_input shape, and item-normalization logic need to
differ here. `_run_actor_sync` is already actor-id-agnostic (takes the
actor id as a plain argument), so no changes to apify_provider.py itself
were needed to support this.

Budget: uses its OWN ApifyCallBudget instance (constructed by the caller,
e.g. agents/series_agent.py, with max_calls=
APIFY_RETAIL_SEARCH_MAX_CALLS_PER_SERIES_RUN), never the primary actor's
shared apify_provider.APIFY_MAX_CALLS_PER_SERIES_RUN=1 budget -- explicit
architecture decision, so this actor's cost is additive and independently
tunable rather than competing with (or being capped by) the primary
actor's own one-call-per-run guarantee.
"""
from __future__ import annotations

from apify_provider import ApifyCallBudget, _first_of, _run_actor_sync, apify_enabled

APIFY_RETAIL_SEARCH_ACTOR_ID = "igview-owner/amazon-search-scraper"

# Static cost cap (explicit architecture decision: fixed constant, no
# adaptive/persisted tuning across runs, matching every other Apify cap in
# this codebase). This actor's own docs report ~10-15 items per page, so a
# single page is already enough to cover a series that only has a handful
# of interior gaps -- and this call only ever runs once budget-wise per
# series-check run (see APIFY_RETAIL_SEARCH_MAX_CALLS_PER_SERIES_RUN
# below), so there is no opportunity to raise this later without also
# raising that budget.
APIFY_RETAIL_SEARCH_MAX_PAGES = 1

# This actor's own default marketplace/language -- pinned explicitly
# (rather than omitted, which would also default to these same values)
# so a future actor-side default change can't silently shift results for
# a US-library-focused app without showing up as a diff here first.
APIFY_RETAIL_SEARCH_COUNTRY = "US"

# Worst case per series-check run: exactly one retail-search actor call --
# same "at most one lookup attempt" intent as the primary actor's own
# APIFY_MAX_CALLS_PER_SERIES_RUN, just tracked on a wholly separate counter
# (see module docstring). Only one query is issued per call regardless of
# how many interior numbers are still missing (a broad "<series> <author>"
# query, not one query per missing number -- see
# discovery_engine._attempt_retail_search_recovery for why a single
# series-wide query is more likely to surface several missing volumes at
# once than spending this one call on a single specific number).
APIFY_RETAIL_SEARCH_MAX_CALLS_PER_SERIES_RUN = 1


def _log(message: str) -> None:
    print(f"[apify_retail_search_provider] {message}", flush=True)


def _normalize_search_result_item(item: dict) -> dict | None:
    """Maps one igview-owner/amazon-search-scraper dataset item to the
    canonical snake_case provider shape shared by every discovery_engine.py
    provider (see apify_provider._normalize_product_item for the sibling
    mapping this mirrors). Missing fields are always explicitly None, never
    an absent key or empty string, matching that same convention.

    This actor's real output field names (product_title/product_url/
    product_photo, confirmed via live validation, 2026-09-02) are used as
    the first-choice keys, with a couple of generic fallbacks via
    `_first_of` in case a future actor version renames them -- same
    tolerance rationale as apify_provider.py's own module docstring.

    Deliberately does NOT synthesize authors/series_number_hint/isbn13 from
    nothing -- this actor's search-results page has no such fields at all
    (unlike apify_provider.py's product-detail scrape, which parses an
    `attributes` list Amazon only attaches to product *detail* pages).
    Those stay None here; downstream fusion/title-inference already knows
    how to work with an under-specified hit like this, the same way it
    already does for e.g. a bare OpenLibrary or web_search hit with no
    series-position data.
    """
    title = _first_of(item, "product_title", "title", "name")
    if not title:
        return None

    asin = _first_of(item, "asin", "ASIN")
    url = _first_of(item, "product_url", "url", "productUrl")
    cover_image = _first_of(item, "product_photo", "image", "thumbnailImage")

    return {
        "source": "apify_retail_search",
        "source_id": str(asin).strip() if asin else (str(url).strip() if url else None),
        "title": str(title).strip(),
        "authors": [],
        "published_date": None,
        "description": None,
        "isbn13": None,
        "source_url": str(url).strip() if url else None,
        "language": "",
        "series_number_hint": None,
        "upcoming_hint": None,
        "series_name_hint": None,
        "series_total_hint": None,
        "asin": str(asin).strip() if asin else None,
        "cover_image": str(cover_image).strip() if cover_image else None,
        # Deliberately omitted (not hardcoded) -- see apify_provider.py's
        # own "confidence" comment (CR-1) for why: discovery_engine.py's
        # _filter_and_merge/_fuse_and_score_candidates stamp this with the
        # real pass-level confidence, and a hardcoded value here would
        # always win over that.
        "confidence": None,
    }


def fetch_retail_search_candidates(query: str, budget: "ApifyCallBudget") -> list[dict]:
    """Returns retail-search-sourced candidates in the standard
    discovery_engine.py provider dict shape (see
    _normalize_search_result_item).

    Exactly one actor call per invocation (no fan-out, no pagination-cap
    override) -- `query` is expected to already be a broad, series-scoped
    free-text string (e.g. "<series name> <author>"), not a request-specific
    query per missing book number; see the module docstring and
    discovery_engine._attempt_retail_search_recovery for why.

    Returns [] (never raises) if APIFY_API_TOKEN isn't configured, the
    call budget is exhausted, or the actor call fails/times out -- exactly
    like apify_provider.fetch_apify_candidates's own contract, so a caller
    can treat this identically to every other provider fetch in this
    codebase.
    """
    if not apify_enabled():
        return []

    items = _run_actor_sync(
        APIFY_RETAIL_SEARCH_ACTOR_ID,
        {
            "query": query,
            "maxPages": APIFY_RETAIL_SEARCH_MAX_PAGES,
            "country": APIFY_RETAIL_SEARCH_COUNTRY,
        },
        budget,
    )
    if not items:
        return []

    candidates: list[dict] = []
    for raw in items:
        # Mirrors apify_provider.fetch_apify_candidates's own error-item
        # skip -- not confirmed present in this actor's own dataset shape
        # (its documented failure mode is "stops pagination early," not an
        # inline error-typed item), but harmless to guard defensively the
        # same way, and keeps both actors' item-skip logic symmetric.
        if raw.get("error"):
            continue
        normalized = _normalize_search_result_item(raw)
        if normalized:
            candidates.append(normalized)
    return candidates
