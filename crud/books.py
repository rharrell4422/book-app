import models


from sqlalchemy.orm import Session
from models import Book


BOOK_COLUMN_KEYS = {column.key for column in Book.__table__.columns}


def _book_payload(data_obj, *, exclude_unset: bool = False) -> dict:
    if hasattr(data_obj, "model_dump"):
        raw = data_obj.model_dump(exclude_none=True, exclude_unset=exclude_unset)
    else:
        raw = data_obj.dict(exclude_none=True, exclude_unset=exclude_unset)
    return {key: value for key, value in raw.items() if key in BOOK_COLUMN_KEYS}


def create_book(db: Session, book):
    payload = _book_payload(book)
    db_book = Book(**payload)
    db.add(db_book)
    db.commit()
    db.refresh(db_book)
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

    payload = _book_payload(book, exclude_unset=True)
    for key, value in payload.items():
        setattr(db_book, key, value)

    db.commit()
    db.refresh(db_book)
    return db_book


def delete_book(db: Session, book_id: int):
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        return False

    db.delete(db_book)
    db.commit()
    return True

