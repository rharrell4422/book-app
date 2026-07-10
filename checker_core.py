from __future__ import annotations

from difflib import SequenceMatcher
import re
from typing import Callable
from urllib.parse import quote_plus

from models import Series
from providers.amazon.asin_series_provider import discover_series_candidates_from_seed_asins
from providers.amazon.html_adapter import extract_amazon_asins_from_search_html
from providers.amazon.product_page_extractor import extract_amazon_product_metadata_from_html

from checker_providers import PROVIDERS, _build_provider_output, _extract_candidates_three_layer, fetch_provider_html_by_name, run_author_discovery_amazon, run_author_discovery_google_html
from checker_rules import (
    _build_canonical_amazon_metadata,
    _classify_candidate_signal,
    _edition_priority,
    _evaluate_hybrid_author_gate,
    _extract_asin_from_value,
    _extract_book_number,
    _micro_filter_reasons,
    _normalize_match_text,
    _passes_amazon_membership_reconciliation,
    _passes_early_author_gate,
    _passes_minimal_scoring,
    _rank_candidate,
    _status_hint_for_amazon,
    _to_canonical_validated_candidate,
    determine_next_book_number,
)


def _log(message: str) -> None:
    print(f"[new_book_checker] {message}", flush=True)


def _provider_ledger_entry(provider_name: str, url: str) -> dict:
    return {
        "provider_name": provider_name,
        "provider_url": url,
        "url": url,
        "http_status": None,
        "html_returned": False,
        "dom_elements_scanned": None,
        "metadata_candidates": None,
        "asin_groups": None,
        "json_blobs_extracted": None,
        "json_book_blobs": None,
        "dom_primary_candidates": 0,
        "json_secondary_candidates": 0,
        "asin_tertiary_candidates": 0,
        "canonical_candidates": 0,
        "classification_valid": 0,
        "classification_invalid": 0,
        "title_pattern_match": False,
        "number_inferred": False,
        "partial_series_match": False,
        "author_match": False,
        "author_weight": 0,
        "author_fallback_used": False,
        "author_fallback_reason": "none",
        "series_author_relaxed": False,
        "asin_present": False,
        "multi_signal_score": 0,
        "accepted_as_missing": False,
        "added_books_count": 0,
        "added_books_asins": [],
        "added_books_titles": [],
        "asin_seed_count": 0,
        "asin_seed_pages_fetched": 0,
        "asin_seed_pages_failed": 0,
        "asin_related_asins": 0,
        "asin_series_candidates": 0,
        "fetch_attempts": 0,
        "header_profile": None,
        "cache_fallback": False,
        "bot_blocked": False,
        "error": None,
    }


def _build_google_author_fallback_url(author_name: str, series_name: str, next_number: int | None) -> str:
    query_parts: list[str] = []

    clean_author = str(author_name or "").strip()
    clean_series = str(series_name or "").strip()
    if clean_author:
        query_parts.append(clean_author)
    if clean_series:
        query_parts.append(clean_series)
    if isinstance(next_number, int) and next_number > 0:
        query_parts.append(f"book {next_number}")

    query_parts.extend(
        [
            "book",
            "novel",
            "release",
            "series",
            "Honour Rae",
            "All The Skills",
        ]
    )

    query = " ".join(part for part in query_parts if str(part).strip()).strip() or "book novel release series Honour Rae All The Skills"
    return f"https://www.google.com/search?q={quote_plus(query)}"


def _print_candidate_extraction(provider_name: str, candidate: dict) -> None:
    title = str(candidate.get("title") or "").strip() or "Unknown title"
    asin = str(candidate.get("asin_or_id") or candidate.get("asin") or "").strip().upper() or "NO-ASIN"
    _log(f"Candidate: {title} {asin}")
    return None


