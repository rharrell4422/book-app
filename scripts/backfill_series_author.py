"""One-time (idempotent) backfill: populate Series.author from the author(s)
recorded on that series' own Book rows.

Discovery (both the old Amazon-scraping engine and the new official-API
engine) searches by author, so a series with no author on file cannot be
discovered at all. This script fixes series that are missing an author but
have a single, consistent author across their books. Series with zero books
or multiple conflicting authors are left alone and reported so they can be
fixed manually.

Run with: python scripts/backfill_series_author.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database import SessionLocal
from models import Book, Series


def _normalize(value: str | None) -> str:
    return " ".join(str(value or "").strip().split()).lower()


def backfill_series_author() -> None:
    db = SessionLocal()
    try:
        series_list = db.query(Series).filter(
            (Series.author.is_(None)) | (Series.author == "")
        ).all()

        updated = 0
        skipped_no_books = 0
        skipped_ambiguous: list[tuple[int, str, list[str]]] = []

        for series in series_list:
            books = (
                db.query(Book)
                .filter(Book.series_id == series.id)
                .filter((Book.record_status.is_(None)) | (Book.record_status != "deleted"))
                .all()
            )
            authors = sorted({str(book.author).strip() for book in books if str(book.author or "").strip()})
            distinct_normalized = {_normalize(a) for a in authors}

            if not authors:
                skipped_no_books += 1
                continue

            if len(distinct_normalized) > 1:
                skipped_ambiguous.append((series.id, series.name, authors))
                continue

            series.author = authors[0]
            updated += 1

        db.commit()

        print(f"Updated {updated} series with a backfilled author.")
        print(f"Skipped {skipped_no_books} series with no active books to infer an author from.")
        if skipped_ambiguous:
            print(f"Skipped {len(skipped_ambiguous)} series with multiple conflicting authors on their books:")
            for series_id, name, authors in skipped_ambiguous:
                print(f"  series_id={series_id} name={name!r} authors={authors}")
    finally:
        db.close()


if __name__ == "__main__":
    backfill_series_author()
