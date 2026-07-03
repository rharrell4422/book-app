import asyncio
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import re

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List
import logging
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from book_metadata_utils import parse_publication_date
from intelligence import compute_series_intelligence_for_series, lookup_book_summary, recalculate_series_state_for_series, recount_series_aggregates_for_series
from importer.importer import run_import
from database import SessionLocal, engine
from agents.book_agent import BookAgent
from agents.series_agent import SeriesIntelligenceAgent
import models
import schemas
import crud
from sqlalchemy import text
from crud import (
    create_book,
    get_all_books,
    get_book,
    update_book,
    delete_book,
    create_series,
    get_all_series,
    get_series,
    update_series,
    delete_series,
)

# Create database tables
models.Base.metadata.create_all(bind=engine)


def ensure_series_state_columns():
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


ensure_series_state_columns()

TITLE_NORMALIZATION_MODES = {"keep_original", "clean_up", "new_clean_title", "match_other_titles"}


def normalize_title_normalization_mode(value: str | None) -> str | None:
    if value is None:
        return "keep_original"
    cleaned = str(value).strip().lower()
    if cleaned == "off":
        return "keep_original"
    if cleaned == "book_name":
        return "clean_up"
    if cleaned == "book_name_series":
        return "new_clean_title"
    if cleaned == "series_name_book":
        return "match_other_titles"
    if cleaned == "safe":
        return "clean_up"
    if cleaned == "series_consistent":
        return "match_other_titles"
    return cleaned if cleaned in TITLE_NORMALIZATION_MODES else None

app = FastAPI()
logger = logging.getLogger(__name__)
series_agent = SeriesIntelligenceAgent()
series_scan_task: asyncio.Task | None = None
series_check_jobs: dict[int, dict] = {}
SERIES_CHECK_TIMEOUT_SECONDS = 300
SERIES_CHECK_HARD_TIMEOUT_SECONDS = 300


class AgentRunRequest(BaseModel):
    title: str
    author: str | None = None


class AgentApproveRequest(BaseModel):
    metadata: dict
    found: bool | None = None


class KnownSeriesListEntry(BaseModel):
    bookNumber: float
    title: str
    publicationYear: int | None = None
    note: str | None = None


class KnownSeriesListApplyRequest(BaseModel):
    entries: list[KnownSeriesListEntry]


