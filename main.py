import asyncio
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import re

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from typing import List, Literal
import logging
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from book_metadata_utils import parse_publication_date
from intelligence import compute_series_intelligence_for_series, lookup_book_summary, recalculate_intelligence, recalculate_series_state_for_series, recount_series_aggregates_for_series
from importer.importer import run_import
from database import SessionLocal, engine
from agents.book_agent import BookAgent
from agents.series_agent import SeriesIntelligenceAgent
import models
import schemas
import crud
from sqlalchemy import or_, text
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

###From Joe
# --- Background Task Wrapper ---
from agents.series_agent import SeriesIntelligenceAgent
from database import SessionLocal

agent = SeriesIntelligenceAgent()

def run_series_check_job(series_id: int):
    print(f"BACKGROUND TASK FIRED for series {series_id}")
    db = SessionLocal()
    try:
        result = agent.run_series_check(db, series_id)
        print(f"BACKGROUND TASK COMPLETED for series {series_id}")
        return result
    except Exception as e:
        print(f"BACKGROUND TASK ERROR for series {series_id}: {e}")
        raise
    finally:
        db.close()
###From Joe



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


class SeriesImportConfirmationDecision(BaseModel):
    book_id: int
    decision: Literal["yes", "no", "dont_know"]
    series_name: str | None = None
    note: str | None = None


class SeriesImportConfirmationResolveRequest(BaseModel):
    decisions: list[SeriesImportConfirmationDecision]


class NormalizeTitlesRequest(BaseModel):
    normalization_mode: str
    custom_pattern: str | None = None
    exclude_upcoming: bool = True


def _is_ghost_book(book: models.Book) -> bool:
    return bool(book.is_missing) or bool(book.is_upcoming_auto) or bool(book.is_upcoming_final)


def _format_book_number(value: float | int | None) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number.is_integer():
        return str(int(number))
    return str(number)


