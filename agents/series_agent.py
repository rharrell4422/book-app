from __future__ import annotations

import logging
import re
from datetime import date

from sqlalchemy.orm import Session

import discovery_engine
import intelligence
from models import Book, Series


logger = logging.getLogger(__name__)


def _console_log(message: str) -> None:
    print(f"[series_agent] {message}", flush=True)


def log_discovery_summary(*, result: dict, terminal_error: str | None = None) -> None:
    """Prints one short, bounded-size block summarizing a Check Now run, for
    quick copy/paste debugging.
    """
    provider_failures = result.get("provider_failures") or []
    available_missing = result.get("available_missing") or []
    upcoming_books = result.get("upcoming_books") or []

    _console_log("===== CHECK NOW DEBUG SUMMARY START =====")
    _console_log(f"series_id={result.get('series_id')} series_name={result.get('series_name')}")
    _console_log(f"status={result.get('status')} found={bool(result.get('found'))} added_count={int(result.get('added_count') or 0)}")
    _console_log(f"discovery_engine={result.get('discovery_engine')} all_providers_failed={bool(result.get('all_providers_failed'))}")

    if provider_failures:
        _console_log(f"--- provider_failures ({len(provider_failures)}) ---")
        for failure in provider_failures[:10]:
            _console_log(f"  FAILED: {failure.get('provider')} | {failure.get('error')}")

    _console_log(f"--- available_missing (found, not yet owned) = {len(available_missing)} ---")
    for book in available_missing[:15]:
        _console_log(f"  MISSING: {book.get('title')} | number={book.get('series_number')}")

    _console_log(f"--- upcoming_books (pre-order / future release) = {len(upcoming_books)} ---")
    for book in upcoming_books[:15]:
        _console_log(f"  UPCOMING: {book.get('title')} | expected={book.get('publication_date')}")

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


def _title_references_series(title: str, series_name: str) -> bool:
    """Whether a title textually identifies the series it belongs to --
    used to decide whether we need to append a "(Series Name Book N)"
    suffix ourselves. Some sources (notably Hardcover, which tracks series
    position as structured data rather than embedding it in the title
    string) return clean, bare titles like "Unmapped" with no series name
    or book number anywhere in the text, which makes an added book hard to
    recognize as part of the series it was found for.
    """
    title_norm = _normalize_title_text(title)
    series_norm = _normalize_title_text(series_name)
    if not title_norm or not series_norm:
        return False
    if series_norm in title_norm:
        return True
    title_tokens = _token_set(title_norm)
    series_tokens = _token_set(series_norm)
    return _token_overlap_ratio(title_tokens, series_tokens) >= 0.75 and len(title_tokens & series_tokens) >= 2


def _partial_series_match(title: str, series_name: str) -> bool:
    title_tokens = _token_set(title)
    target_tokens = _token_set(series_name)
    if not target_tokens:
        return False
    return _token_overlap_ratio(title_tokens, target_tokens) >= 0.5


def _build_known_title_number_keys(books: list[Book]) -> set[str]:
    keys: set[str] = set()
    for book in books:
        title_key = discovery_engine.core_title_key(book.title)
        number = _normalize_identity_number(book.book_number)
        if title_key and number:
            keys.add(f"{title_key}|{number}")
    return keys


def _build_series_identity_sets(books: list[Book]) -> tuple[set[str], set[str], set[str]]:
    known_series_titles: set[str] = set()
    known_series_numbers: set[str] = set()
    bare_title_counts: dict[str, int] = {}
    for book in books:
        title_key = discovery_engine.core_title_key(book.title)
        number = _normalize_identity_number(book.book_number)
        if title_key:
            known_series_titles.add(title_key)
        if number:
            known_series_numbers.add(number)

        # An owned omnibus/boxed-set edition (e.g. "Safehold Boxed Set 1:
        # (Safehold Books 1-3)") only carries a single book_number on its
        # own row, but it really covers every number in that range -- treat
        # each of those as already-known too, so a newly-discovered
        # single-volume reprint of book 2 or 3 (a real book, just not a new
        # one) is recognized as a duplicate instead of "new available".
        for covered in intelligence.extract_omnibus_ranges(book.title):
            known_series_numbers.add(_normalize_identity_number(covered))
        for covered in intelligence.extract_omnibus_ranges(getattr(book, "subtitle", None)):
            known_series_numbers.add(_normalize_identity_number(covered))

        bare_key = discovery_engine.bare_title_key(book.title)
        if bare_key:
            bare_title_counts[bare_key] = bare_title_counts.get(bare_key, 0) + 1

    # Only trust a bare (number-less) title as an identity signal when it's
    # unique across the owned catalog -- otherwise a one-word title shared
    # by two different numbered volumes could get conflated.
    known_bare_titles = {key for key, count in bare_title_counts.items() if count == 1}
    return known_series_titles, known_series_numbers, known_bare_titles


