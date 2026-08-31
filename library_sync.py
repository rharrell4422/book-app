from __future__ import annotations

from datetime import date

from sqlalchemy import or_

import models
from database import SessionLocal
from services.availability_bridge import (
    derive_legacy_fields,
    normalize_availability_status,
    should_self_heal_stale_upcoming,
)


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
        mirrored_rows = 0

        today = date.today()

        for book in canonical_books:
            mirrored_rows += 1
            changed = False

            is_read = bool(book.is_read) or str(book.read_status or "").strip().lower() == "read"
            availability_status = normalize_availability_status(book.availability_status)
            availability_locked = bool(book.availability_locked)

            if is_read:
                # Preserve user-managed read timeline for owned books --
                # the availability axis is left completely alone here (a
                # read book obviously isn't "upcoming", but this function
                # has no basis to guess "available" vs "owned" for it, and
                # doesn't need to -- derive_legacy_fields below already
                # forces read_status="read" purely from is_read regardless
                # of whatever availability_status ends up being).
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

                is_marked_upcoming = availability_status == "upcoming"

                if has_known_date:
                    # A concrete date is the strongest signal available -- once it
                    # has passed, the book is out even if it was previously (or is
                    # still) flagged upcoming, e.g. a stale spreadsheet-import date
                    # from before the release, or an old auto-discovery run.
                    # Without this, a book stuck at availability_status="upcoming"
                    # would never self-heal since that flag alone used to keep it
                    # upcoming forever, even long after its date had passed.
                    should_be_upcoming = is_future_release
                else:
                    # No date to go on (e.g. an announced-but-undated preorder) --
                    # trust whatever previously classified this as upcoming.
                    should_be_upcoming = is_marked_upcoming

                # upcoming -> available is the one self-heal that fires even
                # through a lock: a *locked* "upcoming" whose own release
                # date has since passed is stale by definition, not a case
                # of second-guessing a deliberate user/import choice (see
                # services/availability_bridge.should_self_heal_stale_upcoming
                # and the frontend's matching exception in book-format.ts /
                # getBookStatus). No other transition -- and specifically
                # never "owned"/"available" -> "upcoming" -- ever overrides
                # a lock.
                stale_upcoming_self_heal = (
                    availability_locked
                    and not should_be_upcoming
                    and should_self_heal_stale_upcoming(availability_status, candidate_date, today)
                )

                if not availability_locked or stale_upcoming_self_heal:
                    next_status = "upcoming" if should_be_upcoming else "available"
                    if next_status != availability_status:
                        availability_status = next_status
                        changed = True
                    if stale_upcoming_self_heal:
                        availability_locked = False
                        changed = True

            if book.availability_status != availability_status:
                book.availability_status = availability_status
                changed = True
            if bool(book.availability_locked) != availability_locked:
                book.availability_locked = availability_locked
                changed = True

            for key, value in derive_legacy_fields(is_read, availability_status, availability_locked).items():
                if getattr(book, key) != value:
                    setattr(book, key, value)
                    changed = True

            if changed:
                updated_rows += 1

        if updated_rows > 0:
            db.commit()

        return {
            "series_id": series_id,
            "mirrored_rows": mirrored_rows,
            "updated_rows": updated_rows,
            "synced_at": date.today().isoformat(),
        }
    finally:
        db.close()
