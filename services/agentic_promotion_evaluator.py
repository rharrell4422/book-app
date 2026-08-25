"""Phase 3, candidate promotion (`discovery_agentic_phase1_plan.md`/
`discovery_agentic_phase1_evaluation.md`'s settled architecture, not
re-litigated here): the first module that decides, per book, whether an
agentic confidence/gate decision is *eligible* to be used in place of the
live one -- `evaluate_promotion` -- and persists that decision to the new
`AgenticPromotionDecision` shadow table (`models.py`) via
`store_promotion_decision`/`get_promotion_history`.

This module never calls a live provider, `confidence_engine.py`, or the
live `evaluate_belongs_to_series_gate` gate logic itself -- it only
compares the *outputs* of those, already computed by its caller
(`agents/series_agent.py`'s live routing path, gated by
`settings.AGENTIC_ROUTING_ENABLED`). `evaluate_promotion` is a pure
function: no DB, no I/O, no side effects -- same inputs always produce
the same outcome. `store_promotion_decision`/`get_promotion_history`
mirror `services/agentic_confidence_gate_store.py`'s shape and
guarantees exactly (same fail-soft write, same pure/fail-soft read; see
that module's own docstring, not repeated here) -- the only new table
here is `agentic_promotion_decisions`, which additionally records the
`promotion_outcome` string itself alongside the same live/agentic
confidence+gate pair those other tables already capture.

Contract for `evaluate_promotion`'s four dict arguments (deliberately
loose/duck-typed, matching what `agents/series_agent.py`'s live routing
block and `agents/agentic_series_agent.run_agentic_turn`'s
`confidence_traces`/`gate_traces` entries already produce):

- `live_conf`/`agentic_conf`: dicts that may contain any of
  `"overall"` (or, for `live_conf`, `"confidence"` as a synonym --
  `_observe_live_pipeline`'s `confidence_snapshot` entries use that key,
  since a `SeriesSkeleton` entry only ever stores one grade, not a
  per-dimension breakdown), `"provider_confidence"`, `"title_confidence"`,
  `"number_confidence"`, `"series_alignment_confidence"`. Only keys
  present in *both* dicts are compared -- a dimension only one side
  reports is never treated as a violation or an improvement on its own.
- `live_gate`/`agentic_gate`: dicts exposing a `"belongs_to_series"`
  boolean, either at the top level (`_observe_live_pipeline`'s
  `gate_snapshot` shape) or nested under `"gate_output"`
  (`agentic_series_agent`'s `gate_traces` entry shape) -- both are
  checked.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from database import SessionLocal
from models import AgenticPromotionDecision

logger = logging.getLogger(__name__)

# "unverified" is confidence_engine.py's title_confidence-only grade
# (see that module's docstring) -- never emitted for the other three
# dimensions or for "overall". It folds into an overall "medium" ceiling
# there, but as a per-dimension grade in isolation it reflects "no
# corroborating skeleton entry to check against", not a confirmed
# positive signal -- ranked here just above "zero"/"low" and below
# "medium", a deliberately conservative placement for this comparison
# only (confidence_engine.py's own _LEVEL_RANK has no such entry at all,
# since it is only ever compared there after already folding into
# "medium"/"zero").
_GRADE_RANK = {"zero": 0, "low": 1, "unverified": 1, "medium": 2, "high": 3}

# Compared in this order for determinism only (dict iteration order in
# the returned "reasons" list, etc.) -- comparison itself is unordered
# (every shared key is checked).
_CONFIDENCE_DIMENSIONS = (
    "overall",
    "provider_confidence",
    "title_confidence",
    "number_confidence",
    "series_alignment_confidence",
)


def _grade_rank(value) -> int:
    """Unknown/`None`/malformed grades rank below every real grade
    (`-1`), so a side missing a value can never "win" a comparison
    against a side that has one -- see `evaluate_promotion`'s docstring
    for why that matters for the deterministic-invariant check.
    """
    return _GRADE_RANK.get(value, -1)


def _confidence_dims(conf: dict | None) -> dict:
    """Canonicalizes one side's confidence dict onto `_CONFIDENCE_
    DIMENSIONS`' keys, treating `live_conf`'s `"confidence"` key as a
    synonym for `"overall"` (see module docstring) and dropping anything
    not present at all (as opposed to synthesizing a `None`/`"zero"` --
    an absent key means "no opinion", not "worst possible grade").
    """
    if not isinstance(conf, dict):
        return {}
    dims: dict = {}
    overall = conf.get("overall", conf.get("confidence"))
    if overall is not None:
        dims["overall"] = overall
    for key in _CONFIDENCE_DIMENSIONS[1:]:
        if conf.get(key) is not None:
            dims[key] = conf[key]
    return dims


def _belongs_to_series(gate: dict | None) -> bool | None:
    """Reads `belongs_to_series` from either shape gate dicts show up in
    across this codebase (see module docstring) -- `None` when neither
    shape provides one, meaning "no opinion" rather than "false".
    """
    if not isinstance(gate, dict):
        return None
    if "belongs_to_series" in gate:
        return gate.get("belongs_to_series")
    gate_output = gate.get("gate_output")
    if isinstance(gate_output, dict):
        return gate_output.get("belongs_to_series")
    return None


def evaluate_promotion(live_conf: dict, agentic_conf: dict, live_gate: dict, agentic_gate: dict) -> str:
    """Pure decision function -- no DB, no I/O, deterministic. Returns
    one of `"use_live"`, `"use_agentic"`, `"reject_agentic"` per the
    Phase 1 plan's promotion rules (see module docstring for the input
    contract):

    1. Deterministic-invariant check: an agentic decision that provides
       no usable confidence grade and no usable gate opinion at all --
       while the live side has at least one of those -- gives nothing to
       promote over live and is rejected outright, rather than silently
       falling through to "use_live" as if nothing were wrong.
    2. Required-fields check: for every confidence dimension *both*
       sides report, the agentic grade must rank `>=` the live grade
       (via `_GRADE_RANK`). Any single dimension ranking lower is a
       violation -- this is also where "must not reduce provider
       agreement" lives, since `"provider_confidence"` is just one of
       the dimensions this same check already covers; it is not a
       separate rule with different math.
    3. Gate-contradiction check: if both sides express an opinion on
       `belongs_to_series` and they disagree, that is a violation --
       "the agentic gate must not contradict the live gate".
    4. Any violation from 1-3 -> `"reject_agentic"`.
    5. Otherwise, if the agentic side ranks strictly higher on at least
       one shared confidence dimension -> `"use_agentic"` (gate
       consistency is already guaranteed at this point, since step 3
       would have rejected any contradiction).
    6. Otherwise (no violation, no improvement) -> `"use_live"`.
    """
    live_dims = _confidence_dims(live_conf)
    agentic_dims = _confidence_dims(agentic_conf)
    live_belongs = _belongs_to_series(live_gate)
    agentic_belongs = _belongs_to_series(agentic_gate)

    # Rule 1: a degenerate agentic decision (no confidence opinion AND no
    # gate opinion) can't be evaluated for improvement at all, but the
    # live side actually has something -- reject rather than silently
    # defer to "use_live" as if this were just "no improvement found".
    agentic_has_no_opinion = not agentic_dims and agentic_belongs is None
    live_has_an_opinion = bool(live_dims) or live_belongs is not None
    if agentic_has_no_opinion and live_has_an_opinion:
        return "reject_agentic"

    # Rule 2 (+ "must not reduce provider agreement", same math): every
    # shared confidence dimension must rank agentic >= live.
    shared_confidence_keys = set(live_dims) & set(agentic_dims)
    required_fields_violation = any(
        _grade_rank(agentic_dims[key]) < _grade_rank(live_dims[key]) for key in shared_confidence_keys
    )

    # Rule 3: an explicit disagreement on series membership.
    gate_contradiction = (
        live_belongs is not None and agentic_belongs is not None and bool(live_belongs) != bool(agentic_belongs)
    )

    if required_fields_violation or gate_contradiction:
        return "reject_agentic"

    improves_confidence = any(
        _grade_rank(agentic_dims[key]) > _grade_rank(live_dims[key]) for key in shared_confidence_keys
    )
    if improves_confidence:
        return "use_agentic"

    return "use_live"


def store_promotion_decision(
    series_id: int,
    book_number,
    live_conf: dict,
    agentic_conf: dict,
    live_gate: dict,
    agentic_gate: dict,
    outcome: str,
    *,
    db_session: Session | None = None,
) -> None:
    """Inserts one row into `agentic_promotion_decisions`, recording the
    full live/agentic confidence+gate pair alongside `outcome` (whatever
    `evaluate_promotion` returned -- this function does not itself call
    or validate against that function, so a caller-supplied outcome
    string is stored verbatim).

    Pure write to the shadow table only. Never touches live `skeleton_
    json`/`probes_json`, never calls `confidence_engine.py` or the live
    gate logic. Fail-soft: any exception is caught, logged via
    `services.discovery_telemetry.record_agentic_promotion_error`, and
    swallowed -- this function never raises.

    `db_session`, when provided, is reused as-is; this function commits
    on it (the one write this makes) but never closes it -- same
    convention as every other Phase 1/2/3 store that accepts an optional
    session. When omitted, a session is opened internally, committed,
    and always closed before returning.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        row = AgenticPromotionDecision(
            series_id=series_id,
            book_number=book_number,
            timestamp=datetime.utcnow(),
            live_confidence=live_conf if live_conf is not None else {},
            agentic_confidence=agentic_conf if agentic_conf is not None else {},
            live_gate=live_gate if live_gate is not None else {},
            agentic_gate=agentic_gate if agentic_gate is not None else {},
            promotion_outcome=str(outcome or "use_live"),
        )
        db.add(row)
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            from services.discovery_telemetry import record_agentic_promotion_error

            record_agentic_promotion_error(series_id, str(exc))
        except Exception:
            logger.exception(
                "store_promotion_decision: failed to store decision for series_id=%s "
                "and failed to log the error too; continuing",
                series_id,
            )
    finally:
        if not caller_supplied_db:
            db.close()


def get_promotion_history(series_id: int, *, db_session: Session | None = None) -> list[dict]:
    """Read-only: returns every stored `agentic_promotion_decisions` row
    for `series_id`, oldest first, as plain dicts:

        [{"id": ..., "series_id": ..., "book_number": ..., "timestamp": iso8601 | None,
          "live_confidence": ..., "agentic_confidence": ...,
          "live_gate": ..., "agentic_gate": ..., "promotion_outcome": ...}, ...]

    Pure read. Fails soft -- returns `[]` (logging the failure) rather
    than raising. `db_session`, when provided, is reused as-is and never
    closed by this function; when omitted, a session is opened internally
    and always closed before returning.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        rows = (
            db.query(AgenticPromotionDecision)
            .filter(AgenticPromotionDecision.series_id == series_id)
            .order_by(AgenticPromotionDecision.id.asc())
            .all()
        )
        return [
            {
                "id": row.id,
                "series_id": row.series_id,
                "book_number": row.book_number,
                "timestamp": (row.timestamp.replace(tzinfo=timezone.utc).isoformat() if row.timestamp else None),
                "live_confidence": row.live_confidence,
                "agentic_confidence": row.agentic_confidence,
                "live_gate": row.live_gate,
                "agentic_gate": row.agentic_gate,
                "promotion_outcome": row.promotion_outcome,
            }
            for row in rows
        ]
    except Exception:
        logger.exception("get_promotion_history failed for series_id=%s; returning empty list", series_id)
        return []
    finally:
        if not caller_supplied_db:
            db.close()
