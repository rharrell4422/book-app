"""Step 11 Phase 1 (Provider/Model Scorecard & Tier C Confidence Signals):
computed-on-read aggregation of `shadow_llm_calls`, grouped by
`(shadow_provider, shadow_model_id)` -- deliberately across ALL series,
not scoped to one `series_id` the way every function in `services/tier_c_
shadow_store.py` is. That's the reason this lives in its own module
rather than as a fourth function bolted onto that one: this is a global,
cross-series signal (which provider/model is behaving well *overall*),
not a per-series read.

Same "Option C" precedent `services.tier_c_shadow_store.
get_recent_candidate_aggregates` (Step 10 Phase 5) already established:
computed fresh on every call from existing rows, no persisted table, no
migration, no refresh-cadence question, no staleness. Nothing here
changes any routing/promotion behavior -- purely observational, read-only.

No new infra: no embeddings, no time-series store, no scheduler. Reuses
`services.tier_c_shadow_store._build_candidate_aggregate` for
`conflict_flag` rather than re-implementing that rule a second time --
Step 10 Phase 5 is the single source of truth for what "conflict" means
for a Tier C candidate, and this module must never be able to drift from
it.

Step 11 Phase 3 addition: `check_parse_failure_spikes`, an alert-only
(never auto-demoting) detector built directly on top of
`get_provider_model_scorecard`'s `parse_failure_rate` -- piggybacks on
`services.tier_c_promotion_engine.evaluate_tier_c_promotion`'s existing
per-Check-Now-job cadence rather than introducing a second trigger point
(see `settings.PARSE_FAILURE_WINDOW_SIZE`'s docstring for why).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

import models
import settings
from services.tier_c_shadow_store import _build_candidate_aggregate

logger = logging.getLogger(__name__)

DEFAULT_SCORECARD_WINDOW = 100


def get_provider_model_scorecard(db: Session, *, window: int = DEFAULT_SCORECARD_WINDOW) -> list[dict]:
    """Returns one dict per distinct `(shadow_provider, shadow_model_id)`
    pair that has ever appeared in `shadow_llm_calls`, computed over that
    pair's most recent `window` rows (most-recent by `created_at`,
    default 100 -- deliberately the same default the Step 11 spec's
    parse-failure detector uses, so "the scorecard" and "the alert"
    describe the same lookback window unless a caller asks for
    something else).

    Each dict:
      - "provider", "model_id"
      - "call_count": rows actually considered (<= window)
      - "gate_agreement_rate": fraction of calls with `belongs_to_series_
        agreement == True`, out of calls where that field is not NULL --
        an unparseable response was never a vote either way, so it's
        excluded from this denominator (it shows up in
        `parse_failure_rate` instead). `None` when every row in the
        window is unparseable.
      - "parse_failure_rate": fraction of calls with `parsed_ok == False`,
        out of all `call_count` rows in the window. `None` when
        `call_count` is 0 (never actually reached in practice --
        `get_provider_model_scorecard` only ever emits a dict for a pair
        that has at least one row, by construction of the initial
        distinct-pairs query -- kept as a defensive `None` anyway so this
        function stays correct as a standalone unit).
      - "conflict_involvement_rate": fraction of this provider/model's
        window rows whose `candidate_request_id` belongs to a candidate
        with `conflict_flag == True` (via `_build_candidate_aggregate`,
        computed over ALL of that candidate's rows across every
        provider -- not just the rows in this provider's own window --
        so a fan-out candidate's conflict status reads identically no
        matter which provider's scorecard entry you look at it from).
        Rows with no `candidate_request_id` (only possible for
        historical pre-Step-10-Phase-4 rows -- see `get_recent_candidate_
        aggregates`'s docstring) are excluded from both the numerator and
        denominator, same convention as that function. `None` when no
        row in the window has a `candidate_request_id`.
      - "avg_latency_ms": mean `duration_ms` across rows where it's not
        NULL. `None` when every row in the window predates Step 9
        (nullable `duration_ms`).
      - "avg_cost_usd": mean `total_cost_usd` across all `call_count`
        rows (never NULL -- defaults to 0.0 at write time, so this never
        needs its own NULL-filtering).

    Ordering of the returned list is arbitrary (grouped by provider/
    model, not by recency) -- callers that want a stable order should
    sort the result themselves.
    """
    pairs = (
        db.query(models.ShadowLLMCall.shadow_provider, models.ShadowLLMCall.shadow_model_id).distinct().all()
    )

    scorecard: list[dict] = []
    for provider, model_id in pairs:
        rows = (
            db.query(models.ShadowLLMCall)
            .filter(models.ShadowLLMCall.shadow_provider == provider)
            .filter(models.ShadowLLMCall.shadow_model_id == model_id)
            .order_by(models.ShadowLLMCall.created_at.desc())
            .limit(window)
            .all()
        )
        scorecard.append(_build_provider_model_metrics(db, provider, model_id, rows))

    return scorecard


def _build_provider_model_metrics(
    db: Session, provider: str, model_id: str, rows: list[models.ShadowLLMCall]
) -> dict:
    call_count = len(rows)

    voters = [row for row in rows if row.belongs_to_series_agreement is not None]
    gate_agreement_rate = (
        sum(1 for row in voters if row.belongs_to_series_agreement is True) / len(voters) if voters else None
    )

    parse_failure_rate = sum(1 for row in rows if not row.parsed_ok) / call_count if call_count else None

    latencies = [row.duration_ms for row in rows if row.duration_ms is not None]
    avg_latency_ms = sum(latencies) / len(latencies) if latencies else None

    avg_cost_usd = sum(row.total_cost_usd for row in rows) / call_count if call_count else None

    return {
        "provider": provider,
        "model_id": model_id,
        "call_count": call_count,
        "gate_agreement_rate": gate_agreement_rate,
        "parse_failure_rate": parse_failure_rate,
        "conflict_involvement_rate": _conflict_involvement_rate(db, rows),
        "avg_latency_ms": avg_latency_ms,
        "avg_cost_usd": avg_cost_usd,
    }


def _conflict_involvement_rate(db: Session, rows: list[models.ShadowLLMCall]) -> float | None:
    """The two-level join the Step 11 spec calls for: this provider/
    model's own window `rows` only tell us which candidates it
    participated in -- whether any of those candidates actually had a
    cross-provider conflict requires looking at EVERY provider's rows for
    that `candidate_request_id`, not just this provider's own row for it
    (a single row can never conflict with itself). Delegates the actual
    conflict rule to `_build_candidate_aggregate` (Step 10 Phase 5) so
    "conflict" means exactly one thing across this whole codebase.
    """
    candidate_ids = sorted({row.candidate_request_id for row in rows if row.candidate_request_id is not None})
    if not candidate_ids:
        return None

    all_rows_for_candidates = (
        db.query(models.ShadowLLMCall).filter(models.ShadowLLMCall.candidate_request_id.in_(candidate_ids)).all()
    )
    rows_by_candidate: dict[str, list[models.ShadowLLMCall]] = {cid: [] for cid in candidate_ids}
    for row in all_rows_for_candidates:
        rows_by_candidate[row.candidate_request_id].append(row)

    conflict_by_candidate = {
        cid: _build_candidate_aggregate(cid, rows_by_candidate[cid])["conflict_flag"] for cid in candidate_ids
    }

    considered = [row for row in rows if row.candidate_request_id is not None]
    conflicted = sum(1 for row in considered if conflict_by_candidate[row.candidate_request_id])
    return conflicted / len(considered)


@dataclass(frozen=True)
class ProviderModelMetricsAlert:
    """One provider/model pair whose recent `parse_failure_rate` crossed
    `settings.PARSE_FAILURE_ALERT_THRESHOLD` -- alert-only (Step 11
    scope): nothing reads this to change routing/promotion behavior, it
    exists purely so a human can notice a provider/model consistently
    ignoring the JSON-mode/prompt instructions. Same shape as one entry
    of `get_provider_model_scorecard`'s output, just narrowed to the
    fields relevant to this specific alert.
    """

    provider: str
    model_id: str
    window_size: int
    call_count: int
    parse_failure_rate: float
    threshold: float


def check_parse_failure_spikes(
    db: Session,
    *,
    window: int | None = None,
    threshold: float | None = None,
) -> list[ProviderModelMetricsAlert]:
    """Step 11 Phase 3: computes `get_provider_model_scorecard(db,
    window=window)` and logs (`logger.warning`) one `ProviderModelMetricsAlert`
    for every provider/model pair whose `parse_failure_rate` exceeds
    `threshold` -- strictly greater than, so a pair sitting exactly at
    the threshold does not alert (matches `_decide_transition`'s own ">="
    -vs- plain "<"/">" conventions being deliberate per-rule choices, not
    an accident). No side effects beyond logging: never touches
    `TierCPromotionState`/`TierCPromotionHistory`, never raises on a
    healthy scorecard.

    `window`/`threshold` default to `settings.PARSE_FAILURE_WINDOW_SIZE`/
    `settings.PARSE_FAILURE_ALERT_THRESHOLD` when omitted -- the real call
    site (`services.tier_c_promotion_engine.evaluate_tier_c_promotion`)
    doesn't need to pass them explicitly, but tests/future ad-hoc callers
    can still override either independently of the global config.

    Returns the list of alerts raised (empty when nothing crossed the
    threshold) purely so tests/callers can assert on it directly without
    needing to capture log output -- the logged warning is still the
    primary, intended side effect for Step 11 (no dashboard/UI consumes
    this return value yet).
    """
    window = window if window is not None else settings.PARSE_FAILURE_WINDOW_SIZE
    threshold = threshold if threshold is not None else settings.PARSE_FAILURE_ALERT_THRESHOLD

    alerts: list[ProviderModelMetricsAlert] = []
    for entry in get_provider_model_scorecard(db, window=window):
        rate = entry["parse_failure_rate"]
        if rate is None or rate <= threshold:
            continue
        alert = ProviderModelMetricsAlert(
            provider=entry["provider"],
            model_id=entry["model_id"],
            window_size=window,
            call_count=entry["call_count"],
            parse_failure_rate=rate,
            threshold=threshold,
        )
        alerts.append(alert)
        logger.warning(
            "ProviderModelMetricsAlert: provider=%s model_id=%s parse_failure_rate=%.3f "
            "exceeds threshold=%.3f over last %d calls (window=%d)",
            alert.provider,
            alert.model_id,
            alert.parse_failure_rate,
            alert.threshold,
            alert.call_count,
            alert.window_size,
        )
    return alerts
