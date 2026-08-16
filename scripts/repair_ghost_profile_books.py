"""One-time data repair: fix Book rows whose profile_id doesn't match the
profile_id of the Series they're linked to.

Background: Book.profile_id defaults to "robbie" when not passed explicitly
(see models.py). The "Check for New" book-creation path
(services/series_check_engine.py) used to construct newly discovered Book
rows without ever setting profile_id, so any book discovered while checking
a *non-robbie* profile's series silently got profile_id="robbie" while
staying linked to that other profile's series_id.

The result is an invisible "ghost" row: every profile-scoped books query
(crud.get_books_by_series, crud.get_all_books, etc.) filters by profile_id
and never surfaces it, so neither the owning profile nor "robbie" can see or
delete it in the UI -- yet series-id-only aggregates like
compute_series_intelligence_for_series / recount_series_aggregates_for_series
still counted it, inflating that series' total_books/upcoming/available
flags with a book nobody can actually see (e.g. a phantom "Book 4" showing
up in the has_new_upcoming_books icon and Total count for a profile's
series, with no matching row in "View Books in Series").

This script finds every Book whose profile_id differs from its Series'
profile_id and reassigns the book's profile_id to match its series. It never
touches books with no series_id (those aren't affected by this bug).

Defaults to a dry run (report only). Pass --apply to write changes.

Run with: python scripts/repair_ghost_profile_books.py [--apply]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database import SessionLocal
from models import Book, Series


def repair(apply: bool) -> None:
    db = SessionLocal()
    try:
        mismatched = (
            db.query(Book, Series)
            .join(Series, Book.series_id == Series.id)
            .filter(Book.profile_id != Series.profile_id)
            .all()
        )

        print(f"{'Would repair' if not apply else 'Repaired'} {len(mismatched)} ghost book row(s):\n")
        for book, series in mismatched:
            print(
                f"  book_id={book.id} title={book.title!r} series={series.name!r} (series_id={series.id}): "
                f"profile_id {book.profile_id!r} -> {series.profile_id!r}"
            )
            if apply:
                book.profile_id = series.profile_id

        if apply and mismatched:
            db.commit()
            print("\nChanges committed.")
        elif mismatched:
            print("\nDry run only -- no changes written. Re-run with --apply to commit.")
            db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write the repaired profile_id to the database.")
    args = parser.parse_args()
    repair(apply=args.apply)
