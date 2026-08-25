"""Phase 1 agentic discovery, sixth implementation block: a batch
orchestrator running `services/agentic_replay_runner.py`'s per-series
replay-and-compare across many series at once, purely for offline
evaluation/analysis.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(settled architecture, not re-litigated here): same shadow-mode-only
contract as every earlier Phase 1 block -- **not** wired into any
user-facing route or scheduled job, callable only from internal/admin/
test contexts until a future ticket explicitly promotes it.

`run_batch_agentic_evaluations` never writes anything: it opens (or
reuses a caller-supplied) DB session, only ever reads through it, and
every series in the batch is evaluated via `services.agentic_replay_
runner.replay_and_compare` -- which itself composes only read-only/pure
pieces (`agents.agentic_series_agent.run_agentic_turn`, `services.
agentic_evaluation_harness._observe_live_pipeline`/`_compare_live_vs_
agentic`). One implementation of "replay and compare one series" is
reused per series here, not duplicated.

One series failing (e.g. a bad `series_id`, an unexpected exception deep
in a shared helper) must never abort the rest of the batch -- this is a
diagnostic/evaluation tool, not a critical path, so a per-series failure
is caught, logged, and recorded as an error entry in that series' result
slot rather than propagating.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from database import SessionLocal
from services.agentic_replay_runner import replay_and_compare
from services.discovery_telemetry import record_agentic_batch

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_batch_agentic_evaluations(series_ids: list[int], *, db_session: Session | None = None) -> dict:
    """Runs `services.agentic_replay_runner.replay_and_compare` for every
    id in `series_ids`, in shadow mode, and aggregates the results.

    Returns:
        {
          "count": len(series_ids),
          "results": [<one replay_and_compare report per series_id, in order>, ...],
          "batch_timestamp": iso8601,
        }

    Does NOT modify any persistent state -- see module docstring. Logs
    the finished batch via `services.discovery_telemetry.record_agentic_
    batch` (fail-soft -- a logging failure here never affects the
    returned batch report).

    `db_session`, when provided, is reused as-is (and shared across every
    series in the batch, each still queried/filtered by its own
    `series_id`) and never closed by this function; when omitted, one
    session is opened internally for the whole batch and always closed
    before returning.
    """
    series_ids = list(series_ids or [])
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        results = []
        for series_id in series_ids:
            try:
                report = replay_and_compare(series_id, db_session=db)
            except Exception:
                logger.exception(
                    "run_batch_agentic_evaluations: replay_and_compare failed for series_id=%s; "
                    "recording an error entry and continuing with the rest of the batch",
                    series_id,
                )
                report = {
                    "series_id": series_id,
                    "live_observation": {},
                    "agentic_trace": {},
                    "comparison": {"by_book_number": {}},
                    "timestamp": _now_iso(),
                    "error": "replay_and_compare_failed",
                }
            results.append(report)

        batch_report = {
            "count": len(series_ids),
            "results": results,
            "batch_timestamp": _now_iso(),
        }
    finally:
        if not caller_supplied_db:
            db.close()

    try:
        record_agentic_batch(series_ids, batch_report)
    except Exception:
        logger.exception("run_batch_agentic_evaluations: record_agentic_batch failed for series_ids=%s", series_ids)

    return batch_report
