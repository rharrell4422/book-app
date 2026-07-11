import os
import re
import logging

import httpx
from datetime import date

import discovery_engine

try:
    from models import Series, Book
except Exception:
    raise


logger = logging.getLogger(__name__)
OMNIBUS_RANGE_PATTERN = re.compile(r"\bbooks?\s+\d+(?:\.\d+)?\s*[-–]\s*\d+(?:\.\d+)?\b", re.IGNORECASE)
OMNIBUS_RANGE_CAPTURE_PATTERN = re.compile(
    r"\bbooks?\s+(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)


def purge_orphaned_books(db) -> dict:
    books = db.query(Book).filter(Book.series_id.is_not(None)).all()
    existing_series_ids = {int(row[0]) for row in db.query(Series.id).all()}

    deleted_entries: list[dict] = []
    for book in books:
        if int(book.series_id) in existing_series_ids:
            continue
        deleted_entries.append(
            {
                "book_id": book.id,
                "title": book.title,
                "series_id": book.series_id,
            }
        )

    if deleted_entries:
        db.commit()

    return {
        "deleted_count": len(deleted_entries),
        "deleted_entries": deleted_entries,
    }


def _extract_series_position(text: str | None) -> int | None:
    if not text:
        return None

    match = re.search(r"\b(?:book|volume|vol\.?|#)\s*(\d+)\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def lookup_book_summary(
    title: str,
    author: str | None = None,
    book_number=None,
    series_name: str | None = None,
) -> dict:
    if not title:
        return {
            "found": False,
            "summary": None,
            "source_url": None,
            "matched_title": None,
            "matched_author": None,
        }

    expected_number = None
    if book_number is not None:
        try:
            expected_number = int(float(book_number))
        except (TypeError, ValueError):
            expected_number = None

    def build_lookup_queries(raw_title: str) -> list[str]:
        queries: list[str] = []

        def add_query(value: str | None):
            cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
            if cleaned and cleaned not in queries:
                queries.append(cleaned)

        add_query(raw_title)

        stripped_paren = re.sub(r"\s*\([^)]*\bbook\s*\d+[^)]*\)\s*$", "", raw_title, flags=re.IGNORECASE)
        add_query(stripped_paren)

        stripped_series_suffix = re.sub(r"\s*[:\-]\s*[^:()]*\bbook\s*\d+.*$", "", raw_title, flags=re.IGNORECASE)
        add_query(stripped_series_suffix)

        return queries

    def result_payload(result: dict, fallback_author: str | None = None) -> dict:
        return {
            "found": True,
            "summary": result.get("description"),
            "source_url": result.get("source_url"),
            "matched_title": result.get("title"),
            "matched_author": result.get("author") or fallback_author,
        }

    lookup_queries = build_lookup_queries(title)
    author_candidates = []
    if author and author.strip():
        author_candidates.append(author)
    author_candidates.append(None)

    best_fallback: dict | None = None
    not_found = {
        "found": False,
        "summary": None,
        "source_url": None,
        "matched_title": None,
        "matched_author": None,
    }

    # This previously called search_google_books/search_openlibrary/
    # search_serpapi_web, which were removed in an earlier cleanup pass and
    # would raise NameError on every call. discovery_engine.py now has a
    # real, working Google Books/OpenLibrary client (built for series
    # discovery) -- reuse it here too rather than leaving this broken.
    for query in lookup_queries:
        for author_candidate in author_candidates:
            try:
                google_results = discovery_engine._fetch_google_books(  # noqa: SLF001
                    f'intitle:"{query}"' + (f' inauthor:"{author_candidate}"' if author_candidate else "")
                )
            except Exception as exc:
                logger.info("Book summary lookup: Google Books unavailable (%s)", exc)
                google_results = []

            for result in google_results:
                description = result.get("description")
                if not description:
                    continue

                result_title = result.get("title")
                result_number = discovery_engine.infer_number_from_title(result_title, series_name)

                # Google's intitle: search is a relevance-ranked text match, not
                # an exact-phrase lookup -- it doesn't reliably rank the exact
                # volume first. Regression: a book-1 lookup for "1% Lifesteal"
                # got Google's "Volume 4" result back (ranked above the real
                # "Book one" match) purely because it happened to rank higher,
                # silently attaching book 4's summary to book 1. When we know
                # which book number we're after and a result's title clearly
                # identifies itself as a *different* number, it can never be
                # right -- skip it outright regardless of rank.
                if expected_number is not None and result_number is not None and result_number != expected_number:
                    continue

                payload = result_payload(
                    {
                        "description": description,
                        "source_url": result.get("source_url"),
                        "title": result_title,
                        "author": ", ".join(result.get("authors") or []) or None,
                    },
                    author_candidate,
                )

                if expected_number is None or result_number == expected_number:
                    return payload

                # result_number is None -- an ambiguous result with no
                # parseable number of its own. Only usable as a last-resort
                # fallback if nothing confidently-numbered ever turns up.
                if not best_fallback:
                    best_fallback = payload

    if best_fallback:
        return best_fallback

    return not_found


def recount_series_aggregates_for_series(db, series_id: int) -> dict:
    books = (
        db.query(Book)
        .filter(Book.series_id == series_id)
        .all()
    )

    active_books = [book for book in books if str(getattr(book, "record_status", "active") or "active") != "deleted"]
    deleted_books = [book for book in books if str(getattr(book, "record_status", "active") or "active") == "deleted"]
    upcoming_books = [book for book in active_books if str(getattr(book, "read_status", "") or "").strip().lower() == "upcoming"]

    numbered = []
    for book in active_books:
        value = getattr(book, "book_number", None)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        numbered.append(number)

    total_books = int(max(numbered)) if numbered else 0

    return {
        "series_id": series_id,
        "total_books": total_books,
        "active_count": len(active_books),
        "deleted_count": len(deleted_books),
        "upcoming_count": len(upcoming_books),
    }


def compute_series_intelligence_for_series(db, series_id: int) -> dict:
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        return {
            "series_id": series_id,
            "total_books": 0,
            "missing_orders": [],
            "next_unread_book_number": None,
            "next_upcoming_book_number": None,
            "is_series_finished": False,
            "read_count": 0,
            "unread_count": 0,
        }

    books = (
        db.query(Book)
        .filter(Book.series_id == series_id)
        .all()
    )
    active_books = [book for book in books if str(getattr(book, "record_status", "active") or "active") != "deleted"]

    word_to_number = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "thirteen": 13,
        "fourteen": 14,
        "fifteen": 15,
        "sixteen": 16,
        "seventeen": 17,
        "eighteen": 18,
        "nineteen": 19,
        "twenty": 20,
    }

    def roman_to_int(value: str) -> int | None:
        if not value:
            return None
        roman = str(value).strip().upper()
        if not roman or not re.fullmatch(r"[IVXLCDM]+", roman):
            return None

        values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
        total = 0
        previous = 0
        for char in reversed(roman):
            current = values[char]
            if current < previous:
                total -= current
            else:
                total += current
                previous = current
        return total if total > 0 else None

    def token_to_int(token: str) -> int | None:
        cleaned = str(token or "").strip().lower()
        if not cleaned:
            return None

        if cleaned.isdigit():
            try:
                number = int(cleaned)
            except ValueError:
                return None
            return number if number > 0 else None

        if cleaned in word_to_number:
            return word_to_number[cleaned]

        return roman_to_int(cleaned)

    def extract_omnibus_ranges(text: str) -> set[int]:
        extracted: set[int] = set()
        normalized = str(text or "")
        if not normalized:
            return extracted

        if not re.search(r"\b(?:books?|volumes?|vol\.?)\b", normalized, flags=re.IGNORECASE):
            return extracted

        number_token = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|[ivxlcdm]+)"
        range_pattern = re.compile(
            rf"\b({number_token})\b\s*(?:-|–|—|to|thru|through)\s*\b({number_token})\b",
            re.IGNORECASE,
        )

        for match in range_pattern.finditer(normalized):
            start = token_to_int(match.group(1))
            end = token_to_int(match.group(2))
            if start is None or end is None:
                continue

            low, high = (start, end) if start <= end else (end, start)
            for number in range(low, high + 1):
                extracted.add(number)

        return extracted

    covered_numbers: set[int] = set()
    for book in active_books:
        raw_number = getattr(book, "book_number", None)
        try:
            number = float(raw_number) if raw_number is not None else None
        except (TypeError, ValueError):
            number = None

        if number is not None and number > 0 and float(number).is_integer():
            covered_numbers.add(int(number))

        title_text = str(getattr(book, "title", "") or "")
        subtitle_text = str(getattr(book, "subtitle", "") or "")
        for parsed in extract_omnibus_ranges(title_text):
            covered_numbers.add(parsed)
        for parsed in extract_omnibus_ranges(subtitle_text):
            covered_numbers.add(parsed)

    numbered: list[float] = []
    for book in active_books:
        value = getattr(book, "book_number", None)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        numbered.append(number)

    integer_numbers = sorted(covered_numbers)
    total_books = int(max(integer_numbers)) if integer_numbers else 0
    missing_orders = [number for number in range(1, total_books + 1) if number not in set(integer_numbers)]

    read_count = 0
    unread_candidates: list[int] = []
    upcoming_candidates: list[float] = []

    for book in active_books:
        status = str(getattr(book, "read_status", "") or "").strip().lower()
        is_read = bool(getattr(book, "is_read", False)) or status == "read"

        if is_read:
            read_count += 1

        value = getattr(book, "book_number", None)
        number: float | None
        try:
            number = float(value) if value is not None else None
        except (TypeError, ValueError):
            number = None

        if number is not None and number > 0 and not is_read and status != "upcoming" and float(number).is_integer():
            unread_candidates.append(int(number))

        if number is not None and number > 0 and status == "upcoming":
            upcoming_candidates.append(number)

    unread_count = max(len(active_books) - read_count, 0)

    return {
        "series_id": series_id,
        "total_books": total_books,
        "missing_orders": missing_orders,
        "next_unread_book_number": min(unread_candidates) if unread_candidates else None,
        "next_upcoming_book_number": min(upcoming_candidates) if upcoming_candidates else None,
        "is_series_finished": bool(series.is_finished),
        "read_count": read_count,
        "unread_count": unread_count,
    }


