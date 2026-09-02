"""Step 8 (Tier C Shadow Scoring Persistence + Promotion Path): the
persistence/read layer for Tier C shadow calls and the Tier C promotion
state machine -- the durable counterpart to Step 7's per-run, in-memory-
only `DiscoveryTelemetry.record_tier_c_shadow_score`.

Three responsibilities, kept in one module since they're all thin reads/
writes against the same two Step 8 tables (`models.ShadowLLMCall`/
`models.TierCPromotionState`), not because they're conceptually one thing:

  - `persist_tier_c_shadow_call`: writes one `shadow_llm_calls` row.
    Stores `_score_tier_c_shadow_response`'s already-computed scoring
    output verbatim -- never recomputes it (Step 8 diff, section 1.2).
    Uses its OWN independent DB session (like `agentic/confidence_gate_
    store.py`'s dual-write functions), NOT the caller's shared
    `run_series_check` session -- a shadow-persistence failure must never
    put that shared session into a pending-rollback state and sink the
    real discovery transaction (books, candidate notifications, etc.) it
    would otherwise ride along with.

  - `get_tier_c_promotion_state`: reads the current Tier C promotion state
    for a series, defaulting to "shadow_only" when no row exists (see
    `models.TierCPromotionState`'s docstring for why absence means that
    default rather than an error). Safe to call with the caller's shared
    session -- read-only, no transaction-corruption risk.

  - `check_tier_c_shadow_budget`: Mechanism B (Step 8 diff, "Mechanism B
    Selection" revision) -- a single aggregation over persisted
    `shadow_llm_calls` cost, checked once per Check Now job (not per
    candidate) and cached by the caller for that job's duration. A soft
    cap: a job that starts under budget may finish over it by up to that
    job's own Tier C shadow spend (see the Step 8 diff's section 4.2 for
    why this is an accepted Phase 8a tradeoff, not a bug).
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

import models
import settings
from database import SessionLocal

logger = logging.getLogger(__name__)

DEFAULT_TIER_C_STATE = "shadow_only"


def persist_tier_c_shadow_call(
    *,
    series_id: int,
    run_id: str,
    tier: str = "C",
    gate_belongs_to_series: bool,
    gate_inferred_number: int | None,
    gate_confidence: str | None,
    shadow_provider: str,
    shadow_model_id: str,
    shadow_belongs_to_series: bool | None,
    shadow_inferred_number: int | None,
    shadow_confidence: str | None,
    shadow_is_alternate_title_of_known_book: bool | None,
    parsed_ok: bool,
    belongs_to_series_agreement: bool | None,
    inferred_number_agreement: bool | None,
    confidence_aligned: bool | None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_cost_usd: float = 0.0,
    duration_ms: float | None = None,
    tier_c_state_at_call: str | None = None,
    candidate_request_id: str | None = None,
    db_session: Session | None = None,
) -> None:
    """Inserts one row into `shadow_llm_calls`. Fail-soft: any exception
    (DB error, constraint violation, etc.) is caught, logged, and
    swallowed -- this function never raises back into the Tier C shadow
    call site, which must complete regardless of whether persistence
    succeeded (same convention as every other Phase 1/2/3 shadow-table
    dual-write in this codebase, e.g. `agentic/confidence_gate_store.
    store_agentic_confidence`).

    `db_session`, when provided, is reused as-is and committed on (but
    never closed) -- matches that same precedent's convention. When
    omitted (the expected call shape from `agents/series_agent.py`), a
    fresh session is opened, committed, and always closed, independent of
    whatever session the caller's own discovery transaction is using.

    `candidate_request_id` (Step 10 Phase 1, Multi-Provider Tier C):
    optional and `None` by default, matching every prior additive column
    this function has grown (`duration_ms`/`tier_c_state_at_call` for
    Step 9). No call site passes a real value yet -- see `models.
    ShadowLLMCall.candidate_request_id`'s own docstring for why minting
    one is deferred to the phase that actually needs to group multiple
    providers' rows for the same candidate.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        row = models.ShadowLLMCall(
            series_id=series_id,
            run_id=run_id,
            tier=tier,
            gate_belongs_to_series=bool(gate_belongs_to_series),
            gate_inferred_number=gate_inferred_number,
            gate_confidence=gate_confidence,
            shadow_provider=shadow_provider,
            shadow_model_id=shadow_model_id,
            shadow_belongs_to_series=shadow_belongs_to_series,
            shadow_inferred_number=shadow_inferred_number,
            shadow_confidence=shadow_confidence,
            shadow_is_alternate_title_of_known_book=shadow_is_alternate_title_of_known_book,
            parsed_ok=bool(parsed_ok),
            belongs_to_series_agreement=belongs_to_series_agreement,
            inferred_number_agreement=inferred_number_agreement,
            confidence_aligned=confidence_aligned,
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
            total_cost_usd=float(total_cost_usd or 0.0),
            duration_ms=float(duration_ms) if duration_ms is not None else None,
            tier_c_state_at_call=tier_c_state_at_call,
            candidate_request_id=candidate_request_id,
            created_at=datetime.utcnow(),
        )
        db.add(row)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        logger.exception(
            "persist_tier_c_shadow_call: failed to write shadow_llm_calls row for series_id=%s run_id=%s",
            series_id,
            run_id,
        )
    finally:
        if not caller_supplied_db:
            try:
                db.close()
            except Exception:
                pass


