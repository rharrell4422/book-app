"""Phase 1 agentic substrate: structured, side-channel-only tracing hooks
around existing discovery logic.

Per `discovery_agentic_phase1_plan.md` / `discovery_agentic_phase1_evaluation.md`
(settled architecture -- see those docs for the full review trail, not
re-litigated here), this module covers two Phase-1 tickets:

- **RT-1b** (turn lifecycle): `begin_turn`/`record_tool_call`/
  `record_reasoning_step`/`record_world_model_update`/`end_turn` -- a
  turn-scoped trace of one `run_series_check` call.
- **PB-5** (shadow diagnostics): `shadow_probe`/`shadow_confidence_trace`/
  `shadow_skeleton_merge_trace`/`shadow_gate_trace` -- finer-grained,
  per-decision-point traces wired into `confidence_engine.py`,
  `services/skeleton_store.py`, and the belongs-to-series gate in
  `agents/series_agent.py`. PB-5's functions are deliberately independent
  of RT-1b's (they accept any plain dict as `context`, not specifically a
  `begin_turn`-produced one) so either ticket's instrumentation can be
  torn out or replaced without touching the other's call sites.

Both tickets share the same governing rules, true of every function in
this module:

- Side-channel only. Nothing here returns a value that any caller uses to
  make a routing/confidence/skeleton decision -- every public function
  either returns nothing (`None`) or, for `begin_turn`, a plain dict the
  caller threads through the rest of its own call (never inspected for
  anything except by these same hooks).
- Fail-soft, always. A tracing bug must never be able to break a real
  discovery run -- every public function catches and logs its own
  exceptions rather than propagating them.
- No new persistence. `context` is a plain dict; no ORM models, no new
  tables/columns, no writes to `SeriesSkeleton`/`skeleton_json`/
  `probes_json` or anything else. Any actual skeleton/world-model
  mutation, confidence computation, or gate decision still happens
  exactly where it always has -- this module only *observes* and logs a
  summary of it, strictly after the fact.
- Delegates to `services/discovery_telemetry.py`'s existing
  `DiscoveryTelemetry` where one is available on `context["telemetry"]`
  (the same optional instance `agents/series_agent.py`'s
  `run_series_check` already accepts) -- additive only, via a new
  `record_tool_call` method that does not touch that class's existing
  `provider_calls`/`gate_outcomes` counters (see that module for why those
  stay untouched).

Explicitly NOT in scope for this module (see the Phase-1 plan's own
sequencing): no `agentic_series_agent.py`, no shadow loop, no new
provider/orchestration logic. This is the substrate the later phases build
on, not the loop itself.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _turn_id_for(context: dict) -> str:
    series_id = context.get("series_id")
    started_at = context.get("timestamp") or context.get("turn_started_at") or _now_iso()
    return f"turn:{series_id}:{started_at}"


def begin_turn(context: dict) -> dict:
    """Starts a turn-scoped trace. `context` is whatever identifiers the
    caller already has (series_id, user_id, timestamp, an existing
    `DiscoveryTelemetry`/cache instance, etc.) -- this returns a *new* dict
    (the original `context` argument is never mutated) that layers turn
    bookkeeping on top without dropping or renaming any caller-provided
    key, so a caller can keep reading its own fields back off the returned
    context exactly as it put them in.

    Fail-soft: if anything here raises, the caller still gets back a usable
    dict (a shallow copy of `context`, or `{}` if even that fails) so every
    other hook function can keep operating against *something*.
    """
    try:
        turn_context = dict(context or {})
        turn_context.setdefault("timestamp", _now_iso())
        turn_context.setdefault("turn_started_at", turn_context["timestamp"])
        turn_context.setdefault("turn_id", _turn_id_for(turn_context))
        turn_context.setdefault("reasoning_steps", [])
        turn_context.setdefault("tool_call_count", 0)
        turn_context.setdefault("world_model_update_count", 0)
        logger.info(
            "agentic_turn_begin turn_id=%s series_id=%s user_id=%s",
            turn_context.get("turn_id"),
            turn_context.get("series_id"),
            turn_context.get("user_id"),
        )
        return turn_context
    except Exception:
        logger.exception("agentic_hooks.begin_turn failed; returning best-effort context")
        try:
            return dict(context or {})
        except Exception:
            return {}


def _result_size(raw_result) -> int:
    try:
        if raw_result is None:
            return 0
        if isinstance(raw_result, (list, dict, tuple, set)):
            return len(raw_result)
        return len(str(raw_result))
    except Exception:
        return -1


def record_tool_call(context: dict, provider: str, query: str, raw_result) -> None:
    """Records one provider/tool invocation against the current turn.

    Delegates to `services/discovery_telemetry.py`'s `DiscoveryTelemetry.
    record_tool_call` when the caller has attached a live instance at
    `context["telemetry"]` (the same optional param `run_series_check`
    already threads through discovery) -- additive-only there, see that
    module. Never raises; a telemetry/logging failure here must never be
    able to affect the actual provider call this is just observing after
    the fact.
    """
    try:
        context = context if isinstance(context, dict) else {}
        result_size = _result_size(raw_result)
        telemetry = context.get("telemetry")
        if telemetry is not None and hasattr(telemetry, "record_tool_call"):
            try:
                telemetry.record_tool_call(provider=str(provider or ""), query=str(query or ""), result_size=result_size)
            except Exception:
                logger.exception("agentic_hooks.record_tool_call: telemetry delegation failed")
        context["tool_call_count"] = int(context.get("tool_call_count") or 0) + 1
        logger.info(
            "agentic_tool_call turn_id=%s provider=%s query=%s result_size=%s",
            context.get("turn_id"),
            provider,
            query,
            result_size,
        )
    except Exception:
        logger.exception("agentic_hooks.record_tool_call failed; continuing")


def record_reasoning_step(context: dict, step: dict) -> None:
    """Appends one small, structured decision-point record (e.g.
    `{"phase": "routing", "decision": "needs_review", "confidence": "low"}`)
    to the turn's in-memory trace and logs it. Purely observational: the
    caller passes in a decision it already made using its own existing
    logic; this never feeds back into that decision.
    """
    try:
        context = context if isinstance(context, dict) else {}
        entry = dict(step or {})
        entry.setdefault("turn_id", context.get("turn_id"))
        entry.setdefault("recorded_at", _now_iso())
        steps = context.setdefault("reasoning_steps", [])
        if isinstance(steps, list):
            steps.append(entry)
        logger.info("agentic_reasoning_step turn_id=%s step=%s", context.get("turn_id"), entry)
    except Exception:
        logger.exception("agentic_hooks.record_reasoning_step failed; continuing")


def record_world_model_update(context: dict, update: dict) -> None:
    """Logs a summary of a world-model (skeleton) change the caller *already
    computed and is about to apply/has applied* through the existing,
    unmodified skeleton machinery (`services/skeleton_store.py`) -- this
    never itself reads or writes `SeriesSkeleton`, it only records what the
    caller reports happened.
    """
    try:
        context = context if isinstance(context, dict) else {}
        entry = dict(update or {})
        entry.setdefault("turn_id", context.get("turn_id"))
        entry.setdefault("recorded_at", _now_iso())
        context["world_model_update_count"] = int(context.get("world_model_update_count") or 0) + 1
        logger.info("agentic_world_model_update turn_id=%s update=%s", context.get("turn_id"), entry)
    except Exception:
        logger.exception("agentic_hooks.record_world_model_update failed; continuing")


def end_turn(context: dict) -> None:
    """Closes out a turn-scoped trace with a one-line summary. Safe to call
    even if `begin_turn` itself failed earlier (falls back to whatever
    fields are present, defaulting the rest).
    """
    try:
        context = context if isinstance(context, dict) else {}
        started_raw = context.get("turn_started_at") or context.get("timestamp")
        duration_s = None
        if started_raw:
            try:
                started = datetime.fromisoformat(str(started_raw))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                duration_s = round((datetime.now(timezone.utc) - started).total_seconds(), 3)
            except Exception:
                duration_s = None
        logger.info(
            "agentic_turn_end turn_id=%s series_id=%s duration_s=%s tool_calls=%s reasoning_steps=%s world_model_updates=%s",
            context.get("turn_id"),
            context.get("series_id"),
            duration_s,
            context.get("tool_call_count"),
            len(context.get("reasoning_steps") or []),
            context.get("world_model_update_count"),
        )
    except Exception:
        logger.exception("agentic_hooks.end_turn failed")


# ---------------------------------------------------------------------------
# PB-5: shadow diagnostics
#
# Every function below is called *after* the real, unmodified decision it
# describes has already been made by its caller -- none of them can affect
# routing, confidence grades, skeleton merges, or gate outcomes, because
# none of them are given the chance to run until that decision is final.
# ---------------------------------------------------------------------------


def shadow_probe(context: dict, provider: str, query: str, result) -> None:
    """PB-5: shadow-mode trace of a provider probe -- what was asked, and
    how much came back. Deliberately a separate channel from RT-1b's
    `record_tool_call` (tagged `shadow:<provider>` when it delegates to
    telemetry, so the two are distinguishable in `DiscoveryTelemetry.
    tool_calls` rather than double-counted as the same entry) so PB-5's
    diagnostics can be extended or removed independently of RT-1b's
    turn-lifecycle bookkeeping. Never touches provider selection,
    escalation, normalization, or error handling -- the caller has
    already finished all of that before this is invoked.
    """
    try:
        context = context if isinstance(context, dict) else {}
        result_size = _result_size(result)
        telemetry = context.get("telemetry")
        if telemetry is not None and hasattr(telemetry, "record_tool_call"):
            try:
                telemetry.record_tool_call(
                    provider=f"shadow:{provider}", query=str(query or ""), result_size=result_size
                )
            except Exception:
                logger.exception("agentic_hooks.shadow_probe: telemetry delegation failed")
        logger.info(
            "agentic_shadow_probe turn_id=%s provider=%s query=%s result_size=%s",
            context.get("turn_id"),
            provider,
            query,
            result_size,
        )
    except Exception:
        logger.exception("agentic_hooks.shadow_probe failed; continuing")


def shadow_confidence_trace(context: dict, book_number: float | None, before: dict, after: dict) -> None:
    """PB-5: records confidence_engine's per-candidate decision detail for
    one `book_number` -- `before` (the four raw per-dimension grades:
    provider/title/number/series_alignment confidence, as first computed)
    and `after` (the same dimensions plus the resolved `overall` grade).
    `confidence_engine.py` computes both dicts with its own existing,
    unmodified `_overall_confidence` logic and passes them in strictly
    after the fact -- this function has no way to feed anything back into
    that computation, and never sees a candidate before its dimension
    grades are already final.
    """
    try:
        context = context if isinstance(context, dict) else {}
        entry = {
            "turn_id": context.get("turn_id"),
            "book_number": book_number,
            "before": dict(before or {}),
            "after": dict(after or {}),
            "recorded_at": _now_iso(),
        }
        logger.info("agentic_shadow_confidence_trace %s", entry)
    except Exception:
        logger.exception("agentic_hooks.shadow_confidence_trace failed; continuing")


def shadow_skeleton_merge_trace(context: dict, before, after) -> None:
    """PB-5: records a skeleton merge's before/after `skeleton_json` shape
    -- entry counts and which `book_number`s were added/removed -- without
    reading or writing `SeriesSkeleton` itself. `services/skeleton_store.py`
    passes in the exact `existing_entries`/`new_entries` lists its own
    unmodified `_upsert_skeleton_row`/`merge_fn` logic already computed,
    strictly after a successful commit -- this function cannot alter the
    merge result it's describing.
    """
    try:
        context = context if isinstance(context, dict) else {}
        before_list = before if isinstance(before, list) else []
        after_list = after if isinstance(after, list) else []
        before_numbers = {
            entry.get("book_number")
            for entry in before_list
            if isinstance(entry, dict) and entry.get("book_number") is not None
        }
        after_numbers = {
            entry.get("book_number")
            for entry in after_list
            if isinstance(entry, dict) and entry.get("book_number") is not None
        }
        entry = {
            "turn_id": context.get("turn_id"),
            "series_id": context.get("series_id"),
            "before_count": len(before_list),
            "after_count": len(after_list),
            "added_numbers": sorted(after_numbers - before_numbers),
            "removed_numbers": sorted(before_numbers - after_numbers),
            "recorded_at": _now_iso(),
        }
        logger.info("agentic_shadow_skeleton_merge_trace %s", entry)
    except Exception:
        logger.exception("agentic_hooks.shadow_skeleton_merge_trace failed; continuing")


def shadow_fingerprint_merge_trace(context: dict, before: dict, after: dict) -> None:
    """Series Fingerprint design chain (see
    `discovery_agentic_fingerprint_recommendation.md`): the fingerprint
    analogue of `shadow_skeleton_merge_trace` above, but with genuinely
    different diff semantics -- `SeriesFingerprint.fingerprint_json` is a
    single flat per-series dict (`author_aliases`, `naming_patterns`,
    `provider_bias`, `release_cadence`), not a list of book-number-keyed
    entries, so a book-number-set diff does not apply here. This function
    diffs at the field level instead: list fields report which items were
    added, `provider_bias` reports which provider keys changed value, and
    `release_cadence` reports its own before/after dict wholesale (it's a
    small, fully-recomputed stat blob each round, not worth a sub-diff).
    `services/fingerprint_store.py` passes in the exact
    `existing_fingerprint`/`new_fingerprint` dicts its own unmodified
    `_upsert_fingerprint_row`/`merge_fn` logic already computed, strictly
    after a successful commit -- same non-feedback guarantee as
    `shadow_skeleton_merge_trace`.
    """
    try:
        context = context if isinstance(context, dict) else {}
        before_dict = before if isinstance(before, dict) else {}
        after_dict = after if isinstance(after, dict) else {}

        before_aliases = set(before_dict.get("author_aliases") or [])
        after_aliases = set(after_dict.get("author_aliases") or [])
        before_patterns = set(before_dict.get("naming_patterns") or [])
        after_patterns = set(after_dict.get("naming_patterns") or [])
        before_bias = before_dict.get("provider_bias") or {}
        after_bias = after_dict.get("provider_bias") or {}
        changed_bias = {
            provider: {"before": before_bias.get(provider), "after": value}
            for provider, value in after_bias.items()
            if before_bias.get(provider) != value
        }

        entry = {
            "turn_id": context.get("turn_id"),
            "series_id": context.get("series_id"),
            "added_author_aliases": sorted(after_aliases - before_aliases),
            "added_naming_patterns": sorted(after_patterns - before_patterns),
            "changed_provider_bias": changed_bias,
            "release_cadence_before": before_dict.get("release_cadence"),
            "release_cadence_after": after_dict.get("release_cadence"),
            "recorded_at": _now_iso(),
        }
        logger.info("agentic_shadow_fingerprint_merge_trace %s", entry)
    except Exception:
        logger.exception("agentic_hooks.shadow_fingerprint_merge_trace failed; continuing")


def shadow_gate_trace(context: dict, book_number: float | None, gate_input: dict, gate_output: dict) -> None:
    """PB-5: records the belongs-to-series gate's inputs (title/number
    match signals the caller already computed -- explicit/partial title
    match, targeted-with-number, continues-numbering, universe-tie-in/
    compilation downgrades, etc.) and its output (the resolved
    `belongs_to_series` boolean, plus whatever else the caller includes)
    for one candidate. `agents/series_agent.py` is the only place this
    gate is actually computed (see that module's `run_series_check`) --
    this function only observes the already-final result.
    """
    try:
        context = context if isinstance(context, dict) else {}
        entry = {
            "turn_id": context.get("turn_id"),
            "book_number": book_number,
            "gate_input": dict(gate_input or {}),
            "gate_output": dict(gate_output or {}),
            "recorded_at": _now_iso(),
        }
        logger.info("agentic_shadow_gate_trace %s", entry)
    except Exception:
        logger.exception("agentic_hooks.shadow_gate_trace failed; continuing")
