from __future__ import annotations

from datetime import date

from sqlalchemy import or_

import models
from database import SessionLocal


def update_from_series(series_id: int) -> dict:
    db = SessionLocal()
    try:
        canonical_books = (
            db.query(models.Book)
            .filter(models.Book.series_id == series_id)
            .filter(or_(models.Book.record_status.is_(None), models.Book.record_status != "deleted"))
            .all()
        )

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
                is_future_release = isinstance(candidate_date, date) and candidate_date > today

                explicit_status = str(book.read_status or "").strip().lower()
                is_marked_upcoming = bool(book.is_upcoming_auto) or bool(book.is_upcoming_final) or explicit_status == "upcoming"

                if is_future_release or is_marked_upcoming:
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
