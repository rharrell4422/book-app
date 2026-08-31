import models


from sqlalchemy import or_
from sqlalchemy.orm import Session
import discovery_engine
from models import Book, Series
from intelligence import recalculate_intelligence
from services.availability_bridge import derive_legacy_fields, normalize_availability_status
from services.identity import is_placeholder_author
from services.metadata_provenance import provenance_for_declined_or_manual_entry, provenance_for_find_bind


BOOK_COLUMN_KEYS = {column.key for column in Book.__table__.columns}


class InvalidSeriesForProfileError(ValueError):
    """Raised when a book create/update payload's series_id belongs to a
    different profile than the one making the request. series_id is a
    plain client-settable field on BookBase, so without this check a
    request could link a book into another profile's series just by
    guessing/reusing an id -- everything else in this module already scopes
    reads/writes to the calling profile by row id, but that alone doesn't
    stop a *foreign key value* pointing across profiles.
    """


class BookNumberRequiresSeriesError(ValueError):
    """Raised when a create/update payload sets book_number without a
    series_id. The Add/Edit Book UI's own Standalone/Series toggle (see
    add-book-form-fields.tsx) already prevents this from the app itself by
    clearing bookNumber whenever Standalone is selected -- this is the
    server-side backstop for API misuse or any other/future client that
    bypasses that form, since a book_number with no series_id is exactly
    the orphaned state ("Book #1" that never got attached to a Series row,
    so it silently landed in Standalone with no Check Now available) this
    whole feature exists to prevent.
    """


def _validate_series_belongs_to_profile(db: Session, series_id: int | None, profile_id: str) -> None:
    if series_id is None:
        return
    exists = db.query(Series.id).filter(Series.id == series_id, Series.profile_id == profile_id).first()
    if not exists:
        raise InvalidSeriesForProfileError(f"Series {series_id} does not belong to profile '{profile_id}'")


def _validate_book_number_requires_series(
    payload: dict, *, existing_book_number: float | None = None, existing_series_id: int | None = None
) -> None:
    """Book number only means anything relative to a series -- a book_number
    with no series_id is the exact orphaned state this whole feature exists
    to prevent (see BookNumberRequiresSeriesError). Checks the *effective*
    post-request value of each field, not just whatever this one payload
    happens to touch: a PATCH that clears series_id without also touching
    book_number should be rejected just as much as one that sets both at
    once, so an untouched field (absent from payload, since callers build
    it with exclude_unset=True on update) falls back to the row's current
    value rather than being treated as None.
    """
    effective_book_number = payload["book_number"] if "book_number" in payload else existing_book_number
    effective_series_id = payload["series_id"] if "series_id" in payload else existing_series_id
    if effective_book_number is not None and effective_series_id is None:
        raise BookNumberRequiresSeriesError("Book number requires a series.")


def _backfill_series_author_if_missing(db: Session, series_id: int | None, book_author: str | None) -> None:
    """Discovery searches by Series.author, so keep it populated. If a series
    has no author on file yet, adopt the author of a book being added/edited
    on it rather than leaving discovery permanently unable to run for that
    series.

    Refuses to adopt a placeholder value (e.g. "Unknown author", "N/A") --
    see is_placeholder_author's docstring for why normalization makes those
    more dangerous than an empty value rather than less. This guard only
    covers adoption into Series.author; it never rejects the book create/
    update itself, keeping the change scoped to this one field.
    """
    author = str(book_author or "").strip()
    if not series_id or not author or is_placeholder_author(author):
        return

    series = db.query(Series).filter(Series.id == series_id).first()
    if series and not str(series.author or "").strip():
        series.author = author
        db.commit()


def _infer_series_numbers_from_title(title: str | None, series_name: str | None = None) -> tuple[float | None, int | None]:
    """Delegates to discovery_engine.infer_number_from_title -- the same
    extractor Check Now and confidence/intelligence scoring already use --
    instead of a second, narrower pattern set. This used to only recognize
    a literal "book N" marker; it now also recognizes "#N", "volume N",
    "vol N", spelled-out numbers ("Book Four"), and (when series_name is
    known) a bare "<series name> N" positional form, exactly like discovery
    already does for the same title text. Fractional positions (e.g.
    "Book 3.5") are preserved rather than truncated -- see that function's
    own docstring.
    """
    book_number = discovery_engine.infer_number_from_title(title, series_name)
    if book_number is None:
        return None, None
    series_order = int(book_number) if float(book_number).is_integer() else None
    return book_number, series_order