def _is_known_candidate(
    *,
    isbn13: str,
    title_key: str,
    bare_title_key: str,
    normalized_number: str,
    known_series_isbns: set[str],
    known_series_titles: set[str],
    known_series_numbers: set[str],
    known_title_number_keys: set[str],
    known_bare_titles: set[str],
) -> bool:
    if isbn13 and isbn13 in known_series_isbns:
        return True
    if title_key and normalized_number and f"{title_key}|{normalized_number}" in known_title_number_keys:
        return True
    if title_key and title_key in known_series_titles:
        return True
    # A book number this series already owns is treated as known even
    # without a title match -- owned titles vary a lot in formatting, but
    # the position within the series is a reliable, stable identity signal.
    if normalized_number and normalized_number in known_series_numbers:
        return True
    # Fallback for candidates with no parseable number at all (e.g. a bare
    # search-result title like "Crown" with no "(Series Book 9)" suffix):
    # core_title_key can't match it against the number-bearing owned key,
    # so fall back to the number-less title alone when it uniquely
    # identifies one owned book.
    if not normalized_number and bare_title_key and bare_title_key in known_bare_titles:
        return True
    return False


def _empty_result(series_id: int | None, series_name: str | None, reason: str) -> dict:
    return {
        "series_id": series_id,
        "series_name": series_name,
        "highest_owned_book_number": None,
        "candidate_numbers": [],
        "added_count": 0,
        "added_books": [],
        "found_books": [],
        "candidate_diagnostics": [],
        "complete": True,
        "status": "no_hits",
        "no_new_books": True,
        "reason": reason,
        "has_new_books": False,
        "series_state": None,
        "last_checked": None,
        "next_unread_book_number": None,
        "next_upcoming_book_number": None,
        "missing_books": [],
        "available_missing": [],
        "upcoming_books": [],
        "validated_candidates": [],
        "found": False,
        "candidate": None,
        "provider_failures": [],
        "all_providers_failed": False,
        "asin_discovery": {
            "discovered": 0,
            "processed": 0,
            "fetch_success": 0,
            "fetch_failed": 0,
            "metadata_hits": 0,
        },
        "provider_ledger": [],
        "discovery_engine": "none",
        "agent_pipeline": False,
    }


def _build_added_book_entry(canonical: dict, *, status: str) -> dict:
    is_upcoming = status == "upcoming"
    return {
        "title": canonical.get("title"),
        "author": canonical.get("author"),
        "series_name": canonical.get("series_name"),
        "book_number": canonical.get("series_number"),
        "source_url": canonical.get("url"),
        "provider": canonical.get("provider"),
        "publication_date": None if is_upcoming else canonical.get("date_iso"),
        "expected_date": canonical.get("date_iso") if is_upcoming else None,
        "status_hint": status,
        "asin_or_id": canonical.get("identifier"),
        "is_missing": not is_upcoming,
        "status": status,
        "canonical_metadata": {
            "title_normalized": canonical.get("title"),
            "series_name_normalized": canonical.get("series_name"),
            "book_number_normalized": canonical.get("series_number"),
            "publish_date_normalized": None if is_upcoming else canonical.get("date_iso"),
            "upcoming_date_normalized": canonical.get("date_iso") if is_upcoming else None,
            "availability": status,
            "edition_type": "unknown",
            "title_selector": None,
        },
    }


