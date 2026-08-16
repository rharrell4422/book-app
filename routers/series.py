import logging
from datetime import datetime
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

import crud
import models
import schemas
from intelligence import (
    compute_series_intelligence_for_series,
    recalculate_intelligence,
    recalculate_series_state_for_series,
)
from routers.deps import enforce_access, get_current_profile_id, get_db
from services.series_check_engine import (
    SERIES_CHECK_TIMEOUT_SECONDS,
    run_series_check_job_full,
    series_check_jobs,
)
from services.title_normalization import (
    normalize_series_book_titles,
    normalize_title_normalization_mode,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/series", tags=["series"], dependencies=[Depends(enforce_access)])


# Registered at both "/" and "" (see main.py's redirect_slashes=False) so a
# request that arrives without its trailing slash -- e.g. from a proxy that
# normalized it away -- is handled directly instead of needing a redirect.
@router.post("/", response_model=schemas.SeriesResponse)
@router.post("", response_model=schemas.SeriesResponse, include_in_schema=False)
def create_series(
    series: schemas.SeriesBase, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)
):
    series.title_normalization_mode_override = normalize_title_normalization_mode(series.title_normalization_mode_override)
    return crud.create_series(db=db, series=series, profile_id=profile_id)


@router.get("/", response_model=List[schemas.SeriesResponse])
@router.get("", response_model=List[schemas.SeriesResponse], include_in_schema=False)
def read_series(db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    all_series = crud.get_all_series(db, profile_id)
    responses = []
    for series in all_series:
        response = schemas.SeriesResponse.model_validate(series)
        # Series.books is the raw, unfiltered ORM relationship -- it still
        # includes soft-deleted rows (record_status="deleted"), unlike
        # crud.get_books_by_series() which the detail endpoint uses. Left
        # as-is, a deleted junk discovery (e.g. a bad "book 29" match later
        # deleted) still counts toward the frontend's client-side missing-
        # book gap inference, which has no way to know it was deleted and
        # reports a phantom gap (e.g. "Missing: Book 28") that the detail
        # page -- which correctly excludes deleted rows -- never shows.
        response.books = [
            book for book in response.books
            if str(getattr(book, "record_status", None) or "active") != "deleted"
        ]
        responses.append(response)
    return responses


@router.get("/{series_id}", response_model=schemas.SeriesDetailResponse)
def read_series_by_id(series_id: int, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    db_series = crud.get_series(db, series_id, profile_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    books = crud.get_books_by_series(db, series_id, profile_id)

    sorted_books = sorted(books, key=lambda b: (b.book_number or 0))
    series_author = db_series.author or next((book.author for book in sorted_books if book.author), None)

    intelligence = compute_series_intelligence_for_series(db, series_id)

    is_finished = bool(intelligence.get("is_series_finished"))

    return schemas.SeriesDetailResponse(
        id=db_series.id,
        name=db_series.name,
        author=series_author,
        description=db_series.description,
        genre=db_series.genre,
        tags=db_series.tags,
        is_finished=is_finished,
        total_books=intelligence["total_books"],
        series_status="finished" if is_finished else "ongoing",
        next_unread_book_number=intelligence.get("next_unread_book_number"),
        next_upcoming_book_number=intelligence.get("next_upcoming_book_number"),
        missing_books=intelligence["missing_orders"],
        has_new_books=bool(db_series.has_new_books),
        has_unread_books=bool(db_series.has_unread_books),
        has_upcoming_books=bool(db_series.has_upcoming_books),
        is_caught_up=bool(db_series.is_caught_up),
        read_count=int(intelligence.get("read_count") or 0),
        unread_count=int(intelligence.get("unread_count") or 0),
        title_normalization_mode_override=normalize_title_normalization_mode(db_series.title_normalization_mode_override),
        series_state=db_series.series_state,
        created_at=db_series.created_at,
        updated_at=db_series.updated_at,
        books=[schemas.BookResponse.model_validate(book) for book in sorted_books]
    )


@router.put("/{series_id}", response_model=schemas.SeriesResponse)
def update_series(
    series_id: int,
    series: schemas.SeriesBase,
    db: Session = Depends(get_db),
    profile_id: str = Depends(get_current_profile_id),
):
    series.title_normalization_mode_override = normalize_title_normalization_mode(series.title_normalization_mode_override)
    updated = crud.update_series(db, series_id, series, profile_id)
    if not updated:
        raise HTTPException(status_code=404, detail="Series not found")
    recalculate_intelligence(db, series_id)
    return updated


@router.post("/{series_id}/mark_unfinished")
def mark_series_unfinished(series_id: int, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    db_series = crud.get_series(db, series_id, profile_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    books = crud.get_books_by_series(db, series_id, profile_id)
    for book in books:
        book.is_series_finished = False

    db_series.is_finished = False
    db_series.series_status = "ongoing"

    db.commit()
    recalculate_intelligence(db, series_id)
    is_finished = bool((crud.get_series(db, series_id, profile_id) or db_series).is_finished)

    return {
        "series_id": series_id,
        "updated_books": len(books),
        "is_finished": is_finished,
        "series_status": "finished" if is_finished else "ongoing",
    }


@router.post("/{series_id}/mark_finished")
def mark_series_finished(series_id: int, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    db_series = crud.get_series(db, series_id, profile_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    books = crud.get_books_by_series(db, series_id, profile_id)
    for book in books:
        book.is_series_finished = True

    db_series.is_finished = True
    db_series.series_status = "finished"

    db.commit()
    recalculate_intelligence(db, series_id)
    is_finished = bool((crud.get_series(db, series_id, profile_id) or db_series).is_finished)

    return {
        "series_id": series_id,
        "updated_books": len(books),
        "is_finished": is_finished,
        "series_status": "finished" if is_finished else "ongoing",
    }


@router.post("/{series_id}/check")
async def check_series_for_new_books(
    series_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    profile_id: str = Depends(get_current_profile_id),
):
    db_series = crud.get_series(db, series_id, profile_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    logger.info("CHECK NOW triggered for series_id=%s, series_name=%s", series_id, db_series.name)

    # Only avoid double-starting a check that's *actively* running right now.
    # A previously *completed* job must never block a fresh Check Now click
    # from actually re-running discovery -- otherwise a stale/failed result
    # (e.g. a transient provider 503) would get replayed forever instead of
    # being retried, until the server restarts.
    existing_job = series_check_jobs.get(series_id)
    if existing_job and existing_job.get("status") == "running":
        return {
            "series_id": series_id,
            "session_id": existing_job.get("session_id"),
            "status": "running",
            "progress": int(existing_job.get("progress_percent") or 0),
            "current_pass": existing_job.get("current_pass") or "exact match",
        }

    background_tasks.add_task(run_series_check_job_full, series_id)

    session_id = f"check_{series_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    series_check_jobs[series_id] = {
        "session_id": session_id,
        "status": "running",
        "progress_percent": 0,
        "current_pass": "exact match",
        "result": None,
        "completion": None,
    }

    return {
        "series_id": series_id,
        "session_id": session_id,
        "status": "started",
        "progress": 0,
        "current_pass": "exact match",
    }


@router.get("/{series_id}/check")
def get_series_check_status(series_id: int, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    db_series = crud.get_series(db, series_id, profile_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    job = series_check_jobs.get(series_id)
    if not job:
        return {
            "series_id": series_id,
            "status": "idle",
        }

    payload = {
        "series_id": series_id,
        "session_id": job.get("session_id"),
        "status": job.get("status", "idle"),
        "updated_at": job.get("updated_at"),
        "progress_total": job.get("progress_total", 0),
        "progress_completed": job.get("progress_completed", 0),
        "progress": int(job.get("progress_percent") or 0),
        "current_book_number": job.get("current_book_number"),
        "current_pass": job.get("current_pass"),
        "current_asin": job.get("current_asin"),
        "asins_discovered": int(job.get("asins_discovered") or 0),
        "asins_processed": int(job.get("asins_processed") or 0),
        "asin_fetch_success": int(job.get("asin_fetch_success") or 0),
        "asin_fetch_failed": int(job.get("asin_fetch_failed") or 0),
    }
    if job.get("status") == "completed":
        payload.update(job.get("completion") or {"status": "complete"})
        payload["result"] = job.get("result")
    if job.get("error"):
        payload["error"] = job.get("error")
    return payload


@router.get("/{series_id}/check/status")
def get_series_check_progress_status(
    series_id: int,
    session_id: str | None = None,
    db: Session = Depends(get_db),
    profile_id: str = Depends(get_current_profile_id),
):
    db_series = crud.get_series(db, series_id, profile_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    job = series_check_jobs.get(series_id)
    if not job:
        return {
            "series_id": series_id,
            "session_id": None,
            "status": "idle",
            "progress": 0,
            "current_pass": None,
        }

    if session_id and job.get("session_id") and session_id != job.get("session_id"):
        return {
            "series_id": series_id,
            "session_id": job.get("session_id"),
            "status": "complete",
            "progress": 100,
            "current_pass": None,
            "reason": "session-mismatch",
        }

    total = int(job.get("progress_total") or 0)
    completed = int(job.get("progress_completed") or 0)
    progress = int((completed / total) * 100) if total > 0 else 0
    job["progress_percent"] = progress
    if job.get("status") == "completed":
        progress = 100

    started_raw = job.get("started_at")
    elapsed_seconds = 0
    if started_raw:
        try:
            started_at = datetime.fromisoformat(str(started_raw))
            elapsed_seconds = int((datetime.utcnow() - started_at).total_seconds())
        except ValueError:
            elapsed_seconds = 0

    if job.get("status") == "running":
        return {
            "series_id": series_id,
            "session_id": job.get("session_id"),
            "status": "running",
            "progress": progress,
            "current_pass": job.get("current_pass") or "exact match",
            "current_asin": job.get("current_asin"),
            "asins_discovered": int(job.get("asins_discovered") or 0),
            "asins_processed": int(job.get("asins_processed") or 0),
            "asin_fetch_success": int(job.get("asin_fetch_success") or 0),
            "asin_fetch_failed": int(job.get("asin_fetch_failed") or 0),
            "elapsed_seconds": elapsed_seconds,
            "timed_out": elapsed_seconds >= SERIES_CHECK_TIMEOUT_SECONDS,
        }

    completion = job.get("completion") or {
        "status": "complete",
        "complete": True,
        "missing_books": (job.get("result") or {}).get("missing_books") or [],
        "found_books": (job.get("result") or {}).get("added_books") or [],
        "no_new_books": not bool((job.get("result") or {}).get("added_books")),
        "discovery_engine": (job.get("result") or {}).get("discovery_engine") or "agent_v2",
    }
    payload = {
        "series_id": series_id,
        "session_id": job.get("session_id"),
        "progress": 100,
        "current_pass": None,
        "elapsed_seconds": elapsed_seconds,
        "timed_out": elapsed_seconds >= SERIES_CHECK_TIMEOUT_SECONDS,
        "result": job.get("result"),
    }
    payload.update(completion)
    if job.get("error"):
        payload["error"] = job.get("error")
    return payload


@router.post("/{series_id}/clear_new_books")
def clear_series_new_books(series_id: int, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    db_series = crud.get_series(db, series_id, profile_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    state = recalculate_series_state_for_series(db, series_id)
    if not state:
        raise HTTPException(status_code=404, detail="Series not found")

    payload = {
        "series_id": series_id,
        "series_state": db_series.series_state,
    }
    if not db_series.is_caught_up:
        payload["message"] = "Series is not caught up yet, so the flag stays visible until all books are read and no upcoming books remain."
    return payload


@router.post("/{series_id}/normalize_titles")
def normalize_series_titles(
    series_id: int,
    payload: schemas.NormalizeTitlesRequest,
    db: Session = Depends(get_db),
    profile_id: str = Depends(get_current_profile_id),
):
    db_series = crud.get_series(db, series_id, profile_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    books = crud.get_books_by_series(db, series_id, profile_id)
    result = normalize_series_book_titles(db, db_series, books, payload)
    if result.get("error") == "invalid_normalization_mode":
        raise HTTPException(status_code=422, detail="Invalid normalization_mode")

    db.commit()
    recalculate_intelligence(db, series_id)

    return {"series_id": series_id, **result}


@router.post("/{series_id}/recalculate_intelligence")
def rebuild_series_intelligence(series_id: int, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    db_series = crud.get_series(db, series_id, profile_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    result = recalculate_intelligence(db, series_id)
    if not result:
        raise HTTPException(status_code=500, detail="Unable to recalculate intelligence")
    return result


@router.delete("/{series_id}")
def delete_series(series_id: int, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    deleted_result = crud.delete_series(db, series_id, profile_id)
    if not deleted_result:
        raise HTTPException(status_code=404, detail="Series not found")
    return {
        "message": "Series deleted",
        "series_id": series_id,
        "deleted_books": int(deleted_result.get("deleted_books") or 0),
    }
