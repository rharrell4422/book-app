from __future__ import annotations

import logging
import re
from datetime import date

from sqlalchemy.orm import Session

from models import Book, Series
from new_book_checker import check_for_new_book


logger = logging.getLogger(__name__)

# Minimum combined signal score (title/series/number/asin overlap, see
# _build_multi_signal_diagnostics) required before a discovered candidate is
# accepted as a genuinely missing/upcoming book for ANY series. This must stay
# series-agnostic -- do not add series-, author-, or ASIN-specific carve-outs
# here. If a specific series is producing false positives/negatives, fix the
# general signal logic below instead.
MIN_ACCEPTANCE_SCORE = 6


def _console_log(message: str) -> None:
    print(f"[series_agent] {message}", flush=True)


def _condense_provider_ledger(provider_ledger: list[dict]) -> list[str]:
    """Turn the detailed per-provider ledger (30+ fields each) into ONE short
    line per provider, so the total log size doesn't grow with how much raw
    scraping happened. Full detail is still available in the returned result
    dict for anything that needs it (e.g. an API response) -- this is just
    for what gets printed to the console.
    """
    lines: list[str] = []
    for entry in provider_ledger:
        parts = [f"provider={entry.get('provider_name')}", f"status={entry.get('status')}"]

        http_status = entry.get("http_status")
        if http_status:
            parts.append(f"http={http_status}")
        if entry.get("bot_blocked"):
            parts.append("bot_blocked=yes")
        if entry.get("cache_fallback"):
            parts.append("cached=yes")

        candidates = entry.get("canonical_candidates") or 0
        valid = entry.get("classification_valid") or 0
        invalid = entry.get("classification_invalid") or 0
        if candidates or valid or invalid:
            parts.append(f"candidates={candidates} valid={valid} invalid={invalid}")

        discovered_books = entry.get("author_discovered_books")
        if discovered_books:
            parts.append(f"discovered={discovered_books}")

        asin_seed_count = entry.get("asin_seed_count") or 0
        if asin_seed_count:
            parts.append(
                f"asin_seeds={asin_seed_count} "
                f"pages_ok={entry.get('asin_seed_pages_fetched') or 0} "
                f"pages_failed={entry.get('asin_seed_pages_failed') or 0}"
            )

        if entry.get("accepted_as_missing"):
            parts.append("ACCEPTED_AS_MISSING=YES")

        added = entry.get("added_books_count") or 0
        if added:
            parts.append(f"added={added}")

        error = entry.get("error")
        if error:
            parts.append(f"error={error}")

        lines.append(" | ".join(parts))
    return lines


def log_discovery_summary(*, result: dict, terminal_error: str | None = None) -> None:
    """Prints ONE short, bounded-size block summarizing a Check Now run --
    at most a few dozen lines, no matter how many web pages were scraped or
    candidates were scanned. Everything between the START and END markers
    is meant to be copy/pasted whole for debugging.
    """
    provider_ledger = result.get("provider_ledger") or []
    asin_discovery = result.get("asin_discovery") or {}
    provider_failures = result.get("provider_failures") or []
    validated_candidates = result.get("validated_candidates") or []
    missing_books = result.get("missing_books") or []
    upcoming_books = result.get("upcoming_books") or []

    _console_log("===== CHECK NOW DEBUG SUMMARY START =====")
    _console_log(f"series_id={result.get('series_id')} series_name={result.get('series_name')}")
    _console_log(f"status={result.get('status')} found={bool(result.get('found'))} added_count={int(result.get('added_count') or 0)}")
    _console_log(f"all_providers_failed={bool(result.get('all_providers_failed'))} provider_failures={len(provider_failures)}")
    _console_log(
        "asin_discovery: "
        f"discovered={int(asin_discovery.get('discovered') or 0)} "
        f"processed={int(asin_discovery.get('processed') or 0)} "
        f"fetch_success={int(asin_discovery.get('fetch_success') or 0)} "
        f"fetch_failed={int(asin_discovery.get('fetch_failed') or 0)} "
        f"metadata_hits={int(asin_discovery.get('metadata_hits') or 0)}"
    )

    _console_log(f"--- providers (one line each, {len(provider_ledger)} total) ---")
    for line in _condense_provider_ledger(provider_ledger):
        _console_log(line)

    _console_log(f"--- validated_candidates={len(validated_candidates)} ---")

    _console_log(f"--- missing_books (found, not yet owned) = {len(missing_books)} ---")
    for book in missing_books[:15]:
        _console_log(f"  MISSING: {book.get('title')} | asin={book.get('asin')} | number={book.get('series_number')}")

    _console_log(f"--- upcoming_books (pre-order / future release) = {len(upcoming_books)} ---")
    for book in upcoming_books[:15]:
        _console_log(f"  UPCOMING: {book.get('title')} | asin={book.get('asin')} | expected={book.get('publication_date')}")

    if provider_failures:
        _console_log(f"--- provider_failures (first 10 of {len(provider_failures)}) ---")
        for failure in provider_failures[:10]:
            _console_log(f"  FAILED: {failure.get('provider')} | {failure.get('error')}")

    if terminal_error:
        _console_log(f"terminal_error={terminal_error}")
    _console_log("===== CHECK NOW DEBUG SUMMARY END =====")


