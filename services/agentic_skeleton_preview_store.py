"""Phase 2 dual-write shadow storage (`discovery_agentic_phase1_plan.md`/
`discovery_agentic_phase1_evaluation.md`'s settled architecture, not
re-litigated here): the only read/write path for the `AgenticSkeletonPreview`
shadow table (`models.AgenticSkeletonPreview`, `agentic_skeleton_previews`).

This is a genuinely new write surface -- unlike every prior Phase 1/2
`record_agentic_*` telemetry helper (log-only, `services/discovery_
telemetry.py`), `store_agentic_skeleton_preview` performs a real database
INSERT + commit. What makes it safe alongside the live pipeline's own
writes is scope, not absence of writes: it only ever inserts into this new,
dedicated shadow table, and never reads from, writes to, or otherwise
touches `SeriesSkeleton.skeleton_json` (the live table) or `probes_json`
(not implemented anywhere yet -- see `services/agentic_ttl_validator.py`).
No confidence/gate logic is invoked here either. Both functions are
fail-soft: `store_agentic_skeleton_preview` never raises back to its caller
(`agents/series_agent.py`'s dry-run block, which is itself already wrapped
in its own fail-soft try/except -- this is defense in depth, not the only
guard), logging any failure via `services.discovery_telemetry.
record_agentic_skeleton_preview_error` instead.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from database import SessionLocal
from models import AgenticSkeletonPreview

logger = logging.getLogger(__name__)


def store_agentic_skeleton_preview(series_id: int, preview: dict, *, db_session: Session | None = None) -> None:
    """Inserts one row into the `agentic_skeleton_previews` shadow table
    for `series_id`, capturing `preview` (typically the dry-run turn's
    `agentic_trace["skeleton_merge_previews"]`, whatever shape that is --
    this function stores it as-is, opaquely, in `preview_json`).

    Pure write to the shadow table only. Never touches live `skeleton_
    json`/`probes_json`, never runs confidence/gate logic. Fail-soft: any
    exception (bad session, serialization failure, etc.) is caught,
    logged via `services.discovery_telemetry.record_agentic_skeleton_
    preview_error`, and swallowed -- this function never raises.

    `db_session`, when provided, is reused as-is; this function commits
    on it (the one write this module makes) but never closes it -- same
    convention as every other Phase 1/2 harness function that accepts an
    optional session. When omitted, a session is opened internally,
    committed, and always closed before returning.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        row = AgenticSkeletonPreview(
            series_id=series_id,
            timestamp=datetime.utcnow(),
            preview_json=preview if preview is not None else {},
        )
        db.add(row)
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            from services.discovery_telemetry import record_agentic_skeleton_preview_error

            record_agentic_skeleton_preview_error(series_id, str(exc))
        except Exception:
            logger.exception(
                "store_agentic_skeleton_preview: failed to store preview for series_id=%s "
                "and failed to log the error too; continuing",
                series_id,
            )
    finally:
        if not caller_supplied_db:
            db.close()


def get_agentic_skeleton_previews(series_id: int, *, db_session: Session | None = None) -> list[dict]:
    """Read-only: returns every stored `agentic_skeleton_previews` row for
    `series_id`, oldest first, as plain dicts:

        [{"id": ..., "series_id": ..., "timestamp": iso8601 | None, "preview_json": ...}, ...]

    Pure read. Fails soft -- returns `[]` (logging the failure) rather
    than raising, matching every other Phase 1/2 read-only harness
    function's convention. `db_session`, when provided, is reused as-is
    and never closed by this function; when omitted, a session is opened
    internally and always closed before returning.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        rows = (
            db.query(AgenticSkeletonPreview)
            .filter(AgenticSkeletonPreview.series_id == series_id)
            .order_by(AgenticSkeletonPreview.id.asc())
            .all()
        )
        return [
            {
                "id": row.id,
                "series_id": row.series_id,
                "timestamp": row.timestamp.replace(tzinfo=timezone.utc).isoformat() if row.timestamp else None,
                "preview_json": row.preview_json,
            }
            for row in rows
        ]
    except Exception:
        logger.exception("get_agentic_skeleton_previews failed for series_id=%s; returning empty list", series_id)
        return []
    finally:
        if not caller_supplied_db:
            db.close()
