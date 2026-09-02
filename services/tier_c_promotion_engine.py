"""Step 9 (Tier C Promotion Policy Engine): automates the per-series
Tier C promotion decision that Step 8 left entirely manual -- see
`models.TierCPromotionState`'s docstring, which explicitly deferred this
("no automated policy that ever creates/updates a row here... explicit
Phase 8b future work").

Scope, deliberately narrow (Step 9 design chat's settled resolution, "BOTH
with a phased implementation"):
  - Unit of promotion: `series_id` + Tier C state (`shadow_only` /
    `shadow_advisory` / `live`) -- the *existing* axis from Step 8, now
    evaluated automatically instead of only by manual DB write.
  - NOT in scope: moving providers/models between Tier A/B/C, or any
    global provider/model quality ranking. Tiers A/B/C remain fixed task
    roles (see `llm_client.TIER_MODEL_MAP`); `TierCPromotionState.
    tier_c_provider`/`tier_c_model_id` stay unused placeholders. A future
    global provider/model scorecard (Step 10/11 territory, once multi-
    provider parallel shadow data actually exists) would be a separate
    table/module that *feeds into* this one as an input signal -- it does
    not replace it.
  - Distinct from `agentic/promotion_evaluator.py`'s `AgenticPromotionDecision`
    (live-vs-agentic routing per turn) -- same word, unrelated subsystem.

Evaluation cadence: called once per Check Now job, from
`services/series_check_engine.run_series_check_job_full`, right after
that job's round loop finishes -- piggybacking on an existing trigger
point (the same pattern `tier_c_shadow_store.check_tier_c_shadow_budget`
already uses) rather than introducing scheduler/cron infrastructure this
codebase has never had. Always runs, regardless of whether this job's
Tier C shadow budget was exhausted (`budget_blocked=True` only means *no
new shadow call happened this job*; historical `shadow_llm_calls` rows
from earlier jobs are still valid evidence -- budget gates the expensive
call, never the evaluation itself).

Metrics input, deliberately limited to what's actually persisted today:
  - `shadow_llm_calls` agreement/disagreement over the last `settings.
    TIER_C_PROMOTION_MIN_CALLS` scored calls (`tier_c_shadow_store.
    get_recent_scored_shadow_calls`).
  - Hallucination detection and cross-provider disagreement are explicit
    future inputs (Step 10/11) -- no detector for either exists yet, so
    neither is referenced here. Latency (`duration_ms`) and override
    tracking (`tier_c_state_at_call`) are persisted (Step 9 schema
    additions) but not yet consulted by the transition rules below --
    extension points, not gaps: a future revision can fold them into
    `_decide_transition` without a schema change.

Every evaluation writes a `models.TierCPromotionHistory` row, even a HOLD
or a manual-override skip -- see that model's docstring. Uses its own
independent DB session, fail-soft (logs, never raises), same convention
as `tier_c_shadow_store.persist_tier_c_shadow_call` -- a policy-evaluation
bug must never be able to sink the Check Now job it rides along with.
"""

from __future__ import annotations

import logging
from datetime import datetime

import models
import settings
from database import SessionLocal
from services.tier_c_shadow_store import (
    get_recent_scored_shadow_calls,
    get_tier_c_promotion_state,
    upsert_tier_c_promotion_state,
)

logger = logging.getLogger(__name__)

# Fixed vocabulary for TierCPromotionHistory.evaluation_reason -- see that
# model's docstring. Not free text: callers/tests/future dashboards can
# rely on this exact set (optionally suffixed with ",budget_blocked").
REASON_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
REASON_STABLE = "stable"
REASON_AGREEMENT_HIGH = "shadow_agreement_high"
REASON_DISAGREEMENT_HIGH = "disagreement_high"
REASON_MANUAL_OVERRIDE_ACTIVE = "manual_override_active"

_VALID_STATES = ("shadow_only", "shadow_advisory", "live")


def _decide_transition(
    *,
    current_state: str,
    shadow_calls_considered: int,
    agreement_rate: float | None,
    disagreement_rate: float | None,
    min_calls: int,
    agreement_threshold: float,
    disagreement_threshold: float,
) -> tuple[str, str]:
    """Single, testable, pure decision function (Step 9 spec's explicit
    requirement) -- no DB access, no side effects. Returns
    `(new_state, evaluation_reason)`; `new_state == current_state` means
    HOLD.

    One state transition at a time, in either direction -- a series never
    jumps `shadow_only` straight to `live` in one evaluation, and never
    demotes straight from `live` to `shadow_only` either, matching how
    Step 8's own state machine was already structured (three ordered
    states, not an arbitrary set).

    An unrecognized `current_state` (defensive only -- every real caller
    goes through `get_tier_c_promotion_state`, which always returns one of
    `_VALID_STATES`) HOLDs rather than guessing.
    """
    if current_state not in _VALID_STATES:
        return current_state, "unknown_state"

    if shadow_calls_considered < min_calls:
        return current_state, REASON_INSUFFICIENT_EVIDENCE

    if current_state == "shadow_only":
        if agreement_rate is not None and agreement_rate >= agreement_threshold:
            return "shadow_advisory", REASON_AGREEMENT_HIGH
        return current_state, REASON_STABLE

    if current_state == "shadow_advisory":
        # Checked before promotion: a window that's simultaneously "high
        # agreement" and "high disagreement" can't happen (they're
        # complementary rates over the same calls), but disagreement is
        # the safety-relevant direction -- always resolve it first.
        if disagreement_rate is not None and disagreement_rate >= disagreement_threshold:
            return "shadow_only", REASON_DISAGREEMENT_HIGH
        if agreement_rate is not None and agreement_rate >= agreement_threshold:
            return "live", REASON_AGREEMENT_HIGH
        return current_state, REASON_STABLE

    # current_state == "live"
    if disagreement_rate is not None and disagreement_rate >= disagreement_threshold:
        return "shadow_advisory", REASON_DISAGREEMENT_HIGH
    return current_state, REASON_STABLE