def _book_payload(
    data_obj,
    *,
    db: Session | None = None,
    exclude_unset: bool = False,
    include_none: bool = False,
    infer_numbers: bool = False,
) -> dict:
    if hasattr(data_obj, "model_dump"):
        raw = data_obj.model_dump(exclude_none=not include_none, exclude_unset=exclude_unset)
    else:
        raw = data_obj.dict(exclude_none=not include_none, exclude_unset=exclude_unset)

    payload = {key: value for key, value in raw.items() if key in BOOK_COLUMN_KEYS}

    if "title" in payload and payload.get("title") is not None:
        payload["title"] = str(payload.get("title") or "").strip()

    if infer_numbers:
        had_explicit_book_number = payload.get("book_number") is not None
        series_name = None
        series_id = payload.get("series_id")
        if db is not None and series_id:
            series_row = db.query(Series.name).filter(Series.id == series_id).first()
            series_name = series_row[0] if series_row else None

        inferred_book_number, inferred_series_order = _infer_series_numbers_from_title(payload.get("title"), series_name)
        if payload.get("book_number") is None and inferred_book_number is not None:
            payload["book_number"] = inferred_book_number
            # Provenance for §Phase 3 -- only stamped when this call actually
            # filled in a value the caller didn't already supply/tag itself.
            if not payload.get("book_number_source"):
                payload["book_number_source"] = "title_inferred"
        elif had_explicit_book_number and not payload.get("book_number_source"):
            payload["book_number_source"] = "user"
        if payload.get("series_order") is None and inferred_series_order is not None:
            payload["series_order"] = inferred_series_order

    return payload


def _apply_availability_bridge_for_create(payload: dict) -> None:
    """"Touched key" locking, create-path variant (see the "Two-Axis Status
    Architecture" design chat): a brand-new book with no availability_status
    in its payload gets the same unlocked default as a discovery insert
    (column default "available", unlocked) so Check Now can manage it right
    away; a payload that *does* include availability_status is exactly as
    authoritative as a manual Edit Book choice and locks on the spot.
    Mutates `payload` in place, also filling in the derived legacy fields
    (read_status/is_upcoming_auto/is_upcoming_final) so a not-yet-migrated
    reader of those columns sees a self-consistent row from creation.
    """
    explicit_availability = "availability_status" in payload
    availability_status = normalize_availability_status(payload.get("availability_status"))
    availability_locked = bool(payload.get("availability_locked", explicit_availability))
    is_read = bool(payload.get("is_read", False))

    payload["availability_status"] = availability_status
    payload["availability_locked"] = availability_locked
    payload.update(derive_legacy_fields(is_read, availability_status, availability_locked))


def _apply_availability_bridge_for_update(payload: dict, db_book: Book) -> None:
    """"Touched key" locking, update-path variant: `payload` only contains
    keys the client actually sent (see update_book's exclude_unset=True), so
    presence of "availability_status" in it -- not its value -- is what
    means "the user just chose this", which is exactly what should flip
    availability_locked on. A PUT that never mentions availability_status
    (e.g. editing just the title, or the edit form's "status untouched"
    path -- see book-app-ui/hooks/use-edit-book-form.ts) must leave the
    existing lock state completely alone; this is the fix for the "toggled
    to unread but it silently reverted to available" bug, since a book's
    existing lock can no longer be flipped by a request that doesn't even
    mention this axis. Mutates `payload` in place, also refreshing the
    derived legacy fields against the row's *effective* post-update state
    (payload value if touched, otherwise the row's current value) so they
    never fall out of sync with the two-axis truth on any update, even one
    that leaves both axes alone.
    """
    if "availability_status" in payload:
        payload["availability_status"] = normalize_availability_status(payload["availability_status"])
        if "availability_locked" not in payload:
            payload["availability_locked"] = True

    effective_is_read = bool(payload["is_read"]) if "is_read" in payload else bool(db_book.is_read)
    effective_availability_status = normalize_availability_status(
        payload["availability_status"] if "availability_status" in payload else db_book.availability_status
    )
    effective_availability_locked = bool(
        payload["availability_locked"] if "availability_locked" in payload else db_book.availability_locked
    )
    payload.update(derive_legacy_fields(effective_is_read, effective_availability_status, effective_availability_locked))


def _should_clear_ghost_flags(db_book: Book, payload: dict) -> bool:
    if not (db_book.is_missing or db_book.is_upcoming_auto or db_book.is_upcoming_final):
        return False

    explicit_ghost_keys = {"is_missing", "is_upcoming_auto", "is_upcoming_final"}
    if explicit_ghost_keys & payload.keys():
        return False

    title_changed = "title" in payload and str(payload.get("title") or "").strip() != str(db_book.title or "").strip()
    marked_read = payload.get("is_read") is True
    read_status = str(payload.get("read_status") or "").strip().lower()
    has_read_status = read_status == "read"
    has_read_date = payload.get("read_date") is not None or payload.get("date_finished") is not None

    return title_changed or marked_read or has_read_status or has_read_date


