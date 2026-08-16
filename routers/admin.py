import os
import tempfile
import threading

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from database import DATABASE_PATH, engine
from intelligence import find_ghost_profile_books, purge_orphaned_books, repair_ghost_profile_books
from routers.deps import get_db, require_owner, require_owner_or_backup_token

# No router-level dependency here (unlike other routers) because export_db
# needs a different, more restricted auth path than the rest of /admin --
# see require_owner_or_backup_token. Every route below sets its own
# dependency explicitly instead.
router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/purge_orphaned_books", dependencies=[Depends(require_owner)])
def purge_orphaned_books_endpoint(db: Session = Depends(get_db)):
    """Delete books left pointing at a series that was deleted without
    cascading. Safe to run any time; it's a no-op if there are none."""
    return purge_orphaned_books(db)


@router.get("/ghost_profile_books", dependencies=[Depends(require_owner)])
def list_ghost_profile_books_endpoint(db: Session = Depends(get_db)):
    """Read-only report of books whose profile_id doesn't match the
    profile_id of the series they're linked to (see
    intelligence.find_ghost_profile_books). Safe to call any time; makes no
    changes."""
    entries = find_ghost_profile_books(db)
    return {"count": len(entries), "entries": entries}


@router.post("/repair_ghost_profile_books", dependencies=[Depends(require_owner)])
def repair_ghost_profile_books_endpoint(db: Session = Depends(get_db)):
    """Reassign every book found by /admin/ghost_profile_books to its
    series' own profile_id, directly on the live database -- no
    export/import round trip needed. Safe to run any time; it's a no-op if
    there are none."""
    return repair_ghost_profile_books(db)


@router.get("/export_db", dependencies=[Depends(require_owner_or_backup_token)])
def export_db():
    """Download the current SQLite database file -- use this for backups,
    or to migrate data between environments. Accepts either normal owner
    auth or an X-Backup-Token header (see BACKUP_TOKEN), so an unattended
    scheduled job can pull backups without holding full owner credentials."""
    if not os.path.exists(DATABASE_PATH):
        raise HTTPException(status_code=404, detail="No database file found")
    return FileResponse(
        DATABASE_PATH,
        filename="books_backup.db",
        media_type="application/octet-stream",
    )


@router.post("/import_db", dependencies=[Depends(require_owner)])
async def import_db(file: UploadFile = File(...)):
    """Replace the current database with an uploaded SQLite file.

    Writes to a temp file first and atomically swaps it into place, so an
    interrupted upload can't leave a half-written database behind. Since
    swapping the file out from under a live SQLAlchemy connection pool is
    inherently risky, this deliberately forces the process to exit right
    after a successful swap so every connection reopens clean against the
    new file on restart.
    """
    target_dir = os.path.dirname(os.path.abspath(DATABASE_PATH)) or "."
    os.makedirs(target_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".upload-", suffix=".db")
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            while chunk := await file.read(1024 * 1024):
                tmp_file.write(chunk)

        engine.dispose()
        os.replace(tmp_path, DATABASE_PATH)

        for suffix in ("-wal", "-shm"):
            sidecar = f"{DATABASE_PATH}{suffix}"
            if os.path.exists(sidecar):
                os.remove(sidecar)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    size_bytes = os.path.getsize(DATABASE_PATH)

    # Give the response time to actually reach the client before the
    # process exits. The host platform (Railway/Render/etc.) is expected
    # to restart a long-running web process that exits, same as a crash.
    threading.Timer(1.0, lambda: os._exit(1)).start()

    return {
        "status": "ok",
        "size_bytes": size_bytes,
        "message": "Database replaced. Server is restarting now to reload it cleanly -- give it about 10-20 seconds.",
    }
