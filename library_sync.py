from __future__ import annotations

from datetime import date

from sqlalchemy import or_

import models
from database import SessionLocal


def update_from_series(series_id: int, profile_id: str | None = None) -> dict:
    """`profile_id`: CR-9 -- when given, scopes the Book query to it in
    addition to `series_id`. `series_id` alone would also silently mutate
    any "ghost" cross-profile Book row that happens to carry this
    `series_id` (the owning Series row is checked by its caller, but a
    stray Book row is not implicitly protected by that check). Optional
    (default `None`, meaning unscoped) only for backward compatibility with
    call sites/tests that predate profile scoping; every real production
    call site should pass it.
    """
    db = SessionLocal()
    try:
        query = db.query(models.Book).filter(models.Book.series_id == series_id)
        if profile_id is not None:
            query = query.filter(models.Book.profile_id == profile_id)
        canonical_books = query.filter(
            or_(models.Book.record_status.is_(None), models.Book.record_status != "deleted")
        ).all()

        updated_rows = 0
        inserted_rows = 0
        mirrored_rows = 0

        today = date.today()

        for book in canonical_books:
            mirrored_rows += 1
            changed = False

            is_read = bool(book.is_read) or str(book.read_status or "").strip().lower() == "read"
            if is_read:
                # Preserve user-managed read timeline for owned books.
                if str(book.read_status or "").strip().lower() != "read":
                    book.read_status = "read"
                    changed = True
                if book.is_missing:
                    book.is_missing = False
                    changed = True
            else:
                # Keep future releases as upcoming; everything else unread/non-read should be available.
                publication_date = getattr(book, "publication_date", None)
                release_date = getattr(book, "release_date", None)
                candidate_date = release_date or publication_date
                has_known_date = isinstance(candidate_date, date)
                is_future_release = has_known_date and candidate_date > today

                explicit_status = str(book.read_status or "").strip().lower()
                is_marked_upcoming = bool(book.is_upcoming_auto) or bool(book.is_upcoming_final) or explicit_status == "upcoming"

                if has_known_date:
                    # A concrete date is the strongest signal available -- once it
                    # has passed, the book is out even if it was previously (or is
                    # still) flagged upcoming, e.g. a stale spreadsheet-import date
                    # from before the release, or an old auto-discovery run.
                    # Without this, a book stuck at read_status="upcoming" would
                    # never self-heal since that flag alone used to keep it upcoming
                    # forever, even long after its date had passed.
                    should_be_upcoming = is_future_release
                else:
                    # No date to go on (e.g. an announced-but-undated preorder) --
                    # trust whatever previously classified this as upcoming.
                    should_be_upcoming = is_marked_upcoming

                if should_be_upcoming:
                    if explicit_status != "upcoming":
                        book.read_status = "upcoming"
                        changed = True
                else:
                    if explicit_status != "available":
                        book.read_status = "available"
                        changed = True
                    if bool(book.is_upcoming_auto):
                        book.is_upcoming_auto = False
                        changed = True
                    if bool(book.is_upcoming_final):
                        book.is_upcoming_final = False
                        changed = True

            if changed:
                updated_rows += 1

        if updated_rows > 0:
            db.commit()

        return {
            "series_id": series_id,
            "mirrored_rows": mirrored_rows,
            "inserted_rows": inserted_rows,
            "updated_rows": updated_rows,
            "synced_at": date.today().isoformat(),
        }
    finally:
        db.close()
