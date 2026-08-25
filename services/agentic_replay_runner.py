"""Phase 1 agentic discovery, fifth implementation block: a replay runner
sitting on top of `agents/agentic_series_agent.py` (the shadow loop) and
`services/agentic_evaluation_harness.py` (the live-vs-agentic comparison),
so a single series' shadow turn -- with or without a live comparison --
can be re-run on demand.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(settled architecture, not re-litigated here): like the evaluation harness
before it, this module is **not** wired into any user-facing route or
scheduled job -- callable only from internal/admin/test contexts (a
management script, an admin-only endpoint, `services/agentic_batch_
orchestrator.py`, or a test) until a future ticket explicitly promotes it.

Neither function here writes anything:

- Each opens (or reuses a caller-supplied) DB session and only ever reads
  through it.
- `replay_agentic_turn` calls `agents.agentic_series_agent.run_agentic_turn`,
  itself already shadow-mode-only (see that module's docstring).
- `replay_and_compare` additionally reuses `services.agentic_evaluation_
  harness`'s existing `_observe_live_pipeline` (read-only) and
  `_compare_live_vs_agentic` (pure) rather than reimplementing either --
  one implementation of "observe the live pipeline" and "compare live vs.
  agentic", shared by the harness's own `run_agentic_evaluation_for_series`
  and this module's `replay_and_compare`.

`replay_and_compare` deliberately does NOT call `services.discovery_
telemetry.record_agentic_evaluation` itself (unlike `run_agentic_
evaluation_for_series`) -- when it's driven from `services.agentic_batch_
orchestrator.py`, that module logs once per batch via `record_agentic_
batch` instead of once per series here, to avoid double-logging the same
per-series report through two different telemetry channels.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from agents import agentic_series_agent
from database import SessionLocal
from models import Series
from services.agentic_evaluation_harness import _compare_live_vs_agentic, _observe_live_pipeline

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def replay_agentic_turn(series_id: int, *, db_session: Session | None = None) -> dict:
    """Replays one deterministic agentic turn for `series_id` -- the
    agentic trace only, no live-pipeline comparison (see `replay_and_
    compare` below for that). Read-only: loads the current skeleton/book
    rows through `agents.agentic_series_agent.run_agentic_turn` (which
    does its own reading; this function's own query is just enough to
    thread `user_id` into `context`, matching `services.agentic_
    evaluation_harness.run_agentic_evaluation_for_series`'s same context-
    building step) and never writes anything.

    Returns `{"series_id": series_id, "agentic_trace": ..., "timestamp": iso8601}`.

    `db_session`, when provided, is reused as-is and never closed by this
    function; when omitted, a session is opened internally and always
    closed before returning.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        series = db.query(Series).filter(Series.id == series_id).first()
        user_id = getattr(series, "profile_id", None) if series is not None else None

        context = {
            "series_id": series_id,
            "timestamp": _now_iso(),
            "user_id": user_id,
            "db": db,
        }
        agentic_trace = agentic_series_agent.run_agentic_turn(series_id, context)

        return {
            "series_id": series_id,
            "agentic_trace": agentic_trace,
            "timestamp": _now_iso(),
        }
    finally:
        if not caller_supplied_db:
            db.close()


def replay_and_compare(series_id: int, *, db_session: Session | None = None) -> dict:
    """Convenience wrapper: observes the live pipeline's current state
    (`services.agentic_evaluation_harness._observe_live_pipeline`), replays
    one agentic turn (`replay_agentic_turn` above), and compares the two
    (`services.agentic_evaluation_harness._compare_live_vs_agentic`) --
    the same three pieces `run_agentic_evaluation_for_series` composes,
    reused rather than reimplemented, minus that function's own telemetry
    call (see module docstring for why).

    Returns `{"series_id", "live_observation", "agentic_trace", "comparison", "timestamp"}`.

    `db_session` behaves exactly as in `replay_agentic_turn`: reused and
    never closed when supplied, opened-and-closed internally otherwise.
    The same session is passed to both the live observation and the
    agentic replay, so both sides see one another's exact starting state
    -- no risk of a concurrent write landing between the two reads.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        live_observation = _observe_live_pipeline(series_id, db)
        replay_result = replay_agentic_turn(series_id, db_session=db)
        agentic_trace = replay_result["agentic_trace"]

        comparison = _compare_live_vs_agentic(live_observation, agentic_trace)

        return {
            "series_id": series_id,
            "live_observation": live_observation,
            "agentic_trace": agentic_trace,
            "comparison": comparison,
            "timestamp": _now_iso(),
        }
    finally:
        if not caller_supplied_db:
            db.close()
