"""Durable per-series book-lineup memory (SeriesSkeleton) -- Phase 1 of
agentic discovery (see project design chat).

Phase 1 scope only: deterministic backfill of `SeriesSkeleton` rows from
existing `Book` rows already in the library. Zero LLM cost, zero network
calls, and not read by anything else yet -- discovery, Check Now, and the
Tier 1/Tier 2 gating decision are all unchanged by this module's existence.
Later phases (delta checks, agentic Tier 2 discovery, skeleton<->DB
reconciliation on every check) build on top of the same table/entry shape
started here.
"""

from datetime import datetime

from sqlalchemy.orm import Session

import models

SCHEMA_VERSION = 1


def _book_to_skeleton_entry(book: "models.Book", now_iso: str) -> dict:
    """Deterministic, LLM-free mapping from an owned Book row to a skeleton
    entry. An owned row is the strongest possible evidence a book exists,
    so it's always high confidence -- confidence/status only get
    interesting once Phase 2+ starts folding in provider/LLM-derived
    entries that aren't already in the library.
    """
    is_upcoming = bool(book.is_upcoming_auto or book.is_upcoming_final)
    release_date = book.publication_date or book.release_date
    return {
        "book_number": book.book_number,
        "title": book.title,
        "status": "upcoming" if is_upcoming else "confirmed",
        "confidence": "high",
        "release_date": release_date.isoformat() if release_date else None,
        "edition_hints": [book.edition] if book.edition else [],
        "sources": [
            {
                "provider": "library",
                "url": book.source_url,
                "fetched_at": now_iso,
            }
        ],
        "first_seen_at": now_iso,
        "last_confirmed_at": now_iso,
    }


def backfill_skeleton_for_series(db: Session, series_id: int) -> "models.SeriesSkeleton | None":
    """(Re)builds the skeleton for one series purely from its current active
    Book rows. Safe to call repeatedly -- it's a full deterministic rebuild
    from ground truth each time, not an incremental merge, so it can't drift
    or accumulate stale entries on its own.
    """
    series = db.query(models.Series).filter(models.Series.id == series_id).first()
    if series is None:
        return None

    active_books = [
        book
        for book in (series.books or [])
        if str(book.record_status or "active") != "deleted" and book.book_number is not None
    ]
    active_books.sort(key=lambda book: book.book_number)

    now_iso = datetime.utcnow().isoformat()
    entries = [_book_to_skeleton_entry(book, now_iso) for book in active_books]

    skeleton = (
        db.query(models.SeriesSkeleton)
        .filter(models.SeriesSkeleton.series_id == series_id)
        .first()
    )
    if skeleton is None:
        skeleton = models.SeriesSkeleton(series_id=series_id)
        db.add(skeleton)

    skeleton.skeleton_json = entries
    skeleton.schema_version = SCHEMA_VERSION
    return skeleton


def backfill_all_skeletons() -> None:
    """One-time (and re-runnable) backfill across every series, called on
    boot the same way bootstrap.backfill_series_state already is. Owns its
    own session/commit since it runs standalone at startup, not nested
    inside an existing request's db session.
    """
    from database import SessionLocal

    db = SessionLocal()
    try:
        series_ids = [row.id for row in db.query(models.Series.id).all()]
        for series_id in series_ids:
            backfill_skeleton_for_series(db, series_id)
        db.commit()
    finally:
        db.close()
