import models


from sqlalchemy.orm import Session
from models import Book
from intelligence import recalculate_series_state_for_series, recount_series_aggregates_for_series


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


def _book_payload(data_obj, *, exclude_unset: bool = False) -> dict:
    if hasattr(data_obj, "model_dump"):
        raw = data_obj.model_dump(exclude_none=True, exclude_unset=exclude_unset)
    else:
        raw = data_obj.dict(exclude_none=True, exclude_unset=exclude_unset)

    payload = {key: value for key, value in raw.items() if key in BOOK_COLUMN_KEYS}

    if "title" in payload:
        payload["title"] = str(payload.get("title") or "").strip()

    inferred_book_number, inferred_series_order = _infer_series_numbers_from_title(payload.get("title"))
    if payload.get("book_number") is None and inferred_book_number is not None:
        payload["book_number"] = inferred_book_number
    if payload.get("series_order") is None and inferred_series_order is not None:
        payload["series_order"] = inferred_series_order

    return payload


def create_book(db: Session, book):
    payload = _book_payload(book)
    db_book = Book(**payload)
    db.add(db_book)
    db.commit()
    db.refresh(db_book)
    if db_book.series_id is not None:
        recalculate_series_state_for_series(db, db_book.series_id)
    return db_book


def get_all_books(db: Session):
    return db.query(Book).all()


def get_book(db: Session, book_id: int):
    return db.query(Book).filter(Book.id == book_id).first()


def get_books_by_series(db: Session, series_id: int):
    return (
        db.query(models.Book)
        .filter(models.Book.series_id == series_id)
        .order_by(models.Book.book_number.asc())
        .all()
    )

def update_book(db: Session, book_id: int, book):
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        return None

    previous_series_id = db_book.series_id
    payload = _book_payload(book, exclude_unset=True)
    for key, value in payload.items():
        setattr(db_book, key, value)

    db.commit()
    db.refresh(db_book)
    if db_book.series_id is not None:
        recalculate_series_state_for_series(db, db_book.series_id)
    if previous_series_id is not None and previous_series_id != db_book.series_id:
        recalculate_series_state_for_series(db, previous_series_id)
    return db_book


def delete_book(db: Session, book_id: int):
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        return False

    series_id = db_book.series_id
    db.delete(db_book)
    db.commit()
    if series_id is not None:
        recalculate_series_state_for_series(db, series_id)
        recount_series_aggregates_for_series(db, series_id)
    return True