def evaluate_tier_c_promotion(series_id: int, *, budget_blocked: bool = False) -> None:
    """Runs one Step 9 policy evaluation for `series_id` and persists the
    result. Called once per Check Now job from `services.series_check_
    engine.run_series_check_job_full`, unconditionally (including jobs
    where the cheap pre-check short-circuited the full discovery loop
    entirely, and jobs where `check_tier_c_shadow_budget` blocked new
    Tier C shadow calls) -- this function only ever reads historical
    `shadow_llm_calls` rows, so there is always something valid to
    evaluate even when nothing new happened this job.

    Fail-soft: any exception (DB error, etc.) is caught, logged, and
    swallowed -- same convention as every other Step 8/9 shadow-table
    write in this codebase (see `tier_c_shadow_store.persist_tier_c_
    shadow_call`'s docstring). A policy-evaluation bug must never be able
    to fail the Check Now job it rides along with.

    Opens and closes its own independent session -- never the caller's
    shared discovery session, for the same reason `persist_tier_c_shadow_
    call` does: this write must not be able to put a shared session into
    a pending-rollback state and sink an unrelated, already-committed
    discovery transaction.
    """
    db = SessionLocal()
    try:
        state = get_tier_c_promotion_state(db, series_id)
        current_state = state["tier_c_state"]
        now = datetime.utcnow()

        if state["is_manual_override"] and settings.TIER_C_MANUAL_OVERRIDE_HONORED:
            _write_history(
                db,
                series_id=series_id,
                evaluated_at=now,
                previous_state=current_state,
                new_state=current_state,
                evaluation_reason=_with_budget_suffix(REASON_MANUAL_OVERRIDE_ACTIVE, budget_blocked),
                shadow_calls_considered=0,
                agreement_rate=None,
                manual_override_active=True,
                metrics_snapshot={"is_manual_override": True},
            )
            # last_evaluated_at still advances -- a frozen series was
            # still checked, it just wasn't allowed to act on what it saw.
            upsert_tier_c_promotion_state(db, series_id, tier_c_state=current_state, last_evaluated_at=now)
            db.commit()
            return

        recent_calls = get_recent_scored_shadow_calls(db, series_id, settings.TIER_C_PROMOTION_MIN_CALLS)
        shadow_calls_considered = len(recent_calls)
        agreement_count = sum(1 for c in recent_calls if c.belongs_to_series_agreement is True)
        disagreement_count = sum(1 for c in recent_calls if c.belongs_to_series_agreement is False)
        agreement_rate = (
            agreement_count / shadow_calls_considered if shadow_calls_considered > 0 else None
        )
        disagreement_rate = (
            disagreement_count / shadow_calls_considered if shadow_calls_considered > 0 else None
        )

        new_state, reason = _decide_transition(
            current_state=current_state,
            shadow_calls_considered=shadow_calls_considered,
            agreement_rate=agreement_rate,
            disagreement_rate=disagreement_rate,
            min_calls=settings.TIER_C_PROMOTION_MIN_CALLS,
            agreement_threshold=settings.TIER_C_PROMOTION_AGREEMENT_THRESHOLD,
            disagreement_threshold=settings.TIER_C_DEMOTION_DISAGREEMENT_THRESHOLD,
        )

        _write_history(
            db,
            series_id=series_id,
            evaluated_at=now,
            previous_state=current_state,
            new_state=new_state,
            evaluation_reason=_with_budget_suffix(reason, budget_blocked),
            shadow_calls_considered=shadow_calls_considered,
            agreement_rate=agreement_rate,
            manual_override_active=False,
            metrics_snapshot={
                "shadow_calls_considered": shadow_calls_considered,
                "agreement_count": agreement_count,
                "disagreement_count": disagreement_count,
                "agreement_rate": agreement_rate,
                "disagreement_rate": disagreement_rate,
                "min_calls_required": settings.TIER_C_PROMOTION_MIN_CALLS,
                "agreement_threshold": settings.TIER_C_PROMOTION_AGREEMENT_THRESHOLD,
                "disagreement_threshold": settings.TIER_C_DEMOTION_DISAGREEMENT_THRESHOLD,
                "budget_blocked": budget_blocked,
            },
        )
        upsert_tier_c_promotion_state(db, series_id, tier_c_state=new_state, last_evaluated_at=now)
        db.commit()

        if new_state != current_state:
            logger.info(
                "TierCPromotionPolicyEngine: series_id=%s %s -> %s (%s)",
                series_id,
                current_state,
                new_state,
                reason,
            )
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception("evaluate_tier_c_promotion: failed for series_id=%s", series_id)
    finally:
        db.close()


def _with_budget_suffix(reason: str, budget_blocked: bool) -> str:
    return f"{reason},budget_blocked" if budget_blocked else reason


def _write_history(
    db,
    *,
    series_id: int,
    evaluated_at: datetime,
    previous_state: str,
    new_state: str,
    evaluation_reason: str,
    shadow_calls_considered: int,
    agreement_rate: float | None,
    manual_override_active: bool,
    metrics_snapshot: dict,
) -> None:
    db.add(
        models.TierCPromotionHistory(
            series_id=series_id,
            evaluated_at=evaluated_at,
            previous_state=previous_state,
            new_state=new_state,
            evaluation_reason=evaluation_reason,
            shadow_calls_considered=shadow_calls_considered,
            agreement_rate=agreement_rate,
            manual_override_active=manual_override_active,
            metrics_snapshot=metrics_snapshot,
        )
    )
