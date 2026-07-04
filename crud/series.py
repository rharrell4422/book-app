from sqlalchemy.orm import Session
from sqlalchemy import func
from models import Series, Book


def create_series(db: Session, series):
    db_series = Series(**series.dict())
    db.add(db_series)
    db.commit()
    db.refresh(db_series)
    return db_series


def get_all_series(db: Session):
    return db.query(Series).all()


def get_series(db: Session, series_id: int):
    return db.query(Series).filter(Series.id == series_id).first()


def get_series_by_name(db: Session, series_name: str):
    cleaned = str(series_name or "").strip()
    if not cleaned:
        return None
    return db.query(Series).filter(func.lower(Series.name) == cleaned.lower()).first()


def update_series(db: Session, series_id: int, series):
    db_series = db.query(Series).filter(Series.id == series_id).first()
    if not db_series:
        return None

    for key, value in series.dict(exclude_unset=True).items():
        setattr(db_series, key, value)

    db.commit()
    db.refresh(db_series)
    return db_series


def delete_series(db: Session, series_id: int):
    db_series = db.query(Series).filter(Series.id == series_id).first()
    if not db_series:
        return None

    # Hard-delete all books linked to this series so Library and Series views stay in sync.
    deleted_books = (
        db.query(Book)
        .filter(Book.series_id == series_id)
        .delete(synchronize_session=False)
    )
    db.delete(db_series)
    db.commit()
    return {
        "series_id": series_id,
        "deleted_books": int(deleted_books or 0),
    }
