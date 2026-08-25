"""Phase 9 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_
evaluation.md`, not re-litigated here): the agentic observability &
telemetry layer's per-series health computation, backing `GET /admin/
agentic/health/{series_id}`.

Read-only: never calls a live provider, never calls `run_agentic_turn`,
never writes anything -- entirely derived from `agentic/
promotion_evaluator.get_latest_promotion_decisions` (this series' most
recent stored promotion decision per book_number -- see that function's
own docstring for the Phase 6 determinism guarantees this inherits),
`settings.is_agentic_activated` (this series' real current activation
state), and `services/discovery_telemetry.get_agentic_metrics` (the
process-wide, in-memory Phase 9 counters -- `safety_violations` below is
that GLOBAL counter, not a per-series count: no per-series safety-
violation store exists anywhere in this codebase, so the global count is
the closest honest signal available for inclusion in a per-series
summary; treat it as "how many violations has this process logged
overall", not "how many for this series").
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_FAIL_SOFT_HEALTH: dict = {
    "total_promotions": 0,
    "use_agentic_count": 0,
    "use_live_count": 0,
    "rejected_count": 0,
    "safety_violations": 0,
    "last_promotion_timestamp": None,
    "activation_state": False,
    "determinism_ok": False,
}


def compute_agentic_health(series_id: int, *, db_session=None) -> dict:
    """Returns a per-series agentic health summary:

        {"total_promotions": int,       # distinct book_numbers with a stored decision
         "use_agentic_count": int,
         "use_live_count": int,
         "rejected_count": int,
         "safety_violations": int,      # process-wide counter -- see module docstring
         "last_promotion_timestamp": iso8601 str | None,
         "activation_state": bool,      # settings.is_agentic_activated(series_id) right now
         "determinism_ok": bool}

    `determinism_ok` is `True` unless a stored decision's own shape is
    malformed -- not a dict at all, or a `promotion_outcome` that isn't
    one of the three valid literals (`agentic.safety.
    validate_promotion_outcome`) -- per the Phase 9 spec ("malformed
    history -> determinism_ok=False"). A perfectly healthy series with
    zero stored promotions at all is still `determinism_ok=True`: there
    is no malformed history to fail on, only an empty one.

    Fail-soft: any exception (a broken `db_session`, `settings.
    is_agentic_activated` itself raising, etc.) yields the same shape
    with every count at 0, `activation_state` False, and
    `determinism_ok` False, rather than raising.
    """
    try:
        from agentic.promotion_evaluator import get_latest_promotion_decisions
        from agentic.safety import validate_promotion_outcome
        from services.discovery_telemetry import get_agentic_metrics
        from settings import is_agentic_activated

        latest_by_book_number = get_latest_promotion_decisions(series_id, db_session=db_session)

        use_agentic_count = 0
        use_live_count = 0
        rejected_count = 0
        last_promotion_timestamp: str | None = None
        determinism_ok = True

        for entry in latest_by_book_number.values():
            if not isinstance(entry, dict):
                determinism_ok = False
                continue

            outcome = entry.get("promotion_outcome")
            if not validate_promotion_outcome(outcome):
                determinism_ok = False
            elif outcome == "use_agentic":
                use_agentic_count += 1
            elif outcome == "use_live":
                use_live_count += 1
            elif outcome == "reject_agentic":
                rejected_count += 1

            timestamp = entry.get("timestamp")
            if isinstance(timestamp, str) and (
                last_promotion_timestamp is None or timestamp > last_promotion_timestamp
            ):
                last_promotion_timestamp = timestamp

        metrics = get_agentic_metrics() or {}

        return {
            "total_promotions": len(latest_by_book_number),
            "use_agentic_count": use_agentic_count,
            "use_live_count": use_live_count,
            "rejected_count": rejected_count,
            "safety_violations": metrics.get("agentic_safety_violations", 0),
            "last_promotion_timestamp": last_promotion_timestamp,
            "activation_state": bool(is_agentic_activated(series_id)),
            "determinism_ok": determinism_ok,
        }
    except Exception:
        logger.exception("compute_agentic_health failed for series_id=%s; returning fail-soft summary", series_id)
        return dict(_FAIL_SOFT_HEALTH)
