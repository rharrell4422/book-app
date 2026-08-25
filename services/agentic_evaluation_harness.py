"""Phase 1 agentic discovery, fourth implementation block: a shadow-mode
evaluation harness comparing the live pipeline's already-persisted state
against `agents/agentic_series_agent.py`'s deterministic shadow trace.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(settled architecture, not re-litigated here): this module is scaffolding
for later evaluation/promotion decisions, not a live decision path. It is
**not** wired into any user-facing route or scheduled job -- callable only
from internal/admin/test contexts (a management script, an admin-only
endpoint, or a test) until a future ticket explicitly promotes it.

`run_agentic_evaluation_for_series` never writes anything:

- It opens (or reuses a caller-supplied) DB session and only ever reads
  through it -- no `db.add`/`db.commit`/`db.flush` anywhere in this
  module.
- `_observe_live_pipeline` below is a pure read of already-persisted
  `SeriesSkeleton.skeleton_json` -- it never re-runs discovery (that would
  make live, non-deterministic network calls and isn't idempotent) and
  never calls a write path (`apply_skeleton_updates`, `run_series_check`'s
  own commits, etc.).
- `agents.agentic_series_agent.run_agentic_turn` is itself already
  shadow-mode-only (see that module's own docstring) -- this harness adds
  no new write surface on top of it.
- `_compare_live_vs_agentic` is pure -- a diagnostic dict built only from
  its two dict arguments, read back by nothing else in this module or the
  live pipeline.

Both live and agentic sides deliberately reuse the exact same helpers
Phase 1's earlier blocks already established, rather than reimplementing
anything: `agents.series_agent._build_series_identity_sets`/
`_build_owned_core_title_texts` (identity-set construction, also used by
`agents/agentic_series_agent.py`) and the `SeriesSkeleton`/`Book`/`Series`
models directly (there's no dedicated "read a skeleton" helper in
`services/skeleton_store.py` beyond the ORM query itself, so this module
queries the same way `agents/agentic_series_agent.py` already does).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from agents import agentic_series_agent
from database import SessionLocal
from models import Series, SeriesSkeleton
from services.discovery_telemetry import record_agentic_evaluation

logger = logging.getLogger(__name__)


def _sort_key_for_book_number(key: str):
    """Numeric sort where possible (book numbers are usually numeric
    strings), falling back to lexical -- purely for stable, readable
    report ordering, never for anything decision-related.
    """
    try:
        return (0, float(key))
    except (TypeError, ValueError):
        return (1, key)


def _observe_live_pipeline(series_id: int, db_session: Session) -> dict:
    """Read-only snapshot of the live pipeline's *already-persisted*
    state for `series_id`. Deliberately does NOT re-run discovery --
    that would make live, non-deterministic network calls (see module
    docstring) -- and never writes anything. "Live" here means "what the
    real pipeline has already committed as of right now", the baseline
    `_compare_live_vs_agentic` measures the shadow trace's freshly
    (re)computed values against.

    Returns:
        {
          "skeleton_snapshot": {"<book_number>": skeleton_entry, ...},
          "confidence_snapshot": {"<book_number>": {"confidence": ..., "status": ...}, ...},
          "gate_snapshot": {"<book_number>": {"belongs_to_series": ..., "source_class": ...}, ...},
        }

    Every dict here is keyed by `str(book_number)` (skeleton `book_number`s
    are floats; JSON/report consumers need string keys anyway).
    """
    empty = {"skeleton_snapshot": {}, "confidence_snapshot": {}, "gate_snapshot": {}}
    try:
        series = db_session.query(Series).filter(Series.id == series_id).first()
        if series is None:
            return empty

        skeleton_row = db_session.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == series_id).first()
        skeleton_entries = (
            list(skeleton_row.skeleton_json)
            if skeleton_row is not None and isinstance(skeleton_row.skeleton_json, list)
            else []
        )

        skeleton_snapshot: dict = {}
        confidence_snapshot: dict = {}
        gate_snapshot: dict = {}
        for entry in skeleton_entries:
            if not isinstance(entry, dict) or entry.get("book_number") is None:
                continue
            key = str(entry["book_number"])
            skeleton_snapshot[key] = entry
            confidence_snapshot[key] = {
                "confidence": entry.get("confidence"),
                "status": entry.get("status"),
            }
            # Anything already present in the skeleton -- library-owned,
            # or a "discovered" entry -- already cleared the live
            # belongs-to-series gate at some point in the past: a library
            # row was never subject to it, and a "discovered" row only
            # exists here because a prior run_series_check call's gate
            # (agents/series_agent.py's evaluate_belongs_to_series_gate)
            # accepted it. This reports that already-final outcome, not a
            # fresh recomputation of it.
            gate_snapshot[key] = {
                "belongs_to_series": True,
                "source_class": entry.get("source_class", "library"),
            }

        return {
            "skeleton_snapshot": skeleton_snapshot,
            "confidence_snapshot": confidence_snapshot,
            "gate_snapshot": gate_snapshot,
        }
    except Exception:
        logger.exception("_observe_live_pipeline failed for series_id=%s; returning empty snapshot", series_id)
        return empty


def _compare_live_vs_agentic(live: dict, agentic: dict) -> dict:
    """Structured, purely diagnostic comparison between `_observe_live_
    pipeline`'s snapshot and `agentic_series_agent.run_agentic_turn`'s
    trace. No decisions, no routing changes -- this dict is never read
    back by anything except (eventually) a human or an offline evaluation
    script; nothing in this module or the live pipeline consumes it.

    Returns `{"by_book_number": {"<book_number>": {...}, ...}}`, one
    entry per `book_number` seen on either side, keyed to survive missing
    data gracefully on either side (a fresh series with no skeleton yet
    still produces a valid, empty comparison).
    """
    live = live if isinstance(live, dict) else {}
    agentic = agentic if isinstance(agentic, dict) else {}

    live_skeleton = live.get("skeleton_snapshot") or {}
    live_confidence = live.get("confidence_snapshot") or {}
    live_gate = live.get("gate_snapshot") or {}

    agentic_confidence_by_number = {
        str(entry.get("book_number")): entry
        for entry in (agentic.get("confidence_traces") or [])
        if isinstance(entry, dict) and entry.get("book_number") is not None
    }
    agentic_gate_by_number = {
        str(entry.get("book_number")): entry
        for entry in (agentic.get("gate_traces") or [])
        if isinstance(entry, dict) and entry.get("book_number") is not None
    }

    # The merge-preview phase produces one preview per turn (existing
    # entries + any newly-accepted candidates) -- take the first (there is
    # only ever one in the current run_agentic_turn implementation) and
    # index its "after" entries by book_number for a per-book comparison.
    agentic_preview_by_number: dict = {}
    previews = agentic.get("skeleton_merge_previews") or []
    if previews and isinstance(previews[0], dict):
        for entry in previews[0].get("after") or []:
            if isinstance(entry, dict) and entry.get("book_number") is not None:
                agentic_preview_by_number[str(entry["book_number"])] = entry

    all_keys = (
        set(live_skeleton)
        | set(agentic_confidence_by_number)
        | set(agentic_gate_by_number)
        | set(agentic_preview_by_number)
    )

    by_book_number: dict = {}
    for key in sorted(all_keys, key=_sort_key_for_book_number):
        agentic_conf_entry = agentic_confidence_by_number.get(key)
        agentic_gate_entry = agentic_gate_by_number.get(key)
        by_book_number[key] = {
            "live_confidence": live_confidence.get(key),
            "agentic_confidence": (agentic_conf_entry or {}).get("after"),
            "live_gate": live_gate.get(key),
            "agentic_gate": (agentic_gate_entry or {}).get("gate_output"),
            "live_skeleton_entry": live_skeleton.get(key),
            "agentic_preview_entry": agentic_preview_by_number.get(key),
            "present_in_live": key in live_skeleton,
            "present_in_agentic": key in agentic_confidence_by_number or key in agentic_gate_by_number,
        }

    return {"by_book_number": by_book_number}


def run_agentic_evaluation_for_series(series_id: int, *, db_session: Session | None = None) -> dict:
    """Runs one full shadow-mode agentic evaluation for `series_id`.

    - Observes the live pipeline's current, already-persisted state
      (read-only; see `_observe_live_pipeline`).
    - Runs `agents.agentic_series_agent.run_agentic_turn` in shadow mode
      (that module's own docstring covers its no-write guarantees).
    - Computes a structured comparison between the two (see
      `_compare_live_vs_agentic`).
    - Logs the resulting report via `services.discovery_telemetry.
      record_agentic_evaluation` (fail-soft -- a logging failure here
      never affects the returned report).

    Does NOT modify any persistent state: no DB writes anywhere in this
    function, and every function it calls shares that same guarantee.

    `db_session`, when provided (mainly for tests/admin tooling that
    already have one open), is reused as-is and never closed by this
    function; when omitted, a session is opened internally via
    `database.SessionLocal` and always closed before returning.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        series = db.query(Series).filter(Series.id == series_id).first()
        user_id = getattr(series, "profile_id", None) if series is not None else None

        live_observation = _observe_live_pipeline(series_id, db)

        context = {
            "series_id": series_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
            "db": db,
        }
        agentic_trace = agentic_series_agent.run_agentic_turn(series_id, context)

        comparison = _compare_live_vs_agentic(live_observation, agentic_trace)

        report = {
            "series_id": series_id,
            "live_observation": live_observation,
            "agentic_trace": agentic_trace,
            "comparison": comparison,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        if not caller_supplied_db:
            db.close()

    try:
        record_agentic_evaluation(series_id, report)
    except Exception:
        logger.exception(
            "run_agentic_evaluation_for_series: record_agentic_evaluation failed for series_id=%s; "
            "continuing (report is still returned)",
            series_id,
        )

    return report
