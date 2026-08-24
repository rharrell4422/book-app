"""Admin/repair intelligence helpers: orphaned-book purges and the
ghost-profile and fractional-identity-collision repair tools backing the
/admin routes.

Split out of intelligence.py (RT-4). Independent of core.py/external.py --
see intelligence/core.py's module docstring.

intelligence/__init__.py re-exports everything below, so existing external
callers (routers/admin.py, tests, etc.) are unaffected by this split.

DC-13: the one-time standalone CLI script that used to also call into this
module (scripts/repair_ghost_profile_books.py) was removed as redundant
with the live GET/POST /admin/ghost_profile_books endpoints below, which
apply the same repair directly with no download/upload round trip.
"""
from __future__ import annotations

from models import Series, Book


def purge_orphaned_books(db) -> dict:
    """Delete books whose series_id points at a series row that no longer
    exists (e.g. left over from a deleted series that didn't cascade)."""
    books = db.query(Book).filter(Book.series_id.is_not(None)).all()
    existing_series_ids = {int(row[0]) for row in db.query(Series.id).all()}

    deleted_entries: list[dict] = []
    for book in books:
        if int(book.series_id) in existing_series_ids:
            continue
        deleted_entries.append(
            {
                "book_id": book.id,
                "title": book.title,
                "series_id": book.series_id,
            }
        )
        db.delete(book)

    if deleted_entries:
        db.commit()

    return {
        "deleted_count": len(deleted_entries),
        "deleted_entries": deleted_entries,
    }


def find_ghost_profile_books(db) -> list[dict]:
    """Books whose profile_id doesn't match the profile_id of the series
    they're linked to (see repair_ghost_profile_books for how this happens).
    Read-only -- safe to call any time to check for contamination."""
    rows = (
        db.query(Book, Series)
        .join(Series, Book.series_id == Series.id)
        .filter(Book.profile_id != Series.profile_id)
        .all()
    )
    return [
        {
            "book_id": book.id,
            "title": book.title,
            "current_profile_id": book.profile_id,
            "correct_profile_id": series.profile_id,
            "series_id": series.id,
            "series_name": series.name,
        }
        for book, series in rows
    ]


def repair_ghost_profile_books(db) -> dict:
    """Reassign every "ghost" book found by find_ghost_profile_books to its
    series' own profile_id.

    Background: this repairs rows created before two fixes landed --
    Book.profile_id used to default to "robbie" when not passed explicitly
    (removed by CR-10; see models.py), and at least one book-creation path
    (the "Check for New" discovery job, before it was fixed in
    services/series_check_engine.py) forgot to set it explicitly, silently
    tagging the new row profile_id="robbie" while it stayed linked to
    whichever profile's series triggered the discovery. That row became
    invisible to every profile-scoped books query for the series' *actual*
    owner, yet still showed up in "robbie"'s flat library list (which only
    filters by profile_id, not by whether the linked series also belongs
    to robbie). Both root causes are fixed now, but this repair function
    stays for any row a pre-fix run already left in that state.
    """
    ghosts = find_ghost_profile_books(db)
    if ghosts:
        book_ids = [entry["book_id"] for entry in ghosts]
        books_by_id = {book.id: book for book in db.query(Book).filter(Book.id.in_(book_ids)).all()}
        for entry in ghosts:
            book = books_by_id.get(entry["book_id"])
            if book is not None:
                book.profile_id = entry["correct_profile_id"]
        db.commit()

    return {
        "repaired_count": len(ghosts),
        "repaired_entries": ghosts,
    }


def list_soft_deleted_books(db, series_id: int | None = None) -> list[dict]:
    """Books currently marked record_status="deleted" (soft-deleted, not
    actually removed from the table) -- e.g. the "loser" side of a Check for
    New dedupe collapse. Useful for recovering a row that a collapse pass
    wrongly picked as the duplicate, since soft-deleting never touches the
    row's other fields."""
    query = db.query(Book, Series).outerjoin(Series, Book.series_id == Series.id).filter(Book.record_status == "deleted")
    if series_id is not None:
        query = query.filter(Book.series_id == series_id)
    rows = query.all()
    return [
        {
            "book_id": book.id,
            "title": book.title,
            "book_number": book.book_number,
            "series_id": book.series_id,
            "series_name": series.name if series else None,
            "is_read": bool(book.is_read),
            "read_status": book.read_status,
            "publication_date": book.publication_date.isoformat() if book.publication_date else None,
            "release_date": book.release_date.isoformat() if book.release_date else None,
            "rating": book.rating,
            "notes": book.notes,
        }
        for book, series in rows
    ]


def restore_soft_deleted_book(db, book_id: int) -> dict:
    """Set record_status back to "active" for one specific book row, with no
    other field changes. Does not touch any other row -- if a dedupe
    collapse also merged fields onto whichever row "won", those need fixing
    separately (see list_soft_deleted_books' docstring)."""
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        return {"status": "not_found", "book_id": book_id}
    book.record_status = "active"
    db.commit()
    return {"status": "restored", "book_id": book_id, "title": book.title}


def _truncated_identity_number(book_number) -> int | None:
    """Reproduces the *pre-fix* (buggy) services/identity.py behavior:
    int(float(book_number)), which truncates a fractional number down to
    its whole-number neighbor instead of rounding. Used only to retroactively
    find rows affected by that bug -- see find_fractional_identity_collisions."""
    try:
        if book_number is None:
            return None
        parsed = int(float(book_number))
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def find_fractional_identity_collisions(db) -> list[dict]:
    """Finds the specific damage pattern left by the (now-fixed)
    services/identity.py bug: two books in the same series with genuinely
    *different* book_number values (e.g. 3 and 3.5) that used to truncate to
    the same identity key, so a Check for New dedupe pass treated them as
    the same book and collapsed one into "deleted" (or merged its fields
    onto the other).

    Deliberately narrow, unlike list_soft_deleted_books: a soft-deleted row
    only shows up here if there's a *different-numbered* sibling (active or
    also deleted) in the same series that collides with it under the old
    truncating key. A soft-deleted row from an ordinary, legitimate
    duplicate collapse (same book found via two providers, same or no
    book_number) is not included.
    """
    all_books = (
        db.query(Book, Series)
        .outerjoin(Series, Book.series_id == Series.id)
        .filter(Book.series_id.isnot(None), Book.book_number.isnot(None))
        .all()
    )

    groups: dict[tuple[int, int], list[tuple]] = {}
    for book, series in all_books:
        truncated = _truncated_identity_number(book.book_number)
        if truncated is None:
            continue
        groups.setdefault((book.series_id, truncated), []).append((book, series))

    collisions: list[dict] = []
    for (series_id, truncated), members in groups.items():
        distinct_numbers = {float(book.book_number) for book, _series in members}
        has_deleted = any(str(book.record_status or "") == "deleted" for book, _series in members)
        if len(distinct_numbers) <= 1 or not has_deleted:
            continue

        series_name = next((series.name for _book, series in members if series), None)
        collisions.append(
            {
                "series_id": series_id,
                "series_name": series_name,
                "collided_truncated_number": truncated,
                "members": [
                    {
                        "book_id": book.id,
                        "title": book.title,
                        "book_number": book.book_number,
                        "record_status": book.record_status or "active",
                        "is_read": bool(book.is_read),
                        "read_status": book.read_status,
                    }
                    for book, _series in members
                ],
            }
        )

    return collisions
