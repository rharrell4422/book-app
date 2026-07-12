import os
import tempfile
import threading

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from database import DATABASE_PATH, engine
from routers.deps import require_owner

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_owner)])


@router.get("/export_db")
def export_db():
    """Download the current SQLite database file -- use this for backups,
    or to migrate data between environments."""
    if not os.path.exists(DATABASE_PATH):
        raise HTTPException(status_code=404, detail="No database file found")
    return FileResponse(
        DATABASE_PATH,
        filename="books_backup.db",
        media_type="application/octet-stream",
    )


@router.post("/import_db")
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