def get_tier_c_promotion_state(db: Session, series_id: int) -> dict:
    """Returns the current Tier C promotion state for `series_id` as a
    plain dict (`tier_c_state`/`tier_c_provider`/`tier_c_model_id`/
    `last_evaluated_at`/`is_manual_override`). No row for `series_id` --
    the common case for a series TierCPromotionPolicyEngine (Step 9) has
    never evaluated yet -- returns the `"shadow_only"` default with every
    other field `None`/`False`, matching pre-Step-8 behavior exactly (see
    `models.TierCPromotionState`'s docstring).

    Read-only; safe to call with the caller's shared discovery session.
    Fail-soft: any DB error returns the same `"shadow_only"` default
    rather than raising -- a promotion-state read must never be able to
    sink a Check Now run any more than the state simply not existing
    would.
    """
    try:
        row = db.query(models.TierCPromotionState).filter(
            models.TierCPromotionState.series_id == series_id
        ).first()
    except Exception:
        logger.exception("get_tier_c_promotion_state: failed to read state for series_id=%s", series_id)
        row = None

    if row is None:
        return {
            "tier_c_state": DEFAULT_TIER_C_STATE,
            "tier_c_provider": None,
            "tier_c_model_id": None,
            "last_evaluated_at": None,
            "is_manual_override": False,
        }
    return {
        "tier_c_state": row.tier_c_state or DEFAULT_TIER_C_STATE,
        "tier_c_provider": row.tier_c_provider,
        "tier_c_model_id": row.tier_c_model_id,
        "last_evaluated_at": row.last_evaluated_at,
        "is_manual_override": bool(row.is_manual_override),
    }


def upsert_tier_c_promotion_state(
    db: Session,
    series_id: int,
    *,
    tier_c_state: str,
    last_evaluated_at: datetime,
) -> None:
    """Step 9: writes `tier_c_state`/`last_evaluated_at` for `series_id`,
    creating the row if it doesn't exist yet (first-ever evaluation for a
    series that's been sitting on the implicit `"shadow_only"` default).
    Only called by `services.tier_c_promotion_engine.evaluate_tier_c_
    promotion`, never directly from a call site that also needs the
    caller's own discovery transaction to stay healthy -- see that
    module's docstring for why it uses its own independent session, same
    convention as `persist_tier_c_shadow_call` above.

    Leaves `tier_c_provider`/`tier_c_model_id`/`is_manual_override`
    untouched on an existing row -- this function only ever changes
    `tier_c_state` and `last_evaluated_at`, never the provider/model
    columns (still unused placeholders -- Step 10 territory) or the
    manual-override flag (an admin's own write, never the engine's).

    Does not commit -- the caller (the policy engine, writing a
    TierCPromotionHistory row in the same transaction) commits once for
    both writes, so a single evaluation's state change and its audit row
    can never be observed out of sync with each other.
    """
    row = db.query(models.TierCPromotionState).filter(
        models.TierCPromotionState.series_id == series_id
    ).first()
    if row is None:
        row = models.TierCPromotionState(
            series_id=series_id,
            tier_c_state=tier_c_state,
            is_manual_override=False,
        )
        db.add(row)
    else:
        row.tier_c_state = tier_c_state
    row.last_evaluated_at = last_evaluated_at


def get_recent_scored_shadow_calls(db: Session, series_id: int, limit: int) -> list[models.ShadowLLMCall]:
    """Step 9: the last `limit` `shadow_llm_calls` rows for `series_id`
    that actually carry a comparable agreement signal (`belongs_to_series_
    agreement IS NOT NULL` -- excludes failed/unparseable shadow calls,
    which `_score_tier_c_shadow_response` already leaves NULL rather than
    guessing), most recent first.

    Superseded as `TierCPromotionPolicyEngine`'s metrics input by Step 10
    Phase 5's `get_recent_candidate_aggregates` below (raw-row lookback
    can't distinguish "3 rows, 3 different candidates" from "3 rows, one
    multi-provider fan-out candidate" -- see that function's docstring).
    Kept here, unchanged, as a general-purpose raw-row read -- still
    covered by its own test in `tests/test_tier_c_shadow_store.py` -- for
    any future caller that genuinely wants row-level rather than
    candidate-level data.
    """
    return (
        db.query(models.ShadowLLMCall)
        .filter(models.ShadowLLMCall.series_id == series_id)
        .filter(models.ShadowLLMCall.belongs_to_series_agreement.isnot(None))
        .order_by(models.ShadowLLMCall.created_at.desc())
        .limit(limit)
        .all()
    )


