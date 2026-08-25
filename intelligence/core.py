"""Core series-state intelligence: omnibus-range parsing, per-series
aggregate/missing-book computation, and the recalculate_intelligence
entry point that drives them.

Split out of intelligence.py (RT-4) as the module every other piece of
series-state logic ultimately depends on -- admin.py and external.py both
stay independent of this module (and of each other).

intelligence/__init__.py re-exports everything below, so existing external
callers (agents/series_agent.py, routers/series.py, services/
series_check_engine.py, tests, etc.) are unaffected by this split.
"""
from __future__ import annotations

import re

from models import Series, Book


OMNIBUS_RANGE_PATTERN = re.compile(r"\bbooks?\s+\d+(?:\.\d+)?\s*[-–]\s*\d+(?:\.\d+)?\b", re.IGNORECASE)
OMNIBUS_RANGE_CAPTURE_PATTERN = re.compile(
    r"\bbooks?\s+(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
)

_WORD_TO_NUMBER = {
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


def _roman_to_int(value: str) -> int | None:
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


def _token_to_int(token: str) -> int | None:
    cleaned = str(token or "").strip().lower()
    if not cleaned:
        return None

    if cleaned.isdigit():
        try:
            number = int(cleaned)
        except ValueError:
            return None
        return number if number > 0 else None

    if cleaned in _WORD_TO_NUMBER:
        return _WORD_TO_NUMBER[cleaned]

    return _roman_to_int(cleaned)


def extract_omnibus_ranges(text: str | None) -> set[int]:
    """Parse "Books 1-3" / "Volumes one to three" / "Book I-III" style
    ranges out of a title/subtitle so an owned omnibus edition is recognized
    as covering every individual book number in that range, not just
    whatever single book_number value happens to be stored on the row.
    Shared by intelligence (missing-book calc) and series_agent (discovery
    dedup) so both agree on what an owned omnibus already covers.
    """
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
        start = _token_to_int(match.group(1))
        end = _token_to_int(match.group(2))
        if start is None or end is None:
            continue

        low, high = (start, end) if start <= end else (end, start)
        for number in range(low, high + 1):
            extracted.add(number)

    return extracted


def recount_series_aggregates_for_series(db, series_id: int) -> dict:
    # Filtering by profile_id here too (not just series_id) is
    # defense-in-depth against a book ending up linked to this series_id
    # under a different profile_id than the series' own -- e.g. a bug in a
    # book-creation path that forgets to set profile_id explicitly (CR-10
    # removed the Book model's "robbie" fallback for exactly this failure
    # mode, but this check stays as defense-in-depth against a future
    # regression). Without this, such a "ghost" book is invisible in every
    # profile-scoped books query yet still inflates this series' aggregates.
    series_profile_id = db.query(Series.profile_id).filter(Series.id == series_id).scalar()
    books_query = db.query(Book).filter(Book.series_id == series_id)
    if series_profile_id is not None:
        books_query = books_query.filter(Book.profile_id == series_profile_id)
    books = books_query.all()

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
        # profile_id filter is defense-in-depth -- see the matching comment
        # in recount_series_aggregates_for_series above.
        .filter(Book.series_id == series_id, Book.profile_id == series.profile_id)
        .all()
    )
    active_books = [book for book in books if str(getattr(book, "record_status", "active") or "active") != "deleted"]

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


def recalculate_series_state_for_series(
    db, series_id: int, scan_result: dict | None = None, *, intelligence: dict | None = None
) -> dict:
    """Write compute_series_intelligence_for_series's results onto the
    Series row's own state columns (total_books, missing_books,
    has_unread_books, etc.) and commit.

    RT-5: `intelligence` lets a caller that's already computed it (namely
    recalculate_intelligence, which needs its own copy of the dict anyway)
    pass it straight in instead of this function recomputing it -- an
    identical, redundant Book/Series query pair every single call.
    Defaults to None so every other existing caller (services/
    series_check_engine.py, bootstrap.py, direct callers of this function)
    is unaffected and keeps computing it itself exactly as before.
    """
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        return {
            "series_id": series_id,
            "has_new_books": False,
            "has_unread_books": False,
            "has_upcoming_books": False,
            "is_caught_up": False,
        }

    if intelligence is None:
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
    # RT-5: computed once and handed to recalculate_series_state_for_series
    # below instead of letting it recompute the identical Book/Series query
    # pair itself -- see that function's docstring. total_books is
    # deliberately NOT deduped the same way: aggregates' total_books
    # (floored, counts every numbered active book) intentionally overrides
    # intelligence's own total_books (integer-numbered books only) in the
    # dict merge below -- a real, currently-live quirk left unchanged by
    # this fix (see tests/test_intelligence_recalculate.py).
    intelligence = compute_series_intelligence_for_series(db, series_id)
    recalculate_series_state_for_series(db, series_id, scan_result=scan_result, intelligence=intelligence)
    aggregates = recount_series_aggregates_for_series(db, series_id)

    return {
        **intelligence,
        **aggregates,
    }
