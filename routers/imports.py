import os
import tempfile
import traceback
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

import crud
import models
import schemas
from importer.importer import preview_import, reset_profile_data, run_import
from intelligence import recalculate_intelligence
from routers.deps import enforce_access, get_current_profile_id, get_db, require_owner

router = APIRouter(prefix="/import", tags=["import"], dependencies=[Depends(enforce_access)])

# Onboarding uploads are small (tens of books), but this caps how much a
# single request can write to disk before we give up, so a mistaken upload
# (e.g. picking the wrong multi-megabyte file) fails fast with a clear error
# instead of quietly consuming disk/memory.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
SUPPORTED_UPLOAD_EXTENSIONS = {".csv", ".xlsx", ".xls"}


async def _write_upload_to_tempfile(file: UploadFile) -> str:
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in SUPPORTED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported file type '{ext or '(none)'}'. Upload a .csv, .xlsx, or .xls file.",
        )

    fd, tmp_path = tempfile.mkstemp(prefix=".import-upload-", suffix=ext)
    total_bytes = 0
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            while chunk := await file.read(1024 * 1024):
                total_bytes += len(chunk)
                if total_bytes > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="File is too large.")
                tmp_file.write(chunk)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    if total_bytes == 0:
        os.remove(tmp_path)
        raise HTTPException(status_code=422, detail="Uploaded file is empty.")

    return tmp_path


@router.post("")
def trigger_import(file_path: str, profile_id: str = Depends(get_current_profile_id)):
    """Run the importer against a file already present on the server's
    filesystem (e.g. uploaded via `scp`/Railway volume). `file_path` must be
    provided explicitly -- there is no default file, since that would
    silently re-import stale personal data. Imported rows are attributed to
    whichever profile is active (X-Profile-Id header) for this request --
    e.g. switch to "daughter" in the UI, then run this against her
    spreadsheet, to populate her library specifically."""
    try:
        result = run_import(file_path, profile_id=profile_id)
        return {
            "status": "success",
            "import_summary": result,
        }
    except Exception as e:
        traceback.print_exc()
        raise e


@router.post("/preview")
async def preview_upload(
    file: UploadFile = File(...),
    profile_id: str = Depends(get_current_profile_id),
):
    """Parse an uploaded spreadsheet without writing anything to the
    database -- powers the onboarding wizard's "preview parsed rows" and
    "confirm before import" steps. Safe to call repeatedly for the same
    file."""
    tmp_path = await _write_upload_to_tempfile(file)
    try:
        return preview_import(tmp_path, profile_id=profile_id)
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=422, detail=f"Could not parse file: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@router.post("/upload")
async def upload_import(
    file: UploadFile = File(...),
    profile_id: str = Depends(get_current_profile_id),
):
    """Upload a spreadsheet (CSV/XLSX/Google Sheets export) directly and
    import it for the active profile -- this is the endpoint the onboarding
    wizard calls after the user confirms the preview. Unlike `POST /import`,
    no server-side file path is needed."""
    tmp_path = await _write_upload_to_tempfile(file)
    try:
        result = run_import(tmp_path, profile_id=profile_id)
        return {
            "status": "success",
            "import_summary": result,
        }
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=422, detail=f"Import failed: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


@router.post("/reset_profile", dependencies=[Depends(require_owner)])
def reset_profile(profile_id: str = Depends(get_current_profile_id), db: Session = Depends(get_db)):
    """Delete only the active profile's books and series so onboarding can
    safely retry after a failed or unwanted upload. Intended for use while a
    profile is still empty/being set up -- this is a destructive action for
    whichever profile is active, so the frontend should only expose it from
    the onboarding flow, not from the regular library views."""
    deleted_books, deleted_series = reset_profile_data(db, profile_id)
    return {
        "status": "success",
        "profile_id": profile_id,
        "deleted_books": deleted_books,
        "deleted_series": deleted_series,
    }


@router.get("/series_confirmations")
def get_import_series_confirmation_queue(
    include_resolved: bool = False, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)
):
    books = db.query(models.Book).filter(models.Book.profile_id == profile_id).all()
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


@router.post("/series_confirmations/resolve")
def resolve_import_series_confirmations(
    payload: schemas.SeriesImportConfirmationResolveRequest,
    db: Session = Depends(get_db),
    profile_id: str = Depends(get_current_profile_id),
):
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
        book = crud.get_book(db, decision_item.book_id, profile_id)
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

            canonical_series = crud.get_series_by_name(db, candidate_series_name, profile_id)
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