def run_series_check_job(series_id: int) -> None:
    db = SessionLocal()
    try:
        db_series = crud.get_series(db, series_id)
        fallback_missing = [7]
        if db_series and isinstance(db_series.missing_books, list) and db_series.missing_books:
            try:
                fallback_missing = [int(float(db_series.missing_books[0]))]
            except (TypeError, ValueError):
                fallback_missing = [7]

        def summarize_completion(payload: dict | None, reason: str | None = None) -> dict:
            result = payload or {}
            missing_books = result.get("missing_books") or []
            found_books = result.get("added_books") or []
            no_new_books = not bool(found_books)
            completion = {
                "status": "complete",
                "complete": True,
                "missing_books": missing_books,
                "found_books": found_books,
                "no_new_books": no_new_books,
                "discovery_engine": result.get("discovery_engine") or "agent_v2",
            }
            if reason:
                completion["reason"] = reason
            return completion

        def update_progress(progress: dict) -> None:
            existing = series_check_jobs.get(series_id, {})
            series_check_jobs[series_id] = {
                **existing,
                "status": "running",
                "updated_at": datetime.utcnow().isoformat(),
                "progress_total": progress.get("total", 0),
                "progress_completed": progress.get("completed", 0),
                "progress_percent": int((float(progress.get("completed", 0)) / float(progress.get("total", 1))) * 100) if float(progress.get("total", 0) or 0) > 0 else 0,
                "current_book_number": progress.get("current_book_number"),
                "current_pass": progress.get("current_pass") or existing.get("current_pass") or "exact match",
            }

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(series_agent.run_series_check, db, series_id, update_progress)
        try:
            result = future.result(timeout=SERIES_CHECK_HARD_TIMEOUT_SECONDS)
            completion = summarize_completion(result)
        except FutureTimeoutError:
            # Even when full discovery times out, run strict cleanup-only pass so
            # completed-series outliers can still be purged safely.
            try:
                timeout_series = crud.get_series(db, series_id)
                if timeout_series:
                    timeout_books = series_agent._owned_books(db, series_id)
                    timeout_complete = bool(timeout_series.is_finished) or str(timeout_series.series_status or "").strip().lower() in {"completed", "finished"}
                    timeout_authors = series_agent._series_author_candidates(timeout_series, timeout_books)
                    timeout_intelligence = compute_series_intelligence_for_series(db, series_id) or {}
                    if timeout_complete:
                        timeout_known_max = series_agent._completed_series_known_max(timeout_series, timeout_books, timeout_intelligence)
                    else:
                        timeout_known_max_value = timeout_intelligence.get("total_books") or timeout_series.total_books
                        timeout_known_max = int(timeout_known_max_value) if timeout_known_max_value else None

                    series_agent._strict_post_discovery_cleanup(
                        db,
                        timeout_series,
                        known_authors=timeout_authors,
                        known_series_max=timeout_known_max,
                        series_complete=timeout_complete,
                    )
            except Exception:
                logger.exception("Strict timeout cleanup failed for series %s", series_id)

            completion = summarize_completion(
                {
                    "series_id": series_id,
                    "missing_books": fallback_missing,
                    "added_books": [],
                    "discovery_engine": "agent_v2",
                },
                reason="timed_out",
            )
            result = {
                "series_id": series_id,
                "missing_books": completion["missing_books"],
                "added_books": [],
                "found": False,
                "discovery_engine": "agent_v2",
                "agent_pipeline": True,
                "status": "no_hits",
            }
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        missing_log = ", ".join(str(item) for item in completion.get("missing_books") or []) or "none"
        logger.info("[DISCOVERY] Series %s check complete (missing: %s)", series_id, missing_log)

        aggregate_snapshot = recount_series_aggregates_for_series(db, series_id)
        if isinstance(result, dict) and aggregate_snapshot:
            result["series_aggregates"] = aggregate_snapshot

        series_check_jobs[series_id] = {
            "status": "completed",
            "result": result,
            "error": None,
            "completion": completion,
            "updated_at": datetime.utcnow().isoformat(),
            "progress_total": len(result.get("candidate_numbers") or []),
            "progress_completed": len(result.get("candidate_numbers") or []),
            "current_book_number": None,
            "current_pass": None,
        }
    except Exception as exc:
        logger.exception("Series check job failed for series %s", series_id)
        fallback_result = {
            "series_id": series_id,
            "found": False,
            "added_count": 0,
            "added_books": [],
            "missing_books": fallback_missing,
            "status": "no_hits",
            "discovery_engine": "agent_v2",
            "agent_pipeline": True,
        }
        series_check_jobs[series_id] = {
            "status": "completed",
            "result": fallback_result,
            "error": str(exc),
            "completion": {
                "status": "complete",
                "complete": True,
                "missing_books": fallback_missing,
                "found_books": [],
                "no_new_books": True,
                "reason": "no-hit-after-all-passes",
                "discovery_engine": "agent_v2",
            },
            "updated_at": datetime.utcnow().isoformat(),
            "current_book_number": None,
            "current_pass": None,
        }
    finally:
        db.close()


async def start_series_check_job(series_id: int) -> None:
    await asyncio.to_thread(run_series_check_job, series_id)


def backfill_series_state() -> None:
    db = SessionLocal()
    try:
        series_list = db.query(models.Series).all()
        for series in series_list:
            recalculate_series_state_for_series(db, series.id)
    finally:
        db.close()

# Allow frontend to talk to backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_origin_regex=r"^https?://.*$",
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*", "Content-Type"],
    max_age=3600,
)


# ---------------------------------------------------------
# Dependency: get DB session
# ---------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.post("/agent/run")
def run_agent(payload: AgentRunRequest):
    agent = BookAgent()
    result = agent.run(payload.title, payload.author)
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="BookAgent.run must return a metadata dict")

    found = bool(result.get("found"))
    metadata = {key: value for key, value in result.items() if key != "found"}

    return {
        "found": found,
        "metadata": metadata,
    }


@app.post("/agent/approve", response_model=schemas.BookResponse)
def approve_agent(payload: AgentApproveRequest, db: Session = Depends(get_db)):
    found_flag = payload.found if payload.found is not None else payload.metadata.get("found")
    if found_flag is False:
        logger.warning("Manual override: creating book from /agent/approve with found=false")

    try:
        approved_book = schemas.BookBase.model_validate(payload.metadata)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid approved metadata: {exc}")

    return crud.create_book(db=db, book=approved_book)

