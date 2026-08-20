"""Auto Discovery MVP (see project design chat's finalized spec):

  §2 -- eligibility filter for which series a sweep is even allowed to touch
  §4 -- the rate-limited "Full Auto Discovery" batch button's job runner

Manual "Check Now" (routers/series.py's POST /series/{id}/check) is a full
override and never consults is_series_eligible_for_auto_discovery -- that
predicate only gates the batch sweep started from here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

import models
from database import SessionLocal
from services.identity import is_placeholder_author
from services.series_check_engine import run_series_check_job_full, series_check_jobs

logger = logging.getLogger(__name__)

AUTO_DISCOVERY_COOLDOWN = timedelta(days=7)

# One batch-job slot per profile -- mirrors series_check_jobs (one slot per
# series_id) in services/series_check_engine.py, and is intentionally a
# separate dict from it (a profile-wide sweep and a single series' Check Now
# are different units of concurrency).
discovery_batch_jobs: dict[str, dict] = {}


def is_series_eligible_for_auto_discovery(series: "models.Series") -> bool:
    """Spec §2. All of Series.is_caught_up/is_finished/has_unread_books/
    has_upcoming_books/missing_books are already the persisted, authoritative
    fields (kept current by intelligence.recalculate_intelligence) -- no
    need to recompute them here.
    """
    if bool(series.is_finished):
        return False
    if not bool(series.is_caught_up):
        return False
    if bool(series.has_unread_books) or bool(series.has_upcoming_books):
        return False
    if series.missing_books:
        return False

    author = str(series.author or "").strip()
    if not author or is_placeholder_author(author):
        return False

    # Active-book scoping via Series._active_books, not a raw Book query --
    # a soft-deleted duplicate's stale needs_reresolution flag must never
    # block an otherwise-clean series from being swept.
    if any(bool(getattr(book, "needs_reresolution", False)) for book in series._active_books):
        return False

    return True


def get_eligible_series(db: Session, profile_id: str) -> list["models.Series"]:
    all_series = (
        db.query(models.Series)
        .filter(models.Series.profile_id == profile_id)
        .order_by(models.Series.id)
        .all()
    )
    return [series for series in all_series if is_series_eligible_for_auto_discovery(series)]


def cooldown_remaining_seconds(profile: "models.Profile") -> int:
    if not profile.last_full_discovery_run_at:
        return 0
    elapsed = datetime.utcnow() - profile.last_full_discovery_run_at
    remaining = AUTO_DISCOVERY_COOLDOWN - elapsed
    return max(0, int(remaining.total_seconds()))


def _update_job(profile_id: str, **fields) -> None:
    current = discovery_batch_jobs.get(profile_id, {})
    discovery_batch_jobs[profile_id] = {**current, "updated_at": datetime.utcnow().isoformat(), **fields}


def run_full_auto_discovery_job(profile_id: str, job_id: str, series_ids: list[int]) -> None:
    """Runs in a FastAPI BackgroundTasks worker, after the POST
    /discovery/auto_run_mvp response that kicked it off has already
    returned. `series_ids` is the eligibility snapshot taken at request
    time -- deliberately not re-queried here, same as manual Check Now never
    re-validating anything about the series it's told to check.

    Sweeps series one at a time via run_series_check_job_full, which never
    raises for a provider/LLM failure (it catches its own exceptions into
    that series' own series_check_jobs entry) -- so one series' failure
    can't abort the sweep. Cooldown is stamped only if this function reaches
    its normal end -- i.e. "successful completion" means the sweep finished
    iterating every eligible series, even if individual checks errored.
    A hard crash in this function itself (not a per-series error) leaves
    the cooldown untouched, and the profile-scoped job entry's own
    "running" -> either "completed" transition simply never happens, which
    is what the "still running forever" branch of a lost-job_id poll (see
    routers/discovery.py) is there to eventually flag to the user.
    """
    db = SessionLocal()
    try:
        results: list[dict] = []
        new_books_found = 0

        for index, series_id in enumerate(series_ids):
            series = db.query(models.Series).filter(models.Series.id == series_id).first()
            series_name = series.name if series else f"Series {series_id}"

            # Defensive: don't double-run a series a user happens to have
            # clicked manual "Check Now" on at the same moment.
            existing_series_job = series_check_jobs.get(series_id)
            if existing_series_job and existing_series_job.get("status") == "running":
                results.append({"series_id": series_id, "series_name": series_name, "outcome": "skipped_already_running"})
            else:
                run_series_check_job_full(series_id)
                completion = (series_check_jobs.get(series_id) or {}).get("completion") or {}
                found = completion.get("new_books") or []
                new_books_found += len(found)
                results.append(
                    {
                        "series_id": series_id,
                        "series_name": series_name,
                        "outcome": "checked",
                        "new_books_found": len(found),
                    }
                )

            _update_job(profile_id, job_id=job_id, status="running", completed=index + 1, results=results)

        profile = db.query(models.Profile).filter(models.Profile.id == profile_id).first()
        if profile:
            profile.last_full_discovery_run_at = datetime.utcnow()
            db.commit()

        _update_job(
            profile_id,
            job_id=job_id,
            status="completed",
            completed=len(series_ids),
            results=results,
            new_books_found=new_books_found,
        )
    except Exception as exc:  # noqa: BLE001 -- report, don't crash the worker thread silently
        logger.exception("Full Auto Discovery batch job failed for profile %s", profile_id)
        _update_job(profile_id, job_id=job_id, status="completed", error=str(exc))
    finally:
        db.close()
