"""Database-reset helpers for the importer: wiping either the whole
database or just one profile's rows.

Split out of importer/importer.py (RT-6). Both functions take an
already-open `db` session rather than opening their own -- unlike
pipeline.run_import/preview.preview_import, callers here (cli.py's
--reset-db flag, routers/imports.py's /reset_profile endpoint) already have
one to hand in.

importer/importer.py re-exports both, so existing external callers
(routers/imports.py, tests, etc.) are unaffected by this split.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from models import Series, Book


def reset_database(db: Session):
    # Delete books first to satisfy FK constraints, then series.
    deleted_books = db.query(Book).delete(synchronize_session=False)
    deleted_series = db.query(Series).delete(synchronize_session=False)
    db.commit()
    return deleted_books, deleted_series


def reset_profile_data(db: Session, profile_id: str):
    """Delete only one profile's books and series. Used by onboarding's
    "start over" action to let a still-empty profile safely retry a failed
    or unwanted upload -- unlike `reset_database`, this never touches other
    profiles' libraries.
    """
    deleted_books = (
        db.query(Book).filter(Book.profile_id == profile_id).delete(synchronize_session=False)
    )
    deleted_series = (
        db.query(Series).filter(Series.profile_id == profile_id).delete(synchronize_session=False)
    )
    db.commit()
    return deleted_books, deleted_series