def recalculate_series_state_for_series(db, series_id: int, scan_result: dict | None = None) -> dict:
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        return {
            "series_id": series_id,
            "has_new_books": False,
            "has_unread_books": False,
            "has_upcoming_books": False,
            "is_caught_up": False,
        }

    intelligence = compute_series_intelligence_for_series(db, series_id)
    missing_orders = intelligence.get("missing_orders") or []

    series.total_books = intelligence.get("total_books")
    series.missing_books = list(missing_orders)
    series.next_unread_book_number = intelligence.get("next_unread_book_number")
    series.next_upcoming_book_number = intelligence.get("next_upcoming_book_number")
    series.is_finished = bool(intelligence.get("is_series_finished"))
    series.series_status = "finished" if series.is_finished else "ongoing"

    has_new_from_scan = bool((scan_result or {}).get("added_count") or len((scan_result or {}).get("added_books") or []))
    series.has_new_books = bool(series.has_new_books) or has_new_from_scan
    series.has_unread_books = intelligence.get("next_unread_book_number") is not None
    series.has_upcoming_books = intelligence.get("next_upcoming_book_number") is not None
    series.is_caught_up = not series.has_unread_books and not series.has_upcoming_books and len(missing_orders) == 0

    db.commit()
    db.refresh(series)

    return {
        "series_id": series_id,
        "has_new_books": bool(series.has_new_books),
        "has_unread_books": bool(series.has_unread_books),
        "has_upcoming_books": bool(series.has_upcoming_books),
        "is_caught_up": bool(series.is_caught_up),
    }


def recalculate_intelligence(db, series_id: int, scan_result: dict | None = None) -> dict:
    intelligence = compute_series_intelligence_for_series(db, series_id)
    recalculate_series_state_for_series(db, series_id, scan_result=scan_result)
    aggregates = recount_series_aggregates_for_series(db, series_id)

    return {
        **intelligence,
        **aggregates,
    }


def recompute_series_intelligence(db):
    """
    Recompute intelligence for ALL series in the database.
    This is the function the importer expects.
    """

    all_series = db.query(Series).all()

    for series in all_series:
        recalculate_intelligence(db, series.id)

    return True

