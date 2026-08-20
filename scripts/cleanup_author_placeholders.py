"""One-time (idempotent) repair for the "Unknown author" placeholder bug:
Add Book's save-time fallback and two prefilled-form defaults used to write
the literal string "Unknown author" whenever a locked series had no author
on file yet (see use-add-book-form.ts / add-book/page.tsx / series/[id]
page.tsx history). That value survives the plain non-empty check every
write path already has, and gets *more* dangerous once normalized:
services/identity.py's _normalize_author_for_identity strips the literal
word "author" as a role descriptor, so "Unknown author" and "Unknown" both
collapse to the same non-empty token "unknown" -- which then compares equal
to any *other* placeholder-tainted row, silently fusing otherwise-unrelated
series into the same author identity everywhere author is used as a lookup
key (known-sibling-series sets, the author-wide tracked-series map,
discovery's author-match gate).

The existing scripts/backfill_series_author.py only targets NULL/empty
Series.author values, so it can't repair a series already poisoned with a
placeholder -- this script does that in two phases, then re-runs the
existing backfill so a real author (if the series' own books have one) gets
adopted in its place:

  Phase 1: null out Series.author (and blank out Book.author -- it's a
           NOT NULL column, so "cleared" means "" rather than NULL) wherever
           the normalized value matches the placeholder denylist.
  Phase 2: run the existing backfill_series_author() logic, which adopts a
           genuine author from the series' own (now-cleaned) books where one
           exists.

Both phases are idempotent: once a row's placeholder value has been
cleared, re-running this script finds nothing left to do for it.

After running, any series that got its author nulled out in Phase 1 and did
NOT get a real one restored in Phase 2 has never had a successful Check Now
scan (a placeholder author made every prior scan a guaranteed-empty search)
-- its absence of discovered books is not evidence that none exist, and it
should be re-scanned once it has a real author.

Run with: python scripts/cleanup_author_placeholders.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database import SessionLocal
from models import Book, Series
from services.identity import is_placeholder_author
from scripts.backfill_series_author import backfill_series_author


def _clear_placeholder_series_authors(db) -> list[tuple[int, str, str]]:
    cleared: list[tuple[int, str, str]] = []
    candidates = db.query(Series).filter(Series.author.isnot(None), Series.author != "").all()
    for series in candidates:
        if is_placeholder_author(series.author):
            cleared.append((series.id, series.name, series.author))
            series.author = None
    if cleared:
        db.commit()
    return cleared


def _clear_placeholder_book_authors(db) -> list[tuple[int, str, str]]:
    cleared: list[tuple[int, str, str]] = []
    candidates = db.query(Book).filter(Book.author.isnot(None), Book.author != "").all()
    for book in candidates:
        if is_placeholder_author(book.author):
            cleared.append((book.id, book.title, book.author))
            # Book.author is NOT NULL -- "" is the closest equivalent to NULL
            # available, and is_placeholder_author("") is False, so it can
            # never re-poison a series' backfilled author the way the
            # placeholder value could.
            book.author = ""
    if cleared:
        db.commit()
    return cleared


def cleanup_author_placeholders() -> dict:
    db = SessionLocal()
    try:
        cleared_series = _clear_placeholder_series_authors(db)
        cleared_books = _clear_placeholder_book_authors(db)
    finally:
        db.close()

    # Runs in its own session (backfill_series_author manages its own),
    # after Phase 1's commits are visible -- adopts a real author from each
    # now-authorless series' own books where one exists.
    backfill_series_author()

    print(f"Phase 1: cleared placeholder author from {len(cleared_series)} series.")
    for series_id, name, old_value in cleared_series:
        print(f"  series_id={series_id} name={name!r} was={old_value!r}")

    print(f"Phase 1: cleared placeholder author from {len(cleared_books)} books.")
    for book_id, title, old_value in cleared_books:
        print(f"  book_id={book_id} title={title!r} was={old_value!r}")

    if cleared_series:
        print(
            "\nNote: series listed above had a placeholder author, which made "
            "every prior Check Now scan for them a guaranteed-empty search. "
            "Re-scan them once they have a real author (either backfilled "
            "above or entered manually) -- their lack of discovered books is "
            "not evidence that none exist."
        )

    return {
        "cleared_series_count": len(cleared_series),
        "cleared_series": cleared_series,
        "cleared_books_count": len(cleared_books),
        "cleared_books": cleared_books,
    }


if __name__ == "__main__":
    cleanup_author_placeholders()