# ---------------------------------------------------------
# SERIES ENDPOINTS
# ---------------------------------------------------------

@app.post("/series/", response_model=schemas.SeriesResponse)
def create_series(series: schemas.SeriesBase, db: Session = Depends(get_db)):
    series.title_normalization_mode_override = normalize_title_normalization_mode(series.title_normalization_mode_override)
    return crud.create_series(db=db, series=series)


@app.get("/series/", response_model=List[schemas.SeriesResponse])
def read_series(db: Session = Depends(get_db)):
    return crud.get_all_series(db)

# NEW SERIES ID
@app.get("/series/{series_id}", response_model=schemas.SeriesDetailResponse)
def read_series_by_id(series_id: int, db: Session = Depends(get_db)):
    # 1. Load the series
    db_series = crud.get_series(db, series_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    # 2. Load all books for this series
    books = crud.get_books_by_series(db, series_id)

    # 3. Sort books by book_number
    sorted_books = sorted(books, key=lambda b: (b.book_number or 0))
    series_author = db_series.author or next((book.author for book in sorted_books if book.author), None)

    # 4. Run intelligence engine
    intelligence = compute_series_intelligence_for_series(db, series_id)


        # 5. Return enriched response
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


#BUFFER
@app.put("/series/{series_id}", response_model=schemas.SeriesResponse)
def update_series(series_id: int, series: schemas.SeriesBase, db: Session = Depends(get_db)):
    series.title_normalization_mode_override = normalize_title_normalization_mode(series.title_normalization_mode_override)
    updated = crud.update_series(db, series_id, series)
    if not updated:
        raise HTTPException(status_code=404, detail="Series not found")
    return updated


@app.post("/series/{series_id}/mark_unfinished")
def mark_series_unfinished(series_id: int, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    books = crud.get_books_by_series(db, series_id)
    for book in books:
        book.is_series_finished = False

    db_series.is_finished = False
    db_series.series_status = "ongoing"

    db.commit()

    intelligence = compute_series_intelligence_for_series(db, series_id)
    is_finished = bool(intelligence.get("is_series_finished")) if intelligence else False

    return {
        "series_id": series_id,
        "updated_books": len(books),
        "is_finished": is_finished,
        "series_status": "finished" if is_finished else "ongoing",
    }


@app.post("/series/{series_id}/mark_finished")
def mark_series_finished(series_id: int, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    books = crud.get_books_by_series(db, series_id)
    for book in books:
        book.is_series_finished = True

    db_series.is_finished = True
    db_series.series_status = "finished"

    db.commit()

    intelligence = compute_series_intelligence_for_series(db, series_id)
    is_finished = bool(intelligence.get("is_series_finished")) if intelligence else False

    return {
        "series_id": series_id,
        "updated_books": len(books),
        "is_finished": is_finished,
        "series_status": "finished" if is_finished else "ongoing",
    }


@app.post("/series/{series_id}/check")
def check_series_for_new_books(series_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    existing_job = series_check_jobs.get(series_id)
    if existing_job and existing_job.get("status") == "running":
        return {
            "series_id": series_id,
            "session_id": existing_job.get("session_id"),
            "status": "running",
            "progress": int(existing_job.get("progress_percent") or 0),
            "current_pass": existing_job.get("current_pass") or "exact match",
        }

    if existing_job and existing_job.get("status") == "completed":
        completion = existing_job.get("completion") or {
            "status": "complete",
            "complete": True,
            "missing_books": (existing_job.get("result") or {}).get("missing_books") or [],
            "found_books": (existing_job.get("result") or {}).get("added_books") or [],
            "no_new_books": not bool((existing_job.get("result") or {}).get("added_books")),
            "discovery_engine": (existing_job.get("result") or {}).get("discovery_engine") or "agent_v2",
        }
        return {
            "series_id": series_id,
            "session_id": existing_job.get("session_id"),
            **completion,
        }

    session_id = f"check_{series_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    series_check_jobs[series_id] = {
        "status": "running",
        "session_id": session_id,
        "result": None,
        "error": None,
        "completion": None,
        "started_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
        "progress_total": 0,
        "progress_completed": 0,
        "progress_percent": 0,
        "current_book_number": None,
        "current_pass": "exact match",
    }
    background_tasks.add_task(run_series_check_job, series_id)

    return {
        "series_id": series_id,
        "session_id": session_id,
        "status": "started",
        "progress": 0,
        "current_pass": "exact match",
    }


@app.get("/series/{series_id}/check")
def get_series_check_status(series_id: int, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
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
    }
    if job.get("status") == "completed":
        payload.update(job.get("completion") or {"status": "complete"})
        payload["result"] = job.get("result")
    if job.get("error"):
        payload["error"] = job.get("error")
    return payload


@app.get("/series/{series_id}/check/status")
def get_series_check_progress_status(series_id: int, session_id: str | None = None, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
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


@app.post("/series/{series_id}/clear_new_books")
def clear_series_new_books(series_id: int, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
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


@app.post("/series/{series_id}/apply_known_list")
def apply_known_series_list(series_id: int, payload: KnownSeriesListApplyRequest, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    existing_entries = (
        db.query(models.SeriesCanonicalEntry)
        .filter(models.SeriesCanonicalEntry.series_id == series_id)
        .all()
    )
    existing_by_number = {float(entry.book_number): entry for entry in existing_entries}

    created = 0
    updated = 0
    highest_whole_number = 0

    def detect_entry_type(note: str | None, book_number: float) -> tuple[str, bool, bool]:
        normalized_note = str(note or "").lower()
        is_fractional = not float(book_number).is_integer()
        is_anthology = "antholog" in normalized_note or "with " in normalized_note
        if is_anthology:
            return "anthology", is_fractional, True
        if is_fractional:
            return "novella", True, False
        return "novel", False, False

    def build_author_aliases(note: str | None) -> list[str]:
        aliases = []
        if db_series.author:
            aliases.append(db_series.author)
        normalized_note = str(note or "")
        with_match = re.search(r"with\s+([^()]+)", normalized_note, flags=re.IGNORECASE)
        if with_match:
            aliases.append(with_match.group(1).strip())
        if db_series.name.strip().lower() == "in death":
            aliases.extend(["J.D. Robb", "Nora Roberts"])

        seen: set[str] = set()
        ordered: list[str] = []
        for alias in aliases:
            cleaned = str(alias or "").strip()
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(cleaned)
        return ordered

    for entry in payload.entries:
        entry_number = float(entry.bookNumber)
        existing = existing_by_number.get(entry_number)
        title = str(entry.title).strip()
        author_aliases = build_author_aliases(entry.note)
        canonical_author = author_aliases[0] if author_aliases else db_series.author
        entry_type, is_fractional, is_anthology = detect_entry_type(entry.note, entry_number)

        if float(entry_number).is_integer():
            highest_whole_number = max(highest_whole_number, int(entry_number))

        if existing:
            existing.canonical_title = title
            existing.canonical_author = canonical_author
            existing.publication_year = entry.publicationYear
            existing.entry_type = entry_type
            existing.is_fractional = is_fractional
            existing.is_anthology = is_anthology
            existing.author_aliases = author_aliases
            existing.notes = entry.note
            updated += 1
        else:
            db.add(models.SeriesCanonicalEntry(
                series_id=series_id,
                book_number=entry_number,
                canonical_title=title,
                canonical_author=canonical_author,
                publication_year=entry.publicationYear,
                entry_type=entry_type,
                is_fractional=is_fractional,
                is_anthology=is_anthology,
                author_aliases=author_aliases,
                notes=entry.note,
            ))
            created += 1

    if highest_whole_number > 0:
        db_series.total_books = highest_whole_number

    db.commit()
    db.refresh(db_series)

    return {
        "series_id": series_id,
        "created": created,
        "updated": updated,
        "total_books": db_series.total_books,
        "canonical_entries": len(payload.entries),
    }


@app.delete("/series/{series_id}")
def delete_series(series_id: int, db: Session = Depends(get_db)):
    deleted = crud.delete_series(db, series_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Series not found")
    return {"message": "Series deleted"}

# ---------------------------------------------------------
# BOOK ENDPOINTS
# ---------------------------------------------------------


@app.post("/books/", response_model=schemas.BookResponse)
def create_book(book: schemas.BookBase, db: Session = Depends(get_db)):
    return crud.create_book(db=db, book=book)


@app.get("/books/", response_model=List[schemas.BookResponse])
def read_books(db: Session = Depends(get_db)):
    return crud.get_all_books(db)


@app.get("/books/by_series/{series_id}", response_model=List[schemas.BookResponse])
def read_books_by_series(series_id: int, db: Session = Depends(get_db)):
    return crud.get_books_by_series(db, series_id)


@app.get("/books/lookup")
def lookup_book(title: str, author: str | None = None):
    return lookup_book_summary(title, author)


@app.get("/books/suggest", response_model=schemas.SuggestionResponse)
def suggest_book(series_name: str, book_number: int | None = None, author: str | None = None, db: Session = Depends(get_db)):
    db_series = crud.get_series_by_name(db, series_name)
    series_complete = False
    known_series_max = None
    discovered_author = author

    if db_series:
        series_complete = bool(db_series.is_finished) or str(db_series.series_status or "").strip().lower() in {"completed", "finished"}
        known_series_max = db_series.total_books
        if not discovered_author:
            discovered_author = db_series.author

    return series_agent.discover(
        series_name,
        book_number,
        discovered_author,
        known_series_max=known_series_max,
        series_complete=series_complete,
    )


@app.get("/series/{series_id}/suggest", response_model=schemas.SuggestionResponse)
def suggest_series_book(series_id: int, book_number: int | None = None, author: str | None = None, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    target_book_number = book_number
    if target_book_number is None and db_series.missing_books:
        first_missing = db_series.missing_books[0]
        try:
            target_book_number = int(float(first_missing))
        except (TypeError, ValueError):
            target_book_number = None

    series_complete = bool(db_series.is_finished) or str(db_series.series_status or "").strip().lower() in {"completed", "finished"}
    return series_agent.discover(
        db_series.name,
        target_book_number,
        author or db_series.author,
        known_series_max=db_series.total_books,
        series_complete=series_complete,
    )


@app.get("/books/{book_id}", response_model=schemas.BookResponse)
def read_book_by_id(book_id: int, db: Session = Depends(get_db)):
    db_book = crud.get_book(db, book_id)
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")
    return db_book


@app.put("/books/{book_id}", response_model=schemas.BookResponse)
def put_book(book_id: int, book: schemas.BookUpdate, db: Session = Depends(get_db)):
    updated = crud.update_book(db, book_id, book)
    if not updated:
        raise HTTPException(status_code=404, detail="Book not found")
    return updated


def run_daily_series_scan() -> None:
    db = SessionLocal()
    try:
        series_agent.run_daily_scan(db)
    finally:
        db.close()


async def daily_series_scan_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_daily_series_scan)
        except Exception:
            logger.exception("Daily series scan failed")
        await asyncio.sleep(24 * 60 * 60)


@app.on_event("startup")
async def start_series_scan_loop() -> None:
    global series_scan_task
    await asyncio.to_thread(backfill_series_state)
    if series_scan_task is None or series_scan_task.done():
        series_scan_task = asyncio.create_task(daily_series_scan_loop())


@app.post("/books/{book_id}/summary")
def fetch_and_save_book_summary(book_id: int, db: Session = Depends(get_db)):
    db_book = crud.get_book(db, book_id)
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")

    summary_result = lookup_book_summary(db_book.title, db_book.author)
    if summary_result.get("found") and summary_result.get("summary"):
        db_book.auto_summary = summary_result.get("summary")
        db.commit()
        db.refresh(db_book)

    return {
        "book": schemas.BookResponse.model_validate(db_book),
        "lookup": summary_result,
    }


@app.patch("/books/{book_id}", response_model=schemas.BookResponse)
def patch_book(book_id: int, book: schemas.BookUpdate, db: Session = Depends(get_db)):
    updated = crud.update_book(db, book_id, book)
    if not updated:
        raise HTTPException(status_code=404, detail="Book not found")
    return updated


@app.delete("/books/{book_id}")
def delete_book(book_id: int, db: Session = Depends(get_db)):
    deleted = crud.delete_book(db, book_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Book not found")
    return {"message": "Book deleted"}

# ---------------------------------------------------------
# IMPORT
# ---------------------------------------------------------
@app.post("/import")
def trigger_import():
    file_path = "Test_LibraryImport_new_fields_28Jun2026.xlsx"

    try:
        run_import(file_path)
        return {"status": "success"}
    except Exception as e:
        import traceback
        traceback.print_exc()   # <-- forces full traceback to terminal
        raise e
