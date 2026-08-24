"""Book discovery via official, public book-metadata APIs.

This replaces the old Amazon/Google HTML-scraping pipeline
(checker_core.py / checker_providers.py / checker_rules.py) as the primary
discovery data source. HTML scraping is inherently fragile -- sites detect
and block scrapers and change markup without notice -- while Google Books
and OpenLibrary are free, public JSON APIs intended for exactly this use
case, with no bot-blocking risk.

Strategy: query each API for "<series name> <author>" (a combined
free-text search, not a strict field filter) so the API's own relevance
ranking does the hard work of figuring out which of an author's books
belong to this series -- this is far more precise than pulling an author's
entire bibliography and guessing from title text alone, since many authors
(especially prolific indie/self-published authors) write multiple unrelated
series or standalone works. If that targeted search returns nothing, we
fall back to a plain author-bibliography sweep as a lower-confidence
last resort.

Note: Google Books' unauthenticated (no API key) quota is a small pool
shared globally across all callers without a key, so it may return 429s
even under light use. Set the GOOGLE_BOOKS_API_KEY environment variable
(free from Google Cloud Console) for reliable results -- OpenLibrary has
no such restriction and needs no key.

RT-1a: this module used to hold everything below directly (~4500 lines).
It's now split across four modules by concern, each importable on its own:

  - discovery_text.py: shared, dependency-free text/identity primitives
    (normalization, title parsing, number/date inference).
  - provider_io.py: raw provider fetches (Google Books/OpenLibrary/
    Hardcover/Serper) plus the Anthropic LLM calls that structure/reconcile
    their results.
  - deterministic_fusion.py: pure, no-I/O candidate fusion/scoring/edition-
    collapse and the deterministic contamination/fallback gating.
  - diagnostics.py: pure diagnostic-only external-vs-owned computations and
    drop-reason explanations.

This module now holds only top-level discovery orchestration
(discover_candidates_for_series/discover_candidates_for_author and their
direct helpers) and re-exports everything from the four modules above, so
every existing caller (agents/series_agent.py, provider_protocol.py,
tests, etc.) that does `from discovery_engine import X` or
`discovery_engine.X` keeps working unchanged.
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from services.identity import _edition_priority, _normalize_title_for_identity, _normalize_series_name_for_identity
from services.discovery_telemetry import DiscoveryTelemetry, maybe_pass_scope
from services.discovery_cache import DiscoveryCache, CACHE_MISS
from apify_provider import ApifyCallBudget, apify_enabled, fetch_apify_candidates
from provider_protocol import (
    GoogleBooksProvider,
    OpenLibraryProvider,
    HardcoverProvider,
    WebDiscoveryProvider,
)

from discovery_text import (
    NON_NEW_RELEASE_TITLE_MARKERS,
    NON_NEW_RELEASE_TITLE_PATTERNS,
    PLACEHOLDER_TITLE_MARKERS,
    _SERIES_INDEX_SUFFIX_PATTERN,
    _TITLE_VARIANT_FILLER_TOKENS,
    _SOLO_GENRE_TAGLINE_TOKENS,
    _TITLE_SERIES_MARKER_PATTERN,
    _DASH_SERIES_MARKER_PATTERN,
    _PLACEHOLDER_DATE_PATTERN,
    _LEADING_ARTICLE_PATTERN,
    _WORD_NUMBERS,
    _BUNDLE_TITLE_PATTERN,
    looks_like_placeholder_title,
    normalize_series_branding_name,
    _token_set,
    _token_overlap_ratio,
    _series_names_compatible,
    _title_is_series_variant,
    looks_like_series_index_entry,
    infer_series_hint_from_title_text,
    clean_display_title,
    looks_like_placeholder_date,
    _log,
    normalize_text,
    _strip_leading_article,
    _title_core_segment,
    core_title_key,
    bare_title_key,
    _normalize_number_context,
    _parse_positive_number,
    infer_number_from_title,
    looks_like_non_new_release,
    is_english_or_unknown,
    parse_flexible_date,
    classify_upcoming,
    split_author_names,
    primary_author_name,
    _author_matches,
    _to_int_or_none,
    _integral_or_none,
)
from provider_io import (
    GOOGLE_BOOKS_ENDPOINT,
    OPENLIBRARY_ENDPOINT,
    HARDCOVER_ENDPOINT,
    SERPER_SEARCH_ENDPOINT,
    REQUEST_TIMEOUT_SECONDS,
    WEB_SEARCH_TIMEOUT_SECONDS,
    ANTHROPIC_MODEL,
    WEB_SEARCH_MAX_RESULTS,
    WEB_SEARCH_LOOKAHEAD_BOOKS,
    WEB_SEARCH_DATE_REFINEMENT_MAX,
    WEB_SEARCH_MAX_PARALLEL_WORKERS,
    _HARDCOVER_SEARCH_QUERY,
    REQUEST_HEADERS,
    MAX_PUBLICATION_DATE_BACKFILL_LOOKUPS,
    MAX_MISSING_VOLUME_DATE_VERIFICATION_LOOKUPS,
    _WEB_SEARCH_STRUCTURING_PROMPT,
    _SERIES_OVERVIEW_PROMPT,
    _AMAZON_URL_DOMAINS,
    RECONCILIATION_SERIES_COMPLETENESS_THRESHOLD,
    RECONCILIATION_METADATA_COMPLETENESS_THRESHOLD,
    RECONCILIATION_DISAGREEMENT_RATIO_THRESHOLD,
    RECONCILIATION_MAX_CANDIDATES,
    _LLM_RECONCILIATION_PROMPT,
    _fetch_google_books,
    _fetch_openlibrary,
    _fetch_hardcover,
    backfill_missing_publication_dates,
    verify_missing_volume_recovery_dates,
    _web_search_enabled,
    _llm_structuring_enabled,
    _fetch_serper_web_search,
    _structure_web_results_with_llm,
    generate_series_overview,
    _refine_undated_web_search_results_batch,
    _structure_with_verdict_cache,
    _fetch_apify_discovery,
    _promote_web_search_health_diagnostics,
    _fetch_web_search,
    _fetch_all_providers_parallel,
    _candidate_has_provenance_disagreement,
    _needs_llm_reconciliation,
    _format_candidate_for_reconciliation,
    _coerce_reconciled_float,
    _coerce_reconciled_str,
    _apply_reconciliation_entry,
    _reconcile_candidates_with_llm,
)
from deterministic_fusion import (
    _METADATA_COMPLETENESS_FIELDS,
    _PROVIDER_CONFIDENCE_WEIGHT,
    _EDITION_TITLE_MARKERS,
    FALLBACK_SERIES_COMPLETENESS_THRESHOLD,
    FALLBACK_CONFIDENCE_THRESHOLD,
    _PROVIDER_SORT_RANK,
    _TRANSIENT_CANDIDATE_FIELDS,
    UnifiedCandidate,
    _filter_and_merge,
    _first_present_field,
    _fuse_and_score_candidates,
    _unified_candidate_to_raw_dict,
    _resolve_candidate_number,
    _reconciled_completeness_score,
    _infer_edition_type_from_title,
    _resolved_edition_type,
    _same_underlying_book,
    _edition_strength,
    _strictly_better_metadata,
    _collapse_edition_group,
    _finalize_candidates,
    _series_completeness_and_confidence,
    _should_trigger_author_fallback,
    _is_cross_series_contamination,
    _filter_cross_series_contamination,
    _candidate_sort_key,
    finalize_discovery_output,
)
from diagnostics import (
    MAX_DROP_EXPLANATIONS,
    _DROP_EXPLANATIONS,
    _DEFAULT_DROP_EXPLANATION,
    _record_drop_diagnostic,
    compute_external_missing_vs_owned,
    compute_inferred_number,
    compute_owned_number_coverage,
    compute_new_volume_flags,
    compute_external_gap_ratio,
    compute_drop_explanations,
)

# Loaded here (rather than relying on the entry point having done it first)
# so this module reads the right API key regardless of import order.
load_dotenv()

# Bounds how many gap numbers _reconstruct_series_skeleton will fire a
# targeted web-search lookahead query for in one call -- a series with a
# large number of gaps (e.g. only book 1 and book 12 are owned/found) would
# otherwise turn into a double-digit number of live web searches in a
# single Check Now run. All still batched into one _fetch_web_search call
# (one shared web-search loop + one LLM structuring pass), same as the
# existing highest-owned-number lookahead in _fetch_all_providers_parallel.
MAX_MISSING_VOLUME_LOOKAHEAD_QUERIES = 6


def _reconstruct_series_skeleton(
    unified_candidates: list["UnifiedCandidate"],
    owned_books: list[dict],
    *,
    series_name: str | None = None,
    author: str | None = None,
    telemetry: "DiscoveryTelemetry | None" = None,
    cache: "DiscoveryCache | None" = None,
) -> dict:
    """Infers how many volumes a series is expected to have -- the highest
    integer book number seen anywhere, across owned_books' book_number,
    each unified_candidate's own fused series_number, and (for whichever
    candidates have neither) a title-text inference pass -- builds the full
    1..N "skeleton" of expected volume numbers, and identifies which of
    those numbers has no owned book AND no discovered candidate at all.

    For each such gap (up to MAX_MISSING_VOLUME_LOOKAHEAD_QUERIES), fires a
    targeted web-search+LLM lookahead query ("<series> <author> book <N>")
    specifically for that missing number. This is more surgical than the
    generic targeted "<series> <author>" query or the highest-owned-number-
    only lookahead _fetch_all_providers_parallel already does: it can
    recover a gap buried in the *middle* of an otherwise-complete series
    (e.g. owns/found 1-4 and 6-9 but never found 5), not just a gap at the
    very end.

    Only integer-numbered volumes count toward the skeleton -- a companion
    0.5/3.5-style novella is real content but isn't a "volume" in the
    sequential numbering sense a skeleton like this reconstructs (mirrors
    _fetch_hardcover's own reasoning for keeping series_position as a float
    instead of rounding it).

    PB-6 (doc-only note, no behavior change here): despite the shared name,
    this "skeleton" is unrelated to the durable `models.SeriesSkeleton` row
    that `services/skeleton_store.py` reads/writes -- this one is a
    same-call, in-memory 1..N gap model, recomputed from scratch on every
    invocation and never persisted, used only to decide which interior
    numbers deserve a lookahead search *during this one discovery pass*.
    The two do overlap in what they derive (both infer "which numbers are
    owned/known" from owned books + discovered candidates), which is real,
    tracked duplication (`discovery_agentic_migration_architecture_map.md`:
    this function's search half is a candidate for replacement by an agent
    tool-call informed by the durable skeleton instead), but merging them
    is a structural change deferred to a later wave -- see
    `services/skeleton_store.py`'s module docstring for the reverse
    cross-reference.

    Any newly-recovered candidates are fused back into unified_candidates
    via _fuse_and_score_candidates (existing candidates given top priority,
    so a lookahead hit can only *backfill* an already-found candidate, not
    override it) rather than replacing it outright.

    series_name/author are optional and, when omitted, inferred from
    unified_candidates' own series_name/authors fields -- series_agent.py,
    the real caller, always has both on hand directly (series.name/
    series_author) and passes them explicitly instead of relying on this
    fallback, which exists mainly so this function still does something
    sensible if unified_candidates is empty of any usable hint.

    Returns {"candidates": [...], "expected_total": int | None,
    "missing_numbers": [...], "recovered_numbers": [...]}. "candidates" is
    unified_candidates with any newly-recovered volumes fused in; when
    there's nothing missing, no resolvable series_name/author, no resolvable
    expected total at all, or web search isn't configured
    (SERPER_API_KEY/ANTHROPIC_API_KEY), it's returned unchanged.
    """
    resolved_series_name = series_name or next((c.series_name for c in unified_candidates if c.series_name), None)
    resolved_author = author or next(
        (candidate_author for c in unified_candidates for candidate_author in c.authors if candidate_author), None
    )

    known_numbers: set[int] = set()
    for book in owned_books:
        number = book.get("book_number")
        try:
            if number is not None and float(number).is_integer():
                known_numbers.add(int(float(number)))
        except (TypeError, ValueError):
            continue
    for candidate in unified_candidates:
        number = _resolve_candidate_number(candidate, resolved_series_name)
        if number is not None and float(number).is_integer():
            known_numbers.add(int(number))

    def _result(missing_numbers: list[int], recovered_numbers: list[int], candidates: list["UnifiedCandidate"]) -> dict:
        return {
            "candidates": candidates,
            "expected_total": max(known_numbers) if known_numbers else None,
            "missing_numbers": missing_numbers,
            "recovered_numbers": recovered_numbers,
        }

    if not known_numbers:
        return _result([], [], unified_candidates)

    expected_total = max(known_numbers)
    missing_numbers = sorted(set(range(1, expected_total + 1)) - known_numbers)

    if (
        not missing_numbers
        or not resolved_series_name
        or not resolved_author
        or not (_web_search_enabled() and _llm_structuring_enabled())
    ):
        return _result(missing_numbers, [], unified_candidates)

    targeted_missing = missing_numbers[:MAX_MISSING_VOLUME_LOOKAHEAD_QUERIES]
    lookahead_queries = [f'"{resolved_series_name}" {resolved_author} book {number}' for number in targeted_missing]

    try:
        lookahead_raw = _fetch_web_search(
            lookahead_queries,
            resolved_series_name,
            resolved_author,
            telemetry=telemetry,
            cache=cache,
            pass_label="missing_volume",
        )
    except Exception as exc:  # a lookahead failure should never sink the candidates already found
        _log(f"missing-volume lookahead failed: {exc}")
        return _result(missing_numbers, [], unified_candidates)

    if not lookahead_raw:
        return _result(missing_numbers, [], unified_candidates)

    # Existing candidates fed in under the highest-priority bucket key so
    # fusion's own backfill logic treats a lookahead hit as a supplement to
    # an already-found candidate (when it matches one by identity) rather
    # than a competing, potentially-lower-quality duplicate -- see
    # _fuse_and_score_candidates. Each entry's own "source" field (already
    # baked in via _unified_candidate_to_raw_dict) is untouched by which
    # bucket key it's passed under here.
    existing_raw = [_unified_candidate_to_raw_dict(candidate) for candidate in unified_candidates]
    refreshed = _fuse_and_score_candidates(
        {"hardcover": existing_raw, "google": [], "openlibrary": [], "web": lookahead_raw},
        resolved_author,
        resolved_series_name,
    )

    recovered_numbers = sorted(
        {
            int(candidate.series_number)
            for candidate in refreshed
            if candidate.series_number is not None
            and float(candidate.series_number).is_integer()
            and int(candidate.series_number) in targeted_missing
        }
    )

    return {
        "candidates": refreshed,
        "expected_total": expected_total,
        "missing_numbers": missing_numbers,
        "recovered_numbers": recovered_numbers,
    }


def precheck_for_new_volumes(
    series_name: str,
    author: str,
    ceiling: float,
    *,
    telemetry: "DiscoveryTelemetry | None" = None,
) -> bool:
    """Cheap "is there anything new" check for a series that was recently
    fully checked (see discovery_catchup_architecture_spec.md #7.2) --
    catalog-only (Google Books + OpenLibrary + Hardcover), zero web-search
    calls, zero LLM calls. Returns True if the full multi-round loop
    should run, False if it's safe to short-circuit.

    Hardcover's raw results carry a structured numeric series position
    (`series_number_hint` -- see _fetch_hardcover) directly, so those are
    trusted outright. Google Books and OpenLibrary's raw results don't
    expose one at all -- for those, a number is instead inferred from the
    hit's own title via infer_number_from_title, same title-parsing
    heuristic series_agent.py's belongs_to_series loop already relies on
    for provider hits with no structured hint. This is a deliberate
    accuracy-over-cost trade-off (see discovery_agentic_migration_
    decision_log.md): a noisy, title-inferred number can occasionally
    trigger an extra multi-round pass this check could have short-
    circuited, but the prior Hardcover-only version's false "nothing new"
    could silently miss a real new volume that Google Books/OpenLibrary
    already indexed but Hardcover's own search hadn't yet -- for a
    personal-use precheck, a wasted extra pass is far cheaper than a
    missed release.
    """
    query_author = primary_author_name(author)
    targeted_query_text = f"{series_name} {query_author}".strip()
    with maybe_pass_scope(telemetry, "precheck"):
        fetch_results = _fetch_all_providers_parallel(
            query_author,
            series_name,
            targeted_query_text,
            None,
            author=author,
            enable_web_search=False,
            telemetry=telemetry,
            pass_label="precheck",
        )

    for hit in fetch_results.get("hardcover") or []:
        if not _author_matches(hit.get("authors") or [], author):
            continue
        number = hit.get("series_number_hint")
        if number is None:
            continue
        try:
            if float(number) > ceiling:
                return True
        except (TypeError, ValueError):
            continue

    # Google Books/OpenLibrary carry no structured series-position field at
    # all -- infer_number_from_title's title-parsing heuristic is the only
    # signal available for them (see this function's own docstring for why
    # that's an accepted trade-off here, not a full replacement for a real
    # structured hint).
    for provider in ("google", "openlibrary"):
        for hit in fetch_results.get(provider) or []:
            if not _author_matches(hit.get("authors") or [], author):
                continue
            number = infer_number_from_title(hit.get("title"), series_name)
            if number is None:
                continue
            if number > ceiling:
                return True
    return False


def _run_web_search_diagnostic_probe(
    targeted_query_text: str, telemetry: "DiscoveryTelemetry | None"
) -> dict | None:
    """PB-4 sub-step of discover_candidates_for_series: the diagnostic-only
    coverage probe that fires when a Serper key is configured but no
    Anthropic key is -- there's no way to structure raw hits into real
    candidates in that case, so don't try.

    This exists purely so Serper's own indie/LitRPG/web-serial coverage --
    unverified, and possibly quite different from Brave's -- can be checked
    by hand against a real query before being relied on for anything (see
    discovery_agentic_migration_decision_log.md). No other provider/pass
    runs, nothing is fused/filtered, and nothing here can ever be added to
    the library: this is a standalone coverage probe, not a partial
    discovery run.

    This one lone Serper call has none of _fetch_web_search's own
    per-query error handling -- a bare probe was fine for its original
    "check by hand" purpose, but this branch runs unconditionally in the
    real Check Now path whenever Serper is configured with no Anthropic
    key, so an unhandled 4xx/5xx here used to crash the ENTIRE check in
    under a second, before Hardcover/Google/OpenLibrary ever got a chance
    to run at all (see the Apify integration design chat's follow-up
    finding). Caught and logged instead now -- on failure, skip the probe
    entirely and fall through to the normal pipeline below, which still
    runs Hardcover/Google/OpenLibrary (web search stays off, exactly as it
    already is whenever no Anthropic key is present).

    Returns the diagnostic-mode result dict to return immediately, or None
    if the caller should fall through to the normal discovery pipeline
    (either the probe isn't applicable here, or it failed).
    """
    if not (_web_search_enabled() and not _llm_structuring_enabled()):
        return None

    try:
        probe_snippets = _fetch_serper_web_search(targeted_query_text, telemetry=telemetry)
    except Exception as exc:
        _log(f"web-search coverage probe failed, falling through to normal discovery: {exc}")
        return None

    return {
        "candidates": [],
        "unified_candidates": [],
        "provider_failures": [],
        "all_providers_failed": False,
        "used_author_fallback": False,
        "drop_diagnostics": [],
        "diagnostic_mode": "web_search_coverage_probe",
        "diagnostic_raw_web_snippets": probe_snippets,
    }


def _fetch_targeted_series_providers(
    query_author: str,
    series_name: str,
    targeted_query_text: str,
    highest_owned_book_number: int | None,
    author: str,
    *,
    discovery_drop_diagnostics: list[dict],
    telemetry: "DiscoveryTelemetry | None",
    cache: "DiscoveryCache | None",
    apify_budget: "ApifyCallBudget",
) -> tuple[dict, list[dict], bool]:
    """PB-4 sub-step of discover_candidates_for_series: the primary
    targeted "<series name> <author>" pass.

    Google/OpenLibrary/Hardcover/web-search are fetched concurrently (see
    _fetch_all_providers_parallel) instead of one after another -- query
    construction and per-provider error handling are unchanged, only the
    scheduling is. Live web search fills the coverage gap the catalog APIs
    have for indie/self-published titles and pure announcements -- only
    runs when both a web-search key (Serper) and an Anthropic key are
    configured, since it needs both to search and to structure the
    results. (Just a web-search key with no Anthropic key never reaches
    this far -- see _run_web_search_diagnostic_probe.)

    Returns (fetch_results, provider_failures, any_provider_succeeded):
    fetch_results feeds straight into
    _fuse_reconcile_and_filter_candidates; provider_failures/
    any_provider_succeeded are this pass's contribution to the caller's
    running totals across both the targeted and (if triggered)
    author-fallback passes.
    """
    fetch_results = _fetch_all_providers_parallel(
        query_author,
        series_name,
        targeted_query_text,
        highest_owned_book_number,
        author=author,
        diagnostics=discovery_drop_diagnostics,
        telemetry=telemetry,
        cache=cache,
        pass_label="targeted",
        apify_budget=apify_budget,
    )
    failures = fetch_results["_failures"]
    provider_failures: list[dict] = []
    any_provider_succeeded = False

    google_raw = fetch_results["google"]
    if "google" in failures:
        provider_failures.append({"provider": "google_books", "error": str(failures["google"])})
    else:
        any_provider_succeeded = True

    openlibrary_raw = fetch_results["openlibrary"]
    if "openlibrary" in failures:
        provider_failures.append({"provider": "openlibrary", "error": str(failures["openlibrary"])})
    else:
        any_provider_succeeded = True

    hardcover_raw = fetch_results["hardcover"]
    if "hardcover" in failures:
        provider_failures.append({"provider": "hardcover", "error": str(failures["hardcover"])})
    elif hardcover_raw or os.environ.get("HARDCOVER_API_KEY", "").strip():
        any_provider_succeeded = True

    web_search_raw = fetch_results["web"]
    if _web_search_enabled() and _llm_structuring_enabled():
        if "web" in failures:
            provider_failures.append({"provider": "web_search", "error": str(failures["web"])})
        else:
            any_provider_succeeded = True
        # Surfaces Serper's health here even though "web" not in failures
        # no longer implies Serper itself succeeded -- see
        # _promote_web_search_health_diagnostics's own docstring.
        _promote_web_search_health_diagnostics(
            discovery_drop_diagnostics, provider_failures, "targeted", "web_search"
        )

    return fetch_results, provider_failures, any_provider_succeeded


def _fetch_fallback_series_providers(
    query_author: str,
    series_name: str,
    highest_owned_book_number: int | None,
    author: str,
    *,
    other_known_series_names: set[str] | None,
    enable_fallback_web_search: bool,
    discovery_drop_diagnostics: list[dict],
    telemetry: "DiscoveryTelemetry | None",
    cache: "DiscoveryCache | None",
    apify_budget: "ApifyCallBudget",
) -> tuple[dict, list[dict], bool]:
    """PB-4 sub-step of discover_candidates_for_series: the author-fallback
    pass, triggered by _should_trigger_author_fallback -- the targeted
    pass looking seriously incomplete or low-confidence, not just literally
    empty.

    Scoped by series_name, not a bare author sweep: a plain author-wide
    query has no way to tell this series' books apart from a prolific
    author's other, unrelated series (regression: falling back to plain
    "George Wagner" pulled in higher-numbered books from his other
    thriller series alongside the Jonathan Hunt books). Passing
    series_name through -- and building the OpenLibrary/web-search queries
    around it the same way the targeted (primary) pass already does --
    keeps the fallback pass able to trigger on low completeness/confidence
    without it being author-wide in scope. Web search still stays off by
    default (enable_fallback_web_search opts in) since web-search+LLM
    structuring here is a noisier, costlier signal than the catalog APIs
    already provide.

    Explicit cross-series contamination -- a fallback hit tagged with one
    of this author's OTHER tracked series' names -- is dropped before
    fusion ever sees it, rather than disabling the whole pass just because
    other series exist (see discover_candidates_for_series's own docstring
    and _is_cross_series_contamination).

    Returns (fallback_results, provider_failures, any_provider_succeeded),
    same shape as _fetch_targeted_series_providers.
    """
    fallback_results = _fetch_all_providers_parallel(
        query_author,
        series_name,
        f"{series_name} {query_author}",
        highest_owned_book_number,
        author=author,
        openlibrary_query=f'"{series_name}" "{query_author}"',
        web_search_queries=[
            f"{series_name} {query_author} books",
            f"{series_name} {query_author} series",
        ],
        enable_web_search=enable_fallback_web_search,
        diagnostics=discovery_drop_diagnostics,
        telemetry=telemetry,
        cache=cache,
        pass_label="author_fallback",
        apify_budget=apify_budget,
    )
    fallback_results = _filter_cross_series_contamination(
        fallback_results, series_name, other_known_series_names, diagnostics=discovery_drop_diagnostics
    )
    fallback_failures = fallback_results["_failures"]
    provider_failures: list[dict] = []
    any_provider_succeeded = False

    google_fallback = fallback_results["google"]
    if "google" in fallback_failures:
        provider_failures.append({"provider": "google_books_fallback", "error": str(fallback_failures["google"])})
    else:
        any_provider_succeeded = True

    openlibrary_fallback = fallback_results["openlibrary"]
    if "openlibrary" in fallback_failures:
        provider_failures.append(
            {"provider": "openlibrary_fallback", "error": str(fallback_failures["openlibrary"])}
        )
    else:
        any_provider_succeeded = True

    hardcover_fallback = fallback_results["hardcover"]
    if "hardcover" in fallback_failures:
        provider_failures.append({"provider": "hardcover_fallback", "error": str(fallback_failures["hardcover"])})
    elif hardcover_fallback or os.environ.get("HARDCOVER_API_KEY", "").strip():
        any_provider_succeeded = True

    if enable_fallback_web_search:
        web_fallback = fallback_results["web"]
        if _web_search_enabled() and _llm_structuring_enabled():
            if "web" in fallback_failures:
                provider_failures.append(
                    {"provider": "web_search_fallback", "error": str(fallback_failures["web"])}
                )
            else:
                any_provider_succeeded = True
            _promote_web_search_health_diagnostics(
                discovery_drop_diagnostics, provider_failures, "author_fallback", "web_search_fallback"
            )

    return fallback_results, provider_failures, any_provider_succeeded


def _fuse_reconcile_and_filter_candidates(
    fetch_results: dict,
    author: str,
    series_name: str,
    exclude_title_keys: set[str],
    *,
    confidence: str,
    diagnostics: list[dict],
    telemetry: "DiscoveryTelemetry | None",
) -> tuple[list[dict], list]:
    """PB-4 sub-step of discover_candidates_for_series, shared by both the
    targeted pass and the (optional) author-fallback pass: fuse each real
    book's raw hits (across all four providers) into one enriched
    candidate, conditionally reconcile with the LLM, collapse duplicate
    editions, then filter/merge into the flat dict shape series_agent.py
    expects.

    Hardcover is listed first inside _fuse_and_score_candidates (and
    inside fetch_results itself): when multiple sources return the same
    book, fusion's own backfill keeps whichever copy appears first as the
    base, and Hardcover's explicit series-position/release-status fields
    are more trustworthy than Google Books/OpenLibrary free-text for
    indie/self-published LitRPG, which both of those APIs tend to index/
    cover poorly. Web search is listed last since it's the least
    structured of the four sources.

    Conditional LLM reconciliation only runs when fusion alone left the
    set looking incomplete, internally disagreeing, or thin on metadata
    (see _needs_llm_reconciliation) -- and runs before _filter_and_merge so
    any normalized/merged candidate it produces still goes through the
    exact same filtering every other candidate does. Edition-aware
    collapse (_finalize_candidates) runs last, immediately before
    candidates are converted to the dict shape _filter_and_merge expects.

    Returns (combined, fused_candidates): `combined` is what
    discover_candidates_for_series accumulates into its "candidates"
    return value (via finalize_discovery_output); `fused_candidates` is the
    richer pre-filter UnifiedCandidate list it also returns as
    "unified_candidates" (see _reconstruct_series_skeleton).
    """
    fused_candidates = _fuse_and_score_candidates(fetch_results, author, series_name)
    if _needs_llm_reconciliation(fused_candidates, series_name):
        fused_candidates = _reconcile_candidates_with_llm(
            fused_candidates, series_name, diagnostics=diagnostics, telemetry=telemetry
        )
    fused_candidates = _finalize_candidates(fused_candidates)
    combined = _filter_and_merge(
        [_unified_candidate_to_raw_dict(candidate) for candidate in fused_candidates],
        author,
        exclude_title_keys,
        confidence=confidence,
        series_name=series_name,
    )
    return combined, fused_candidates


def _build_series_discovery_result(
    combined: list[dict],
    final_fused_candidates: list,
    provider_failures: list[dict],
    all_providers_failed: bool,
    used_author_fallback: bool,
    discovery_drop_diagnostics: list[dict],
) -> dict:
    """PB-4 sub-step of discover_candidates_for_series: assemble its final
    return dict, once both the targeted and (if triggered) author-fallback
    passes have already been fetched, fused, and merged together."""
    return {
        # Deterministically sorted and stripped of transient fusion-internal
        # fields -- see finalize_discovery_output. Runs after both the
        # targeted and (if triggered) fallback passes have already been
        # merged together above, so it's the true last step on this list.
        "candidates": finalize_discovery_output(combined),
        # Pre-filter fused candidates -- additive, existing "candidates" key
        # unchanged -- so a caller that needs the richer UnifiedCandidate
        # objects (confidence_score, metadata_completeness_score,
        # source_provenance, resolved series_number) doesn't have to redo
        # the fetch+fuse work itself. See _reconstruct_series_skeleton.
        # Deliberately NOT passed through finalize_discovery_output --
        # that strips exactly the fields _reconstruct_series_skeleton needs.
        "unified_candidates": final_fused_candidates,
        "provider_failures": provider_failures,
        "all_providers_failed": all_providers_failed,
        "used_author_fallback": used_author_fallback,
        # Phase 3.5, PURE SHADOW MODE -- see _record_drop_diagnostic. Logged
        # only (series_agent.py merges this with its own agent_drop_diagnostics);
        # nothing here changes "candidates" above.
        "drop_diagnostics": discovery_drop_diagnostics,
    }


def discover_candidates_for_series(
    series_name: str,
    author: str,
    *,
    exclude_title_keys: set[str] | None = None,
    allow_author_fallback: bool = True,
    other_known_series_names: set[str] | None = None,
    enable_fallback_web_search: bool = False,
    progress_callback=None,
    highest_owned_book_number: int | None = None,
    telemetry: "DiscoveryTelemetry | None" = None,
    cache: "DiscoveryCache | None" = None,
) -> dict:
    """Find candidate books for a specific series by a specific author.

    Primary pass: a targeted "<series name> <author>" search on both APIs,
    which leans on each API's own relevance ranking to associate books with
    the series (via title/description text), rather than trying to infer
    series membership purely from title patterns.

    Fallback pass: a second, series-scoped sweep (query text/OpenLibrary
    query/web-search queries all built around "<series name> <author>", same
    shape as the primary pass) rather than a bare author-bibliography sweep,
    so a brand new release whose indexed text doesn't yet mention the series
    name can still surface without pulling in this author's other, unrelated
    series. Triggered by _should_trigger_author_fallback -- the targeted
    pass looking seriously incomplete or low-confidence, not just literally
    empty -- rather than the old "only if the targeted pass found nothing at
    all" rule (still covered, as the most extreme case of "incomplete").
    allow_author_fallback remains a hard, caller-controlled kill switch
    (e.g. discover_series_by_name deliberately always sets it False --
    it's already running the targeted search on demand specifically to fill
    in one series, so a broad author sweep on top would be redundant).

    Having other tracked series by this author no longer disables the
    fallback pass outright -- pass their names as other_known_series_names
    and any fallback hit EXPLICITLY tagged as one of those (not simply
    unlabeled) is dropped before it ever reaches fusion; see
    _is_cross_series_contamination. The fallback's own results are additive
    to the targeted pass's, not a replacement for them, since the targeted
    pass may well have already found real, legitimate matches even while
    still triggering fallback for being incomplete.

    The fallback pass has never queried web search by default, since
    web-search+LLM structuring here is a noisier, costlier signal than the
    catalog APIs already provide -- enable_fallback_web_search opts into it
    anyway for a caller that wants the extra coverage.
    """
    exclude_title_keys = exclude_title_keys or set()
    series_name = str(series_name or "").strip()
    author = str(author or "").strip()
    provider_failures: list[dict] = []
    # Phase 3.5 of agentic discovery, PURE SHADOW MODE: structured record of
    # every point below that silently drops a raw result/candidate with no
    # other trace (see _record_drop_diagnostic). Purely additive -- nothing
    # here changes which candidates get filtered/merged/returned.
    discovery_drop_diagnostics: list[dict] = []

    if not author:
        return {
            "candidates": [],
            "unified_candidates": [],
            "provider_failures": [],
            "all_providers_failed": False,
            "used_author_fallback": False,
            "drop_diagnostics": [],
        }

    if progress_callback:
        progress_callback({"current_pass": f"Searching for {series_name or author}"})

    # Query APIs with just the first co-author's name (structured author
    # fields rarely contain multiple concatenated names), but keep
    # matching/filtering against the full original string so legitimate
    # co-authored results still pass.
    query_author = primary_author_name(author)
    targeted_query_text = f"{series_name} {query_author}".strip()

    # One unmissable line per Check Now showing exactly which provider
    # gates are open, without ever printing the key values themselves --
    # added specifically so a "nothing hit Serper"-type report can be
    # confirmed/ruled out straight from Railway logs (env vars can be set
    # on the wrong Railway service/environment, or not picked up without a
    # redeploy, in a way that's otherwise invisible from the Check Now UI
    # alone).
    _log(
        f"provider gates for series={series_name!r}: "
        f"serper={'on' if _web_search_enabled() else 'OFF (SERPER_API_KEY not set)'}, "
        f"anthropic={'on' if _llm_structuring_enabled() else 'OFF (ANTHROPIC_API_KEY not set)'}, "
        f"hardcover={'on' if os.environ.get('HARDCOVER_API_KEY', '').strip() else 'off'}, "
        f"apify={'on' if apify_enabled() else 'OFF (APIFY_API_TOKEN not set)'}"
    )

    probe_result = _run_web_search_diagnostic_probe(targeted_query_text, telemetry)
    if probe_result is not None:
        return probe_result

    any_provider_succeeded = False

    # One shared budget for this entire run -- both the targeted pass below
    # and the author-fallback pass further down (if triggered) are passed
    # this SAME instance, so APIFY_MAX_CALLS_PER_SERIES_RUN caps total
    # Apify usage across the whole discover_candidates_for_series() call,
    # not per pass. See apify_provider.ApifyCallBudget's own docstring.
    apify_budget = ApifyCallBudget()

    fetch_results, targeted_provider_failures, targeted_any_succeeded = _fetch_targeted_series_providers(
        query_author,
        series_name,
        targeted_query_text,
        highest_owned_book_number,
        author,
        discovery_drop_diagnostics=discovery_drop_diagnostics,
        telemetry=telemetry,
        cache=cache,
        apify_budget=apify_budget,
    )
    provider_failures.extend(targeted_provider_failures)
    any_provider_succeeded = any_provider_succeeded or targeted_any_succeeded

    combined, fused_candidates = _fuse_reconcile_and_filter_candidates(
        fetch_results,
        author,
        series_name,
        exclude_title_keys,
        confidence="targeted",
        diagnostics=discovery_drop_diagnostics,
        telemetry=telemetry,
    )
    # Tracks whichever fused candidate set actually produced `combined` --
    # reassigned below if the fallback pass runs -- so callers that want the
    # pre-filter UnifiedCandidate objects themselves (e.g. series_agent.py's
    # missing-volume skeleton reconstruction) get the right one either way.
    final_fused_candidates = fused_candidates

    used_author_fallback = False
    if allow_author_fallback and _should_trigger_author_fallback(fused_candidates, series_name, highest_owned_book_number):
        used_author_fallback = True
        if progress_callback:
            progress_callback({"current_pass": f"Broadening search to all books by {author}"})

        fallback_results, fallback_provider_failures, fallback_any_succeeded = _fetch_fallback_series_providers(
            query_author,
            series_name,
            highest_owned_book_number,
            author,
            other_known_series_names=other_known_series_names,
            enable_fallback_web_search=enable_fallback_web_search,
            discovery_drop_diagnostics=discovery_drop_diagnostics,
            telemetry=telemetry,
            cache=cache,
            apify_budget=apify_budget,
        )
        provider_failures.extend(fallback_provider_failures)
        any_provider_succeeded = any_provider_succeeded or fallback_any_succeeded

        # Additive, not a replacement: the targeted pass above may have
        # already found real matches even while still triggering fallback
        # for looking incomplete, so the targeted pass's own title keys are
        # excluded here too -- the fallback only ever contributes *new*
        # candidates the targeted pass didn't already surface, each still
        # correctly tagged confidence="author_fallback" (weaker trust than
        # "targeted" -- series_agent.py's belongs_to_series leans on that
        # distinction) rather than the two passes' results being conflated.
        already_found_title_keys = {core_title_key(str(candidate.get("title") or "")) for candidate in combined}
        fallback_combined, fused_fallback_candidates = _fuse_reconcile_and_filter_candidates(
            fallback_results,
            author,
            series_name,
            exclude_title_keys | already_found_title_keys,
            confidence="author_fallback",
            diagnostics=discovery_drop_diagnostics,
            telemetry=telemetry,
        )
        combined = combined + fallback_combined
        final_fused_candidates = fused_candidates + fused_fallback_candidates

    # "All providers failed" should mean we got no usable data at all (every
    # call raised), not just that filtering left zero new candidates -- a
    # provider that successfully returned data (even if it was all already
    # owned, or simply had no coverage) is a normal, successful outcome.
    all_providers_failed = bool(provider_failures) and not any_provider_succeeded

    if progress_callback:
        progress_callback({"current_pass": "Done", "total": 1, "completed": 1})

    return _build_series_discovery_result(
        combined,
        final_fused_candidates,
        provider_failures,
        all_providers_failed,
        used_author_fallback,
        discovery_drop_diagnostics,
    )


# Bounds the number of extra per-candidate lookups _enrich_missing_series_hints
# performs -- keeps a "More by this author" run from ballooning into dozens
# of sequential API calls for a very prolific author.


MAX_SERIES_HINT_LOOKUPS = 25


def _enrich_missing_series_hints(candidates: list[dict], author: str) -> list[dict]:
    """Recover series membership for candidates the initial author-wide
    catalog sweep couldn't tag with a series name.

    Live regression: an author-bibliography search on Hardcover for "Glynn
    Stewart" returned "Refuge", "Crusade" and "Ashen Stars" with no series
    info at all, even though Hardcover's own per-title search --
    "Refuge Glynn Stewart" -- correctly identifies it as Exile #2. The
    author-wide bibliography query and a title-specific query apparently hit
    different index paths on Hardcover's side; only the latter reliably
    carries series data for every title. Re-querying per-title is more
    expensive, so it's only done for candidates that still have nothing
    after the cheap author-wide pass, and capped (MAX_SERIES_HINT_LOOKUPS).

    Also recovers the inverse case: a title that IS the bare series name and
    was never itself published as a book (e.g. "ONSET" -- the real books are
    "To Serve and Protect", "My Enemy's Enemy", etc.). If every sibling
    result from the per-title search shares one series name that matches the
    candidate's own title, the candidate is tagged with that series name so
    looks_like_series_index_entry can drop it downstream as a stub listing.
    """
    query_author = primary_author_name(author)
    enriched: list[dict] = []
    lookups_used = 0
    for candidate in candidates:
        if candidate.get("series_name_hint") or lookups_used >= MAX_SERIES_HINT_LOOKUPS:
            enriched.append(candidate)
            continue
        title = str(candidate.get("title") or "").strip()
        if not title:
            enriched.append(candidate)
            continue

        lookups_used += 1
        try:
            siblings = _fetch_hardcover(f"{title} {query_author}", max_results=8)
        except Exception:
            enriched.append(candidate)
            continue

        target_key = bare_title_key(title)
        self_match = next(
            (
                r
                for r in siblings
                if bare_title_key(r.get("title")) == target_key and r.get("series_name_hint")
            ),
            None,
        )
        if self_match:
            enriched.append(
                {
                    **candidate,
                    "series_name_hint": self_match.get("series_name_hint"),
                    "series_number_hint": candidate.get("series_number_hint")
                    or self_match.get("series_number_hint"),
                    "series_total_hint": candidate.get("series_total_hint")
                    or self_match.get("series_total_hint"),
                }
            )
            continue

        candidate_as_series = normalize_series_branding_name(title) or normalize_text(title)
        sibling_series_names = {
            normalize_text(r.get("series_name_hint")) for r in siblings if r.get("series_name_hint")
        }
        if siblings and candidate_as_series and sibling_series_names == {candidate_as_series}:
            enriched.append({**candidate, "series_name_hint": title})
            continue

        enriched.append(candidate)
    return enriched


def discover_candidates_for_author(
    author: str,
    *,
    exclude_title_keys: set[str] | None = None,
    progress_callback=None,
) -> dict:
    """Find candidate books by a specific author, across ALL of their series
    and standalone works -- the "More by this author" feature.

    Deliberately much lighter than discover_candidates_for_series: one plain
    author-bibliography query per catalog API (no series name to search
    against) plus at most one web-search query -- no lookahead
    queries, since there's no single "next book number" to look ahead from
    when the results can span several different series at once. Each
    candidate carries its own guessed series name (see series_name_hint on
    the Hardcover/web-search providers) rather than being checked against
    one fixed series name.
    """
    exclude_title_keys = exclude_title_keys or set()
    author = str(author or "").strip()
    provider_failures: list[dict] = []

    if not author:
        return {"candidates": [], "provider_failures": [], "all_providers_failed": False}

    if progress_callback:
        progress_callback({"current_pass": f"Searching all books by {author}"})

    query_author = primary_author_name(author)
    any_provider_succeeded = False

    # No single "next book number" to look ahead from here (results can
    # span several different series at once), so pass the plain "<author>
    # new books" query explicitly rather than the lookahead-aware default --
    # see _fetch_all_providers_parallel's docstring.
    fetch_results = _fetch_all_providers_parallel(
        query_author,
        None,
        query_author,
        None,
        author=author,
        openlibrary_query=f'author:"{query_author}"',
        web_search_queries=[f"{query_author} new books"],
    )
    failures = fetch_results["_failures"]

    google_raw = fetch_results["google"]
    if "google" in failures:
        provider_failures.append({"provider": "google_books", "error": str(failures["google"])})
    else:
        any_provider_succeeded = True

    openlibrary_raw = fetch_results["openlibrary"]
    if "openlibrary" in failures:
        provider_failures.append({"provider": "openlibrary", "error": str(failures["openlibrary"])})
    else:
        any_provider_succeeded = True

    hardcover_raw = fetch_results["hardcover"]
    if "hardcover" in failures:
        provider_failures.append({"provider": "hardcover", "error": str(failures["hardcover"])})
    elif hardcover_raw or os.environ.get("HARDCOVER_API_KEY", "").strip():
        any_provider_succeeded = True

    web_search_raw = fetch_results["web"]
    if _web_search_enabled() and _llm_structuring_enabled():
        if "web" in failures:
            provider_failures.append({"provider": "web_search", "error": str(failures["web"])})
        else:
            any_provider_succeeded = True

    fused_candidates = _fuse_and_score_candidates(fetch_results, author, None)
    # Edition-aware collapse -- see _finalize_candidates -- same as
    # discover_candidates_for_series: keeps different-ISBN editions of the
    # same real book (e.g. hardcover vs. audiobook) from being reported as
    # two separate "new by this author" candidates.
    fused_candidates = _finalize_candidates(fused_candidates)
    combined = _filter_and_merge(
        [_unified_candidate_to_raw_dict(candidate) for candidate in fused_candidates],
        author,
        exclude_title_keys,
        confidence="author_wide",
    )

    # Only worth the extra per-title lookups when Hardcover is actually
    # configured -- without a key it would just be a guaranteed-empty call
    # per candidate.
    if os.environ.get("HARDCOVER_API_KEY", "").strip():
        if progress_callback:
            progress_callback({"current_pass": "Filling in series details"})
        combined = _enrich_missing_series_hints(combined, author)
        # Enrichment can newly reveal that a candidate IS the bare series
        # name itself (the "ONSET" case) -- re-run the same stub-listing
        # check the initial merge already applies so those get dropped too.
        combined = [
            c
            for c in combined
            if not (
                looks_like_series_index_entry(
                    str(c.get("title") or ""),
                    c.get("series_name_hint"),
                    str(c.get("isbn13") or "").strip(),
                    bool(c.get("series_number_hint"))
                    or bool(infer_number_from_title(str(c.get("title") or ""), c.get("series_name_hint"))),
                )
                or _title_is_series_variant(
                    str(c.get("title") or ""),
                    c.get("series_name_hint"),
                    str(c.get("isbn13") or "").strip(),
                    c.get("series_number_hint"),
                )
            )
        ]

    all_providers_failed = bool(provider_failures) and not any_provider_succeeded

    if progress_callback:
        progress_callback({"current_pass": "Done", "total": 1, "completed": 1})

    return {
        # See finalize_discovery_output -- deterministic order, transient
        # fusion-internal fields stripped.
        "candidates": finalize_discovery_output(combined),
        "provider_failures": provider_failures,
        "all_providers_failed": all_providers_failed,
    }

