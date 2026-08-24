import os
import tempfile
import traceback

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from importer.importer import preview_import, reset_profile_data, run_import
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
    database -- powers the onboarding wizard's "preview parsed rows" step.
    Safe to call repeatedly for the same file."""
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
