"""Bulk re-resolution (Phase 7 of the Add Book metadata intake redesign --
see project design chat's consolidated spec, §7.5).

Sweeps a profile's books for rows whose metadata is either never-verified
(metadata_source is NULL/"user"/"import" -- typed by hand or bulk-imported,
never confirmed against a provider catalog) or was bound at FIND's LOW
confidence tier (metadata_source="provider" but needs_reresolution=True --
see services/metadata_provenance.py's provenance_for_find_bind), and re-runs
FIND for each one to try to upgrade it to a confident, provider-sourced
bind.

Explicitly excludes metadata_source="discovery" rows: Check Now is exempt
from FIND confidence entirely (services/series_check_engine.py stamps its
own provenance directly), so those rows are already as verified as this
pipeline gets and are never revisited here.

Deliberately synchronous and capped per call (`limit`), not a background
job -- each eligible row costs one live multi-provider FIND call (Google
Books + OpenLibrary + Hardcover), so an unbounded sweep over a large
library would be slow and rate-limit-risky. The caller (a "Re-run"
button/endpoint) re-invokes this until `processed < limit`, which just
means the last call drained the whole eligible queue.
"""
from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

import models
from services.find_engine import find_book_candidates
from services.identity import owned_title_for_identity
from services.metadata_provenance import provenance_for_find_bind

DEFAULT_BATCH_LIMIT = 25
MAX_BATCH_LIMIT = 200


def _eligible_books_query(db: Session, profile_id: str):
    return (
        db.query(models.Book)
        .filter(models.Book.profile_id == profile_id)
        .filter(or_(models.Book.record_status.is_(None), models.Book.record_status != "deleted"))
        .filter(
            or_(
                models.Book.metadata_source.is_(None),
                models.Book.metadata_source.in_(("user", "import")),
                models.Book.needs_reresolution.is_(True),
            )
        )
    )


def count_eligible_books(db: Session, profile_id: str) -> int:
    """For a UI badge/count ("14 books need re-resolution") without having
    to actually run any of the batch's FIND calls."""
    return _eligible_books_query(db, profile_id).count()


def _series_name_for(db: Session, series_id: int | None) -> str | None:
    if not series_id:
        return None
    row = db.query(models.Series.name).filter(models.Series.id == series_id).first()
    return row[0] if row else None


def reresolve_book(db: Session, book: models.Book) -> dict:
    """Re-runs FIND for a single book and applies the top-ranked candidate,
    if any, exactly like a Bind (see provenance_for_find_bind). Commits the
    row's own change immediately so one row's update isn't lost if a later
    row in the same batch errors -- FIND's own provider fan-out already
    isolates a single provider's failure (see services/find_engine.py);
    this is that same isolation one level up, across rows in a batch.
    Never raises for a provider/network failure -- that row is reported as
    "error" and left completely untouched instead of aborting the batch.
    """
    query_title = owned_title_for_identity(book)
    series_name = _series_name_for(db, book.series_id)
    try:
        result = find_book_candidates(query_title, book.author, book.book_number, series_name, max_results=1)
    except Exception as exc:  # noqa: BLE001 -- one row's failure shouldn't sink the batch
        return {"book_id": book.id, "title": book.title, "outcome": "error", "detail": str(exc)[:300]}

    candidates = result.get("candidates") or []
    if not candidates:
        return {"book_id": book.id, "title": book.title, "outcome": "no_match"}

    best = candidates[0]
    provenance = provenance_for_find_bind(best["confidence"])
    book.canonical_title = best.get("title") or book.canonical_title
    book.metadata_source = provenance["metadata_source"]
    book.needs_reresolution = provenance["needs_reresolution"]
    # Fills gaps only -- an existing isbn13/source_url (e.g. from a prior
    # low-confidence bind) is never overwritten by a fresh match.
    if best.get("isbn13") and not book.isbn13:
        book.isbn13 = best["isbn13"]
    if best.get("source_url") and not book.source_url:
        book.source_url = best["source_url"]
    db.commit()

    return {
        "book_id": book.id,
        "title": book.title,
        "outcome": "updated",
        "confidence": best["confidence"],
        "canonical_title": book.canonical_title,
        "needs_reresolution": book.needs_reresolution,
    }


def bulk_reresolve(db: Session, profile_id: str, limit: int = DEFAULT_BATCH_LIMIT) -> dict:
    limit = max(1, min(limit, MAX_BATCH_LIMIT))
    books = _eligible_books_query(db, profile_id).order_by(models.Book.id).limit(limit).all()
    results = [reresolve_book(db, book) for book in books]

    return {
        "processed": len(results),
        "updated": sum(1 for r in results if r["outcome"] == "updated"),
        "no_match": sum(1 for r in results if r["outcome"] == "no_match"),
        "errors": sum(1 for r in results if r["outcome"] == "error"),
        "remaining": count_eligible_books(db, profile_id),
        "results": results,
    }
