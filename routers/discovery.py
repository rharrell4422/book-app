import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from routers.deps import enforce_access, get_current_profile_id, get_db
from services.auto_discovery import (
    cooldown_remaining_seconds,
    discovery_batch_jobs,
    get_eligible_series,
    run_full_auto_discovery_job,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/discovery", tags=["discovery"], dependencies=[Depends(enforce_access)])


# Auto Discovery MVP button (spec §4). POST starts (or reports the status
# of) a profile-wide sweep; GET polls it by job_id.
@router.post("/auto_run_mvp", response_model=schemas.AutoDiscoveryRunResponse)
def start_full_auto_discovery(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    profile_id: str = Depends(get_current_profile_id),
):
    # Profile-scoped "already running" guard (§4.C.2) -- mirrors the
    # single-series Check Now guard in routers/series.py's POST /check.
    existing_job = discovery_batch_jobs.get(profile_id)
    if existing_job and existing_job.get("status") == "running":
        return schemas.AutoDiscoveryRunResponse(
            status="running",
            job_id=existing_job.get("job_id"),
            total=existing_job.get("total"),
            completed=existing_job.get("completed"),
        )

    profile = db.query(models.Profile).filter(models.Profile.id == profile_id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    remaining_seconds = cooldown_remaining_seconds(profile)
    if remaining_seconds > 0:
        remaining_hours = max(1, round(remaining_seconds / 3600))
        return schemas.AutoDiscoveryRunResponse(
            status="cooldown",
            remaining_seconds=remaining_seconds,
            message=f"Full Auto Discovery can be run again in about {remaining_hours} hour(s).",
        )

    eligible_ids = [series.id for series in get_eligible_series(db, profile_id)]
    job_id = uuid.uuid4().hex
    discovery_batch_jobs[profile_id] = {
        "job_id": job_id,
        "status": "running",
        "total": len(eligible_ids),
        "completed": 0,
        "updated_at": datetime.utcnow().isoformat(),
        "results": [],
    }
    background_tasks.add_task(run_full_auto_discovery_job, profile_id, job_id, eligible_ids)

    return schemas.AutoDiscoveryRunResponse(status="started", job_id=job_id, total=len(eligible_ids))


@router.get("/auto_run_mvp/status", response_model=schemas.AutoDiscoveryStatusResponse)
def get_full_auto_discovery_status(job_id: str, profile_id: str = Depends(get_current_profile_id)):
    job = discovery_batch_jobs.get(profile_id)
    if not job or job.get("job_id") != job_id:
        # Either this job_id was never started for this profile, or the
        # in-memory job dict was cleared by a server restart/redeploy
        # (§4.C.3) -- either way, the client's job_id no longer resolves to
        # anything meaningful.
        return schemas.AutoDiscoveryStatusResponse(status="interrupted")

    return schemas.AutoDiscoveryStatusResponse(
        status=job.get("status", "idle"),
        job_id=job.get("job_id"),
        total=job.get("total"),
        completed=job.get("completed"),
        updated_at=job.get("updated_at"),
        results=job.get("results"),
        new_books_found=job.get("new_books_found"),
        message=job.get("error"),
    )
