import models


from sqlalchemy.orm import Session
from models import Book


def create_book(db: Session, book):
    db_book = Book(**book.dict())
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

    for key, value in book.dict(exclude_unset=True).items():
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

