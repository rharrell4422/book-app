"""Phase 1 agentic discovery, eleventh implementation block: a purely
diagnostic promotion-readiness checklist for one series, built entirely
on top of `services/agentic_evaluation_harness.run_agentic_evaluation_
for_series`'s already-computed report -- no new evaluation logic, no new
reads beyond that one call, no writes anywhere.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(settled architecture, not re-litigated here): "This is NOT a promotion
mechanism -- only a diagnostic checklist." `generate_promotion_readiness`
never flips anything live -- there is no Phase 2 switch anywhere in this
codebase for it to flip. It exists so a human looking at `/admin/agentic/
promotion/{series_id}` can decide, by eye, whether a given series' shadow
trace looks trustworthy enough to eventually feed a real promotion
decision -- a decision this module deliberately does not make itself.

Each of the six checks below is derived from `run_agentic_evaluation_
for_series`'s own report sections -- `comparison`, `drift_report`, and
`ttl_report` -- rather than recomputing anything:

- `has_recent_agentic_trace`: true when this run's `agentic_trace` (see
  `agents/agentic_series_agent.py`) actually produced at least one
  confidence trace entry. There is no persisted history of past agentic
  runs to check against (see `services/agentic_admin_ui_stubs.py`'s
  `get_agentic_history` for that same gap) -- "recent" here honestly
  means "this fresh run just now", not "was run before". A series with
  no skeleton entries at all (nothing to evaluate) or that doesn't exist
  correctly reports `False`, since `run_agentic_turn` produces an empty
  `confidence_traces` list in both cases.
- `drift_within_threshold`: true when `drift_report["summary"]
  ["count_changed"] == 0` -- i.e. no book_number present on both sides
  had its title/author/metadata/confidence differ between the live
  skeleton and the shadow loop's merge preview. A zero-tolerance bar
  deliberately, not a fuzzy "mostly matches" one -- appropriate for a
  Phase 1 diagnostic gate that's meant to be conservative.
- `ttl_clean`: true when `ttl_report["discovered_ttl"]["expired"]` is
  empty -- no `discovered` entry has aged out under
  `services/skeleton_store.py`'s retention policy, which would otherwise
  force a re-check/re-discovery loop for that book_number on the next
  real merge.
- `confidence_stable`: true when, for every book_number the comparison
  saw on *both* sides, the live skeleton's own `confidence` grade
  matches the shadow loop's freshly recomputed `overall` grade
  (`comparison["by_book_number"][...]["live_confidence"]["confidence"]`
  vs. `["agentic_confidence"]["overall"]`). Vacuously true if no
  book_number appears on both sides (nothing to compare yet).
- `gate_consistent`: same shape as `confidence_stable`, comparing
  `live_gate`/`agentic_gate`'s `belongs_to_series` booleans.
- `skeleton_preview_consistent`: true when the shadow loop's merge
  preview covers the exact same set of book_numbers the live skeleton
  does -- `drift_report["missing_in_live"]`/`["missing_in_preview"]`
  both empty. Deliberately distinct from `drift_within_threshold` above:
  this checks *coverage* (same book_numbers known on both sides), that
  one checks *content* (same field values for book_numbers known on
  both sides).

`summary.ready_for_phase2` is simply `all(checks.values())` -- every
check must pass; there is no partial-credit weighting. `summary.notes`
lists which checks failed (or says everything passed), for a human
skimming the admin endpoint without wanting to parse `checks` themselves.

On any internal failure, `generate_promotion_readiness` fails conservatively
(every check `False`, `ready_for_phase2: False`, an explanatory note) --
the right default for a readiness gate is "not ready", never a false
positive built from missing/malformed data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from services.agentic_evaluation_harness import run_agentic_evaluation_for_series

logger = logging.getLogger(__name__)

_CHECK_LABELS = {
    "has_recent_agentic_trace": "a recent agentic trace with real content",
    "drift_within_threshold": "no title/author/metadata/confidence drift",
    "ttl_clean": "no expired discovered entries",
    "confidence_stable": "matching live vs. agentic confidence grades",
    "gate_consistent": "matching live vs. agentic belongs-to-series gate",
    "skeleton_preview_consistent": "matching live vs. preview book_number coverage",
}


def _has_recent_agentic_trace(agentic_trace: dict) -> bool:
    agentic_trace = agentic_trace if isinstance(agentic_trace, dict) else {}
    return bool(agentic_trace.get("confidence_traces"))


def _drift_within_threshold(drift_report: dict) -> bool:
    drift_report = drift_report if isinstance(drift_report, dict) else {}
    summary = drift_report.get("summary") or {}
    try:
        return int(summary.get("count_changed", 0)) == 0
    except (TypeError, ValueError):
        return False


def _ttl_clean(ttl_report: dict) -> bool:
    ttl_report = ttl_report if isinstance(ttl_report, dict) else {}
    discovered_ttl = ttl_report.get("discovered_ttl") or {}
    expired = discovered_ttl.get("expired")
    return not expired


def _skeleton_preview_consistent(drift_report: dict) -> bool:
    drift_report = drift_report if isinstance(drift_report, dict) else {}
    return not drift_report.get("missing_in_live") and not drift_report.get("missing_in_preview")


def _comparison_entries_present_on_both_sides(comparison: dict) -> list:
    comparison = comparison if isinstance(comparison, dict) else {}
    by_book_number = comparison.get("by_book_number") or {}
    return [
        entry
        for entry in by_book_number.values()
        if isinstance(entry, dict) and entry.get("present_in_live") and entry.get("present_in_agentic")
    ]


def _confidence_stable(comparison: dict) -> bool:
    for entry in _comparison_entries_present_on_both_sides(comparison):
        live_confidence = entry.get("live_confidence") or {}
        agentic_confidence = entry.get("agentic_confidence") or {}
        if live_confidence.get("confidence") != agentic_confidence.get("overall"):
            return False
    return True


def _gate_consistent(comparison: dict) -> bool:
    for entry in _comparison_entries_present_on_both_sides(comparison):
        live_gate = entry.get("live_gate") or {}
        agentic_gate = entry.get("agentic_gate") or {}
        if live_gate.get("belongs_to_series") != agentic_gate.get("belongs_to_series"):
            return False
    return True


def _build_notes(checks: dict) -> str:
    failed = [name for name, passed in checks.items() if not passed]
    if not failed:
        return "All promotion-readiness checks passed."
    described = "; ".join(f"{name} ({_CHECK_LABELS.get(name, name)})" for name in failed)
    return f"Not ready: failed checks -- {described}."


def _empty_checklist(series_id: int, note: str) -> dict:
    checks = {name: False for name in _CHECK_LABELS}
    return {
        "series_id": series_id,
        "checks": checks,
        "summary": {"ready_for_phase2": False, "notes": note},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def generate_promotion_readiness(series_id: int, *, db_session: Session | None = None) -> dict:
    """Runs one fresh `run_agentic_evaluation_for_series` for `series_id`
    and derives a structured promotion-readiness checklist from its
    `comparison`/`drift_report`/`ttl_report` sections (see module
    docstring for exactly what each check means).

    Returns:
        {
          "series_id": series_id,
          "checks": {
            "has_recent_agentic_trace": bool,
            "drift_within_threshold": bool,
            "ttl_clean": bool,
            "confidence_stable": bool,
            "gate_consistent": bool,
            "skeleton_preview_consistent": bool,
          },
          "summary": {"ready_for_phase2": bool, "notes": str},
          "timestamp": iso8601,
        }

    NOT a promotion mechanism -- purely diagnostic, read-only (delegates
    every read to `run_agentic_evaluation_for_series`, which has its own
    no-write guarantees; this function adds no new write surface).
    `db_session`, when provided, is passed straight through and reused
    as-is/never closed here, same convention as every other Phase 1
    harness function.
    """
    try:
        evaluation = run_agentic_evaluation_for_series(series_id, db_session=db_session)

        agentic_trace = evaluation.get("agentic_trace") or {}
        comparison = evaluation.get("comparison") or {}
        drift_report = evaluation.get("drift_report") or {}
        ttl_report = evaluation.get("ttl_report") or {}

        checks = {
            "has_recent_agentic_trace": _has_recent_agentic_trace(agentic_trace),
            "drift_within_threshold": _drift_within_threshold(drift_report),
            "ttl_clean": _ttl_clean(ttl_report),
            "confidence_stable": _confidence_stable(comparison),
            "gate_consistent": _gate_consistent(comparison),
            "skeleton_preview_consistent": _skeleton_preview_consistent(drift_report),
        }

        return {
            "series_id": series_id,
            "checks": checks,
            "summary": {
                "ready_for_phase2": all(checks.values()),
                "notes": _build_notes(checks),
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        logger.exception(
            "generate_promotion_readiness failed for series_id=%s; reporting not-ready conservatively", series_id
        )
        return _empty_checklist(series_id, "Checklist generation failed; treated as not ready (see server logs).")
