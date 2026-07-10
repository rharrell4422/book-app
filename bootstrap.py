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