class SeriesIntelligenceAgent:
    def run_series_check(
        self,
        db: Session,
        series_id: int,
        progress_callback=None,
        emit_summary: bool = True,
    ) -> dict:
        series = db.query(Series).filter(Series.id == series_id).first()
        if not series:
            result = _empty_result(None, None, "series-not-found")
            if emit_summary:
                log_discovery_summary(result=result, terminal_error="series-not-found")
            return result

        _console_log(f"CHECK NOW triggered for series: {series.name}")

        try:
            series_author = str(series.author or "").strip()

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

            if not series_author:
                result = _empty_result(series.id, series.name, "series-missing-author")
                result["highest_owned_book_number"] = highest_owned_book_number
                if emit_summary:
                    log_discovery_summary(result=result, terminal_error="series-missing-author")
                return result

            known_series_titles, known_series_numbers, known_bare_titles = _build_series_identity_sets(active_series_books)
            known_title_number_keys = _build_known_title_number_keys(active_series_books)
            known_series_isbns = {
                str(book.isbn13 or "").strip() for book in active_series_books if str(book.isbn13 or "").strip()
            }

            # Exclude titles the user already owns anywhere by this exact
            # author (any series), so a same-author's other tracked series
            # doesn't leak candidates into this one.
            other_series_by_author = [
                other
                for other in db.query(Series).filter(Series.author.isnot(None)).all()
                if other.id != series_id and _authors_match_exact(series_author, other.author)
            ]
            author_owned_titles = {discovery_engine.core_title_key(book.title) for book in active_series_books if book.title}
            for other in other_series_by_author:
                other_books = [
                    book
                    for book in db.query(Book).filter(Book.series_id == other.id).all()
                    if (book.record_status or "") != "deleted"
                ]
                author_owned_titles |= {discovery_engine.core_title_key(book.title) for book in other_books if book.title}

            # The broad author-bibliography fallback (used only when a
            # targeted series+author search finds nothing) risks pulling in
            # this author's unrelated other work, so only allow it when this
            # is the only series tracked for them in the library.
            author_is_unambiguous = len(other_series_by_author) == 0

            discovery = discovery_engine.discover_candidates_for_series(
                series.name,
                series_author,
                exclude_title_keys=author_owned_titles,
                allow_author_fallback=author_is_unambiguous,
                progress_callback=progress_callback,
            )
            candidates = discovery["candidates"]
            provider_failures = discovery["provider_failures"]
            all_providers_failed = discovery["all_providers_failed"]

            _console_log(f"Candidates found: {len(candidates)} (author_fallback_used={discovery['used_author_fallback']})")

            today = date.today()
            available_missing: list[dict] = []
            upcoming_books: list[dict] = []
            candidate_diagnostics: list[dict] = []

            for raw in candidates:
                title = str(raw.get("title") or "").strip()
                title_key = discovery_engine.core_title_key(title)
                # Hardcover's search index tags each hit with its actual
                # position in the series -- when present, that's a more
                # reliable source of the book number than parsing free-text
                # title formatting, so prefer it over inference.
                inferred_number = raw.get("series_number_hint") or discovery_engine.infer_number_from_title(
                    title, series.name
                )
                resolved_number = _normalize_identity_number(inferred_number) if inferred_number else ""

                # Targeted-search results are relevance-ranked by the API
                # against "<series name> <author>", but that ranking isn't a
                # strict filter -- a prolific author's unrelated books (e.g.
                # a different series, an anthology, a companion volume) can
                # still come back as "targeted" hits with zero textual tie to
                # the series being checked (regression: searching "Safehold
                # David Weber" surfaced "Bolo!", "Worlds Of Honor", and "At
                # All Costs" -- unrelated Weber titles from other series).
                # Trusting confidence=="targeted" alone is only safe when the
                # source also gave a real series-position number for it
                # (structured data, e.g. Hardcover's series_number_hint, or a
                # "Book N" pattern in the title itself) -- a same-author hit
                # with no number and no textual series reference is too weak
                # a signal on its own to add to the library as a new book.
                came_from_targeted_search = raw.get("confidence") == "targeted"
                explicit_series_match = _title_pattern_match(title, series.name, known_series_titles)
                partial_match = _partial_series_match(title, series.name)
                continues_numbering = bool(
                    inferred_number and highest_owned_book_number and inferred_number > highest_owned_book_number
                )
                targeted_with_number = bool(came_from_targeted_search and inferred_number)
                belongs_to_series = bool(
                    targeted_with_number or explicit_series_match or partial_match or continues_numbering
                )

                candidate_diagnostics.append(
                    {
                        "title": title,
                        "source": raw.get("source"),
                        "confidence": raw.get("confidence"),
                        "explicit_series_match": explicit_series_match,
                        "partial_match": partial_match,
                        "inferred_number": inferred_number,
                        "continues_numbering": continues_numbering,
                        "targeted_with_number": targeted_with_number,
                        "accepted": belongs_to_series,
                    }
                )

                if not belongs_to_series:
                    continue

                isbn13 = str(raw.get("isbn13") or "").strip()
                already_known = _is_known_candidate(
                    isbn13=isbn13,
                    title_key=title_key,
                    bare_title_key=discovery_engine.bare_title_key(title),
                    normalized_number=resolved_number,
                    known_series_isbns=known_series_isbns,
                    known_series_titles=known_series_titles,
                    known_series_numbers=known_series_numbers,
                    known_title_number_keys=known_title_number_keys,
                    known_bare_titles=known_bare_titles,
                )
                if already_known:
                    continue

                # Different providers can return the same real book under
                # differently-formatted titles within a *single* check run
                # (e.g. Hardcover's "Havoc in the Deathyards, A
                # Completionist Chronicles Short Story" vs OpenLibrary's
                # bare "Havoc in the Deathyards") -- growing these sets as
                # candidates get accepted lets the identity check above
                # catch that on the very next candidate, the same way it
                # catches matches against pre-existing owned books.
                if isbn13:
                    known_series_isbns.add(isbn13)
                if title_key:
                    known_series_titles.add(title_key)
                if title_key and resolved_number:
                    known_title_number_keys.add(f"{title_key}|{resolved_number}")
                if resolved_number:
                    known_series_numbers.add(resolved_number)
                if not resolved_number:
                    candidate_bare_key = discovery_engine.bare_title_key(title)
                    if candidate_bare_key:
                        known_bare_titles.add(candidate_bare_key)

                parsed_date = discovery_engine.parse_flexible_date(raw.get("published_date"))
                # Hardcover explicitly flags books it knows aren't out yet
                # even when it has no parseable release date -- fall back to
                # that hint only when there's no date to compare against.
                is_upcoming = bool(parsed_date and parsed_date > today) or bool(
                    parsed_date is None and raw.get("upcoming_hint")
                )

                # Give the stored title a recognizable series suffix when
                # the source didn't provide one (see _title_references_series)
                # so it's obvious at a glance which series/position a newly
                # added book belongs to, instead of a bare title like
                # "Unmapped" that could be mistaken for an unrelated find.
                display_title = title
                if inferred_number and not _title_references_series(title, series.name):
                    display_title = f"{title}: ({series.name} Book {inferred_number})"

                canonical = {
                    "title": display_title,
                    "author": series.author,
                    "series_name": series.name,
                    "series_number": inferred_number,
                    "date_iso": parsed_date.isoformat() if parsed_date else None,
                    "url": raw.get("source_url"),
                    "provider": raw.get("source"),
                    "identifier": isbn13 or f"{raw.get('source')}:{raw.get('source_id')}",
                }

                if is_upcoming:
                    upcoming_books.append(canonical)
                else:
                    available_missing.append(canonical)

            found = bool(available_missing or upcoming_books)
            series.has_new_books = found
            series.last_checked = today
            db.commit()
            db.refresh(series)

            added_books = [_build_added_book_entry(canonical, status="available") for canonical in available_missing]
            added_books += [_build_added_book_entry(canonical, status="upcoming") for canonical in upcoming_books]

            _console_log(f"CHECK NOW completed successfully for series: {series.name}")

            result = {
                "series_id": series.id,
                "series_name": series.name,
                "highest_owned_book_number": highest_owned_book_number,
                "candidate_numbers": [],
                "added_count": len(added_books),
                "added_books": added_books,
                "found_books": added_books,
                "candidate_diagnostics": candidate_diagnostics,
                "complete": True,
                "status": "complete" if found else "no_hits",
                "no_new_books": not found,
                "reason": None if found else "no-hit-after-new-book-check",
                "has_new_books": series.has_new_books,
                "series_state": series.series_state,
                "last_checked": series.last_checked,
                "next_unread_book_number": series.next_unread_book_number,
                "next_upcoming_book_number": series.next_upcoming_book_number,
                "missing_books": available_missing,
                "available_missing": available_missing,
                "upcoming_books": upcoming_books,
                "validated_candidates": [],
                "found": found,
                "candidate": (available_missing[0] if available_missing else (upcoming_books[0] if upcoming_books else None)),
                "provider_failures": provider_failures,
                "all_providers_failed": all_providers_failed,
                "asin_discovery": {
                    "discovered": len(candidates),
                    "processed": len(candidates),
                    "fetch_success": len(candidates),
                    "fetch_failed": 0,
                    "metadata_hits": len(added_books),
                },
                "provider_ledger": [],
                "discovery_engine": "official_api_v1",
                "agent_pipeline": True,
            }
            if emit_summary:
                log_discovery_summary(result=result)
            return result
        except Exception as exc:
            if emit_summary:
                log_discovery_summary(
                    result=_empty_result(series_id, getattr(series, "name", None), "error"),
                    terminal_error=f"{type(exc).__name__}: {exc}",
                )
            raise