def _normalize_author(value: str | None) -> str:
    return str(value or "").strip().lower()


def _authors_match_exact(series_author: str | None, candidate_author: str | None) -> bool:
    series_norm = _normalize_author(series_author)
    candidate_norm = _normalize_author(candidate_author)
    if not series_norm or not candidate_norm:
        return False
    return series_norm == candidate_norm


def _to_canonical_book(candidate: dict, provider_name: str, series_name: str, series_author: str) -> dict:
    return {
        "title": str(candidate.get("title") or "").strip(),
        "author": str(candidate.get("author") or "").strip() or series_author,
        "asin": str(candidate.get("asin") or candidate.get("asin_or_id") or "").strip().upper(),
        "series_name": str(candidate.get("series_name") or candidate.get("series") or series_name).strip() or series_name,
        "series_number": candidate.get("series_number") if candidate.get("series_number") is not None else candidate.get("book_number"),
        "publication_date": candidate.get("publication_date") or candidate.get("release_date") or candidate.get("expected_date"),
        "status": str(candidate.get("status") or candidate.get("status_hint") or "published").strip().lower(),
        "provider": str(candidate.get("provider") or provider_name).strip() or provider_name,
        "url": str(candidate.get("url") or candidate.get("source_url") or "").strip(),
        "source_layer": str(candidate.get("source_layer") or "").strip(),
    }


