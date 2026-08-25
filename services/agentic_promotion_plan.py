"""Phase 2 kickoff, first implementation block: a purely diagnostic
"what would promotion require" plan for one series, built entirely on
top of `services/agentic_evaluation_harness.run_agentic_evaluation_
for_series`'s already-computed report -- same no-new-reads, no-writes
posture as Phase 1's `services/agentic_promotion_checklist.py`, which
this module deliberately reuses rather than duplicating.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(settled architecture, not re-litigated here) and the Phase 2 kickoff
instructions ("This block does NOT promote the agent. It builds the
scaffolding required for safe promotion."): `build_phase2_promotion_plan`
never flips anything live. There is no Phase 2 switch anywhere in this
codebase -- `agents/series_agent.py` still owns 100% of live routing,
confidence, gate, and skeleton-write decisions, exactly as it did before
this module existed. This function only describes, for a human, what a
future promotion would need to be true, and how close `series_id`
currently is to that bar.

Requirement flags reuse Phase 1's own check helpers from `services.
agentic_promotion_checklist` (`_confidence_stable`, `_gate_consistent`,
`_drift_within_threshold`, `_ttl_clean`, `_skeleton_preview_consistent`)
rather than reimplementing the same comparison logic a second,
potentially-drifting way -- see that module's own docstring for exactly
what each one means. Two additional signals are new here and have no
Phase 1 equivalent:

- `provider_stability_verified`: Phase 1's shadow loop (`agents/agentic_
  series_agent.py`) never makes a live provider call at all -- the
  "Provider Probe Phase" is deterministic query construction only (see
  that module's own docstring for why). There is therefore no live
  provider failure this signal could ever observe, and no persisted
  multi-run history to check "stability across the last N evaluations"
  against (see `services/agentic_admin_ui_stubs.get_agentic_history`'s
  identical gap). Honestly, this checks only *this* fresh run's
  `provider_calls`: verified when every recorded call used the expected,
  deterministic escalation order (Serper first -- see `agents/agentic_
  series_agent.py`'s own `record_reasoning_step` for
  "matches_live_escalation_order") and at least one call was actually
  made. A future ticket wiring a real (or fixture-recorded) provider
  into that phase, plus a persisted evaluation-history store, is what
  would let this check something real across multiple runs instead of
  one.
- `no_recent_errors`: whether this fresh run's `agentic_trace` recorded
  an early-abort reasoning step (`decision == "stop"`, e.g.
  "series-not-found") -- again a single-run signal, not a historical
  window, for the same reason.

`risk_assessment.risk_level` is a simple count of how many of the seven
boolean signals below (`confidence_alignment`/`gate_alignment`/
`skeleton_alignment`'s `aligned`, plus `ttl_clean`, `drift_within_
threshold`, `provider_stability_verified`, `no_recent_errors`) are
`False`: zero failing -> "low", one or two -> "medium", three or more ->
"high". `promotion_steps` is a fixed, static checklist of what promotion
would eventually require -- not something this function decides to
execute, and not conditioned on the current risk level (that's a human
decision for a later ticket, per "Manual approval required" being the
final step).

On any internal failure, fails conservatively (`risk_level: "high"`,
every alignment/requirement flag `False`) -- same "never a false
positive from missing/malformed data" posture as `generate_promotion_
readiness`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from services.agentic_evaluation_harness import run_agentic_evaluation_for_series
from services.agentic_promotion_checklist import (
    _confidence_stable,
    _drift_within_threshold,
    _gate_consistent,
    _skeleton_preview_consistent,
    _ttl_clean,
)
from services.discovery_telemetry import record_agentic_promotion_plan

logger = logging.getLogger(__name__)

# Fixed, static plan of what a future promotion would require -- not
# derived from any per-series data, and not itself executed by this
# function (see module docstring: "This block does NOT promote the
# agent"). Every step here is either shadow-only ("(shadow only)") or an
# explicit human gate ("Manual approval required"); nothing in this list
# implies a live routing change happens automatically.
PROMOTION_STEPS = [
    "Enable dry-run live routing",
    "Enable dual-write skeleton updates (shadow only)",
    "Enable agentic confidence override (shadow only)",
    "Enable agentic gate override (shadow only)",
    "Enable agentic skeleton merge preview logging",
    "Run 7-day stability window",
    "Manual approval required",
]


def _by_book_number(source: dict, field: str) -> dict:
    """Small helper: pulls one named field out of every entry in a
    `{"<book_number>": {...}}`-shaped dict, dropping the rest -- used to
    build the `requirements["*_alignment"]["live"/"agentic"/"preview"]`
    summaries below without repeating the same dict-comprehension four
    times.
    """
    source = source if isinstance(source, dict) else {}
    return {key: (entry or {}).get(field) if isinstance(entry, dict) else None for key, entry in source.items()}


def _confidence_alignment(comparison: dict) -> dict:
    comparison = comparison if isinstance(comparison, dict) else {}
    by_book_number = comparison.get("by_book_number") or {}
    return {
        "live": {key: (entry.get("live_confidence") or {}).get("confidence") for key, entry in by_book_number.items()},
        "agentic": {
            key: (entry.get("agentic_confidence") or {}).get("overall") for key, entry in by_book_number.items()
        },
        "aligned": _confidence_stable(comparison),
    }


def _gate_alignment(comparison: dict) -> dict:
    comparison = comparison if isinstance(comparison, dict) else {}
    by_book_number = comparison.get("by_book_number") or {}
    return {
        "live": {
            key: (entry.get("live_gate") or {}).get("belongs_to_series") for key, entry in by_book_number.items()
        },
        "agentic": {
            key: (entry.get("agentic_gate") or {}).get("belongs_to_series") for key, entry in by_book_number.items()
        },
        "aligned": _gate_consistent(comparison),
    }


def _skeleton_alignment(drift_report: dict) -> dict:
    drift_report = drift_report if isinstance(drift_report, dict) else {}
    by_book_number = drift_report.get("by_book_number") or {}
    return {
        "live": {key: entry.get("live") for key, entry in by_book_number.items()},
        "preview": {key: entry.get("preview") for key, entry in by_book_number.items()},
        "aligned": _skeleton_preview_consistent(drift_report),
    }


def _provider_stability_verified(agentic_trace: dict) -> bool:
    """See module docstring's "provider_stability_verified" section for
    why this is a single-run, deterministic-query-construction-only
    signal rather than a real cross-run provider-drift check.
    """
    agentic_trace = agentic_trace if isinstance(agentic_trace, dict) else {}
    provider_calls = agentic_trace.get("provider_calls")
    if not provider_calls:
        return False
    return all(isinstance(call, dict) and call.get("provider") == "serper" for call in provider_calls)


def _no_recent_errors(agentic_trace: dict) -> bool:
    """See module docstring's "no_recent_errors" section -- flags this
    fresh run's own early-abort reasoning step, not a historical window.
    """
    agentic_trace = agentic_trace if isinstance(agentic_trace, dict) else {}
    reasoning_steps = agentic_trace.get("reasoning_steps") or []
    return not any(
        isinstance(step, dict) and step.get("decision") == "stop" for step in reasoning_steps
    )


def _risk_level_and_notes(signals: dict) -> tuple:
    failed = [name for name, passed in signals.items() if not passed]
    failed_count = len(failed)
    if failed_count == 0:
        risk_level = "low"
    elif failed_count <= 2:
        risk_level = "medium"
    else:
        risk_level = "high"

    if not failed:
        notes = "All promotion requirement signals aligned; no blockers identified."
    else:
        notes = f"{failed_count} of {len(signals)} requirement signals not aligned: {', '.join(failed)}."
    return risk_level, notes


def _empty_plan(series_id: int, note: str) -> dict:
    empty_alignment = {"live": {}, "agentic": {}, "aligned": False}
    return {
        "series_id": series_id,
        "requirements": {
            "confidence_alignment": dict(empty_alignment),
            "gate_alignment": dict(empty_alignment),
            "skeleton_alignment": {"live": {}, "preview": {}, "aligned": False},
            "ttl_clean": False,
            "drift_within_threshold": False,
            "provider_stability_verified": False,
            "no_recent_errors": False,
        },
        "risk_assessment": {"risk_level": "high", "notes": note},
        "promotion_steps": list(PROMOTION_STEPS),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_phase2_promotion_plan(series_id: int, *, db_session: Session | None = None) -> dict:
    """Runs one fresh `run_agentic_evaluation_for_series` for `series_id`
    and derives a structured Phase 2 promotion plan from it: alignment
    summaries (confidence/gate/skeleton), the same `ttl_clean`/`drift_
    within_threshold` requirement flags Phase 1's `generate_promotion_
    readiness` already computes, two new single-run signals (`provider_
    stability_verified`/`no_recent_errors` -- see module docstring), a
    derived risk level, and a fixed, static list of promotion steps.

    Returns:
        {
          "series_id": series_id,
          "requirements": {
            "confidence_alignment": {"live": {...}, "agentic": {...}, "aligned": bool},
            "gate_alignment": {"live": {...}, "agentic": {...}, "aligned": bool},
            "skeleton_alignment": {"live": {...}, "preview": {...}, "aligned": bool},
            "ttl_clean": bool,
            "drift_within_threshold": bool,
            "provider_stability_verified": bool,
            "no_recent_errors": bool,
          },
          "risk_assessment": {"risk_level": "low"|"medium"|"high", "notes": str},
          "promotion_steps": [...],
          "timestamp": iso8601,
        }

    NOT a promotion mechanism -- purely diagnostic, read-only (delegates
    every read to `run_agentic_evaluation_for_series`, which has its own
    no-write guarantees; this function adds no new write surface).
    Logs the plan via `services.discovery_telemetry.record_agentic_
    promotion_plan` (fail-soft -- a logging failure never affects the
    returned plan). `db_session`, when provided, is passed straight
    through and reused as-is/never closed here, same convention as every
    other Phase 1/2 harness function.
    """
    try:
        evaluation = run_agentic_evaluation_for_series(series_id, db_session=db_session)

        agentic_trace = evaluation.get("agentic_trace") or {}
        comparison = evaluation.get("comparison") or {}
        drift_report = evaluation.get("drift_report") or {}
        ttl_report = evaluation.get("ttl_report") or {}

        confidence_alignment = _confidence_alignment(comparison)
        gate_alignment = _gate_alignment(comparison)
        skeleton_alignment = _skeleton_alignment(drift_report)
        ttl_clean = _ttl_clean(ttl_report)
        drift_within_threshold = _drift_within_threshold(drift_report)
        provider_stability_verified = _provider_stability_verified(agentic_trace)
        no_recent_errors = _no_recent_errors(agentic_trace)

        signals = {
            "confidence_alignment": confidence_alignment["aligned"],
            "gate_alignment": gate_alignment["aligned"],
            "skeleton_alignment": skeleton_alignment["aligned"],
            "ttl_clean": ttl_clean,
            "drift_within_threshold": drift_within_threshold,
            "provider_stability_verified": provider_stability_verified,
            "no_recent_errors": no_recent_errors,
        }
        risk_level, notes = _risk_level_and_notes(signals)

        plan = {
            "series_id": series_id,
            "requirements": {
                "confidence_alignment": confidence_alignment,
                "gate_alignment": gate_alignment,
                "skeleton_alignment": skeleton_alignment,
                "ttl_clean": ttl_clean,
                "drift_within_threshold": drift_within_threshold,
                "provider_stability_verified": provider_stability_verified,
                "no_recent_errors": no_recent_errors,
            },
            "risk_assessment": {"risk_level": risk_level, "notes": notes},
            "promotion_steps": list(PROMOTION_STEPS),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        logger.exception(
            "build_phase2_promotion_plan failed for series_id=%s; reporting high risk conservatively", series_id
        )
        plan = _empty_plan(series_id, "Promotion plan generation failed; treated as high risk (see server logs).")

    try:
        record_agentic_promotion_plan(series_id, plan)
    except Exception:
        logger.exception(
            "build_phase2_promotion_plan: record_agentic_promotion_plan failed for series_id=%s; continuing",
            series_id,
        )

    return plan
