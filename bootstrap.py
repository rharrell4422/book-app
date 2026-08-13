"""Startup-time database bootstrapping: schema migrations (via Alembic) and
the one-time state backfills run when the app boots.
"""

import os

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

import models
from database import SessionLocal, engine
from intelligence import recalculate_series_state_for_series

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

# The revision that captures a frozen snapshot of the schema as it existed
# right before Alembic was introduced into this project (see that
# migration's docstring). Any database that already has the `series`/`books`
# tables but no `alembic_version` table predates Alembic entirely -- e.g. a
# deployment (like the Railway production instance) that was last deployed
# before this change. Running `upgrade` from scratch against a DB like that
# would try to CREATE TABLE series/books that already exist (with real data)
# and crash on boot, so that case is detected and stamped at the baseline
# instead of replayed.
_PRE_ALEMBIC_BASELINE_REVISION = "bf8439427a1e"


def run_migrations() -> None:
    """Brings the DB schema up to date by running every Alembic migration
    that hasn't been applied yet (a no-op if it's already at head). Runs on
    every boot so a fresh DB, an existing one, and a freshly-deployed
    Railway instance all converge on the same schema without a separate
    deploy step -- this replaces the old ad-hoc `ensure_series_state_columns`
    + `models.Base.metadata.create_all` pair that only handled a couple of
    hand-picked columns.
    """
    alembic_cfg = Config(os.path.join(_REPO_ROOT, "alembic.ini"))
    alembic_cfg.set_main_option("script_location", os.path.join(_REPO_ROOT, "alembic"))

    inspector = inspect(engine)
    has_alembic_version_table = inspector.has_table("alembic_version")
    has_pre_existing_schema = inspector.has_table("series") and inspector.has_table("books")

    if not has_alembic_version_table and has_pre_existing_schema:
        command.stamp(alembic_cfg, _PRE_ALEMBIC_BASELINE_REVISION)

    command.upgrade(alembic_cfg, "head")


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
