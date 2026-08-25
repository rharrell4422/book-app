"""Phase 1 agentic discovery, eleventh implementation block (second half):
minimal server-side data stubs for a future internal admin UI. These
return plain JSON-shaped dicts only -- no HTML rendering (that's
`services/agentic_report_generator.py`'s job) and no writes anywhere.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(settled architecture, not re-litigated here): both functions below are
read-only and purely diagnostic, callable only from internal/admin/test
contexts (see `routers/admin_agentic.py`) -- neither is linked from any
user-facing UI.

Honesty note on "agentic evaluations" in `list_agentic_series`'s spec'd
docstring, and on `get_agentic_history` entirely: there is no persisted,
queryable store of past agentic evaluation runs anywhere in this
codebase. `services/discovery_telemetry.py`'s `record_agentic_*` helpers
(called from `services/agentic_evaluation_harness.py`) are a log-only
fallback -- see that module's own docstring -- not a table or file this
module could read back. Rather than fabricating history that doesn't
exist, both functions below are honest about that gap: `list_agentic_
series` reports series that have a `SeriesSkeleton` row (the one real,
queryable signal that a series has ever had discovery data to evaluate
against) and `get_agentic_history` returns an empty `history` list with
an explanatory `note`, exactly like `services/agentic_ttl_validator.py`'s
existing precedent for `probes_ttl`. Wiring in a real structured
evaluation-history store is explicit future work, not this ticket.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from database import SessionLocal
from models import SeriesSkeleton

logger = logging.getLogger(__name__)

_NO_HISTORY_STORE_NOTE = (
    "no persisted store of past agentic evaluation runs exists yet (Phase 1) -- see "
    "services/discovery_telemetry.py's record_agentic_* helpers, which are a log-only "
    "fallback, not a queryable table or file. This is an honest empty result, not a "
    "fabricated one, until a real evaluation-history store lands."
)


def list_agentic_series(db_session: Session | None = None) -> dict:
    """Read-only list of every `series_id` with a `SeriesSkeleton` row --
    the durable "has discovery data to evaluate" signal (see module
    docstring for why this doubles as the "or agentic evaluations" half
    of the spec today: there is no separate evaluation-history store to
    query yet).

    Returns:
        {"series_ids": [...], "count": int, "timestamp": iso8601}

    No writes. `db_session`, when provided, is reused as-is and never
    closed by this function; when omitted, a session is opened
    internally and always closed before returning.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        series_ids = [
            row[0]
            for row in db.query(SeriesSkeleton.series_id).order_by(SeriesSkeleton.series_id).all()
        ]
    except Exception:
        logger.exception("list_agentic_series failed; returning empty list")
        series_ids = []
    finally:
        if not caller_supplied_db:
            db.close()

    return {
        "series_ids": series_ids,
        "count": len(series_ids),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def get_agentic_history(series_id: int, db_session: Session | None = None) -> dict:
    """Read-only historical agentic evaluation log for `series_id`.

    Returns:
        {
          "series_id": series_id,
          "history": [{"timestamp": ..., "report_exists": bool, "html_exists": bool}, ...],
          "note": "...",
          "timestamp": iso8601,
        }

    `history` is always `[]` today -- see module docstring for why (no
    persisted evaluation-history store exists yet to read from). `note`
    explains that gap explicitly rather than the caller having to guess
    whether an empty list means "no history" or "this is broken".
    `db_session` is accepted for interface parity with `list_agentic_
    series`/the rest of Phase 1's harness functions and future-proofing
    once a real store exists, but is unused today since there is nothing
    per-series to query yet. No writes.
    """
    return {
        "series_id": series_id,
        "history": [],
        "note": _NO_HISTORY_STORE_NOTE,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
