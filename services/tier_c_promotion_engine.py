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

Step 11 Phase 3 addition: this function's own per-Check-Now-job cadence
is also where `services.provider_model_scorecard.
check_parse_failure_spikes` (a GLOBAL, cross-series check, unlike
everything else in this function) piggybacks -- same "no new scheduler"
rationale, just reused a second time for an unrelated global signal
rather than introducing its own trigger point. Alert-only (logs, never
demotes); wrapped in its own fail-soft try/except so a scorecard-query
bug can never prevent THIS series' own evaluation below from running.

Metrics input:
  - Step 9 (single-provider): `shadow_llm_calls` agreement/disagreement
    over the last `settings.TIER_C_PROMOTION_MIN_CALLS` scored calls
    (`tier_c_shadow_store.get_recent_scored_shadow_calls`).
  - Step 10 Phase 5 (multi-provider): replaced the above with `tier_c_
    shadow_store.get_recent_candidate_aggregates` -- the same `agreement_
    rate`/`disagreement_rate` semantics (Tier C vs. the deterministic
    gate), now computed over the last `TIER_C_PROMOTION_MIN_CALLS`
    per-candidate gate-comparison votes instead of raw rows, so a sampled
    3-way fan-out candidate contributes exactly one vote to these rates,
    not three. Numerically identical to Step 9's behavior for every
    single-provider candidate (today's only kind -- see `settings.
    TIER_C_PARALLEL_SHADOW_SAMPLE_RATE`'s docstring), so this is a no-op
    until Phase 6 raises that sample rate above `0.0`.
  - Cross-provider consensus/conflict (Step 10 Phase 5, per the spec's
    "additive signal" instruction): recorded in `TierCPromotionHistory.
    metrics_snapshot` alongside the existing agreement/disagreement
    fields, but never consulted by `_decide_transition` -- promotion/
    demotion still turns on Tier-C-vs-gate agreement alone. A genuine
    hallucination detector is still explicit future work (Step 11+); this
    only surfaces "the providers didn't agree with each other," not "one
    of them is wrong."
  - Step 11 Phase 2 addition: `cross_provider_avg_consensus_score` above
    is a BLENDED average across every candidate in the window, including
    single-provider candidates, which trivially score `consensus_score=
    1.0` (see `_build_candidate_aggregate`'s docstring -- "one voice can't
    conflict with itself"). At `settings.TIER_C_PARALLEL_SHADOW_SAMPLE_
    RATE`'s current low value, most candidates in any window are single-
    provider, so that blended average sits close to 1.0 almost
    regardless of how badly the rare multi-provider candidates disagreed
    -- diluted, not wrong. `cross_provider_avg_consensus_score_multi_
    provider_only`/`cross_provider_multi_provider_candidate_count` below
    are the same underlying `consensus_score` values, filtered to
    candidates where `voter_count >= 2` (i.e. at least two providers
    actually produced a comparable, parseable decision for that
    candidate -- see `_build_candidate_aggregate`'s `voter_count` field),
    added alongside the blended field rather than replacing it: the
    blended field keeps its own "how noisy is Tier C output overall"
    meaning, while this filtered one is what a future consensus-based
    demotion signal (Step 11 Phase 4) should read instead, so it isn't
    diluted into near-uselessness by the single-provider majority. Still
    purely additive/observational here too -- not consulted by `_decide_
    transition`.
  - Step 11 Phase 4 addition: `_decide_transition` NOW consults one more
    signal, gated behind `settings.TIER_C_CONSENSUS_SIGNAL_ENABLED`
    (default `False`): `_has_sustained_low_consensus` (below) reads a
    series' last `settings.TIER_C_CONSENSUS_SIGNAL_LOOKBACK` promotion-
    history entries that had multi-provider evidence and, if ALL of them
    show `cross_provider_avg_consensus_score_multi_provider_only` below
    `settings.TIER_C_CONSENSUS_LOW_THRESHOLD`, sets a `consensus_hold`
    flag for this evaluation.

    The finalized Step 11 spec offered two possible shapes for this
    signal -- "a soft demotion vote OR a hold flag... never demote
    solely on consensus." This implementation picked the HOLD shape,
    not the demotion-vote shape: `consensus_hold=True` can only ever
    *block a promotion* (`shadow_only -> shadow_advisory` or
    `shadow_advisory -> live`) that would otherwise have happened on
    gate-agreement grounds alone -- it can NEVER cause a series to move
    backward. This trivially and completely satisfies "never demote
    solely on consensus" (it demotes never, solely on consensus or
    otherwise), whereas a "soft demotion vote" shape would have required
    deciding how much weight consensus gets when combined with gate-
    disagreement evidence -- an under-specified design question this
    phase deliberately avoids by not needing an answer to it at all.
    Gate-disagreement (`TIER_C_DEMOTION_DISAGREEMENT_THRESHOLD`) remains
    the ONLY path that can move a series backward, completely unchanged
    by this phase.
  - Latency (`duration_ms`) and override tracking (`tier_c_state_at_
    call`) are persisted (Step 9 schema additions) but not yet consulted
    by the transition rules below -- extension points, not gaps: a future
    revision can fold them into `_decide_transition` without a schema
    change.

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
from services.provider_model_scorecard import check_parse_failure_spikes
from services.tier_c_shadow_store import (
    get_recent_candidate_aggregates,
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
# Step 11 Phase 4: a promotion that would otherwise have happened on
# gate-agreement grounds alone was held back due to sustained low
# multi-provider consensus -- see this module's docstring. Only ever
# produced when settings.TIER_C_CONSENSUS_SIGNAL_ENABLED is True.
REASON_CONSENSUS_HOLD = "consensus_hold"

_VALID_STATES = ("shadow_only", "shadow_advisory", "live")
# Defensive cap on how many TierCPromotionHistory rows _has_sustained_
# low_consensus will ever scan for one series -- generous enough that it
# should never actually bind in practice (multi-provider-having entries
# are the rare case at today's sample rate, so most real series will
# exhaust this before finding settings.TIER_C_CONSENSUS_SIGNAL_LOOKBACK
# qualifying entries), purely a defensive bound against an unbounded
# query on a series with an extremely long evaluation history.
_CONSENSUS_HOLD_HISTORY_SCAN_LIMIT = 500


def _decide_transition(
    *,
    current_state: str,
    shadow_calls_considered: int,
    agreement_rate: float | None,
    disagreement_rate: float | None,
    min_calls: int,
    agreement_threshold: float,
    disagreement_threshold: float,
    consensus_hold: bool = False,
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

    `consensus_hold` (Step 11 Phase 4, default `False` -- every pre-Phase-
    4 caller/test keeps its exact prior behavior unchanged): when `True`,
    blocks ONLY a promotion this call would otherwise have made
    (`shadow_only -> shadow_advisory` or `shadow_advisory -> live`),
    reporting `REASON_CONSENSUS_HOLD` instead. Checked strictly AFTER the
    disagreement/demotion check in each branch, so it can never mask or
    interfere with a demotion -- see this module's docstring for why the
    hold shape (vs. a "soft demotion vote") was chosen specifically so
    this parameter could never move `current_state` backward, only
    prevent it from moving forward.
    """
    if current_state not in _VALID_STATES:
        return current_state, "unknown_state"

    if shadow_calls_considered < min_calls:
        return current_state, REASON_INSUFFICIENT_EVIDENCE

    if current_state == "shadow_only":
        if agreement_rate is not None and agreement_rate >= agreement_threshold:
            if consensus_hold:
                return current_state, REASON_CONSENSUS_HOLD
            return "shadow_advisory", REASON_AGREEMENT_HIGH
        return current_state, REASON_STABLE

    if current_state == "shadow_advisory":
        # Checked before promotion: a window that's simultaneously "high
        # agreement" and "high disagreement" can't happen (they're
        # complementary rates over the same calls), but disagreement is
        # the safety-relevant direction -- always resolve it first, and
        # unconditionally on consensus_hold (a hold only ever blocks the
        # promotion branch below, never this one).
        if disagreement_rate is not None and disagreement_rate >= disagreement_threshold:
            return "shadow_only", REASON_DISAGREEMENT_HIGH
        if agreement_rate is not None and agreement_rate >= agreement_threshold:
            if consensus_hold:
                return current_state, REASON_CONSENSUS_HOLD
            return "live", REASON_AGREEMENT_HIGH
        return current_state, REASON_STABLE

    # current_state == "live" -- no promotion branch exists here for
    # consensus_hold to ever block; this state can only stay or demote.
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
        _check_parse_failure_spikes_fail_soft(db)

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

        # Step 10 Phase 5: one entry per Tier C *candidate* (a sampled
        # multi-provider fan-out collapses to one gate-comparison vote
        # here, per `_build_candidate_aggregate`'s tie-break rule) rather
        # than one entry per raw `shadow_llm_calls` row -- see this
        # module's docstring for why that's a no-op for every single-
        # provider candidate that exists in production today.
        recent_aggregates = get_recent_candidate_aggregates(db, series_id, settings.TIER_C_PROMOTION_MIN_CALLS)
        shadow_calls_considered = len(recent_aggregates)
        agreement_count = sum(1 for agg in recent_aggregates if agg["gate_agreement"] is True)
        disagreement_count = sum(1 for agg in recent_aggregates if agg["gate_agreement"] is False)
        agreement_rate = (
            agreement_count / shadow_calls_considered if shadow_calls_considered > 0 else None
        )
        disagreement_rate = (
            disagreement_count / shadow_calls_considered if shadow_calls_considered > 0 else None
        )

        # Cross-provider consensus/conflict: additive-only (spec's own
        # wording) -- computed here purely for metrics_snapshot
        # visibility. conflict_candidate_count/avg_consensus_score stay
        # None-safe when no candidate in this window had more than one
        # provider respond (today's only case), matching every other "no
        # evidence yet" field in this snapshot. (The multi-provider-only
        # variant below IS consulted by _decide_transition, but only via
        # _has_sustained_low_consensus's own separate, cross-evaluation
        # read below -- never these single-evaluation values directly.)
        conflict_candidate_count = sum(1 for agg in recent_aggregates if agg["conflict_flag"])
        consensus_scores = [
            agg["consensus_score"] for agg in recent_aggregates if agg["consensus_score"] is not None
        ]
        avg_cross_provider_consensus_score = (
            sum(consensus_scores) / len(consensus_scores) if consensus_scores else None
        )

        # Step 11 Phase 2: same consensus_score values, filtered to
        # candidates where >=2 providers actually responded with a
        # parseable decision (voter_count >= 2) -- see this module's
        # docstring for why the blended average above is too diluted by
        # single-provider candidates to serve as a real signal.
        multi_provider_consensus_scores = [
            agg["consensus_score"]
            for agg in recent_aggregates
            if agg["consensus_score"] is not None and agg["voter_count"] >= 2
        ]
        multi_provider_candidate_count = len(multi_provider_consensus_scores)
        avg_cross_provider_consensus_score_multi_provider_only = (
            sum(multi_provider_consensus_scores) / multi_provider_candidate_count
            if multi_provider_candidate_count > 0
            else None
        )

        # Step 11 Phase 4: reads THIS series' past promotion-history rows
        # (i.e. excludes the evaluation being computed right now, which
        # hasn't been written yet) -- see _has_sustained_low_consensus's
        # own docstring. False (a no-op) whenever settings.TIER_C_
        # CONSENSUS_SIGNAL_ENABLED is False, its own default.
        consensus_hold = _has_sustained_low_consensus(db, series_id)

        new_state, reason = _decide_transition(
            current_state=current_state,
            shadow_calls_considered=shadow_calls_considered,
            agreement_rate=agreement_rate,
            disagreement_rate=disagreement_rate,
            min_calls=settings.TIER_C_PROMOTION_MIN_CALLS,
            agreement_threshold=settings.TIER_C_PROMOTION_AGREEMENT_THRESHOLD,
            disagreement_threshold=settings.TIER_C_DEMOTION_DISAGREEMENT_THRESHOLD,
            consensus_hold=consensus_hold,
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
                # Step 10 Phase 5: additive cross-provider signal only --
                # see this module's docstring. Not consulted by
                # _decide_transition above.
                "cross_provider_conflict_candidate_count": conflict_candidate_count,
                "cross_provider_avg_consensus_score": avg_cross_provider_consensus_score,
                # Step 11 Phase 2: multi-provider-only variant of the two
                # fields above -- see this module's docstring for why this
                # is a separate field, not a replacement. Still additive/
                # observational only; not consulted by _decide_transition.
                "cross_provider_multi_provider_candidate_count": multi_provider_candidate_count,
                "cross_provider_avg_consensus_score_multi_provider_only": (
                    avg_cross_provider_consensus_score_multi_provider_only
                ),
                # Step 11 Phase 4: whether THIS evaluation's promotion (if
                # any) was blocked by _has_sustained_low_consensus -- see
                # this module's docstring. consensus_signal_enabled is
                # recorded alongside it so a historical row is
                # self-describing even after the setting is later
                # flipped/tuned.
                "consensus_signal_enabled": settings.TIER_C_CONSENSUS_SIGNAL_ENABLED,
                "consensus_hold_active": consensus_hold,
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


def _check_parse_failure_spikes_fail_soft(db) -> None:
    """Step 11 Phase 3: runs once per call to `evaluate_tier_c_promotion`
    (i.e. once per Check Now job, per this module's docstring), before
    that call's series-specific evaluation below -- global, so it's not
    scoped to `series_id` at all. Deliberately wrapped in its OWN try/
    except, separate from the outer function's: a scorecard-query bug
    here must never be able to prevent THIS series' own promotion
    evaluation from running, any more than that evaluation itself is
    allowed to sink the Check Now job it rides along with. Uses the
    caller's own `db` session (read-only queries, no writes -- unlike
    every other DB access in this module, this never needs its own
    independent session).
    """
    try:
        check_parse_failure_spikes(db)
    except Exception:
        logger.exception("evaluate_tier_c_promotion: parse-failure spike check failed")


def _has_sustained_low_consensus(db, series_id: int) -> bool:
    """Step 11 Phase 4: `False` immediately (no query at all) when
    `settings.TIER_C_CONSENSUS_SIGNAL_ENABLED` is `False` -- its own
    default, so this is a complete no-op for every deployment that
    hasn't explicitly opted in.

    When enabled: scans `series_id`'s `TierCPromotionHistory` rows, most
    recent first (capped at `_CONSENSUS_HOLD_HISTORY_SCAN_LIMIT` as a
    defensive bound -- see that constant's own comment), collecting only
    the entries whose `metrics_snapshot["cross_provider_multi_provider_
    candidate_count"]` is truthy (i.e. actually had >=1 candidate with
    >=2 providers responding that evaluation -- see Step 11 Phase 2).
    Entries with zero multi-provider evidence are skipped over entirely,
    not counted as "not low" -- they simply aren't evidence either way
    (today's common case at the current `TIER_C_PARALLEL_SHADOW_SAMPLE_
    RATE`).

    Returns `True` only when AT LEAST `settings.TIER_C_CONSENSUS_SIGNAL_
    LOOKBACK` such qualifying entries exist AND EVERY one of the most
    recent `LOOKBACK` of them has `cross_provider_avg_consensus_score_
    multi_provider_only` below `settings.TIER_C_CONSENSUS_LOW_THRESHOLD`
    -- "sustained," not a single bad evaluation window. Fewer than
    `LOOKBACK` qualifying entries in the scanned history (including zero)
    returns `False` -- insufficient multi-provider evidence to call
    anything "sustained" yet, same "innocent until enough evidence
    exists" posture as `TIER_C_PROMOTION_MIN_CALLS` itself.

    Read-only; uses the caller's own session (this is called from inside
    `evaluate_tier_c_promotion`'s existing try/except, so a query error
    here propagates up to that function's own fail-soft handling rather
    than needing its own -- unlike `_check_parse_failure_spikes_fail_
    soft`, which wraps a call site that runs BEFORE anything else in this
    function has had a chance to do real work).
    """
    if not settings.TIER_C_CONSENSUS_SIGNAL_ENABLED:
        return False

    lookback = settings.TIER_C_CONSENSUS_SIGNAL_LOOKBACK
    if lookback <= 0:
        return False

    rows = (
        db.query(models.TierCPromotionHistory)
        .filter(models.TierCPromotionHistory.series_id == series_id)
        .order_by(models.TierCPromotionHistory.id.desc())
        .limit(_CONSENSUS_HOLD_HISTORY_SCAN_LIMIT)
        .all()
    )

    qualifying_consensus_scores: list[float | None] = []
    for row in rows:
        snapshot = row.metrics_snapshot or {}
        if not snapshot.get("cross_provider_multi_provider_candidate_count"):
            continue
        qualifying_consensus_scores.append(snapshot.get("cross_provider_avg_consensus_score_multi_provider_only"))
        if len(qualifying_consensus_scores) >= lookback:
            break

    if len(qualifying_consensus_scores) < lookback:
        return False

    threshold = settings.TIER_C_CONSENSUS_LOW_THRESHOLD
    return all(score is not None and score < threshold for score in qualifying_consensus_scores)


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
