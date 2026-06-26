import pandas as pd
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

import models
import schemas
from crud import (
    create_book,
    update_book,
    get_book,
    get_all_books,
    create_series,
    update_series,
    get_series,
    get_all_series,
)


# ---------------------------------------------------------
# Helper: Convert Excel serial date → Python date
# ---------------------------------------------------------
def excel_date_to_date(value: Any) -> Optional[datetime]:
    if value is None or value == "" or str(value).strip() == "nan":
        return None

    try:
        # Excel serial dates: days since 1899-12-30
        base = datetime(1899, 12, 30)
        return base + timedelta(days=float(value))
    except:
        return None


# ---------------------------------------------------------
# Helper: Normalize strings
# ---------------------------------------------------------
def clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value if value else None


# ---------------------------------------------------------
# Helper: Normalize book number
# ---------------------------------------------------------
def clean_book_number(value: Any) -> Optional[int]:
    if value is None or value == "" or str(value).strip() == "nan":
        return None
    try:
        return int(float(value))
    except:
        return None


# ---------------------------------------------------------
# Main Import Function
# ---------------------------------------------------------
def import_from_file(db: Session, filename: str) -> Dict[str, Any]:
    # Detect file type
    if filename.lower().endswith(".csv"):
        df = pd.read_csv(filename)
    else:
        df = pd.read_excel(filename)

    # Summary counters
    summary = {
        "rows_processed": 0,
        "books_created": 0,
        "books_updated": 0,
        "series_created": 0,
        "series_updated": 0,
        "rows_skipped": 0,
        "skipped_details": [],
    }

    # Cache existing series for speed
    series_cache = {s.name.lower(): s for s in get_all_series(db)}
    # Cache existing books for speed (ISBN or title+author)
    books_cache = get_all_books(db)

    for _, row in df.iterrows():
        summary["rows_processed"] += 1

        title = clean_str(row.get("Title"))
        author = clean_str(row.get("Author"))

        if not title or not author:
            summary["rows_skipped"] += 1
            summary["skipped_details"].append(f"Missing title/author: {row}")
            continue

        isbn = clean_str(row.get("ISBN"))
        series_name = clean_str(row.get("Series Name"))
        book_number = clean_book_number(row.get("Book #"))
        series_finished = clean_str(row.get("Series Finished"))
        record_status = clean_str(row.get("Record Status"))
        date_read = excel_date_to_date(row.get("Date Read"))
        next_release = excel_date_to_date(row.get("Next Release Date"))

        is_read = record_status == "Read"
        is_upcoming = record_status == "Upcoming"

        # ---------------------------------------------------------
        # SERIES HANDLING
        # ---------------------------------------------------------
        series_obj = None
        if series_name:
            key = series_name.lower()
            if key in series_cache:
                series_obj = series_cache[key]
            else:
                # Create new series
                new_series = schemas.SeriesCreate(
                    name=series_name,
                    total_books=None,
                    is_finished=(series_finished == "Yes"),
                )
                series_obj = create_series(db, new_series)
                series_cache[key] = series_obj
                summary["series_created"] += 1

            # Update series finished flag if provided
            if series_finished is not None:
                updated = update_series(
                    db,
                    series_obj.id,
                    schemas.SeriesUpdate(is_finished=(series_finished == "Yes")),
                )
                if updated:
                    summary["series_updated"] += 1

        # ---------------------------------------------------------
        # BOOK MATCHING
        # ---------------------------------------------------------
        matched_book = None

        # 1. Match by ISBN
        if isbn:
            for b in books_cache:
                if b.isbn == isbn:
                    matched_book = b
                    break

        # 2. Match by title + author
        if not matched_book:
            for b in books_cache:
                if b.title.lower() == title.lower() and b.author.lower() == author.lower():
                    matched_book = b
                    break

        # ---------------------------------------------------------
        # CREATE OR UPDATE BOOK
        # ---------------------------------------------------------
        if matched_book:
            # Update existing
            update_data = schemas.BookUpdate(
                title=title,
                author=author,
                isbn=isbn,
                series_id=series_obj.id if series_obj else None,
                series_order=book_number,
                is_series_finished=(series_finished == "Yes") if series_finished else None,
                is_read=is_read,
                read_date=date_read,
                next_release_date=next_release,
                is_upcoming=is_upcoming,
            )
            update_book(db, matched_book.id, update_data)
            summary["books_updated"] += 1

        else:
            # Create new
            new_book = schemas.BookCreate(
                title=title,
                author=author,
                isbn=isbn,
                series_id=series_obj.id if series_obj else None,
                series_order=book_number,
                is_series_finished=(series_finished == "Yes") if series_finished else None,
                is_read=is_read,
                read_date=date_read,
                next_release_date=next_release,
                is_upcoming=is_upcoming,
            )
            created = create_book(db, new_book)
            books_cache.append(created)
            summary["books_created"] += 1

    return summary
