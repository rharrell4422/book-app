"""Phase 5 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_
evaluation.md`, not re-litigated here): unified resolution layer.

Phases 3/4 built the per-book "should this book's confidence/gate be the
live value or the agentic one" decision directly inline inside
`agents/series_agent.py`'s Phase 3/4 promotion block -- one `if
series_is_activated and promo_outcome == "use_agentic": ... else: ...`
per book, mixed in with the promotion-evaluation loop itself. That was
fine for two phases of incremental, additive changes, but it meant the
actual *resolution* logic (which one wins) was duplicated reasoning
spread across a loop body rather than a single, independently testable
unit.

This module extracts exactly that resolution decision -- and nothing
else -- into one pure(ish) function, `resolve_routing_decisions`. It is
NOT a new decision authority: `services/agentic_promotion_evaluator.
evaluate_promotion` still decides "use_agentic" vs "use_live" vs
"reject_agentic" for each book, and `store_promotion_decision` still
writes the shadow-table row -- both of those stay exactly where they
already were, in `agents/series_agent.py`'s promotion block, called once
per traced book before this module is ever invoked. All this module
does is take those already-made decisions (as a `book_number ->
{outcome, live_confidence, agentic_confidence, live_gate, agentic_gate}`
dict) plus the live snapshots, and mechanically produce the two final
`book_number -> value` dicts routing should use -- centralizing the
flag/activation gating and the per-book "which side wins" branch that
used to live inline.

No new behavior versus Phase 4: same two gates
(`settings.AGENTIC_ROUTING_ENABLED`, `settings.is_agentic_activated`),
same per-book "use_agentic" -> agentic side, anything else -> live side
resolution. Still never touches `SeriesSkeleton.skeleton_json`/
`probes_json`, still never calls a provider, still fail-soft (any
exception here falls back to the live snapshots verbatim, never raises
into the caller).
"""

from __future__ import annotations

import logging

import settings

logger = logging.getLogger(__name__)


def resolve_routing_decisions(
    series_id: int,
    live_confidence_snapshot: dict,
    live_gate_snapshot: dict,
    promotion_decisions: dict,
) -> tuple[dict, dict]:
    """Unified resolution layer: given a series' live confidence/gate
    snapshots (each `{book_number: value}`) and the promotion decisions
    already made for that turn (`{book_number: {"outcome": "use_agentic"
    | "use_live" | "reject_agentic", "live_confidence": ..., "agentic_
    confidence": ..., "live_gate": ..., "agentic_gate": ...}}`), returns
    the `(resolved_confidence, resolved_gate)` dicts routing should
    actually use.

    Behavior (identical to what Phase 3/4 did inline in `agents/
    series_agent.py`, just centralized here):

    - `settings.AGENTIC_ROUTING_ENABLED` off: returns the live snapshots
      unchanged (as new dict copies) -- agentic decisions never even
      considered.
    - Flag on but `settings.is_agentic_activated(series_id)` is `False`:
      also returns the live snapshots unchanged -- "record, don't
      apply" (Phase 3 behavior for a not-yet-activated series).
    - Flag on and the series IS activated: for every `book_number` seen
      in either snapshot or `promotion_decisions`, uses that book's own
      `promotion_decisions[book_number]["outcome"]` -- the agentic side
      exactly when it is `"use_agentic"`, the live side for any other
      outcome (`"use_live"`, `"reject_agentic"`, or missing/malformed).

    A `book_number` with no entry in `promotion_decisions` (e.g. because
    the caller only ran promotion evaluation for a subset of the live
    snapshot's books) simply passes its live value straight through --
    "no decision" is never treated as "use agentic".

    Fail-soft: any exception (malformed input, `settings.
    is_agentic_activated` itself raising, etc.) is caught and logged,
    and this returns the live snapshots verbatim rather than raising --
    callers get the same live-only behavior as if the flag were off.

    Pure aside from that logging: never writes to the database, never
    touches `skeleton_json`/`probes_json`, never calls a provider.
    """
    try:
        live_confidence_snapshot = live_confidence_snapshot or {}
        live_gate_snapshot = live_gate_snapshot or {}
        promotion_decisions = promotion_decisions or {}

        if not settings.AGENTIC_ROUTING_ENABLED:
            return dict(live_confidence_snapshot), dict(live_gate_snapshot)

        if not settings.is_agentic_activated(series_id):
            return dict(live_confidence_snapshot), dict(live_gate_snapshot)

        resolved_confidence: dict = {}
        resolved_gate: dict = {}
        all_book_numbers = set(live_confidence_snapshot) | set(live_gate_snapshot) | set(promotion_decisions)
        for book_number in all_book_numbers:
            decision = promotion_decisions.get(book_number) or {}
            live_conf = live_confidence_snapshot.get(book_number, decision.get("live_confidence"))
            live_gate_value = live_gate_snapshot.get(book_number, decision.get("live_gate"))

            if decision.get("outcome") == "use_agentic":
                resolved_confidence[book_number] = decision.get("agentic_confidence", live_conf)
                resolved_gate[book_number] = decision.get("agentic_gate", live_gate_value)
            else:
                resolved_confidence[book_number] = live_conf
                resolved_gate[book_number] = live_gate_value

        return resolved_confidence, resolved_gate
    except Exception:
        logger.exception(
            "resolve_routing_decisions failed for series_id=%s; falling back to live snapshots", series_id
        )
        return dict(live_confidence_snapshot or {}), dict(live_gate_snapshot or {})