def create_book(db: Session, book, profile_id: str):
    payload = _book_payload(book, db=db, infer_numbers=True)
    # metadata_source/needs_reresolution are always server-derived here from
    # find_confidence (a transient, non-column field -- see schemas.BookBase's
    # docstring), never trusted directly from the request body, even though
    # the schema happens to also expose those two as raw settable fields for
    # other, non-API write paths. This is the one and only place a
    # POST /books/ request's metadata_source can come from.
    find_confidence = getattr(book, "find_confidence", None)
    payload.update(
        provenance_for_find_bind(find_confidence) if find_confidence else provenance_for_declined_or_manual_entry()
    )
    _apply_availability_bridge_for_create(payload)
    _validate_series_belongs_to_profile(db, payload.get("series_id"), profile_id)
    _validate_book_number_requires_series(payload)
    payload["profile_id"] = profile_id
    db_book = Book(**payload)
    db.add(db_book)
    db.commit()
    db.refresh(db_book)
    if db_book.series_id is not None:
        _backfill_series_author_if_missing(db, db_book.series_id, db_book.author)
        recalculate_intelligence(db, db_book.series_id)
    return db_book


def get_all_books(db: Session, profile_id: str, limit: int | None = None, offset: int | None = None):
    query = (
        db.query(Book)
        .filter(Book.profile_id == profile_id)
        .filter(or_(Book.record_status.is_(None), Book.record_status != "deleted"))
        # Stable ordering is required once limit/offset paging is in play --
        # without it, successive pages can repeat or skip rows depending on
        # the database's default (unordered) scan order.
        .order_by(Book.id)
    )
    if offset is not None:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def get_book(db: Session, book_id: int, profile_id: str):
    return db.query(Book).filter(Book.id == book_id, Book.profile_id == profile_id).first()


def get_books_by_series(db: Session, series_id: int, profile_id: str):
    return (
        db.query(models.Book)
        .filter(models.Book.series_id == series_id, models.Book.profile_id == profile_id)
        .filter(or_(models.Book.record_status.is_(None), models.Book.record_status != "deleted"))
        .order_by(models.Book.book_number.asc())
        .all()
    )

def update_book(db: Session, book_id: int, book, profile_id: str):
    db_book = db.query(Book).filter(Book.id == book_id, Book.profile_id == profile_id).first()
    if not db_book:
        return None

    previous_series_id = db_book.series_id
    # Keep explicit nulls on update so users can intentionally clear fields like
    # book_number/series_order in mixed-numbering series.
    payload = _book_payload(book, exclude_unset=True, include_none=True)
    if "series_id" in payload:
        _validate_series_belongs_to_profile(db, payload.get("series_id"), profile_id)
    _validate_book_number_requires_series(
        payload, existing_book_number=db_book.book_number, existing_series_id=db_book.series_id
    )
    if _should_clear_ghost_flags(db_book, payload):
        payload.setdefault("is_missing", False)
        payload.setdefault("is_upcoming_auto", False)
        payload.setdefault("is_upcoming_final", False)
        # Mirror the ghost-clear onto the new availability axis too, but
        # only when this request didn't already touch it explicitly --
        # an edit that title-corrects/confirms/marks-read a stale "upcoming"
        # ghost row should knock it out of "upcoming" the same way it always
        # cleared is_upcoming_auto/final, and unlocked (not "owned"/
        # "available" is nobody's explicit choice here) so discovery/Check
        # Now can still manage it going forward.
        if "availability_status" not in payload and normalize_availability_status(db_book.availability_status) == "upcoming":
            payload["availability_status"] = "available"
            payload.setdefault("availability_locked", False)
    _apply_availability_bridge_for_update(payload, db_book)
    for key, value in payload.items():
        setattr(db_book, key, value)

    db.commit()
    db.refresh(db_book)
    if db_book.series_id is not None:
        _backfill_series_author_if_missing(db, db_book.series_id, db_book.author)
        recalculate_intelligence(db, db_book.series_id)
    if previous_series_id is not None and previous_series_id != db_book.series_id:
        recalculate_intelligence(db, previous_series_id)
    return db_book


def delete_book(db: Session, book_id: int, profile_id: str):
    db_book = db.query(Book).filter(Book.id == book_id, Book.profile_id == profile_id).first()
    if not db_book:
        return False

    series_id = db_book.series_id
    db.delete(db_book)
    db.commit()
    if series_id is not None:
        recalculate_intelligence(db, series_id)
    return True

