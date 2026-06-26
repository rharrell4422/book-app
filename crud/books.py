from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import date

import models
import schemas


# ---------------------------------------------------------
# CREATE BOOK
# ---------------------------------------------------------
def create_book(db: Session, book: schemas.BookCreate):
    db_book = models.Book(
        title=book.title,
        author=book.author,
        isbn=book.isbn,
        format=book.format,
        publication_date=book.publication_date,
        series_id=book.series_id,
        series_order=book.series_order,
        series_total_books=book.series_total_books,
        is_series_finished=book.is_series_finished,
        is_read=book.is_read,
        read_date=book.read_date,
        rating=book.rating,
        notes=book.notes,
        check_url=book.check_url,
        is_upcoming_auto=book.is_upcoming_auto,
        is_upcoming_final=book.is_upcoming_final,
    )

    db.add(db_book)
    db.commit()
    db.refresh(db_book)
    return db_book


# ---------------------------------------------------------
# READ ALL BOOKS
# ---------------------------------------------------------
def get_all_books(db: Session) -> List[models.Book]:
    return db.query(models.Book).order_by(models.Book.title.asc()).all()


# ---------------------------------------------------------
# READ BOOK BY ID
# ---------------------------------------------------------
def get_book(db: Session, book_id: int) -> Optional[models.Book]:
    return db.query(models.Book).filter(models.Book.id == book_id).first()


# ---------------------------------------------------------
# UPDATE BOOK
# ---------------------------------------------------------
def update_book(db: Session, book_id: int, book: schemas.BookUpdate):
    db_book = db.query(models.Book).filter(models.Book.id == book_id).first()
    if not db_book:
        return None

    # Update only fields provided
    update_data = book.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_book, key, value)

    db.commit()
    db.refresh(db_book)
    return db_book


# ---------------------------------------------------------
# DELETE BOOK
# ---------------------------------------------------------
def delete_book(db: Session, book_id: int):
    db_book = db.query(models.Book).filter(models.Book.id == book_id).first()
    if not db_book:
        return None

    db.delete(db_book)
    db.commit()
    return True