def _extract_book_number_from_title(title: str) -> float | None:
    match = re.search(r"\bbook\s+(\d+(?:\.\d+)?)\b", title or "", flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def _normalize_title_cleanup_only(raw_title: str) -> str:
    title = str(raw_title or "").strip()
    if not title:
        return ""

    title = re.sub(r"\s+ebook\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+kindle\s+edition\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*\(unabridged\)\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r":\s*", ": ", title)
    title = re.sub(r"\(\s+", "(", title)
    title = re.sub(r"\s+\)", ")", title)
    title = re.sub(r"\s{2,}", " ", title)

    title = re.sub(r":\s*a\s+litrpg\s+apocalypse\s*:?$", ": A LitRPG", title, flags=re.IGNORECASE).strip()
    title = re.sub(
        r":\s*a\s+litrpg\s+(?:adventure|novel|saga|epic|fantasy|progression\s+fantasy)\s*:?$",
        ": A LitRPG",
        title,
        flags=re.IGNORECASE,
    ).strip()
    title = re.sub(
        r":\s*litrpg\s+(?:adventure|novel|saga|epic|fantasy|progression\s+fantasy)\s*:?$",
        ": LitRPG",
        title,
        flags=re.IGNORECASE,
    ).strip()

    return re.sub(r"\s{2,}", " ", title).strip()


def _normalize_title_clean_up(raw_title: str, series_name: str | None = None) -> str:
    title = _normalize_title_cleanup_only(raw_title)
    if not title:
        return ""

    title = re.sub(r":\s*:", ": ", title)

    repeated_pattern = re.compile(r"^(.*?):\s*\((book\s+[^)]+)\)\s*:\s*\(([^)]*\bbook\s*\d+[^)]*)\)\s*$", flags=re.IGNORECASE)
    repeated_match = repeated_pattern.match(title)
    if repeated_match:
        stem = str(repeated_match.group(1) or "").strip()
        book_word = str(repeated_match.group(2) or "").strip()
        suffix = str(repeated_match.group(3) or "").strip()
        return re.sub(r"\s{2,}", " ", f"{stem}: {book_word} ({suffix})").strip()

    clean_series_name = str(series_name or "").strip()
    if clean_series_name:
        escaped = re.escape(clean_series_name)
        title = re.sub(rf"^({escaped})\s*:\s*{escaped}\s*", r"\1: ", title, flags=re.IGNORECASE).strip()

    return title


def _normalize_title_book_name_only(raw_title: str) -> str:
    cleaned = _normalize_title_cleanup_only(raw_title)
    if not cleaned:
        return ""

    stripped = re.sub(r"\s*:\s*\([^)]*\)\s*$", "", cleaned, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*:\s*.*$", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s+[-–]\s+.*$", "", stripped, flags=re.IGNORECASE)
    stripped = stripped.strip()
    return stripped or cleaned


def _normalize_title_new_clean(raw_title: str, series_name: str | None = None, book_number: float | int | None = None) -> str:
    cleaned = _normalize_title_clean_up(raw_title, series_name)
    if not cleaned:
        return ""

    inferred_book_number = _extract_book_number_from_title(cleaned)
    resolved_number = book_number if book_number is not None else inferred_book_number

    inferred_series = ""
    inferred_series_match = re.search(r"\(\s*([^()]*?)\s+book\s*\d+(?:\.\d+)?\s*\)\s*$", cleaned, flags=re.IGNORECASE)
    if inferred_series_match:
        inferred_series = str(inferred_series_match.group(1) or "").strip()

    clean_series_name = str(series_name or inferred_series or "").strip()
    if not clean_series_name or resolved_number is None:
        return _normalize_title_book_name_only(cleaned)

    pretty_number = _format_book_number(resolved_number)
    core_title = _normalize_title_book_name_only(cleaned)
    return re.sub(r"\s{2,}", " ", f"{core_title} ({clean_series_name} Book {pretty_number})").strip()


def _infer_series_title_pattern(books: list[models.Book]) -> str:
    with_suffix = 0
    title_only = 0

    for book in books or []:
        title = str(getattr(book, "title", "") or "").strip()
        if not title:
            continue
        if re.search(r"\([^)]*\bbook\s*\d+(?:\.\d+)?[^)]*\)\s*$", title, flags=re.IGNORECASE):
            with_suffix += 1
        else:
            title_only += 1

    return "with_suffix" if with_suffix >= title_only else "title_only"


def _normalize_title_for_mode(
    raw_title: str,
    mode: str,
    series_name: str | None,
    book_number: float | int | None,
    books: list[models.Book],
) -> str:
    raw = str(raw_title or "").strip()
    if not raw or mode == "keep_original":
        return raw

    if mode == "clean_up":
        return _normalize_title_clean_up(raw, series_name)

    if mode == "new_clean_title":
        return _normalize_title_new_clean(raw, series_name, book_number)

    clean_title = _normalize_title_clean_up(raw, series_name)
    series_pattern = _infer_series_title_pattern(books)
    if series_pattern == "title_only":
        return _normalize_title_book_name_only(clean_title)
    return _normalize_title_new_clean(clean_title, series_name, book_number)


def _apply_custom_title_pattern(
    pattern: str | None,
    original_title: str,
    series_name: str | None,
    book_number: float | int | None,
    book_subtitle: str | None,
) -> str:
    clean_pattern = str(pattern or "").strip()
    book_title = _normalize_title_book_name_only(original_title)
    if not clean_pattern:
        return book_title

    inferred_subtitle = ""
    cleaned_original = _normalize_title_cleanup_only(original_title)
    without_suffix = re.sub(r"\s*\([^)]*\bbook\s*\d+(?:\.\d+)?[^)]*\)\s*$", "", cleaned_original, flags=re.IGNORECASE).strip()
    if ":" in without_suffix:
        inferred_subtitle = str(without_suffix.split(":", 1)[1] or "").strip()
    elif " - " in without_suffix:
        inferred_subtitle = str(without_suffix.split(" - ", 1)[1] or "").strip()

    resolved_subtitle = str(book_subtitle or inferred_subtitle or "").strip()

    replacements = {
        "{series_name}": str(series_name or "").strip(),
        "{book_number}": _format_book_number(book_number),
        "{book_title}": book_title,
        "{book_subtitle}": resolved_subtitle,
        "{original_title}": str(original_title or "").strip(),
    }

    raw_patterns = [part.strip() for part in re.split(r"\s*\|\|\s*|\n+", clean_pattern) if part.strip()]
    patterns = raw_patterns or [clean_pattern]

    def render_candidate(candidate: str) -> str:
        rendered = str(candidate or "")

        def replace_optional_block(match: re.Match) -> str:
            block = str(match.group(1) or "")
            tokens = set(re.findall(r"\{[a-z_]+\}", block))
            if tokens and any(not str(replacements.get(token) or "").strip() for token in tokens):
                return ""

            block_rendered = block
            for token, value in replacements.items():
                block_rendered = block_rendered.replace(token, value)
            return block_rendered

        previous = None
        while previous != rendered:
            previous = rendered
            rendered = re.sub(r"\[\[([\s\S]*?)\]\]", replace_optional_block, rendered)

        for token, value in replacements.items():
            rendered = rendered.replace(token, value)

        rendered = re.sub(r"\(\s*\)", "", rendered)
        rendered = re.sub(r"\[\s*\]", "", rendered)
        rendered = re.sub(r"\s+([,;:.!?])", r"\1", rendered)
        rendered = re.sub(r"\s{2,}", " ", rendered)
        return rendered.strip(" -,:;")

    first_rendered = ""
    for candidate in patterns:
        rendered = render_candidate(candidate)
        if not rendered:
            continue
        if not first_rendered:
            first_rendered = rendered
        if rendered != book_title:
            return rendered

    return first_rendered or book_title


def _is_upcoming_future_book(book: models.Book, *, today: date) -> bool:
    status = str(getattr(book, "read_status", "") or "").strip().lower()
    publication_date = getattr(book, "publication_date", None)
    if status != "upcoming" or not isinstance(publication_date, date):
        return False
    return publication_date > today

###Changed from def run_series_check_job(series_id: int) -> None:
### to def run_series_check_job_full(series_id: int) -> None:

def run_series_check_job_full(series_id: int) -> None:
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

        rebuild_snapshot = recalculate_intelligence(db, series_id, scan_result=result if isinstance(result, dict) else None)
        if isinstance(result, dict) and rebuild_snapshot:
            result["series_aggregates"] = {
                "total_books": rebuild_snapshot.get("total_books"),
                "active_count": rebuild_snapshot.get("active_count"),
                "deleted_count": rebuild_snapshot.get("deleted_count"),
                "upcoming_count": rebuild_snapshot.get("upcoming_count"),
            }

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
    recalculate_intelligence(db, series_id)
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
    recalculate_intelligence(db, series_id)
    is_finished = bool((crud.get_series(db, series_id) or db_series).is_finished)

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
    recalculate_intelligence(db, series_id)
    is_finished = bool((crud.get_series(db, series_id) or db_series).is_finished)

    return {
        "series_id": series_id,
        "updated_books": len(books),
        "is_finished": is_finished,
        "series_status": "finished" if is_finished else "ongoing",
    }

##From Joe
def test_background_task():
    print("TEST BACKGROUND TASK RAN")
###From Joe

############ Joe Replacements
@app.post("/series/{series_id}/check")
async def check_series_for_new_books(
    series_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
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

    # NEW: schedule the background task
    print(f"ENDPOINT: scheduling background task for series {series_id}")

    # REAL: your agent background task (changed from:run_series_check_job) )
    background_tasks.add_task(run_series_check_job_full, series_id)


    # NEW: initialize job state:
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
#########Joe Replacements


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


@app.post("/series/{series_id}/delete_ghost_books")
def delete_series_ghost_books(series_id: int, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    candidate_books = (
        db.query(models.Book)
        .filter(models.Book.series_id == series_id)
        .filter(or_(models.Book.record_status.is_(None), models.Book.record_status != "deleted"))
        .all()
    )

    deleted_books: list[dict] = []
    for book in candidate_books:
        if not _is_ghost_book(book):
            continue
        book.record_status = "deleted"
        deleted_books.append(
            {
                "id": book.id,
                "title": book.title,
                "book_number": book.book_number,
            }
        )

    if deleted_books:
        db.commit()

        # After ghost purge, sync known total to actual active catalog footprint
        # so stale placeholder-driven totals do not survive recalc.
        remaining_active_books = (
            db.query(models.Book)
            .filter(models.Book.series_id == series_id)
            .filter(or_(models.Book.record_status.is_(None), models.Book.record_status != "deleted"))
            .all()
        )

        numbered_values: list[int] = []
        for book in remaining_active_books:
            raw_value = book.book_number if book.book_number is not None else book.series_order
            try:
                numeric_value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if numeric_value > 0 and numeric_value.is_integer():
                numbered_values.append(int(numeric_value))

        numbered_max = max(numbered_values) if numbered_values else 0
        db_series.total_books = max(len(remaining_active_books), numbered_max)
        db.commit()

    recalculate_intelligence(db, series_id)

    return {
        "series_id": series_id,
        "deleted_count": len(deleted_books),
        "deleted_books": deleted_books,
    }


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
    recalculate_intelligence(db, series_id)

    return {
        "series_id": series_id,
        "created": created,
        "updated": updated,
        "total_books": db_series.total_books,
        "canonical_entries": len(payload.entries),
    }


@app.post("/series/{series_id}/normalize_titles")
def normalize_series_titles(series_id: int, payload: NormalizeTitlesRequest, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    raw_mode = str(payload.normalization_mode or "").strip().lower()
    is_custom_mode = raw_mode == "custom"
    mode = raw_mode if is_custom_mode else normalize_title_normalization_mode(raw_mode)
    if not mode:
        raise HTTPException(status_code=422, detail="Invalid normalization_mode")

    books = crud.get_books_by_series(db, series_id)
    today = datetime.utcnow().date()
    updated_rows: list[dict] = []
    skipped_upcoming_ids: list[int] = []
    empty_title_count = 0
    considered_count = 0

    for book in books:
        current_title = str(getattr(book, "title", "") or "").strip()
        if not current_title:
            empty_title_count += 1
            continue

        if payload.exclude_upcoming and _is_upcoming_future_book(book, today=today):
            skipped_upcoming_ids.append(int(book.id))
            continue

        considered_count += 1

        resolved_number = getattr(book, "book_number", None)
        if resolved_number is None:
            resolved_number = getattr(book, "series_order", None)

        if is_custom_mode:
            normalized_title = _apply_custom_title_pattern(
                payload.custom_pattern,
                current_title,
                db_series.name,
                resolved_number,
                getattr(book, "subtitle", None),
            )
        else:
            normalized_title = _normalize_title_for_mode(
                current_title,
                mode,
                db_series.name,
                resolved_number,
                books,
            )

        normalized_title = str(normalized_title or "").strip()
        if not normalized_title or normalized_title == current_title:
            continue

        book.title = normalized_title
        updated_rows.append({
            "id": int(book.id),
            "from": current_title,
            "to": normalized_title,
        })

    if not is_custom_mode:
        db_series.title_normalization_mode_override = mode

    db.commit()

    recalculate_intelligence(db, series_id)

    unchanged_count = max(0, considered_count - len(updated_rows))

    return {
        "series_id": series_id,
        "normalization_mode": "custom" if is_custom_mode else mode,
        "updated_count": len(updated_rows),
        "considered_count": considered_count,
        "unchanged_count": unchanged_count,
        "skipped_upcoming_count": len(skipped_upcoming_ids),
        "skipped_upcoming_ids": skipped_upcoming_ids,
        "updated_books": updated_rows,
        "normalization_diagnostics": {
            "total_books": len(books),
            "empty_title_count": empty_title_count,
            "skipped_upcoming_count": len(skipped_upcoming_ids),
            "considered_count": considered_count,
            "updated_count": len(updated_rows),
            "unchanged_count": unchanged_count,
        },
    }


@app.post("/series/{series_id}/recalculate_intelligence")
def rebuild_series_intelligence(series_id: int, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    result = recalculate_intelligence(db, series_id)
    if not result:
        raise HTTPException(status_code=500, detail="Unable to recalculate intelligence")
    return result


@app.delete("/series/{series_id}")
def delete_series(series_id: int, db: Session = Depends(get_db)):
    deleted_result = crud.delete_series(db, series_id)
    if not deleted_result:
        raise HTTPException(status_code=404, detail="Series not found")
    return {
        "message": "Series deleted",
        "series_id": series_id,
        "deleted_books": int(deleted_result.get("deleted_books") or 0),
    }

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
        result = run_import(file_path)
        return {
            "status": "success",
            "import_summary": result,
        }
    except Exception as e:
        import traceback
        traceback.print_exc()   # <-- forces full traceback to terminal
        raise e


@app.get("/import/series_confirmations")
def get_import_series_confirmation_queue(include_resolved: bool = False, db: Session = Depends(get_db)):
    books = db.query(models.Book).all()
    queue: list[dict] = []

    for book in books:
        metadata = book.import_raw_row if isinstance(book.import_raw_row, dict) else {}
        if not metadata:
            continue

        required = bool(metadata.get("series_confirmation_required"))
        decision = str(metadata.get("series_confirmation_decision") or "").strip().lower() or None

        if not include_resolved and not required:
            continue

        queue.append(
            {
                "book_id": int(book.id),
                "title": book.title,
                "author": book.author,
                "current_series_id": book.series_id,
                "current_series_name": book.series.name if book.series else None,
                "candidate_series_name": metadata.get("series_candidate_name"),
                "reason": metadata.get("series_confirmation_reason"),
                "decision": decision,
                "title_has_series_number": bool(metadata.get("title_has_series_number")),
                "updated_at": book.updated_at.isoformat() if book.updated_at else None,
            }
        )

    queue.sort(key=lambda row: row.get("book_id") or 0)
    return {
        "pending_count": sum(1 for row in queue if row.get("decision") in (None, "", "dont_know")),
        "total_count": len(queue),
        "items": queue,
    }


@app.post("/import/series_confirmations/resolve")
def resolve_import_series_confirmations(payload: SeriesImportConfirmationResolveRequest, db: Session = Depends(get_db)):
    if not payload.decisions:
        return {
            "processed": 0,
            "updated": 0,
            "results": [],
        }

    results: list[dict] = []
    updated = 0
    affected_series_ids: set[int] = set()

    for decision_item in payload.decisions:
        book = crud.get_book(db, decision_item.book_id)
        if not book:
            results.append(
                {
                    "book_id": int(decision_item.book_id),
                    "status": "not_found",
                }
            )
            continue

        metadata = book.import_raw_row if isinstance(book.import_raw_row, dict) else {}
        metadata = dict(metadata)
        old_series_id = int(book.series_id) if book.series_id is not None else None

        candidate_series_name = str(decision_item.series_name or metadata.get("series_candidate_name") or "").strip() or None
        selected_decision = str(decision_item.decision)

        if selected_decision == "yes":
            if not candidate_series_name:
                results.append(
                    {
                        "book_id": int(book.id),
                        "status": "missing_candidate_series",
                        "decision": selected_decision,
                    }
                )
                continue

            canonical_series = crud.get_series_by_name(db, candidate_series_name)
            if not canonical_series:
                results.append(
                    {
                        "book_id": int(book.id),
                        "status": "canonical_series_not_found",
                        "decision": selected_decision,
                        "candidate_series_name": candidate_series_name,
                    }
                )
                continue

            book.series_id = canonical_series.id
            metadata["series_confirmation_required"] = False
            metadata["series_candidate_name"] = canonical_series.name
            metadata["series_confirmation_reason"] = metadata.get("series_confirmation_reason") or "user_confirmed"
            metadata["series_confirmation_decision"] = "yes"
            metadata["series_confirmation_decided_at"] = datetime.utcnow().isoformat()
            if decision_item.note:
                metadata["series_confirmation_note"] = str(decision_item.note)

            if old_series_id is not None:
                affected_series_ids.add(old_series_id)
            affected_series_ids.add(int(canonical_series.id))
            updated += 1
            results.append(
                {
                    "book_id": int(book.id),
                    "status": "linked",
                    "decision": "yes",
                    "series_id": int(canonical_series.id),
                    "series_name": canonical_series.name,
                }
            )

        elif selected_decision == "no":
            book.series_id = None
            metadata["series_confirmation_required"] = False
            metadata["series_confirmation_decision"] = "no"
            metadata["series_confirmation_decided_at"] = datetime.utcnow().isoformat()
            if decision_item.note:
                metadata["series_confirmation_note"] = str(decision_item.note)

            if old_series_id is not None:
                affected_series_ids.add(old_series_id)
            updated += 1
            results.append(
                {
                    "book_id": int(book.id),
                    "status": "left_unlinked",
                    "decision": "no",
                }
            )

        else:
            metadata["series_confirmation_required"] = True
            metadata["series_confirmation_decision"] = "dont_know"
            metadata["series_confirmation_decided_at"] = datetime.utcnow().isoformat()
            if decision_item.note:
                metadata["series_confirmation_note"] = str(decision_item.note)
            updated += 1
            results.append(
                {
                    "book_id": int(book.id),
                    "status": "kept_pending",
                    "decision": "dont_know",
                }
            )

        book.import_raw_row = metadata
        db.add(book)

    db.commit()

    for series_id in sorted(affected_series_ids):
        recalculate_intelligence(db, int(series_id))

    return {
        "processed": len(payload.decisions),
        "updated": updated,
        "recalculated_series_ids": sorted(affected_series_ids),
        "results": results,
    }
