from sqlalchemy.orm import Session
from models import Book, Series
from schemas import BookCreate, BookUpdate, SeriesCreate, SeriesUpdate
from intelligence import compute_series_intelligence
from intelligence import check_for_new_books


# ---------------------------------------------------------
# BOOK CRUD
# ---------------------------------------------------------

def create_book(db: Session, book: BookCreate):
    db_book = Book(
        title=book.title,
        author=book.author,
        year=book.year,
        genre=book.genre,
        series_id=book.series_id,
        book_number=book.book_number,
        series_total_books=book.series_total_books,
        release_date=book.release_date,
        is_read=book.is_read,
        read_date=book.read_date,
        is_upcoming=book.is_upcoming,
        is_upcoming_auto=book.is_upcoming_auto,
        is_upcoming_final=book.is_upcoming or book.is_upcoming_auto
    )

    db.add(db_book)
    db.commit()
    db.refresh(db_book)

    # Recompute series intelligence if this book belongs to a series
    if db_book.series_id:
        series = db.query(Series).filter(Series.id == db_book.series_id).first()
        compute_series_intelligence(series, db)

    return db_book


def get_book(db: Session, book_id: int):
    return db.query(Book).filter(Book.id == book_id).first()


def get_all_books(db: Session):
    return db.query(Book).all()


def update_book(db: Session, book_id: int, book: BookUpdate):
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        return None

    for field, value in book.dict(exclude_unset=True).items():
        setattr(db_book, field, value)

    # Update final upcoming flag
    db_book.is_upcoming_final = db_book.is_upcoming or db_book.is_upcoming_auto

    db.commit()
    db.refresh(db_book)

    # Recompute series intelligence if needed
    if db_book.series_id:
        series = db.query(Series).filter(Series.id == db_book.series_id).first()
        compute_series_intelligence(series, db)

    return db_book


def delete_book(db: Session, book_id: int):
    db_book = db.query(Book).filter(Book.id == book_id).first()
    if not db_book:
        return None

    series_id = db_book.series_id

    db.delete(db_book)
    db.commit()

    # Recompute series intelligence after deletion
    if series_id:
        series = db.query(Series).filter(Series.id == series_id).first()
        compute_series_intelligence(series, db)

    return True


def recompute_series_intelligence(db: Session, series_id: int):
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        return None

    intelligence = compute_series_intelligence(series, db)
    return intelligence


# ---------------------------------------------------------
# SERIES CRUD
# ---------------------------------------------------------

def create_series(db: Session, series: SeriesCreate):
    db_series = Series(
        name=series.name,
        author=series.author,
        check_url=series.check_url,
        series_finished=series.series_finished,
        check_series=series.check_series,
        last_checked=series.last_checked,
        series_total_books_manual=series.series_total_books_manual
    )

    db.add(db_series)
    db.commit()
    db.refresh(db_series)

    # Initial intelligence compute (empty series)
    compute_series_intelligence(db_series, db)

    return db_series


def get_series(db: Session, series_id: int):
    return db.query(Series).filter(Series.id == series_id).first()


def get_all_series(db: Session):
    return db.query(Series).all()


def update_series(db: Session, series_id: int, series: SeriesUpdate):
    db_series = db.query(Series).filter(Series.id == series_id).first()
    if not db_series:
        return None

    for field, value in series.dict(exclude_unset=True).items():
        setattr(db_series, field, value)

    db.commit()
    db.refresh(db_series)

    # Recompute intelligence after update
    compute_series_intelligence(db_series, db)

    return db_series


def delete_series(db: Session, series_id: int):
    db_series = db.query(Series).filter(Series.id == series_id).first()
    if not db_series:
        return None

    db.delete(db_series)
    db.commit()

    return True


def check_series_for_new_books(db: Session, series_id: int):
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        return None

    intelligence = check_for_new_books(series, db)
    return intelligence
