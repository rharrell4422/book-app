from sqlalchemy.orm import Session
from sqlalchemy import func, inspect
from models import Series, Book

# SeriesBase (the create/update schema) also carries a handful of derived,
# read-only fields -- read_count, unread_count, series_state, etc. -- that
# exist as @property on the Series model for API responses, not as mapped
# columns. Passing them through to the Series(**kwargs) constructor blows up
# with "property 'x' of 'Series' object has no setter", so only forward the
# fields that are actually real columns on the model.
_SERIES_COLUMN_NAMES = {column.key for column in inspect(Series).columns}


def create_series(db: Session, series, profile_id: str):
    payload = {k: v for k, v in series.model_dump().items() if k in _SERIES_COLUMN_NAMES}
    payload["profile_id"] = profile_id
    db_series = Series(**payload)
    db.add(db_series)
    db.commit()
    db.refresh(db_series)
    return db_series


def get_all_series(db: Session, profile_id: str, limit: int | None = None, offset: int | None = None):
    query = db.query(Series).filter(Series.profile_id == profile_id).order_by(Series.id)
    if offset is not None:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def get_series(db: Session, series_id: int, profile_id: str):
    return db.query(Series).filter(Series.id == series_id, Series.profile_id == profile_id).first()


def get_series_by_name(db: Session, series_name: str, profile_id: str):
    cleaned = str(series_name or "").strip()
    if not cleaned:
        return None
    return (
        db.query(Series)
        .filter(Series.profile_id == profile_id, func.lower(Series.name) == cleaned.lower())
        .first()
    )


def update_series(db: Session, series_id: int, series, profile_id: str):
    db_series = db.query(Series).filter(Series.id == series_id, Series.profile_id == profile_id).first()
    if not db_series:
        return None

    for key, value in series.model_dump(exclude_unset=True).items():
        if key in _SERIES_COLUMN_NAMES:
            setattr(db_series, key, value)

    db.commit()
    db.refresh(db_series)
    return db_series


def delete_series(db: Session, series_id: int, profile_id: str):
    db_series = db.query(Series).filter(Series.id == series_id, Series.profile_id == profile_id).first()
    if not db_series:
        return None

    # Hard-delete all books linked to this series so Library and Series views
    # stay in sync. CR-9: profile_id is included here too, not just on the
    # Series lookup above -- filtering by series_id alone would also
    # delete/mutate any "ghost" cross-profile Book row that happened to
    # share this series_id, which the Series-row check above never
    # protects against.
    deleted_books = (
        db.query(Book)
        .filter(Book.series_id == series_id, Book.profile_id == profile_id)
        .delete(synchronize_session=False)
    )
    db.delete(db_series)
    db.commit()
    return {
        "series_id": series_id,
        "deleted_books": int(deleted_books or 0),
    }
