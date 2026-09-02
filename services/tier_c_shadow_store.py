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
    `last_evaluated_at`). No row for `series_id` -- the common case in
    Phase 8a, since no automated promotion policy exists yet to ever
    create one -- returns the `"shadow_only"` default with every other
    field `None`, matching pre-Step-8 behavior exactly (see `models.
    TierCPromotionState`'s docstring).

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
        }
    return {
        "tier_c_state": row.tier_c_state or DEFAULT_TIER_C_STATE,
        "tier_c_provider": row.tier_c_provider,
        "tier_c_model_id": row.tier_c_model_id,
        "last_evaluated_at": row.last_evaluated_at,
    }


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
