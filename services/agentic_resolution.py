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

Phase 6 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_
evaluation.md`, not re-litigated here) adds a determinism/ordering
guarantee on top, with zero change to *which* value wins per book: this
function resolves books in ascending-`book_number` order, and both
returned dicts always have their keys inserted in ascending-`book_number`
order too, regardless of what order `promotion_decisions`/the live
snapshots were built in. That matters because a Python `set` (previously
used to collect "every book_number seen") has no guaranteed iteration
order at all -- two runs over the exact same input could resolve books in
a different order even though every individual book's resolved value
was already correct. The resolved *values* were never nondeterministic;
only the *order* callers iterate over them in was.

Phase 7 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_
evaluation.md`, not re-litigated here) adds one more check per book,
defense-in-depth on top of `evaluate_promotion`'s own Phase 7 safety
gate: even for a book whose stored `outcome` is `"use_agentic"` (meaning
`evaluate_promotion` already approved it, including its own Phase 7
`services.agentic_safety.validate_agentic_decision` check at the time it
ran), this function independently re-runs that same check right before
actually applying the agentic side. This is deliberately redundant --
the point is that a resolution-time bug, a stale/replayed decision, or a
future caller that bypasses `evaluate_promotion` entirely can never
result in an unsafe agentic value reaching live routing, without this
module needing to import anything from `services/agentic_promotion_
evaluator.py` at all (see `services/agentic_safety.py`'s own docstring
for why it's intentionally self-contained). A book that fails this
re-check resolves to its live value instead, exactly as if its outcome
had been `"use_live"` -- the failure is logged via `services.discovery_
telemetry.record_agentic_safety_violation` (fail-soft; never raises).
"""

from __future__ import annotations

import logging

import settings
from services.agentic_safety import validate_agentic_decision

logger = logging.getLogger(__name__)


def _safe_sort_key(value) -> float:
    """Phase 6: a book_number that isn't a real `int`/`float` sorts to
    `+inf` -- last, deterministically -- rather than raising `TypeError`
    when compared against a genuine number or another malformed value.
    """
    return value if isinstance(value, (int, float)) else float("inf")


def _sorted_dict_by_key(raw: dict) -> dict:
    """Phase 6: returns a new dict with `raw`'s same key/value pairs,
    inserted in ascending-key order (via `_safe_sort_key`, so a
    malformed/non-numeric key can never raise `TypeError` sorting
    against a real book_number) -- never mutates `raw` itself.
    """
    try:
        return {key: raw[key] for key in sorted(raw, key=_safe_sort_key)}
    except Exception:
        return dict(raw)


def _record_cache_access(cache_hit: bool) -> None:
    """Fail-soft telemetry side-channel for the Phase 9 `agentic_cache_
    hits`/`agentic_cache_misses` counters above -- never raises.
    Deliberately a local helper (matching `_log_safety_violation`'s own
    convention just below) rather than a module-level import, for the
    same reason every other telemetry call in this module is a
    function-scoped import: avoids any risk of a circular import at
    import time.
    """
    try:
        from services.discovery_telemetry import record_agentic_cache_hit, record_agentic_cache_miss

        if cache_hit:
            record_agentic_cache_hit()
        else:
            record_agentic_cache_miss()
    except Exception:
        logger.exception("_record_cache_access: failed to record cache metric (cache_hit=%s)", cache_hit)


def _log_safety_violation(series_id, book_number, reason: str) -> None:
    """Fail-soft telemetry side-channel for the Phase 7 defense-in-depth
    veto above -- never raises. Deliberately a local helper (rather than
    importing `services.agentic_promotion_evaluator`'s equivalent) so
    this module stays independent of that one, per this module's own
    docstring.
    """
    try:
        from services.discovery_telemetry import record_agentic_safety_violation

        record_agentic_safety_violation(series_id, book_number, reason)
    except Exception:
        logger.exception(
            "resolve_routing_decisions: failed to log safety violation for series_id=%s book_number=%s",
            series_id,
            book_number,
        )


def resolve_routing_decisions(
    series_id: int,
    live_confidence_snapshot: dict,
    live_gate_snapshot: dict,
    promotion_decisions: dict,
    *,
    cache=None,
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

    Phase 7: an outcome of `"use_agentic"` is additionally re-validated
    here, independently of `evaluate_promotion`'s own check, via
    `services.agentic_safety.validate_agentic_decision` -- see module
    docstring. Failing that re-check resolves the book to its live value
    (same as `"use_live"`/`"reject_agentic"`), not the agentic one, and
    logs a fail-soft safety-violation record.

    Phase 6: both returned dicts have their keys in ascending-book_number
    order (see module docstring) -- `promotion_decisions` is consulted in
    that same sorted order while resolving, so this holds regardless of
    the order `live_confidence_snapshot`/`live_gate_snapshot`/
    `promotion_decisions` were themselves built in.

    Fail-soft: any exception (malformed input, `settings.
    is_agentic_activated` itself raising, etc.) is caught and logged,
    and this returns the live snapshots verbatim (still sorted) rather
    than raising -- callers get the same live-only behavior as if the
    flag were off.

    Pure aside from that logging: never writes to the database, never
    touches `skeleton_json`/`probes_json`, never calls a provider.

    Phase 8 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
    phase1_evaluation.md`, not re-litigated here): `promotion_decisions`
    is already an in-memory dict this function only ever reads from
    (`.get(book_number)`), so there was never a real per-book DB
    round-trip or recomputation to eliminate here -- but the optional
    `cache` keyword (a `services.agentic_cache.AgenticTurnCache`) still
    memoizes that lookup by `book_number`, so this function never reads
    `promotion_decisions[book_number]` more than once per book even if
    something upstream calls this function more than once for the same
    `series_id`/turn sharing one `cache` instance. Omitting `cache` (the
    default) reproduces the exact pre-Phase-8 behavior. Per `services.
    agentic_cache`'s own docstring, use a *dedicated* `AgenticTurnCache`
    instance here -- do not share the one (if any) passed into `services.
    agentic_promotion_evaluator.evaluate_promotion`, since that function
    caches a different value shape (an outcome string, not this
    function's decision dict) under the same `book_number` keys.

    Phase 9 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
    phase1_evaluation.md`, not re-litigated here): every `cache` lookup
    above also bumps `services.discovery_telemetry`'s in-memory
    `agentic_cache_hits`/`agentic_cache_misses` counter (fail-soft, never
    raises) -- purely observational, never consulted by anything in this
    function's own control flow.
    """
    try:
        live_confidence_snapshot = live_confidence_snapshot or {}
        live_gate_snapshot = live_gate_snapshot or {}
        promotion_decisions = promotion_decisions or {}

        if not settings.AGENTIC_ROUTING_ENABLED:
            return _sorted_dict_by_key(live_confidence_snapshot), _sorted_dict_by_key(live_gate_snapshot)

        if not settings.is_agentic_activated(series_id):
            return _sorted_dict_by_key(live_confidence_snapshot), _sorted_dict_by_key(live_gate_snapshot)

        resolved_confidence: dict = {}
        resolved_gate: dict = {}
        all_book_numbers = set(live_confidence_snapshot) | set(live_gate_snapshot) | set(promotion_decisions)
        for book_number in sorted(all_book_numbers, key=_safe_sort_key):
            decision = None
            if cache is not None:
                try:
                    # Phase 9: recorded *before* the actual lookup below,
                    # since `get_or_set_promotion` itself mutates the
                    # cache on a miss -- checking membership first is the
                    # only way to observe "was this already cached"
                    # rather than "is this cached now".
                    cache_hit = book_number in cache.promotion
                    decision = (
                        cache.get_or_set_promotion(
                            book_number, lambda bn=book_number: promotion_decisions.get(bn) or {}
                        )
                        or {}
                    )
                    _record_cache_access(cache_hit)
                except Exception:
                    # Phase 8 fail-soft: a broken cache must not prevent
                    # this book_number from resolving -- fall back below.
                    logger.exception(
                        "resolve_routing_decisions: cache lookup failed for book_number=%s; "
                        "resolving without it",
                        book_number,
                    )
                    decision = None
            if decision is None:
                decision = promotion_decisions.get(book_number) or {}
            live_conf = live_confidence_snapshot.get(book_number, decision.get("live_confidence"))
            live_gate_value = live_gate_snapshot.get(book_number, decision.get("live_gate"))

            if decision.get("outcome") == "use_agentic" and validate_agentic_decision(
                live_conf, decision.get("agentic_confidence"), live_gate_value, decision.get("agentic_gate")
            ):
                resolved_confidence[book_number] = decision.get("agentic_confidence", live_conf)
                resolved_gate[book_number] = decision.get("agentic_gate", live_gate_value)
            else:
                if decision.get("outcome") == "use_agentic":
                    # evaluate_promotion approved this book, but the
                    # independent defense-in-depth re-check above just
                    # vetoed it -- fall back to live, same as any other
                    # non-"use_agentic" outcome, and log why.
                    _log_safety_violation(
                        series_id, book_number, "resolve_routing_decisions defense-in-depth re-check vetoed"
                    )
                resolved_confidence[book_number] = live_conf
                resolved_gate[book_number] = live_gate_value

        return resolved_confidence, resolved_gate
    except Exception:
        logger.exception(
            "resolve_routing_decisions failed for series_id=%s; falling back to live snapshots", series_id
        )
        return _sorted_dict_by_key(live_confidence_snapshot or {}), _sorted_dict_by_key(live_gate_snapshot or {})
