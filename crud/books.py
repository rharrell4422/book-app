import models
import re


from sqlalchemy import or_
from sqlalchemy.orm import Session
from models import Book
from intelligence import recalculate_intelligence


BOOK_COLUMN_KEYS = {column.key for column in Book.__table__.columns}


def _infer_series_numbers_from_title(title: str | None) -> tuple[float | None, int | None]:
    if not title:
        return None, None

    match = re.search(r"\bbook\s+(\d+(?:\.\d+)?)\b", str(title), flags=re.IGNORECASE)
    if not match:
        return None, None

    book_number = float(match.group(1))
    series_order = int(book_number) if book_number.is_integer() else None
    return book_number, series_order


def _book_payload(
    data_obj,
    *,
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
        inferred_book_number, inferred_series_order = _infer_series_numbers_from_title(payload.get("title"))
        if payload.get("book_number") is None and inferred_book_number is not None:
            payload["book_number"] = inferred_book_number
        if payload.get("series_order") is None and inferred_series_order is not None:
            payload["series_order"] = inferred_series_order

    return payload


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


def create_book(db: Session, book):
    payload = _book_payload(book)
    db_book = Book(**payload)
    db.add(db_book)
    db.commit()
    db.refresh(db_book)
    if db_book.series_id is not None:
        recalculate_intelligence(db, db_book.series_id)
    return db_book


def get_all_books(db: Session):
    return db.query(Book).filter(or_(Book.record_status.is_(None), Book.record_status != "deleted")).all()


def get_book(db: Session, book_id: int):
    return db.query(Book).filter(Book.id == book_id).first()


def get_books_by_series(db: Session, series_id: int):
    return (
        db.query(models.Book)
        .filter(models.Book.series_id == series_id)
        .filter(or_(models.Book.record_status.is_(None), models.Book.record_status != "deleted"))
        .order_by(models.Book.book_number.asc())
        .all()
    )

def update_book(db: Session, book_id: int, book):
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        return None

    previous_series_id = db_book.series_id
    # Keep explicit nulls on update so users can intentionally clear fields like
    # book_number/series_order in mixed-numbering series.
    payload = _book_payload(book, exclude_unset=True, include_none=True)
    if _should_clear_ghost_flags(db_book, payload):
        payload.setdefault("is_missing", False)
        payload.setdefault("is_upcoming_auto", False)
        payload.setdefault("is_upcoming_final", False)
    for key, value in payload.items():
        setattr(db_book, key, value)

    db.commit()
    db.refresh(db_book)
    if db_book.series_id is not None:
        recalculate_intelligence(db, db_book.series_id)
    if previous_series_id is not None and previous_series_id != db_book.series_id:
        recalculate_intelligence(db, previous_series_id)
    return db_book


def delete_book(db: Session, book_id: int):
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        return False

    series_id = db_book.series_id
    db.delete(db_book)
    db.commit()
    if series_id is not None:
        recalculate_intelligence(db, series_id)
    return True