def _build_candidate_aggregate(candidate_request_id: str, rows: list[models.ShadowLLMCall]) -> dict:
    """Step 10 Phase 5: collapses every `shadow_llm_calls` row sharing one
    `candidate_request_id` into a single per-candidate aggregate dict.
    `rows` is every row for that id (not pre-filtered on agreement/
    parseability) -- both metrics computed here apply their own filtering
    independently, exactly mirroring `services.tier_c_orchestrator.
    _aggregate_gate_comparison_votes`'s "unparseable responses don't get a
    vote" rule (this is the read-time counterpart of that same rule,
    applied to already-persisted rows instead of in-process response
    dicts):

      - `gate_agreement` (the "gate-comparison vote" the spec calls for):
        majority vote of `belongs_to_series_agreement` among rows where
        it's not `None`, with an exact 2-way tie resolved as disagreement
        -- `agree_count > disagree_count` already encodes this correctly
        for every N (see `_aggregate_gate_comparison_votes`'s docstring
        for why no separate tie special-case is needed). `None` only when
        every row for this id is unparseable, which the caller
        (`get_recent_candidate_aggregates`) never actually produces since
        it only selects ids that had at least one non-NULL agreement row
        -- kept as a real `None` case here anyway so this function stays
        correct as a standalone unit, not just correct given its one
        caller's current filtering.
      - `consensus_score`/`conflict_flag` (the spec's separate "cross-
        provider consensus/conflict metrics" -- an ADDITIVE signal, not
        folded into `gate_agreement`): measures whether the providers
        agreed with EACH OTHER on `shadow_belongs_to_series`, independent
        of whether that agreed-upon answer matched the gate. A candidate
        where all 3 providers unanimously said `True` has
        `consensus_score=1.0` (perfect cross-provider agreement) even if
        the gate said `False` (so `gate_agreement=False` for that same
        candidate) -- these two metrics deliberately measure different
        things: "did Tier C (in aggregate) disagree with the
        deterministic gate" vs. "were the different LLM providers
        internally consistent with each other". A single-provider
        candidate (today's only real case -- see `settings.TIER_C_
        PARALLEL_SHADOW_SAMPLE_RATE`'s docstring) trivially has
        `consensus_score=1.0`/`conflict_flag=False` (one voice can't
        conflict with itself).
    """
    voters = [row for row in rows if row.belongs_to_series_agreement is not None]
    agree_count = sum(1 for row in voters if row.belongs_to_series_agreement is True)
    disagree_count = len(voters) - agree_count
    gate_agreement = (agree_count > disagree_count) if voters else None

    decision_rows = [row for row in rows if row.shadow_belongs_to_series is not None]
    consensus_score = None
    conflict_flag = False
    if decision_rows:
        true_count = sum(1 for row in decision_rows if row.shadow_belongs_to_series is True)
        false_count = len(decision_rows) - true_count
        consensus_score = max(true_count, false_count) / len(decision_rows)
        conflict_flag = true_count > 0 and false_count > 0

    return {
        "candidate_request_id": candidate_request_id,
        "provider_count": len(rows),
        "providers": sorted({row.shadow_provider for row in rows}),
        "voter_count": len(voters),
        "gate_agreement": gate_agreement,
        "consensus_score": consensus_score,
        "conflict_flag": conflict_flag,
    }


