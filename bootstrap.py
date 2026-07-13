"""Startup-time database bootstrapping: lightweight schema migrations run
against SQLite directly (no Alembic in this project yet) and the one-time
state backfill run when the app boots.
"""

from sqlalchemy import text

import models
from database import SessionLocal, engine
from intelligence import recalculate_series_state_for_series


def ensure_series_state_columns() -> None:
    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(series)")).fetchall()}
        if "has_new_books" not in columns:
            conn.execute(text("ALTER TABLE series ADD COLUMN has_new_books BOOLEAN NOT NULL DEFAULT 0"))
        if "has_unread_books" not in columns:
            conn.execute(text("ALTER TABLE series ADD COLUMN has_unread_books BOOLEAN NOT NULL DEFAULT 0"))
        if "has_upcoming_books" not in columns:
            conn.execute(text("ALTER TABLE series ADD COLUMN has_upcoming_books BOOLEAN NOT NULL DEFAULT 0"))
        if "is_caught_up" not in columns:
            conn.execute(text("ALTER TABLE series ADD COLUMN is_caught_up BOOLEAN NOT NULL DEFAULT 0"))
        if "title_normalization_mode_override" not in columns:
            conn.execute(text("ALTER TABLE series ADD COLUMN title_normalization_mode_override TEXT NULL"))


def backfill_series_state() -> None:
    db = SessionLocal()
    try:
        series_list = db.query(models.Series).all()
        for series in series_list:
            recalculate_series_state_for_series(db, series.id)
    finally:
        db.close()


def clear_stale_ghost_flags_on_read_books() -> None:
    """One-time repair for books that got marked read through a write path
    that didn't clear their Check Now "ghost" flags (is_missing /
    is_upcoming_auto / is_upcoming_final) -- e.g. a bulk sync or an older
    import that set is_read directly. A read book should never still be
    flagged as an undealt-with new discovery; leaving the stale flag set
    made Series.has_new_available_books / has_new_upcoming_books report a
    "new book found" icon for series where every visible book was already
    read. The has_new_available_books/has_new_upcoming_books properties are
    now self-healing against this going forward, but existing rows still
    need a one-time cleanup so is_missing/is_upcoming_* accurately reflect
    "not yet read" everywhere else they're used too.
    """
    db = SessionLocal()
    try:
        stale_books = (
            db.query(models.Book)
            .filter(models.Book.is_read.is_(True))
            .filter(
                (models.Book.is_missing.is_(True))
                | (models.Book.is_upcoming_auto.is_(True))
                | (models.Book.is_upcoming_final.is_(True))
            )
            .all()
        )
        if not stale_books:
            return
        affected_series_ids = set()
        for book in stale_books:
            book.is_missing = False
            book.is_upcoming_auto = False
            book.is_upcoming_final = False
            if book.series_id is not None:
                affected_series_ids.add(book.series_id)
        db.commit()
        for series_id in affected_series_ids:
            recalculate_series_state_for_series(db, series_id)
    finally:
        db.close()
