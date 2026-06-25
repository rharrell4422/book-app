# intelligence.py
# Complete Intelligence Engine (Books + Series + Summaries + Upcoming Detection)
# Rebuilt cleanly for your current schema

import datetime
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any

from models import Book, Series
# ---------------------------------------------------------------------------
# UTILITY HELPERS
# ---------------------------------------------------------------------------

def normalize_date(value) -> Optional[datetime.date]:
    """Convert strings or datetimes into a clean date object."""
    if not value:
        return None
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, datetime.datetime):
        return value.date()
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%d").date()
    except:
        return None


def today() -> datetime.date:
    """Returns today's date."""
    return datetime.date.today()


def is_future_date(value) -> bool:
    """True if the date is in the future."""
    d = normalize_date(value)
    return d is not None and d > today()
# ---------------------------------------------------------------------------
# BOOK INTELLIGENCE ENGINE
# ---------------------------------------------------------------------------

def compute_book_intelligence(db: Session, book: Book) -> Dict[str, Any]:
    """
    Recomputes intelligence fields for a single book:
    - Normalize dates
    - Detect upcoming releases
    - Generate summary if read
    - Update timestamps
    """

    # Normalize publication date
    book.publication_date = normalize_date(book.publication_date)

    # Upcoming detection
    book.is_upcoming = False
    if book.publication_date and is_future_date(book.publication_date):
        book.is_upcoming = True

    # Summary generation (only if read and no summary exists)
    if book.is_read and not book.auto_summary:
        book.auto_summary = generate_summary(book)

    # Update timestamp
    book.updated_at = datetime.datetime.utcnow()

    db.add(book)
    db.commit()

    return {
        "book_id": book.id,
        "title": book.title,
        "is_read": book.is_read,
        "is_upcoming": book.is_upcoming,
        "publication_date": book.publication_date,
        "auto_summary": book.auto_summary,
    }
# ---------------------------------------------------------------------------
# SUMMARY GENERATOR
# ---------------------------------------------------------------------------

def generate_summary(book: Book) -> str:
    """
    Creates a simple, local summary for a book based on its metadata.
    This avoids external APIs and keeps the app self-contained.
    """

    parts = []

    if book.title:
        parts.append(f"'{book.title}'")

    if book.author:
        parts.append(f"by {book.author}")

    if book.series_id and book.book_number:
        parts.append(f"(Book {book.book_number} in its series)")

    if book.notes:
        parts.append(f"Notes: {book.notes}")

    # Fallback if nothing else exists
    if not parts:
        return "No summary available."

    return " — ".join(parts)
# ---------------------------------------------------------------------------
# UPCOMING + NEXT BOOK DETECTION HELPERS
# ---------------------------------------------------------------------------

def get_next_unread_book(books: List[Book]) -> Optional[Book]:
    """Return the first unread book in ascending book_number order."""
    for b in sorted(books, key=lambda x: (x.book_number or 9999)):
        if not b.is_read:
            return b
    return None


def get_next_upcoming_book(books: List[Book]) -> Optional[Book]:
    """Return the next book with a future publication date."""
    future_books = [
        b for b in books
        if b.publication_date and is_future_date(b.publication_date)
    ]

    if not future_books:
        return None

    return sorted(future_books, key=lambda x: x.publication_date)[0]


def find_missing_book_numbers(books: List[Book]) -> List[int]:
    """Detect gaps in the book_number sequence."""
    numbers = sorted([b.book_number for b in books if b.book_number is not None])

    if not numbers:
        return []

    missing = []
    for n in range(numbers[0], numbers[-1] + 1):
        if n not in numbers:
            missing.append(n)

    return missing
# ---------------------------------------------------------------------------
# SERIES INTELLIGENCE ENGINE
# ---------------------------------------------------------------------------

def compute_series_intelligence(db: Session, series: Series) -> Dict[str, Any]:
    """
    Recomputes intelligence fields for a series:
    - total_books (auto count)
    - is_finished (True if all books are read)
    - next_unread_book
    - next_upcoming_book
    - missing book numbers
    """

    # Load all books in this series
    books = (
        db.query(Book)
        .filter(Book.series_id == series.id)
        .order_by(Book.book_number.asc())
        .all()
    )

    total_books = len(books)
    read_books = sum(1 for b in books if b.is_read)
    unread_books = total_books - read_books

    next_unread = get_next_unread_book(books)
    next_upcoming = get_next_upcoming_book(books)
    missing_numbers = find_missing_book_numbers(books)

    # Update series fields
    series.total_books = total_books
    series.is_finished = total_books > 0 and unread_books == 0
    series.updated_at = datetime.datetime.utcnow()

    db.add(series)
    db.commit()

    return {
        "series_id": series.id,
        "series_name": series.name,
        "total_books": total_books,
        "read_books": read_books,
        "unread_books": unread_books,
        "is_finished": series.is_finished,
        "next_unread_book": next_unread.book_number if next_unread else None,
        "next_upcoming_book": next_upcoming.book_number if next_upcoming else None,
        "missing_numbers": missing_numbers,
    }
# ---------------------------------------------------------------------------
# FASTAPI-SAFE WRAPPERS + EXPORTS
# ---------------------------------------------------------------------------

def recompute_book(db: Session, book_id: int) -> Dict[str, Any]:
    """Convenience wrapper to recompute intelligence for a single book by ID."""
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        return {"error": f"Book {book_id} not found."}
    return compute_book_intelligence(db, book)


def recompute_series(db: Session, series_id: int) -> Dict[str, Any]:
    """Convenience wrapper to recompute intelligence for a single series by ID."""
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        return {"error": f"Series {series_id} not found."}
    return compute_series_intelligence(db, series)


def recompute_all_series(db: Session) -> List[Dict[str, Any]]:
    """Recompute intelligence for all series in the database."""
    series_list = db.query(Series).all()
    results = []
    for s in series_list:
        results.append(compute_series_intelligence(db, s))
    return results


def recompute_all_books(db: Session) -> List[Dict[str, Any]]:
    """Recompute intelligence for all books in the database."""
    books = db.query(Book).all()
    results = []
    for b in books:
        results.append(compute_book_intelligence(db, b))
    return results


# ---------------------------------------------------------------------------
# EXPORTS
# ---------------------------------------------------------------------------

__all__ = [
    "compute_book_intelligence",
    "compute_series_intelligence",
    "generate_summary",
    "get_next_unread_book",
    "get_next_upcoming_book",
    "find_missing_book_numbers",
    "recompute_book",
    "recompute_series",
    "recompute_all_books",
    "recompute_all_series",
]