def get_recent_candidate_aggregates(db: Session, series_id: int, limit: int) -> list[dict]:
    """Step 10 Phase 5: the per-candidate counterpart to `get_recent_
    scored_shadow_calls` above -- `TierCPromotionPolicyEngine`'s metrics
    input from this phase onward (see `services.tier_c_promotion_engine`'s
    module docstring). Two-step query, per the finalized spec:

      1. Select the most recent `limit` DISTINCT `candidate_request_id`
         values for `series_id` that have at least one row with
         `belongs_to_series_agreement IS NOT NULL` -- "most recent" means
         by that candidate's own most recent row (`MAX(created_at)` per
         group), not by any single row's timestamp. Rows with a `NULL`
         `candidate_request_id` are excluded from this selection entirely
         (see below).
      2. Fetch every `shadow_llm_calls` row for those selected ids
         (unfiltered this time -- an unparseable third provider in an
         otherwise-scoreable 3-way fan-out still counts toward `provider_
         count`/cost, just not toward the vote), then collapse each id's
         rows via `_build_candidate_aggregate`.

    Returns one dict per candidate (see `_build_candidate_aggregate`'s
    docstring for shape), most-recent-candidate-first -- the same order
    `get_recent_scored_shadow_calls` already returned raw rows in, so
    `evaluate_tier_c_promotion`'s "last N, most recent first" semantics
    carry over unchanged, just at candidate granularity instead of row
    granularity.

    `candidate_request_id IS NOT NULL` is a deliberate, permanent filter,
    not a defensive stopgap: Step 10 Phase 4 mints a real
    `candidate_request_id` for every `shadow_llm_calls` row it ever
    writes -- single-provider included -- specifically so this query
    could stay this simple (see `tier_c_orchestrator.run_tier_c_shadow_
    call`'s docstring). The only rows that can ever lack one are historical
    rows written between Phase 3's deploy and Phase 4's -- a small, aging-
    out window that naturally falls out of "most recent `limit`" as real
    evaluations accumulate, not a case this function needs to special-case
    for.
    """
    recent_candidate_ids = (
        db.query(models.ShadowLLMCall.candidate_request_id)
        .filter(models.ShadowLLMCall.series_id == series_id)
        .filter(models.ShadowLLMCall.candidate_request_id.isnot(None))
        .filter(models.ShadowLLMCall.belongs_to_series_agreement.isnot(None))
        .group_by(models.ShadowLLMCall.candidate_request_id)
        .order_by(func.max(models.ShadowLLMCall.created_at).desc())
        .limit(limit)
        .all()
    )
    candidate_ids = [row[0] for row in recent_candidate_ids]
    if not candidate_ids:
        return []

    all_rows = (
        db.query(models.ShadowLLMCall)
        .filter(models.ShadowLLMCall.series_id == series_id)
        .filter(models.ShadowLLMCall.candidate_request_id.in_(candidate_ids))
        .all()
    )
    rows_by_candidate: dict[str, list[models.ShadowLLMCall]] = {cid: [] for cid in candidate_ids}
    for row in all_rows:
        rows_by_candidate[row.candidate_request_id].append(row)

    return [_build_candidate_aggregate(cid, rows_by_candidate[cid]) for cid in candidate_ids]


def _window_start(now: datetime, *, monthly: bool) -> datetime:
    if monthly:
        return datetime(now.year, now.month, 1)
    return datetime(now.year, now.month, now.day)


def _cost_since(db: Session, series_id: int, window_start: datetime) -> float:
    total = (
        db.query(func.sum(models.ShadowLLMCall.total_cost_usd))
        .filter(models.ShadowLLMCall.series_id == series_id)
        .filter(models.ShadowLLMCall.created_at >= window_start)
        .scalar()
    )
    return float(total or 0.0)


def check_tier_c_shadow_budget(db: Session, series_id: int) -> bool:
    """Mechanism B (Step 8 diff, "Mechanism B Selection" revision):
    returns `True` (allowed) unless `series_id` has already met or
    exceeded a configured daily/monthly Tier C shadow cost ceiling
    (`settings.TIER_C_SHADOW_MAX_DAILY_COST_USD`/`_MONTHLY_COST_USD`).

    Called ONCE per Check Now job (`services/series_check_engine.
    run_series_check_job_full`, after precheck decides to run the full
    loop -- see that function's own sequencing comment), never per
    candidate; the caller caches this result for the job's whole
    duration. A soft cap, by design: this only looks at cost accrued
    *before* the job started, so a job that begins under budget can still
    finish over it by up to its own Tier C shadow spend (see this
    module's docstring).

    Fail-open (`True`) when both ceilings are unset (the default -- no
    budget enforcement configured) and on any error reading persisted
    cost -- a budget-check bug must never block a legitimate Tier C
    shadow call any more than the deterministic gate's own behavior is
    ever allowed to be affected by a Tier C failure elsewhere.
    """
    daily_ceiling = settings.TIER_C_SHADOW_MAX_DAILY_COST_USD
    monthly_ceiling = settings.TIER_C_SHADOW_MAX_MONTHLY_COST_USD
    if daily_ceiling is None and monthly_ceiling is None:
        return True

    try:
        now = datetime.utcnow()
        if daily_ceiling is not None:
            daily_cost = _cost_since(db, series_id, _window_start(now, monthly=False))
            if daily_cost >= daily_ceiling:
                return False
        if monthly_ceiling is not None:
            monthly_cost = _cost_since(db, series_id, _window_start(now, monthly=True))
            if monthly_cost >= monthly_ceiling:
                return False
    except Exception:
        logger.exception("check_tier_c_shadow_budget: failed to compute cost for series_id=%s; allowing", series_id)
        return True

    return True
