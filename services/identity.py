"""Identity/dedup matching used when persisting discovered candidates against
existing library rows (Check Now write path).

NOTE: agents/series_agent.py has its own, deliberately different, identity
helpers (e.g. its own `_authors_match_exact`). That module answers a
different question -- "does this raw search result plausibly belong to this
series at all?" -- and is intentionally more lenient. This module answers a
stricter question -- "is this candidate the same row as one already in the
database?" -- for safe insert/update/skip decisions. They are not
interchangeable, so they are kept as separate implementations rather than
merged.
"""

import re
from datetime import date

import models


def _normalize_discovered_title(value: str | None) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return re.sub(r"[^a-z0-9]+", "", cleaned)


def _normalize_identity_text(value: str | None) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return re.sub(r"[^a-z0-9]+", " ", cleaned).strip()


def _normalize_author_for_identity(value: str | None) -> str:
    text = _normalize_identity_text(value)
    text = re.sub(r"\band\s+\d+\s+more\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(author|narrator|editor)\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _authors_match_exact(series_author: str | None, candidate_author: str | None) -> bool:
    series_norm = _normalize_author_for_identity(series_author)
    candidate_norm = _normalize_author_for_identity(candidate_author)
    if not series_norm or not candidate_norm:
        return False
    return series_norm == candidate_norm


def _normalize_series_name_for_identity(value: str | None) -> str:
    text = _normalize_identity_text(value)
    text = re.sub(r"\b(series|book series)\b", "", text).strip()
    return re.sub(r"\s+", " ", text).strip()


def _normalize_title_for_identity(value: str | None) -> str:
    text = str(value or "").strip()
    text = re.sub(
        r"\((?:audible|audible audio|audio cd|kindle|kindle edition|paperback|hardcover|mass market paperback)[^)]*\)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*[:\-]\s*(audible|kindle|paperback|hardcover)\b.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+,\s+book\s+\d+\b", "", text, flags=re.IGNORECASE)
    return _normalize_identity_text(text)


def _normalized_book_number_value(value) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        parsed = int(float(value))
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _series_book_identity_key(series_name: str | None, book_number) -> str | None:
    normalized_series = _normalize_series_name_for_identity(series_name)
    normalized_book_number = _normalized_book_number_value(book_number)
    if not normalized_series or normalized_book_number is None:
        return None
    return f"{normalized_series}|{normalized_book_number}"


def _canonical_title_identity_key(title: str | None) -> str | None:
    normalized_title = _normalize_title_for_identity(title)
    return normalized_title or None


def _edition_priority(value: str | None) -> int:
    edition = str(value or "").strip().lower()
    priorities = {
        "hardcover": 5,
        "paperback": 4,
        "ebook": 3,
        "audio": 2,
        "unknown": 1,
        "": 1,
    }
    return priorities.get(edition, 1)


def _is_upcoming_future_book(book: "models.Book", *, today: date) -> bool:
    status = str(getattr(book, "read_status", "") or "").strip().lower()
    publication_date = getattr(book, "publication_date", None)
    if status != "upcoming" or not isinstance(publication_date, date):
        return False
    return publication_date > today