def _normalize_loose_text(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _to_book_number(value) -> int | None:
    if value is None:
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return number


def _candidate_identity_key(candidate: dict) -> str:
    asin = str(candidate.get("asin") or candidate.get("asin_or_id") or "").strip().upper()
    title = _normalize_match_text(str(candidate.get("title") or ""))
    number = str(_to_book_number(candidate.get("book_number")) or "")
    return "|".join([asin, title, number])


def _candidate_text_blob(candidate: dict) -> str:
    fields = (
        candidate.get("title"),
        candidate.get("author"),
        candidate.get("series_name"),
        candidate.get("series"),
        candidate.get("snippet"),
        candidate.get("url"),
        candidate.get("availability"),
        candidate.get("status_hint"),
        candidate.get("category"),
        candidate.get("product_category"),
        candidate.get("department"),
    )
    return " ".join(str(value or "") for value in fields).strip().lower()


def _is_sponsored_or_ui_or_symbol(candidate: dict) -> bool:
    blob = _candidate_text_blob(candidate)
    ui_tokens = (
        "a-section",
        "a-link-normal",
        "a-size-base",
        "celwidget",
        "s-result-item",
        "data-csa",
        "widget",
        "nav-",
    )
    sponsored_tokens = (
        "sponsored",
        "ad feedback",
        "advertisement",
        "promoted",
    )
    symbol_tokens = (
        "slotid",
        "rhf",
        "octopus",
        "search-alias",
        "p13n",
        "amazon internal",
        "internal symbol",
    )
    if any(token in blob for token in ui_tokens):
        return True
    if any(token in blob for token in sponsored_tokens):
        return True
    if any(token in blob for token in symbol_tokens):
        return True
    return False


def _normalize_identity_text(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_canonical_patterns(library_entry) -> list[dict]:
    patterns: list[dict] = []
    series_name = str(getattr(library_entry, "name", "") or "").strip()
    active_books = [
        book for book in (getattr(library_entry, "books", []) or []) if str(getattr(book, "record_status", "active") or "active") != "deleted"
    ]
    for book in active_books:
        title = str(getattr(book, "title", "") or "").strip()
        author = str(getattr(book, "author", "") or "").strip() or str(getattr(library_entry, "author", "") or "").strip()
        number = _to_book_number(getattr(book, "book_number", None) or getattr(book, "series_order", None))
        patterns.append(
            {
                "author": author.casefold(),
                "series": series_name.casefold(),
                "book_number": number,
                "title_pattern": _normalize_loose_text(title),
            }
        )

    for entry in (getattr(library_entry, "canonical_entries", []) or []):
        title = str(getattr(entry, "canonical_title", "") or "").strip()
        author = str(getattr(entry, "canonical_author", "") or "").strip() or str(getattr(library_entry, "author", "") or "").strip()
        number = _to_book_number(getattr(entry, "book_number", None))
        patterns.append(
            {
                "author": author.casefold(),
                "series": series_name.casefold(),
                "book_number": number,
                "title_pattern": _normalize_loose_text(title),
            }
        )
    return patterns


def _author_discovered_candidates_stage(library_entry, raw_books: list[dict]) -> list[dict]:
    series_name = str(getattr(library_entry, "name", "") or "").strip()
    series_author = str(getattr(library_entry, "author", "") or "").strip()
    normalized_series = _normalize_loose_text(series_name)
    known_patterns = {
        str(item.get("title_pattern") or "").strip()
        for item in _extract_canonical_patterns(library_entry)
        if str(item.get("title_pattern") or "").strip()
    }

    plausible: list[dict] = []
    seen_keys: set[str] = set()
    for raw in raw_books:
        if not isinstance(raw, dict):
            continue

        title = str(raw.get("title") or "").strip()
        discovered_id = str(raw.get("asin") or raw.get("asin_or_id") or raw.get("retailer_id") or "").strip().upper()
        asin = discovered_id if re.fullmatch(r"[A-Z0-9]{10}", discovered_id) else ""
        discovered_author = str(raw.get("author") or series_author).strip()
        if not title:
            continue
        if discovered_author != series_author:
            continue

        normalized_title = _normalize_loose_text(title)
        has_series_phrase = bool(normalized_series and normalized_series in normalized_title)
        has_known_pattern = any(pattern in normalized_title or normalized_title in pattern for pattern in known_patterns)
        if not (has_series_phrase or has_known_pattern):
            continue
        dedupe_key = f"{discovered_id}|{_normalize_loose_text(title)}"
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        plausible.append(
            {
                "title": title,
                "asin": asin,
                "asin_or_id": discovered_id,
                "author": discovered_author,
                "cover_url": str(raw.get("cover_url") or "").strip(),
                "publication_date": raw.get("publication_date"),
                "series_text": str(raw.get("series_text") or "").strip(),
                "url": str(raw.get("url") or (f"https://m.amazon.com/dp/{asin}" if asin else "")).strip() or (f"https://m.amazon.com/dp/{asin}" if asin else ""),
                "provider": str(raw.get("provider") or "author_discovery_amazon").strip() or "author_discovery_amazon",
                "source_layer": "author_discovered_candidates",
            }
        )

    return plausible


def _asin_fallback_discovery(library_entry) -> list[dict]:
    fallback_candidates: list[dict] = []
    series_name = str(getattr(library_entry, "name", "") or "").strip()
    series_author = str(getattr(library_entry, "author", "") or "").strip()
    active_books = [
        book for book in (getattr(library_entry, "books", []) or []) if str(getattr(book, "record_status", "active") or "active") != "deleted"
    ]

    seen_asins: set[str] = set()
    for book in active_books:
        asin_value = str(getattr(book, "asin", "") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin_value) or asin_value in seen_asins:
            continue
        seen_asins.add(asin_value)

        product_url = f"https://m.amazon.com/dp/{asin_value}"
        fetch_result = fetch_provider_html_by_name("amazon_asin_series", product_url, amazon_mode="product")
        if not fetch_result.get("ok"):
            fallback_candidates.append(
                {
                    "title": "",
                    "author": series_author,
                    "series_name": series_name,
                    "book_number": None,
                    "asin": asin_value,
                    "asin_or_id": asin_value,
                    "url": product_url,
                    "provider": "amazon_asin_series",
                    "source_layer": "asin_fallback",
                    "status_hint": "unknown",
                    "snippet": str(fetch_result.get("error") or "fetch-failed"),
                    "failure_reason": str(fetch_result.get("error") or "fetch-failed"),
                }
            )
            continue

        product_html = str(fetch_result.get("html") or fetch_result.get("raw_html") or "")
        extracted = extract_amazon_product_metadata_from_html(product_html, product_url, expected_asin=asin_value) or {}
        fallback_candidates.append(
            {
                "title": str(extracted.get("title") or "").strip(),
                "author": str(extracted.get("author") or "").strip(),
                "series_name": str(extracted.get("series_name") or series_name).strip(),
                "book_number": extracted.get("book_number"),
                "asin": str(extracted.get("asin_or_id") or asin_value).strip().upper(),
                "asin_or_id": str(extracted.get("asin_or_id") or asin_value).strip().upper(),
                "isbn": str(extracted.get("isbn") or "").strip(),
                "url": str(extracted.get("url") or product_url).strip() or product_url,
                "provider": "amazon_asin_series",
                "source_layer": "asin_fallback",
                "publication_date": extracted.get("publish_date") or extracted.get("publication_date"),
                "release_date": extracted.get("release_date") or extracted.get("publish_date"),
                "upcoming_date": extracted.get("upcoming_date"),
                "availability": extracted.get("availability"),
                "status_hint": str(extracted.get("availability") or "unknown").strip().lower() or "unknown",
                "snippet": str(extracted.get("failure_reason") or "").strip(),
                "title_selector": extracted.get("title_selector"),
                "expected_asin": extracted.get("expected_asin"),
                "failure_reason": extracted.get("failure_reason"),
            }
        )

    return fallback_candidates


def _title_loosely_matches(candidate_title: str, canonical_title: str) -> bool:
    if not candidate_title or not canonical_title:
        return False
    if candidate_title == canonical_title:
        return True
    if candidate_title in canonical_title or canonical_title in candidate_title:
        return True
    similarity = SequenceMatcher(None, candidate_title, canonical_title).ratio()
    return similarity >= 0.82


def _strict_identity_filter(candidate: dict, library_entry) -> bool:
    if not isinstance(candidate, dict):
        return False

    candidate_author = str(candidate.get("author") or "").strip()
    candidate_series = str(candidate.get("series_name") or candidate.get("series") or "").strip()
    if not candidate_author or not candidate_series:
        return False

    library_author = str(getattr(library_entry, "author", "") or "").strip()
    library_series = str(getattr(library_entry, "name", "") or "").strip()
    if not library_author or not library_series:
        return False

    return _normalize_identity_text(candidate_author) == _normalize_identity_text(library_author) and _normalize_identity_text(candidate_series) == _normalize_identity_text(library_series)


def _matches_any_canonical_pattern(candidate: dict, patterns: list[dict]) -> bool:
    if not patterns:
        return False

    candidate_author = str(candidate.get("author") or "").strip().casefold()
    candidate_series = str(candidate.get("series_name") or candidate.get("series") or "").strip().casefold()
    candidate_number = _to_book_number(candidate.get("book_number"))
    candidate_title_pattern = _normalize_loose_text(str(candidate.get("title") or ""))
    title_text = str(candidate.get("title") or "")

    if not candidate_author or not candidate_series:
        return False

    has_author_series_match = any(
        candidate_author == str(pattern.get("author") or "") and candidate_series == str(pattern.get("series") or "")
        for pattern in patterns
    )
    if not has_author_series_match:
        return False

    if candidate_number is None or candidate_number == 1:
        return True

    metadata_match = re.search(r"\bbook\s*(\d+)\b", title_text, flags=re.IGNORECASE)
    if metadata_match:
        try:
            metadata_number = int(metadata_match.group(1))
        except (TypeError, ValueError):
            return False
        if metadata_number != candidate_number:
            return False

    has_number_match = any(
        candidate_author == str(pattern.get("author") or "")
        and candidate_series == str(pattern.get("series") or "")
        and candidate_number == _to_book_number(pattern.get("book_number"))
        for pattern in patterns
    )
    if not has_number_match:
        return False

    canonical_title_patterns = [str(pattern.get("title_pattern") or "") for pattern in patterns if str(pattern.get("title_pattern") or "")]
    return any(_title_loosely_matches(candidate_title_pattern, pattern) for pattern in canonical_title_patterns)


def _is_garbage(candidate: dict, library_entry=None) -> bool:
    if not isinstance(candidate, dict):
        return True

    candidate_title = str(candidate.get("title") or "").strip()
    candidate_author = str(candidate.get("author") or "").strip()
    candidate_series = str(candidate.get("series_name") or candidate.get("series") or "").strip()
    candidate_number_raw = candidate.get("book_number")
    candidate_number = _to_book_number(candidate_number_raw)

    if not candidate_title:
        return True
    if not candidate_author:
        return True
    if not candidate_series:
        return True

    if library_entry is not None and not _strict_identity_filter(candidate, library_entry):
        return True

    if candidate_number_raw is not None and candidate_number is None:
        return True
    if candidate_number is not None and candidate_number <= 0 and candidate_number != 1:
        return True

    if _is_sponsored_or_ui_or_symbol(candidate):
        return True

    has_metadata = bool(
        str(candidate.get("asin") or candidate.get("asin_or_id") or "").strip()
        or str(candidate.get("isbn") or "").strip()
        or candidate_number is not None
        or str(candidate.get("publication_date") or candidate.get("release_date") or candidate.get("expected_date") or "").strip()
    )
    if not has_metadata:
        return True

    return False


def _apply_strict_post_filtering(candidates: list[dict], library_entry) -> list[dict]:
    return [candidate for candidate in candidates if _strict_identity_filter(candidate, library_entry)]


def _looks_plausibly_real(candidate: dict, library_entry) -> bool:
    if _is_garbage(candidate, library_entry):
        return False
    if _is_sponsored_or_ui_or_symbol(candidate):
        return False

    patterns = _extract_canonical_patterns(library_entry)
    candidate_author = str(candidate.get("author") or "").strip().casefold()
    candidate_series = str(candidate.get("series_name") or candidate.get("series") or "").strip().casefold()
    candidate_number = _to_book_number(candidate.get("book_number"))
    if candidate_number is None:
        return False

    expected_number = determine_next_book_number(library_entry)
    if candidate_number != expected_number:
        return False

    library_author = str(getattr(library_entry, "author", "") or "").strip().casefold()
    library_series = str(getattr(library_entry, "name", "") or "").strip().casefold()
    if candidate_author != library_author:
        return False
    if candidate_series != library_series:
        return False

    candidate_title_pattern = _normalize_loose_text(str(candidate.get("title") or ""))
    canonical_title_patterns = [str(pattern.get("title_pattern") or "") for pattern in patterns if str(pattern.get("title_pattern") or "")]
    has_title_pattern_match = any(_title_loosely_matches(candidate_title_pattern, pattern) for pattern in canonical_title_patterns)
    return not has_title_pattern_match


def _apply_three_tier_filter(candidates: list[dict], library_entry) -> tuple[list[dict], list[dict]]:
    patterns = _extract_canonical_patterns(library_entry)
    accepted: list[dict] = []
    needs_user_confirmation: list[dict] = []

    for candidate in candidates:
        if _is_garbage(candidate, library_entry):
            continue
        if _matches_any_canonical_pattern(candidate, patterns):
            accepted.append(candidate)
            continue
        if _looks_plausibly_real(candidate, library_entry):
            marked = dict(candidate)
            marked["needs_user_confirmation"] = True
            needs_user_confirmation.append(marked)
            continue

    return accepted, needs_user_confirmation


def _emit_three_tier_feature_flags() -> None:
    print("[three_tier] fallback_discovery_enabled=True", flush=True)
    print("[three_tier] strict_post_filtering_enabled=True", flush=True)
    print("[three_tier] canonical_matching_enabled=True", flush=True)
    print("[three_tier] human_override_enabled=True", flush=True)


def check_for_new_book(
    series: Series,
    progress_callback: Callable[[dict], None] | None = None,
    seed_asins: list[str] | None = None,
) -> dict:
    series_id = getattr(series, "id", None)
    series_name = str(getattr(series, "name", "") or "").strip()
    author_name = str(getattr(series, "author", "") or "").strip()
    _log(f"CHECK NOW triggered for series: {series_name}")

    next_number = determine_next_book_number(series)

    if not series_name:
        return {"found": False, "candidate": None}

    ranked: list[tuple[int, dict, str]] = []
    provider_failures: list[dict] = []
    successful_html_count = 0
    amazon_book_candidates: list[dict] = []
    amazon_asin_candidates: list[dict] = []
    seen_amazon_asins: set[str] = set()
    amazon_product_fetch_success = 0
    amazon_product_fetch_failed = 0
    amazon_product_metadata_hits = 0
    first_extracted_product_metadata: dict | None = None
    first_product_extraction_failure: dict | None = None
    validated_candidates: list[dict] = []
    validated_candidate_keys: set[str] = set()
    all_candidates: list[dict] = []
    normalized_seed_asins = sorted(
        {
            str(asin or "").strip().upper()
            for asin in (seed_asins or [])
            if re.fullmatch(r"[A-Z0-9]{10}", str(asin or "").strip().upper())
        }
    )
    has_series_and_author = bool(series_name and author_name)
    amazon_series_page_failed = False
    provider_ledger: list[dict] = []

    author_discovery_result = run_author_discovery_amazon(author_name)
    amazon_author_discovery_ledger_entry = _provider_ledger_entry("author_discovery_amazon", str(author_discovery_result.get("url") or ""))
    amazon_author_discovery_ledger_entry["provider_url"] = str(author_discovery_result.get("url") or "")
    amazon_author_discovery_ledger_entry["author_discovery_called"] = True
    amazon_author_discovery_ledger_entry["http_status"] = author_discovery_result.get("http_status")
    amazon_author_discovery_ledger_entry["fetch_attempts"] = int(author_discovery_result.get("fetch_attempts") or 0)
    amazon_author_discovery_ledger_entry["header_profile"] = author_discovery_result.get("header_profile")
    amazon_author_discovery_ledger_entry["cache_fallback"] = bool(author_discovery_result.get("cache_fallback"))
    amazon_author_discovery_ledger_entry["bot_blocked"] = bool(author_discovery_result.get("bot_blocked"))
    amazon_author_discovery_ledger_entry["html_returned"] = bool(author_discovery_result.get("ok"))

    amazon_author_discovered_raw_books = author_discovery_result.get("books") if isinstance(author_discovery_result.get("books"), list) else []
    raw_discovered_books_count = len(amazon_author_discovered_raw_books)
    amazon_author_discovery_ledger_entry["asin_series_candidates"] = raw_discovered_books_count
    amazon_author_discovery_ledger_entry["author_discovered_books"] = raw_discovered_books_count
    amazon_author_discovery_ledger_entry["raw_discovered_books_count"] = raw_discovered_books_count

    amazon_author_discovery_status = str(author_discovery_result.get("status") or "").strip().lower()
    amazon_author_discovery_unavailable = amazon_author_discovery_status == "unavailable"
    amazon_author_discovery_empty = raw_discovered_books_count == 0

    should_use_google_fallback = (
        amazon_author_discovery_unavailable
        or int(author_discovery_result.get("http_status") or 0) == 503
        or amazon_author_discovery_empty
    )

    google_author_discovery_result = {
        "ok": False,
        "provider": "author_discovery_google_html",
        "url": "",
        "books": [],
        "http_status": None,
        "fetch_attempts": 0,
        "error": None,
    }
    if should_use_google_fallback:
        _log("author discovery fallback: google_html")
        google_fallback_url = _build_google_author_fallback_url(author_name, series_name, next_number)
        google_author_discovery_result = run_author_discovery_google_html(author_name, series_name, query_url=google_fallback_url)

    final_google_author_discovery_result = dict(google_author_discovery_result)

    google_author_discovery_ledger_entry = _provider_ledger_entry("author_discovery_google_html", str(final_google_author_discovery_result.get("url") or ""))
    google_author_discovery_ledger_entry["provider_url"] = str(final_google_author_discovery_result.get("url") or "")
    google_author_discovery_ledger_entry["author_discovery_called"] = bool(should_use_google_fallback)
    google_author_discovery_ledger_entry["http_status"] = final_google_author_discovery_result.get("http_status")
    google_author_discovery_ledger_entry["fetch_attempts"] = int(final_google_author_discovery_result.get("fetch_attempts") or 0)
    google_author_discovery_ledger_entry["html_returned"] = bool(final_google_author_discovery_result.get("ok"))
    google_author_discovery_ledger_entry["linked_pages_fetched"] = int(final_google_author_discovery_result.get("linked_pages_fetched") or 0)

    google_author_discovered_raw_books = (
        final_google_author_discovery_result.get("books") if isinstance(final_google_author_discovery_result.get("books"), list) else []
    )
    google_author_discovery_ledger_entry["asin_series_candidates"] = len(google_author_discovered_raw_books)
    google_author_discovery_ledger_entry["author_discovered_books"] = len(google_author_discovered_raw_books)

    if not amazon_author_discovered_raw_books and not google_author_discovered_raw_books:
        _log("author discovery unavailable")
        amazon_author_discovery_ledger_entry["status"] = "unavailable"
        amazon_author_discovery_ledger_entry["error"] = str(author_discovery_result.get("error") or "author discovery unavailable")
    else:
        amazon_author_discovery_ledger_entry["status"] = "success"

    if should_use_google_fallback:
        google_author_discovery_ledger_entry["status"] = "success" if google_author_discovered_raw_books else "unavailable"
        if not google_author_discovered_raw_books:
            google_author_discovery_ledger_entry["error"] = str(final_google_author_discovery_result.get("error") or "author discovery unavailable")
    else:
        google_author_discovery_ledger_entry["status"] = "skipped"

    author_discovered_raw_books = list(amazon_author_discovered_raw_books) + list(google_author_discovered_raw_books)
    author_discovered_candidates = _author_discovered_candidates_stage(series, author_discovered_raw_books)

    author_discovered_candidates_by_provider: dict[str, list[dict]] = {
        "author_discovery_amazon": [],
        "author_discovery_google_html": [],
    }
    for item in author_discovered_candidates:
        provider_name = str(item.get("provider") or "").strip()
        if provider_name in author_discovered_candidates_by_provider:
            author_discovered_candidates_by_provider[provider_name].append(item)

    amazon_author_discovery_ledger_entry["classification_valid"] = len(author_discovered_candidates_by_provider.get("author_discovery_amazon") or [])
    amazon_author_discovery_ledger_entry["author_discovered_plausible_candidates"] = len(author_discovered_candidates_by_provider.get("author_discovery_amazon") or [])
    google_author_discovery_ledger_entry["classification_valid"] = len(author_discovered_candidates_by_provider.get("author_discovery_google_html") or [])
    google_author_discovery_ledger_entry["author_discovered_plausible_candidates"] = len(author_discovered_candidates_by_provider.get("author_discovery_google_html") or [])

    provider_ledger.append(amazon_author_discovery_ledger_entry)
    provider_ledger.append(google_author_discovery_ledger_entry)

    for provider in PROVIDERS:
        provider_ledger_entry = _provider_ledger_entry(provider.name, "")
        if provider.name == "amazon_books" and has_series_and_author and not amazon_series_page_failed:
            continue

        query_url: str | None = None
        try:
            query_url = provider.url_builder(series_name, author_name, next_number)
            provider_ledger_entry["url"] = query_url
            if provider.name == "amazon_asin_series":
                provider_ledger_entry["asin_seed_count"] = len(normalized_seed_asins)
                if not normalized_seed_asins:
                    provider_ledger_entry["status"] = "skipped"
                    provider_ledger_entry["error"] = "no-seed-asins"
                    continue

                asin_series_result = discover_series_candidates_from_seed_asins(
                    seed_asins=normalized_seed_asins,
                    series_name=series_name,
                    author_name=author_name,
                    fetch_product_html=lambda product_url: fetch_provider_html_by_name("amazon_books", product_url, amazon_mode="product"),
                )
                parsed_candidates = asin_series_result.get("candidates") if isinstance(asin_series_result.get("candidates"), list) else []
                asin_series_metrics = asin_series_result.get("metrics") if isinstance(asin_series_result.get("metrics"), dict) else {}

                provider_ledger_entry["asin_seed_count"] = int(asin_series_metrics.get("asin_seed_count") or len(normalized_seed_asins))
                provider_ledger_entry["asin_seed_pages_fetched"] = int(asin_series_metrics.get("asin_seed_pages_fetched") or 0)
                provider_ledger_entry["asin_seed_pages_failed"] = int(asin_series_metrics.get("asin_seed_pages_failed") or 0)
                provider_ledger_entry["asin_related_asins"] = int(asin_series_metrics.get("asin_related_asins") or 0)
                provider_ledger_entry["asin_series_candidates"] = int(asin_series_metrics.get("asin_series_candidates") or 0)
                provider_ledger_entry["html_returned"] = provider_ledger_entry["asin_seed_pages_fetched"] > 0
                provider_ledger_entry["canonical_candidates"] = len(parsed_candidates)

                for parsed_candidate in parsed_candidates:
                    if isinstance(parsed_candidate, dict):
                        parsed_candidate.setdefault("provider", provider.name)

                for candidate in parsed_candidates:
                    candidate_author = str(candidate.get("author") or "").strip()
                    candidate_title = str(candidate.get("title") or "").strip()
                    candidate_series_name = str(candidate.get("series_name") or "").strip()
                    allow_candidate, used_fallback, gate_reason = _evaluate_hybrid_author_gate(
                        target_author=author_name,
                        candidate_author=candidate_author,
                        target_series_name=series_name,
                        candidate_title=candidate_title,
                        candidate_series_name=candidate_series_name,
                        provider_name=provider.name,
                    )
                    if not allow_candidate:
                        provider_ledger_entry["classification_invalid"] += 1
                        continue

                    classification = _classify_candidate_signal(candidate, series_name, author_name)
                    if classification == "invalid":
                        provider_ledger_entry["classification_invalid"] += 1
                        continue
                    candidate["status"] = classification
                    candidate["status_hint"] = classification

                    score = _rank_candidate(candidate, series_name, author_name, next_number, provider.name)
                    ranked.append((score, candidate, provider.name))
                    provider_ledger_entry["classification_valid"] += 1

                    canonical_validated = _to_canonical_validated_candidate(candidate, provider.name, series_name)
                    candidate_key = "|".join(
                        [
                            str(canonical_validated.get("asin") or "").strip().upper(),
                            _normalize_match_text(str(canonical_validated.get("title") or "")),
                            str(canonical_validated.get("series_number") if canonical_validated.get("series_number") is not None else ""),
                        ]
                    )
                    if candidate_key not in validated_candidate_keys:
                        validated_candidate_keys.add(candidate_key)
                        validated_candidates.append(canonical_validated)

                provider_ledger_entry["status"] = "success"
                continue

            if provider.name in {"amazon_books", "amazon_series_page"}:
                _log(f"Amazon query URL: {query_url}")
            else:
                _log(f"Provider query URL for {provider.name}: {query_url}")

            fetch_result = fetch_provider_html_by_name(provider.name, query_url)
            provider_ledger_entry["http_status"] = fetch_result.get("status_code")
            provider_ledger_entry["fetch_attempts"] = int(fetch_result.get("fetch_attempts") or 0)
            provider_ledger_entry["header_profile"] = fetch_result.get("header_profile")
            provider_ledger_entry["cache_fallback"] = bool(fetch_result.get("cache_fallback"))
            provider_ledger_entry["bot_blocked"] = bool(fetch_result.get("bot_blocked"))
            if not fetch_result.get("ok"):
                if provider.name == "amazon_series_page":
                    amazon_series_page_failed = True
                error_message = str(fetch_result.get("error") or "no-html")
                provider_ledger_entry["error"] = error_message
                provider_ledger_entry["status"] = "failure"
                provider_failures.append(
                    {
                        "provider": provider.name,
                        "query": query_url,
                        "error": error_message,
                    }
                )
                continue

            html = str(fetch_result.get("raw_html") or fetch_result.get("html") or "")
            provider_ledger_entry["html_returned"] = bool(html)
            _log(f"Provider {provider.name} returned HTML")
            successful_html_count += 1
            if provider.name == "amazon_series_page":
                amazon_series_page_failed = False

            provider_output = _build_provider_output(provider, html, series_name)
            extraction_result = _extract_candidates_three_layer(provider_output, series_name)
            provider_metrics = extraction_result.get("metrics") if isinstance(extraction_result.get("metrics"), dict) else {}
            provider_ledger_entry["dom_elements_scanned"] = provider_metrics.get("dom_elements_scanned")
            provider_ledger_entry["metadata_candidates"] = provider_metrics.get("metadata_candidates")
            provider_ledger_entry["asin_groups"] = provider_metrics.get("asin_groups")
            provider_ledger_entry["json_blobs_extracted"] = provider_metrics.get("json_blobs_extracted")
            provider_ledger_entry["json_book_blobs"] = provider_metrics.get("json_book_blobs")
            provider_ledger_entry["dom_primary_candidates"] = provider_metrics.get("dom_primary_candidates")
            provider_ledger_entry["json_secondary_candidates"] = provider_metrics.get("json_secondary_candidates")
            provider_ledger_entry["asin_tertiary_candidates"] = provider_metrics.get("asin_tertiary_candidates")

            parsed_candidates = extraction_result.get("merged") or []

            if provider.name in {"amazon_books", "amazon_series_page"}:
                asin_hits = extract_amazon_asins_from_search_html(html, series_name)
                search_page_candidates = parsed_candidates
                amazon_book_candidates = []
                amazon_asin_candidates = []

                search_page_author_by_asin: dict[str, str] = {}
                search_page_title_by_asin: dict[str, str] = {}
                search_page_series_by_asin: dict[str, str] = {}
                for search_candidate in search_page_candidates:
                    search_asin = _extract_asin_from_value(
                        str(search_candidate.get("asin_or_id") or search_candidate.get("asin") or search_candidate.get("url") or "")
                    )
                    if not search_asin:
                        continue
                    search_author = str(search_candidate.get("author") or "").strip()
                    search_title = str(search_candidate.get("title") or "").strip()
                    search_series = str(search_candidate.get("series_name") or "").strip()
                    if search_asin not in search_page_author_by_asin:
                        search_page_author_by_asin[search_asin] = search_author
                    if search_asin not in search_page_title_by_asin:
                        search_page_title_by_asin[search_asin] = search_title
                    if search_asin not in search_page_series_by_asin:
                        search_page_series_by_asin[search_asin] = search_series

                for asin_hit in asin_hits:
                    asin_value = str(asin_hit or "").strip().upper()
                    if not asin_value or asin_value in seen_amazon_asins:
                        continue

                    predicted_author = str(search_page_author_by_asin.get(asin_value) or "").strip()
                    predicted_title = str(search_page_title_by_asin.get(asin_value) or "").strip()
                    predicted_series_name = str(search_page_series_by_asin.get(asin_value) or "").strip()

                    allow_candidate, used_fallback, gate_reason = _evaluate_hybrid_author_gate(
                        target_author=author_name,
                        candidate_author=predicted_author,
                        target_series_name=series_name,
                        candidate_title=predicted_title,
                        candidate_series_name=predicted_series_name,
                        provider_name=provider.name,
                    )
                    if not allow_candidate:
                        provider_ledger_entry["classification_invalid"] += 1
                        if progress_callback is not None:
                            progress_callback(
                                {
                                    "total": len(asin_hits),
                                    "completed": 0,
                                    "current_book_number": None,
                                    "current_pass": "amazon-early-author-gate",
                                    "current_asin": asin_value,
                                    "asins_discovered": len(asin_hits),
                                    "asins_processed": 0,
                                    "asin_fetch_success": amazon_product_fetch_success,
                                    "asin_fetch_failed": amazon_product_fetch_failed,
                                }
                            )
                        continue

                    seen_amazon_asins.add(asin_value)
                    amazon_asin_candidates.append(
                        {
                            "asin": asin_value,
                            "title": predicted_title,
                            "author": predicted_author,
                            "series_name": predicted_series_name,
                            "used_title_fallback": used_fallback,
                            "early_gate_reason": gate_reason,
                            "url": f"https://m.amazon.com/dp/{asin_value}",
                        }
                    )

                for discovered_candidate in author_discovered_candidates:
                    discovered_asin = str(discovered_candidate.get("asin") or discovered_candidate.get("asin_or_id") or "").strip().upper()
                    if not re.fullmatch(r"[A-Z0-9]{10}", discovered_asin):
                        continue
                    if discovered_asin in seen_amazon_asins:
                        continue

                    seen_amazon_asins.add(discovered_asin)
                    amazon_asin_candidates.append(
                        {
                            "asin": discovered_asin,
                            "title": str(discovered_candidate.get("title") or "").strip(),
                            "author": str(discovered_candidate.get("author") or author_name).strip(),
                            "series_name": series_name,
                            "used_title_fallback": False,
                            "early_gate_reason": "author_discovery_stage",
                            "url": str(discovered_candidate.get("url") or f"https://m.amazon.com/dp/{discovered_asin}").strip() or f"https://m.amazon.com/dp/{discovered_asin}",
                            "source_layer": "author_discovered_candidates",
                        }
                    )

                if provider.name == "amazon_books":
                    for discovered_candidate in author_discovered_candidates:
                        discovered_asin = str(discovered_candidate.get("asin") or "").strip().upper()
                        if discovered_asin and re.fullmatch(r"[A-Z0-9]{10}", discovered_asin):
                            continue
                        parsed_candidates.append(
                            {
                                "title": str(discovered_candidate.get("title") or "").strip(),
                                "author": str(discovered_candidate.get("author") or author_name).strip(),
                                "series_name": series_name,
                                "book_number": None,
                                "url": str(discovered_candidate.get("url") or "").strip(),
                                "snippet": str(discovered_candidate.get("series_text") or "").strip(),
                                "publication_date": discovered_candidate.get("publication_date"),
                                "expected_date": None,
                                "status_hint": "unknown",
                                "asin_or_id": str(discovered_candidate.get("asin_or_id") or "").strip(),
                                "source_layer": "author_discovered_candidates",
                                "provider": str(discovered_candidate.get("provider") or "author_discovery_google_html").strip(),
                            }
                        )

                if progress_callback is not None:
                    progress_callback(
                        {
                            "total": len(amazon_asin_candidates),
                            "completed": 0,
                            "current_book_number": None,
                            "current_pass": "amazon-product-fetch",
                            "current_asin": None,
                            "asins_discovered": len(amazon_asin_candidates),
                            "asins_processed": 0,
                            "asin_fetch_success": 0,
                            "asin_fetch_failed": 0,
                        }
                    )

                seen_amazon_book_keys: set[tuple[str, str]] = set()
                pending_canonical_candidates: list[dict] = []
                for index, asin_hit in enumerate(amazon_asin_candidates, start=1):
                    asin_value = str(asin_hit.get("asin") or "").strip().upper()
                    product_url = f"https://m.amazon.com/dp/{asin_value}"

                    product_fetch_result = fetch_provider_html_by_name("amazon_books", product_url, amazon_mode="product")
                    if not product_fetch_result.get("ok"):
                        amazon_product_fetch_failed += 1
                        provider_failures.append(
                            {
                                "provider": "amazon_books_product",
                                "query": product_url,
                                "error": str(product_fetch_result.get("error") or "no-html"),
                            }
                        )
                        if progress_callback is not None:
                            progress_callback(
                                {
                                    "total": len(amazon_asin_candidates),
                                    "completed": index,
                                    "current_book_number": None,
                                    "current_pass": "amazon-product-fetch",
                                    "current_asin": asin_value,
                                    "asins_discovered": len(amazon_asin_candidates),
                                    "asins_processed": index,
                                    "asin_fetch_success": amazon_product_fetch_success,
                                    "asin_fetch_failed": amazon_product_fetch_failed,
                                }
                            )
                        continue

                    amazon_product_fetch_success += 1
                    product_html = str(product_fetch_result.get("html") or "")
                    metadata_candidate = extract_amazon_product_metadata_from_html(
                        product_html,
                        product_url,
                        expected_asin=asin_value,
                    )

                    failure_reason = str(metadata_candidate.get("failure_reason") or "").strip()
                    if failure_reason:
                        if first_product_extraction_failure is None:
                            first_product_extraction_failure = {
                                "failure_reason": failure_reason,
                                "asin_or_id": str(metadata_candidate.get("asin_or_id") or asin_value).strip().upper(),
                                "expected_asin": str(metadata_candidate.get("expected_asin") or asin_value).strip().upper(),
                                "title_selector": metadata_candidate.get("title_selector"),
                                "url": str(metadata_candidate.get("url") or product_url).strip() or product_url,
                            }
                    else:
                        if first_extracted_product_metadata is None:
                            first_extracted_product_metadata = {
                                "title": metadata_candidate.get("title"),
                                "author": metadata_candidate.get("author"),
                                "asin_or_id": metadata_candidate.get("asin_or_id"),
                                "series_name": metadata_candidate.get("series_name"),
                                "book_number": metadata_candidate.get("book_number"),
                                "publish_date": metadata_candidate.get("publish_date"),
                                "upcoming_date": metadata_candidate.get("upcoming_date"),
                                "availability": metadata_candidate.get("availability"),
                                "title_selector": metadata_candidate.get("title_selector"),
                                "url": metadata_candidate.get("url"),
                            }

                        canonical, normalization_reasons = _build_canonical_amazon_metadata(
                            target_series_name=series_name,
                            metadata_candidate={
                                **metadata_candidate,
                                "asin_or_id": str(metadata_candidate.get("asin_or_id") or asin_value).strip().upper(),
                                "url": str(metadata_candidate.get("url") or product_url).strip() or product_url,
                            },
                        )
                        if canonical is None:
                            provider_ledger_entry["classification_invalid"] += 1
                            continue
                        if normalization_reasons and provider.name not in {"amazon_books", "amazon_series_page"}:
                            provider_ledger_entry["classification_invalid"] += 1
                            continue

                        candidate_asin = str(canonical.get("asin_or_id") or asin_value).strip().upper()
                        title = str(canonical.get("title") or "").strip()
                        author = str(canonical.get("author") or "").strip()
                        extracted_series_name = str(canonical.get("series_name") or "").strip()
                        resolved_book_number = canonical.get("book_number")
                        publish_date = canonical.get("publish_date")
                        upcoming_date = canonical.get("upcoming_date")
                        availability = str(canonical.get("availability") or "").strip().lower()
                        release_date = canonical.get("release_date")
                        candidate_url = str(canonical.get("url") or product_url).strip() or product_url

                        if provider.name not in {"amazon_books", "amazon_series_page"} and not _passes_early_author_gate(author_name, author):
                            provider_ledger_entry["classification_invalid"] += 1
                            continue

                        is_member_match, membership_reasons = _passes_amazon_membership_reconciliation(
                            target_series_name=series_name,
                            target_author_name=author_name,
                            expected_next_number=next_number,
                            title=title,
                            extracted_series_name=extracted_series_name,
                            extracted_author=author,
                            extracted_book_number=resolved_book_number,
                        )
                        if provider.name not in {"amazon_books", "amazon_series_page"} and not is_member_match:
                            provider_ledger_entry["classification_invalid"] += 1
                            continue

                        dedupe_key = (candidate_asin or asin_value, title.lower())
                        if dedupe_key in seen_amazon_book_keys:
                            continue
                        seen_amazon_book_keys.add(dedupe_key)
                        pending_canonical_candidates.append(canonical)

                    if progress_callback is not None:
                        progress_callback(
                            {
                                "total": len(amazon_asin_candidates),
                                "completed": index,
                                "current_book_number": None,
                                "current_pass": "amazon-product-fetch",
                                "current_asin": asin_value,
                                "asins_discovered": len(amazon_asin_candidates),
                                "asins_processed": index,
                                "asin_fetch_success": amazon_product_fetch_success,
                                "asin_fetch_failed": amazon_product_fetch_failed,
                            }
                        )

                merged_by_key: dict[str, dict] = {}
                for canonical in pending_canonical_candidates:
                    key = str(canonical.get("canonical_key") or "").strip()
                    if not key:
                        key = f"asin:{canonical.get('asin_or_id') or ''}"

                    current = merged_by_key.get(key)
                    if current is None:
                        merged_by_key[key] = canonical
                        continue

                    current_priority = _edition_priority(str(current.get("edition_type") or "unknown"))
                    candidate_priority = _edition_priority(str(canonical.get("edition_type") or "unknown"))
                    current_date_score = 1 if current.get("publish_date") else 0
                    candidate_date_score = 1 if canonical.get("publish_date") else 0

                    if (candidate_priority, candidate_date_score) > (current_priority, current_date_score):
                        merged_by_key[key] = canonical

                for canonical in merged_by_key.values():
                    amazon_product_metadata_hits += 1

                    normalized_book_candidate = {
                        "title": canonical.get("title"),
                        "author": canonical.get("author"),
                        "asin_or_id": canonical.get("asin_or_id"),
                        "release_date": canonical.get("release_date"),
                        "series_name": canonical.get("series_name"),
                        "book_number": canonical.get("book_number"),
                        "publish_date": canonical.get("publish_date"),
                        "upcoming_date": canonical.get("upcoming_date"),
                        "availability": canonical.get("availability") or "unknown",
                        "url": canonical.get("url"),
                        "edition_type": canonical.get("edition_type"),
                        "title_selector": canonical.get("title_selector"),
                    }
                    amazon_book_candidates.append(normalized_book_candidate)

                    availability = str(canonical.get("availability") or "").strip().lower()
                    if availability == "upcoming":
                        status_hint = "upcoming"
                    elif availability == "available":
                        status_hint = "available"
                    else:
                        status_hint = _status_hint_for_amazon(
                            f"{canonical.get('title') or ''} {canonical.get('author') or ''}".strip(),
                            canonical.get("release_date"),
                        )

                    parsed_candidates.append(
                        {
                            "title": canonical.get("title"),
                            "author": canonical.get("author"),
                            "series_name": canonical.get("series_name"),
                            "book_number": canonical.get("book_number") if canonical.get("book_number") is not None else _extract_book_number(str(canonical.get("title") or "")),
                            "url": canonical.get("url"),
                            "snippet": "",
                            "publication_date": canonical.get("publish_date"),
                            "expected_date": canonical.get("upcoming_date"),
                            "status_hint": status_hint,
                            "asin_or_id": canonical.get("asin_or_id"),
                            "canonical_metadata": {
                                "title_normalized": canonical.get("title"),
                                "series_name_normalized": canonical.get("series_name"),
                                "book_number_normalized": canonical.get("book_number"),
                                "publish_date_normalized": canonical.get("publish_date"),
                                "upcoming_date_normalized": canonical.get("upcoming_date"),
                                "availability": canonical.get("availability"),
                                "edition_type": canonical.get("edition_type"),
                                "title_selector": canonical.get("title_selector"),
                            },
                        }
                    )
            provider_ledger_entry["canonical_candidates"] = len(parsed_candidates)

            for parsed_candidate in parsed_candidates:
                if isinstance(parsed_candidate, dict):
                    parsed_candidate.setdefault("provider", provider.name)

            for candidate in parsed_candidates:
                candidate_author = str(candidate.get("author") or "").strip()
                candidate_title = str(candidate.get("title") or "").strip()
                candidate_series_name = str(candidate.get("series_name") or "").strip()
                allow_candidate, used_fallback, gate_reason = _evaluate_hybrid_author_gate(
                    target_author=author_name,
                    candidate_author=candidate_author,
                    target_series_name=series_name,
                    candidate_title=candidate_title,
                    candidate_series_name=candidate_series_name,
                    provider_name=provider.name,
                )
                if not allow_candidate:
                    provider_ledger_entry["classification_invalid"] += 1
                    continue

                if used_fallback and provider.name not in {"amazon_books", "amazon_series_page"}:
                    provider_ledger_entry["classification_invalid"] += 1
                    continue

                classification = _classify_candidate_signal(candidate, series_name, author_name)
                if classification == "invalid":
                    provider_ledger_entry["classification_invalid"] += 1
                    continue
                candidate["status"] = classification
                candidate["status_hint"] = classification

                reasons = _micro_filter_reasons(candidate, series_name, provider.source_type)
                if reasons:
                    provider_ledger_entry["classification_invalid"] += 1
                    continue

                score = _rank_candidate(candidate, series_name, author_name, next_number, provider.name)
                ranked.append((score, candidate, provider.name))
                provider_ledger_entry["classification_valid"] += 1

                canonical_validated = _to_canonical_validated_candidate(candidate, provider.name, series_name)
                candidate_key = "|".join(
                    [
                        str(canonical_validated.get("asin") or "").strip().upper(),
                        _normalize_match_text(str(canonical_validated.get("title") or "")),
                        str(canonical_validated.get("series_number") if canonical_validated.get("series_number") is not None else ""),
                    ]
                )
                if candidate_key not in validated_candidate_keys:
                    validated_candidate_keys.add(candidate_key)
                    validated_candidates.append(canonical_validated)
                    all_candidates.append(canonical_validated)

            provider_ledger_entry["status"] = "success"
        except Exception as exc:
            provider_ledger_entry["status"] = "failure"
            provider_ledger_entry["error"] = f"{type(exc).__name__}: {exc}"
            provider_ledger_entry["html_returned"] = False
            provider_ledger_entry["dom_elements_scanned"] = None
            provider_ledger_entry["metadata_candidates"] = None
            provider_ledger_entry["asin_groups"] = None
            provider_ledger_entry["json_blobs_extracted"] = None
            provider_ledger_entry["json_book_blobs"] = None
            provider_ledger_entry["canonical_candidates"] = 0
            provider_ledger_entry["classification_valid"] = 0
            provider_ledger_entry["classification_invalid"] = 0
            provider_failures.append(
                {
                    "provider": provider.name,
                    "query": provider_ledger_entry.get("url") or query_url or "",
                    "error": provider_ledger_entry["error"],
                }
            )
        finally:
            provider_ledger.append(provider_ledger_entry)

    fallback_candidates = _asin_fallback_discovery(series)
    all_candidates.extend(fallback_candidates)

    filtered_candidates = _apply_strict_post_filtering(all_candidates, series)
    patterns = _extract_canonical_patterns(series)

    accepted_candidates: list[dict] = []
    needs_user_confirmation: list[dict] = []
    for candidate in filtered_candidates:
        if _matches_any_canonical_pattern(candidate, patterns):
            accepted_candidates.append(candidate)
        elif _looks_plausibly_real(candidate, series):
            marked = dict(candidate)
            marked["needs_user_confirmation"] = True
            needs_user_confirmation.append(marked)

    accepted_keys = {_candidate_identity_key(item) for item in accepted_candidates}
    ranked = [item for item in ranked if _candidate_identity_key(item[1]) in accepted_keys]
    validated_candidates = accepted_candidates

    validated_ids = {
        str(item.get("asin") or item.get("asin_or_id") or "").strip().upper()
        for item in validated_candidates
        if str(item.get("asin") or item.get("asin_or_id") or "").strip()
    }
    discovered_ids_by_provider = {
        "author_discovery_amazon": {
            str(item.get("asin") or item.get("asin_or_id") or "").strip().upper()
            for item in author_discovered_candidates_by_provider.get("author_discovery_amazon") or []
            if str(item.get("asin") or item.get("asin_or_id") or "").strip()
        },
        "author_discovery_google_html": {
            str(item.get("asin") or item.get("asin_or_id") or "").strip().upper()
            for item in author_discovered_candidates_by_provider.get("author_discovery_google_html") or []
            if str(item.get("asin") or item.get("asin_or_id") or "").strip()
        },
    }

    amazon_author_discovery_accepted_count = sum(
        1 for identifier in discovered_ids_by_provider["author_discovery_amazon"] if identifier in validated_ids
    )
    google_author_discovery_accepted_count = sum(
        1 for identifier in discovered_ids_by_provider["author_discovery_google_html"] if identifier in validated_ids
    )

    amazon_author_discovery_rejected_count = max(
        0,
        len(author_discovered_candidates_by_provider.get("author_discovery_amazon") or []) - amazon_author_discovery_accepted_count,
    )
    google_author_discovery_rejected_count = max(
        0,
        len(author_discovered_candidates_by_provider.get("author_discovery_google_html") or []) - google_author_discovery_accepted_count,
    )

    amazon_author_discovery_ledger_entry["added_books_count"] = amazon_author_discovery_accepted_count
    amazon_author_discovery_ledger_entry["classification_invalid"] = amazon_author_discovery_rejected_count
    amazon_author_discovery_ledger_entry["author_discovery_accepted"] = amazon_author_discovery_accepted_count
    amazon_author_discovery_ledger_entry["author_discovery_rejected"] = amazon_author_discovery_rejected_count

    google_author_discovery_ledger_entry["added_books_count"] = google_author_discovery_accepted_count
    google_author_discovery_ledger_entry["classification_invalid"] = google_author_discovery_rejected_count
    google_author_discovery_ledger_entry["author_discovery_accepted"] = google_author_discovery_accepted_count
    google_author_discovery_ledger_entry["author_discovery_rejected"] = google_author_discovery_rejected_count

    author_discovery_accepted_count = amazon_author_discovery_accepted_count + google_author_discovery_accepted_count
    author_discovery_rejected_count = amazon_author_discovery_rejected_count + google_author_discovery_rejected_count

    missing_books = [item for item in validated_candidates if str(item.get("status") or "").strip().lower() == "published"]
    upcoming_books = [item for item in validated_candidates if str(item.get("status") or "").strip().lower() == "upcoming"]

    _emit_three_tier_feature_flags()

    if ranked:
        ranked.sort(
            key=lambda item: (
                item[0],
                1 if _passes_minimal_scoring(item[1], series_name, author_name, next_number) else 0,
            ),
            reverse=True,
        )
        best_score, best_candidate, best_provider = ranked[0]
        final_classification = str(best_candidate.get("status") or "published").strip().lower()
        _log(f"CHECK NOW completed successfully for series: {series_name}")
        return {
            "found": True,
            "candidate": {
                "title": str(best_candidate.get("title") or "").strip(),
                "author": str(best_candidate.get("author") or author_name).strip(),
                "number": str(best_candidate.get("book_number") or "").strip(),
                "url": str(best_candidate.get("url") or "").strip(),
                "provider": best_provider,
                "publication_date": best_candidate.get("publication_date"),
                "expected_date": best_candidate.get("expected_date"),
                "status_hint": best_candidate.get("status_hint"),
                "asin_or_id": best_candidate.get("asin_or_id"),
            },
            "provider_failures": provider_failures,
            "all_providers_failed": False,
            "amazon_book_candidates": amazon_book_candidates,
            "amazon_asin_candidates": amazon_asin_candidates,
            "asin_discovery": {
                "discovered": len(amazon_asin_candidates),
                "processed": len(amazon_asin_candidates),
                "fetch_success": amazon_product_fetch_success,
                "fetch_failed": amazon_product_fetch_failed,
                "metadata_hits": amazon_product_metadata_hits,
            },
            "missing_books": missing_books,
            "upcoming_books": upcoming_books,
            "validated_candidates": validated_candidates,
            "accepted_candidates": accepted_candidates,
            "needs_user_confirmation": needs_user_confirmation,
            "first_extracted_product_metadata": first_extracted_product_metadata,
            "first_product_extraction_failure": first_product_extraction_failure,
            "provider_ledger": provider_ledger,
            "author_discovery": {
                "called": True,
                "discovered": len(author_discovered_raw_books),
                "plausible": len(author_discovered_candidates),
                "accepted": author_discovery_accepted_count,
                "rejected": author_discovery_rejected_count,
                "author_discovery_amazon": {
                    "called": True,
                    "discovered": len(amazon_author_discovered_raw_books),
                    "plausible": len(author_discovered_candidates_by_provider.get("author_discovery_amazon") or []),
                    "accepted": amazon_author_discovery_accepted_count,
                    "rejected": amazon_author_discovery_rejected_count,
                },
                "author_discovery_google_html": {
                    "called": bool(should_use_google_fallback),
                    "discovered": len(google_author_discovered_raw_books),
                    "plausible": len(author_discovered_candidates_by_provider.get("author_discovery_google_html") or []),
                    "accepted": google_author_discovery_accepted_count,
                    "rejected": google_author_discovery_rejected_count,
                },
            },
        }

    return {
        "found": False,
        "candidate": None,
        "provider_failures": provider_failures,
        "all_providers_failed": successful_html_count == 0,
        "amazon_book_candidates": amazon_book_candidates,
        "amazon_asin_candidates": amazon_asin_candidates,
        "asin_discovery": {
            "discovered": len(amazon_asin_candidates),
            "processed": len(amazon_asin_candidates),
            "fetch_success": amazon_product_fetch_success,
            "fetch_failed": amazon_product_fetch_failed,
            "metadata_hits": amazon_product_metadata_hits,
        },
        "missing_books": missing_books,
        "upcoming_books": upcoming_books,
        "validated_candidates": validated_candidates,
        "accepted_candidates": accepted_candidates,
        "needs_user_confirmation": needs_user_confirmation,
        "first_extracted_product_metadata": first_extracted_product_metadata,
        "first_product_extraction_failure": first_product_extraction_failure,
        "provider_ledger": provider_ledger,
        "author_discovery": {
            "called": True,
            "discovered": len(author_discovered_raw_books),
            "plausible": len(author_discovered_candidates),
            "accepted": author_discovery_accepted_count,
            "rejected": author_discovery_rejected_count,
            "author_discovery_amazon": {
                "called": True,
                "discovered": len(amazon_author_discovered_raw_books),
                "plausible": len(author_discovered_candidates_by_provider.get("author_discovery_amazon") or []),
                "accepted": amazon_author_discovery_accepted_count,
                "rejected": amazon_author_discovery_rejected_count,
            },
            "author_discovery_google_html": {
                "called": bool(should_use_google_fallback),
                "discovered": len(google_author_discovered_raw_books),
                "plausible": len(author_discovered_candidates_by_provider.get("author_discovery_google_html") or []),
                "accepted": google_author_discovery_accepted_count,
                "rejected": google_author_discovery_rejected_count,
            },
        },
    }