def _normalize_identity_text(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _normalize_title_text(value: str | None) -> str:
    cleaned = _normalize_identity_text(value)
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _title_stem(value: str | None) -> str:
    title = _normalize_title_text(value)
    title = re.sub(r"\b(book|volume|vol|series)\s*\d+\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\b#\s*\d+\b", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def _token_set(value: str | None) -> set[str]:
    return {token for token in _normalize_title_text(value).split() if token}


def _token_overlap_ratio(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, min(len(left), len(right)))


def _series_allows_author_relaxation(series_author: str) -> bool:
    """Relax the author-match requirement when the library has no author on
    file for this series -- there is nothing to match against, so author
    mismatch cannot be used to reject a candidate. This must remain a
    data-driven condition (missing author), not a per-series allowlist.
    """
    return not str(series_author or "").strip()


def _normalize_identity_number(value) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    if number.is_integer():
        return str(int(number))
    return str(number).strip()


def _infer_number_from_title(title: str | None) -> int | None:
    cleaned = _normalize_title_text(title)
    if not cleaned:
        return None

    patterns = (
        r"\bbook\s*(\d+)\b",
        r"\bvolume\s*(\d+)\b",
        r"\bvol\.?\s*(\d+)\b",
        r"\b#\s*(\d+)\b",
        r"\((?:[^)]*?)book\s*(\d+)(?:[^)]*?)\)",
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            parsed = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _title_has_series_marker(title: str) -> bool:
    cleaned = _normalize_identity_text(title)
    if not cleaned:
        return False
    patterns = (
        r"\bbook\s*\d+\b",
        r"\bvolume\s*\d+\b",
        r"\bvol\.?\s*\d+\b",
        r"\b#\s*\d+\b",
        r"\b\d+(?:st|nd|rd|th)\b",
    )
    return any(re.search(pattern, cleaned, flags=re.IGNORECASE) for pattern in patterns)


def _title_pattern_match(title: str, series_name: str, known_series_titles: set[str]) -> bool:
    title_norm = _normalize_title_text(title)
    series_norm = _normalize_title_text(series_name)
    if not title_norm:
        return False

    if series_norm and series_norm in title_norm:
        return True

    title_tokens = _token_set(title_norm)
    if series_norm:
        series_tokens = _token_set(series_norm)
        if _token_overlap_ratio(title_tokens, series_tokens) >= 0.75 and len(title_tokens & series_tokens) >= 2:
            return True

    candidate_stem = _title_stem(title_norm)
    for known_title in known_series_titles:
        known_stem = _title_stem(known_title)
        if not known_stem:
            continue
        if known_stem == candidate_stem:
            return True
        if _token_overlap_ratio(_token_set(candidate_stem), _token_set(known_stem)) >= 0.75:
            return True
    return False


def _partial_series_match(title: str, candidate_series_name: str, series_name: str) -> bool:
    title_tokens = _token_set(title)
    candidate_series_tokens = _token_set(candidate_series_name)
    target_tokens = _token_set(series_name)
    if not target_tokens:
        return False

    title_overlap = _token_overlap_ratio(title_tokens, target_tokens)
    series_overlap = _token_overlap_ratio(candidate_series_tokens, target_tokens)
    overlap_count = len((title_tokens | candidate_series_tokens) & target_tokens)
    return overlap_count >= 1 and max(title_overlap, series_overlap) >= 0.5


def _resolve_candidate_number(candidate: dict) -> tuple[str, bool]:
    raw_number = candidate.get("series_number") if candidate.get("series_number") is not None else candidate.get("book_number")
    normalized = _normalize_identity_number(raw_number)
    if normalized:
        return normalized, False

    inferred = _infer_number_from_title(str(candidate.get("title") or ""))
    if inferred is None:
        return "", False
    return str(inferred), True


def _build_known_title_number_keys(books: list[Book]) -> set[str]:
    keys: set[str] = set()
    for book in books:
        title = _normalize_title_text(book.title)
        number = _normalize_identity_number(book.book_number)
        if title and number:
            keys.add(f"{title}|{number}")
    return keys


def _is_known_candidate(
    *,
    asin: str,
    normalized_title: str,
    normalized_number: str,
    known_series_asins: set[str],
    known_series_titles: set[str],
    known_series_numbers: set[str],
    known_title_number_keys: set[str],
) -> bool:
    if asin and asin in known_series_asins:
        return True
    if normalized_title and normalized_number and f"{normalized_title}|{normalized_number}" in known_title_number_keys:
        return True
    if normalized_title and normalized_title in known_series_titles and normalized_number and normalized_number in known_series_numbers:
        return True
    return False


def _build_multi_signal_diagnostics(
    *,
    candidate: dict,
    series_name: str,
    series_author: str,
    known_series_asins: set[str],
    known_series_titles: set[str],
    known_series_numbers: set[str],
) -> dict:
    title = str(candidate.get("title") or "")
    normalized_title = _normalize_title_text(title)
    source_layer = str(candidate.get("source_layer") or "").strip().lower()
    is_asin_fallback = source_layer == "asin_fallback"
    candidate_series_name = str(candidate.get("series_name") or candidate.get("series") or "")
    asin = str(candidate.get("asin") or candidate.get("asin_or_id") or "").strip().upper()
    valid_asin = bool(re.fullmatch(r"[A-Z0-9]{10}", asin))
    asin_present = bool(asin)

    resolved_number, number_inferred = _resolve_candidate_number(candidate)
    title_pattern_match = _title_pattern_match(title, series_name, known_series_titles)
    partial_series_match = _partial_series_match(title, candidate_series_name, series_name)

    normalized_series_name = _normalize_title_text(series_name)
    normalized_candidate_series_name = _normalize_title_text(candidate_series_name)

    # A candidate can carry its OWN genuine series metadata (e.g. discovered
    # via a related-ASIN crawl off a seed book's product page) that clearly
    # names a different series -- most commonly another series by the same
    # author. That is strong, reliable evidence of a wrong-series match that
    # author/number/asin overlap cannot override, so it hard-vetoes
    # acceptance below regardless of score. Series-agnostic: only fires when
    # the candidate's own series field is non-empty and shares no meaningful
    # overlap with the target series.
    series_clearly_mismatched = bool(
        normalized_candidate_series_name
        and normalized_candidate_series_name != normalized_series_name
        and not partial_series_match
    )

    candidate_author = str(candidate.get("author") or "").strip()
    author_present = bool(candidate_author)
    author_match = _authors_match_exact(series_author, candidate_author) if author_present else False
    series_author_relaxed = _series_allows_author_relaxation(series_author)

    known_asin_match = asin_present and asin in known_series_asins
    known_title_match = bool(normalized_title and normalized_title in known_series_titles)
    known_number_match = bool(resolved_number and resolved_number in known_series_numbers)

    author_weight = 0
    if author_match:
        author_weight = 2
    elif author_present and not series_author_relaxed:
        author_weight = -2

    non_author_score = 0
    non_author_score += 4 if title_pattern_match else 0
    non_author_score += 3 if partial_series_match else 0
    non_author_score += 3 if resolved_number else 0
    non_author_score += 2 if asin_present else 0
    non_author_score += 2 if known_title_match else 0
    non_author_score += 2 if known_number_match else 0
    non_author_score += 3 if known_asin_match else 0

    score = non_author_score + author_weight

    strong_combination = (
        (title_pattern_match and bool(resolved_number))
        or (partial_series_match and bool(resolved_number))
        or (asin_present and (title_pattern_match or partial_series_match or bool(resolved_number)))
        or title_pattern_match
    )

    strong_identity = bool(non_author_score >= 8 and strong_combination)

    author_fallback_used = False
    author_fallback_reason = "none"
    author_gate_pass = (not author_present) or author_match
    if author_present and not author_match:
        relaxed_path = series_author_relaxed and strong_identity
        generic_path = strong_identity and known_asin_match and title_pattern_match
        if relaxed_path:
            author_gate_pass = True
            author_fallback_used = True
            author_fallback_reason = "series-specific-relaxation"
        elif generic_path:
            author_gate_pass = True
            author_fallback_used = True
            author_fallback_reason = "strong-identity-fallback"
        else:
            author_gate_pass = False
            author_fallback_reason = "author-mismatch"

    accepted_as_missing = bool(
        score >= MIN_ACCEPTANCE_SCORE and strong_combination and author_gate_pass and not series_clearly_mismatched
    )

    # ASIN-fallback candidates come from re-fetching a *known owned* ASIN's
    # product page (see checker_core._asin_fallback_discovery), so they carry
    # less independent discovery signal than a freshly-discovered candidate.
    # Require a tighter, but still fully general, identity match: author and
    # series must match the library exactly (or be absent on the candidate
    # side), and the title must clearly reference this series (by name or by
    # the resolved book number).
    series_match_or_empty = (not normalized_candidate_series_name) or (normalized_candidate_series_name == normalized_series_name)
    author_match_or_empty = (not author_present) or author_match
    title_has_series_name = bool(normalized_series_name and normalized_series_name in normalized_title)
    title_has_number = bool(resolved_number and re.search(rf"\b{re.escape(str(resolved_number))}\b", _normalize_identity_text(title)))

    asin_fallback_strict_accept = bool(
        is_asin_fallback
        and valid_asin
        and author_match_or_empty
        and series_match_or_empty
        and (title_has_series_name or title_has_number)
    )

    if is_asin_fallback:
        accepted_as_missing = asin_fallback_strict_accept

    return {
        "title_pattern_match": title_pattern_match,
        "number_inferred": number_inferred,
        "partial_series_match": partial_series_match,
        "author_match": author_match,
        "author_weight": author_weight,
        "author_present": author_present,
        "author_gate_pass": author_gate_pass,
        "author_fallback_used": author_fallback_used,
        "author_fallback_reason": author_fallback_reason,
        "series_author_relaxed": series_author_relaxed,
        "asin_present": asin_present,
        "multi_signal_score": score,
        "non_author_score": non_author_score,
        "series_clearly_mismatched": series_clearly_mismatched,
        "accepted_as_missing": accepted_as_missing,
        "asin_fallback_relaxed_accept": asin_fallback_strict_accept,
        "source_layer": source_layer,
        "normalized_title": normalized_title,
        "resolved_number": resolved_number,
        "asin": asin,
    }


def _ensure_provider_ledger_detection_fields(provider_ledger: list[dict]) -> None:
    for entry in provider_ledger:
        entry.setdefault("title_pattern_match", False)
        entry.setdefault("number_inferred", False)
        entry.setdefault("partial_series_match", False)
        entry.setdefault("author_match", False)
        entry.setdefault("author_weight", 0)
        entry.setdefault("author_fallback_used", False)
        entry.setdefault("author_fallback_reason", "none")
        entry.setdefault("series_author_relaxed", False)
        entry.setdefault("asin_seed_count", 0)
        entry.setdefault("asin_seed_pages_fetched", 0)
        entry.setdefault("asin_seed_pages_failed", 0)
        entry.setdefault("asin_related_asins", 0)
        entry.setdefault("asin_series_candidates", 0)
        entry.setdefault("asin_present", False)
        entry.setdefault("multi_signal_score", 0)
        entry.setdefault("accepted_as_missing", False)
        entry.setdefault("added_books_count", 0)
        entry.setdefault("added_books_asins", [])
        entry.setdefault("added_books_titles", [])


def _annotate_provider_ledger_with_detection(provider_ledger: list[dict], provider_name: str, diagnostics: dict) -> None:
    for entry in provider_ledger:
        entry_provider = str(entry.get("provider_name") or "").strip().lower()
        if entry_provider != str(provider_name or "").strip().lower():
            continue
        entry["title_pattern_match"] = bool(entry.get("title_pattern_match")) or bool(diagnostics.get("title_pattern_match"))
        entry["number_inferred"] = bool(entry.get("number_inferred")) or bool(diagnostics.get("number_inferred"))
        entry["partial_series_match"] = bool(entry.get("partial_series_match")) or bool(diagnostics.get("partial_series_match"))
        entry["author_match"] = bool(entry.get("author_match")) or bool(diagnostics.get("author_match"))
        entry["author_weight"] = max(int(entry.get("author_weight") or 0), int(diagnostics.get("author_weight") or 0))
        entry["author_fallback_used"] = bool(entry.get("author_fallback_used")) or bool(diagnostics.get("author_fallback_used"))
        if diagnostics.get("author_fallback_used"):
            entry["author_fallback_reason"] = str(diagnostics.get("author_fallback_reason") or "none")
        entry["series_author_relaxed"] = bool(entry.get("series_author_relaxed")) or bool(diagnostics.get("series_author_relaxed"))
        entry["asin_present"] = bool(entry.get("asin_present")) or bool(diagnostics.get("asin_present"))
        entry["multi_signal_score"] = max(int(entry.get("multi_signal_score") or 0), int(diagnostics.get("multi_signal_score") or 0))
        entry["accepted_as_missing"] = bool(entry.get("accepted_as_missing")) or bool(diagnostics.get("accepted_as_missing"))


def _build_series_identity_sets(books: list[Book]) -> tuple[set[str], set[str], set[str]]:
    known_series_asins: set[str] = set()
    known_series_titles: set[str] = set()
    known_series_numbers: set[str] = set()

    for book in books:
        asin = _normalize_identity_text(str(book.asin or "")).upper()
        title = _normalize_title_text(book.title)
        number = _normalize_identity_number(book.book_number)
        if asin:
            known_series_asins.add(asin)
        if title:
            known_series_titles.add(title)
        if number:
            known_series_numbers.add(number)

    return known_series_asins, known_series_titles, known_series_numbers


class SeriesIntelligenceAgent:
    def run_daily_scan(self, db) -> None:
        return None

    def run_series_check(
        self,
        db: Session,
        series_id: int,
        progress_callback=None,
        emit_summary: bool = True,
    ) -> dict:
        series = db.query(Series).filter(Series.id == series_id).first()
        if not series:
            result = {
                "series_id": None,
                "series_name": None,
                "highest_owned_book_number": None,
                "candidate_numbers": [],
                "added_count": 0,
                "added_books": [],
                "found_books": [],
                "candidate_diagnostics": [],
                "trusted_series_reconciliation": None,
                "canonical_missing_entries": [],
                "canonical_found_entries": [],
                "canonical_upcoming_entries": [],
                "canonical_rejected_entries": [],
                "complete": True,
                "status": "no_hits",
                "no_new_books": True,
                "reason": "series-not-found",
                "discovery_mode": None,
                "has_new_books": False,
                "series_state": None,
                "last_checked": None,
                "next_unread_book_number": None,
                "next_upcoming_book_number": None,
                "missing_books": [],
                "found": False,
                "discovery_engine": "none",
                "agent_pipeline": False,
            }
            if emit_summary:
                log_discovery_summary(result=result, terminal_error="series-not-found")
            return result

        _console_log(f"CHECK NOW triggered for series: {series.name}")

        try:
            active_series_books = [
                book
                for book in db.query(Book).filter(Book.series_id == series_id).all()
                if (book.record_status or "") != "deleted"
            ]
            highest_owned_book_number = max(
                (
                    int(float(book.book_number))
                    for book in active_series_books
                    if book.book_number is not None and not bool(book.is_missing)
                ),
                default=None,
            )
            setattr(series, "highest_owned_book_number", highest_owned_book_number)

            seed_asins = sorted(
                {
                    str(book.asin or "").strip().upper()
                    for book in active_series_books
                    if str(book.asin or "").strip()
                }
            )

            checker_result = check_for_new_book(series, progress_callback=progress_callback, seed_asins=seed_asins)
            found = bool(checker_result.get("found"))
            candidate = checker_result.get("candidate") or None
            provider_failures = checker_result.get("provider_failures") or []
            all_providers_failed = bool(checker_result.get("all_providers_failed"))
            amazon_book_candidates = checker_result.get("amazon_book_candidates") or []
            validated_candidates_raw = checker_result.get("validated_candidates") or []
            provider_ledger = checker_result.get("provider_ledger")
            if provider_ledger is None:
                provider_ledger = []

            _console_log(f"Candidates found: {len(validated_candidates_raw)}")

        # Build canonical candidates and compare against owned ASIN inventory.
            series_author = str(series.author or "").strip()
            active_library_books = [
                book
                for book in db.query(Book).filter(Book.series_id == series_id).all()
                if (book.record_status or "") != "deleted"
            ]
            known_series_asins, known_series_titles, known_series_numbers = _build_series_identity_sets(active_library_books)
            known_title_number_keys = _build_known_title_number_keys(active_library_books)
            _ensure_provider_ledger_detection_fields(provider_ledger)

            # NOTE: checker_core.check_for_new_book already applies strict
            # identity/canonical-pattern filtering (see _apply_strict_post_filtering,
            # _matches_any_canonical_pattern, _looks_plausibly_real) before returning
            # validated_candidates, and by design most of those candidates will have
            # been enriched with real Amazon product metadata before reaching here
            # (see checker_core's ASIN enrichment stage). We must NOT re-filter by
            # provider name here -- doing so previously discarded every legitimately
            # discovered candidate that didn't come from an "amazon*"-named provider,
            # silently breaking author-page/Google discovery for every series.
            canonical_validated_candidates: list[dict] = []
            for raw_candidate in validated_candidates_raw:
                canonical = _to_canonical_book(raw_candidate, str(raw_candidate.get("provider") or ""), series.name, series_author)
                if not canonical.get("title"):
                    continue
                if not str(canonical.get("provider") or "").strip().lower().startswith("amazon") and not _authors_match_exact(series_author, canonical.get("author")):
                    continue
                canonical_validated_candidates.append(canonical)

            discovery_validated_candidates = list(canonical_validated_candidates)
            discovery_validated_candidates.sort(
                key=lambda candidate: 1 if str(candidate.get("provider") or "").strip().lower() == "amazon_asin_series" else 0,
                reverse=True,
            )

            discovered_series_candidates: list[dict] = []
            discovered_series_asins: set[str] = set()
            discovered_series_titles: set[str] = set()
            discovered_series_numbers: set[str] = set()
            candidate_diagnostics: list[dict] = []

            for canonical in discovery_validated_candidates:
                diagnostics = _build_multi_signal_diagnostics(
                    candidate=canonical,
                    series_name=series.name,
                    series_author=series_author,
                    known_series_asins=known_series_asins,
                    known_series_titles=known_series_titles,
                    known_series_numbers=known_series_numbers,
                )
                _annotate_provider_ledger_with_detection(
                    provider_ledger,
                    str(canonical.get("provider") or "amazon"),
                    diagnostics,
                )

                candidate_diagnostics.append(
                    {
                        "title": canonical.get("title"),
                        "asin": diagnostics.get("asin"),
                        "resolved_number": diagnostics.get("resolved_number"),
                        "title_pattern_match": diagnostics.get("title_pattern_match"),
                        "number_inferred": diagnostics.get("number_inferred"),
                        "partial_series_match": diagnostics.get("partial_series_match"),
                        "author_match": diagnostics.get("author_match"),
                        "author_weight": diagnostics.get("author_weight"),
                        "author_fallback_used": diagnostics.get("author_fallback_used"),
                        "author_fallback_reason": diagnostics.get("author_fallback_reason"),
                        "series_author_relaxed": diagnostics.get("series_author_relaxed"),
                        "asin_present": diagnostics.get("asin_present"),
                        "multi_signal_score": diagnostics.get("multi_signal_score"),
                        "accepted_as_missing": diagnostics.get("accepted_as_missing"),
                    }
                )

                if not diagnostics.get("accepted_as_missing"):
                    continue

                resolved_number = str(diagnostics.get("resolved_number") or "").strip()
                inferred_number_value = int(resolved_number) if resolved_number.isdigit() else None

                discovered_series_candidates.append(
                    {
                        **canonical,
                        "book_number": canonical.get("book_number") if canonical.get("book_number") is not None else inferred_number_value,
                        "series_number": canonical.get("series_number") if canonical.get("series_number") is not None else inferred_number_value,
                        "missing_detection": diagnostics,
                    }
                )
                asin = _normalize_identity_text(canonical.get("asin") or canonical.get("asin_or_id") or "").upper()
                title = _normalize_identity_text(canonical.get("title"))
                number = resolved_number or _normalize_identity_number(canonical.get("series_number") if canonical.get("series_number") is not None else canonical.get("book_number"))
                if asin:
                    discovered_series_asins.add(asin)
                if title:
                    discovered_series_titles.add(title)
                if number:
                    discovered_series_numbers.add(number)

            available_missing: list[dict] = []
            upcoming_books: list[dict] = []
            for canonical in discovered_series_candidates:
                candidate_status = str(canonical.get("status") or "published").strip().lower()
                candidate_asin = str(canonical.get("asin") or "").strip().upper()
                diagnostics = canonical.get("missing_detection") if isinstance(canonical.get("missing_detection"), dict) else {}
                accepted_as_missing = bool(diagnostics.get("accepted_as_missing"))
                is_known_identity = _is_known_candidate(
                    asin=candidate_asin,
                    normalized_title=_normalize_title_text(canonical.get("title") or ""),
                    normalized_number=str(diagnostics.get("resolved_number") or ""),
                    known_series_asins=known_series_asins,
                    known_series_titles=known_series_titles,
                    known_series_numbers=known_series_numbers,
                    known_title_number_keys=known_title_number_keys,
                )
                is_upcoming = candidate_status == "upcoming"

                canonical["accepted_as_missing"] = accepted_as_missing
                canonical["is_known_identity"] = bool(is_known_identity)
                canonical["is_upcoming"] = is_upcoming

                if is_upcoming:
                    upcoming_books.append(canonical)
                    continue

                if not (accepted_as_missing and not is_known_identity and not is_upcoming):
                    continue
                available_missing.append({**canonical, "status": "available", "is_missing": True})

            found = bool(available_missing or upcoming_books)
            series.has_new_books = found
            series.last_checked = date.today()
            db.commit()
            db.refresh(series)

            missing_books = available_missing
            added_books = []
            for canonical in available_missing:
                _console_log(f"Candidate: {canonical.get('title') or series.name} {canonical.get('asin') or 'NO-ASIN'}")
                added_books.append(
                    {
                        "title": canonical.get("title"),
                        "author": canonical.get("author") or series.author,
                        "series_name": canonical.get("series_name") or series.name,
                        "book_number": canonical.get("series_number"),
                        "source_url": canonical.get("url"),
                        "provider": canonical.get("provider") or "amazon",
                        "publication_date": canonical.get("publication_date"),
                        "expected_date": None,
                        "status_hint": "available",
                        "asin_or_id": canonical.get("asin"),
                        "is_missing": True,
                        "status": "available",
                        "canonical_metadata": {
                            "title_normalized": canonical.get("title"),
                            "series_name_normalized": canonical.get("series_name") or series.name,
                            "book_number_normalized": canonical.get("series_number"),
                            "publish_date_normalized": canonical.get("publication_date"),
                            "upcoming_date_normalized": None,
                            "availability": "available",
                            "edition_type": "unknown",
                            "title_selector": None,
                        },
                    }
                )

            for canonical in upcoming_books:
                added_books.append(
                    {
                        "title": canonical.get("title"),
                        "author": canonical.get("author") or series.author,
                        "series_name": canonical.get("series_name") or series.name,
                        "book_number": canonical.get("series_number"),
                        "source_url": canonical.get("url"),
                        "provider": canonical.get("provider") or "amazon",
                        "publication_date": None,
                        "expected_date": canonical.get("publication_date"),
                        "status_hint": "upcoming",
                        "asin_or_id": canonical.get("asin"),
                        "is_missing": False,
                        "status": "upcoming",
                        "canonical_metadata": {
                            "title_normalized": canonical.get("title"),
                            "series_name_normalized": canonical.get("series_name") or series.name,
                            "book_number_normalized": canonical.get("series_number"),
                            "publish_date_normalized": None,
                            "upcoming_date_normalized": canonical.get("publication_date"),
                            "availability": "upcoming",
                            "edition_type": "unknown",
                            "title_selector": None,
                        },
                    }
                )

            added_books_count = len(added_books)
            added_books_asins = [
                str(book.get("asin_or_id") or "").strip().upper()
                for book in added_books
                if str(book.get("asin_or_id") or "").strip()
            ]
            added_books_titles = [
                str(book.get("title") or "").strip()
                for book in added_books
                if str(book.get("title") or "").strip()
            ]

            for entry in provider_ledger:
                entry["added_books_count"] = added_books_count
                entry["added_books_asins"] = added_books_asins
                entry["added_books_titles"] = added_books_titles

            _console_log(f"CHECK NOW completed successfully for series: {series.name}")

            result = {
                "series_id": series.id,
                "series_name": series.name,
                "highest_owned_book_number": highest_owned_book_number,
                "candidate_numbers": [],
                "added_count": added_books_count,
                "added_books": added_books,
                "found_books": added_books,
                "candidate_diagnostics": candidate_diagnostics,
                "added_books_count": added_books_count,
                "added_books_asins": added_books_asins,
                "added_books_titles": added_books_titles,
                "trusted_series_reconciliation": None,
                "canonical_missing_entries": [],
                "canonical_found_entries": [],
                "canonical_upcoming_entries": [],
                "canonical_rejected_entries": [],
                "complete": True,
                "status": "complete" if found else "no_hits",
                "no_new_books": not found,
                "reason": None if found else "no-hit-after-new-book-check",
                "discovery_mode": None,
                "has_new_books": series.has_new_books,
                "series_state": series.series_state,
                "last_checked": series.last_checked,
                "next_unread_book_number": series.next_unread_book_number,
                "next_upcoming_book_number": series.next_upcoming_book_number,
                "missing_books": missing_books,
                "available_missing": available_missing,
                "upcoming_books": upcoming_books,
                "validated_candidates": discovery_validated_candidates,
                "discovered_series_asins": sorted(discovered_series_asins),
                "discovered_series_titles": sorted(discovered_series_titles),
                "discovered_series_numbers": sorted(discovered_series_numbers),
                "found": found,
                "candidate": candidate or (discovery_validated_candidates[0] if discovery_validated_candidates else None),
                "provider_failures": provider_failures,
                "all_providers_failed": all_providers_failed,
                "asin_discovery": checker_result.get("asin_discovery") or {
                    "discovered": 0,
                    "processed": 0,
                    "fetch_success": 0,
                    "fetch_failed": 0,
                    "metadata_hits": 0,
                },
                "amazon_asin_candidates": checker_result.get("amazon_asin_candidates") or [],
                "first_extracted_product_metadata": checker_result.get("first_extracted_product_metadata"),
                "first_product_extraction_failure": checker_result.get("first_product_extraction_failure"),
                "provider_ledger": provider_ledger,
                "discovery_engine": "new_book_checker",
                "agent_pipeline": True,
            }
            if emit_summary:
                log_discovery_summary(result=result)
            return result
        except Exception as exc:
            if emit_summary:
                log_discovery_summary(
                    result={
                        "series_id": series_id,
                        "series_name": getattr(series, "name", None),
                        "status": "error",
                        "found": False,
                        "added_count": 0,
                        "provider_failures": [],
                        "all_providers_failed": False,
                        "asin_discovery": {
                            "discovered": 0,
                            "processed": 0,
                            "fetch_success": 0,
                            "fetch_failed": 0,
                            "metadata_hits": 0,
                        },
                        "validated_candidates": [],
                        "missing_books": [],
                        "upcoming_books": [],
                    },
                    terminal_error=f"{type(exc).__name__}: {exc}",
                )
            raise
