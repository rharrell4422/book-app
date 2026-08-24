"""RT-1b (Phase 1 agentic substrate): structured, turn-scoped tracing hooks
around existing discovery logic.

Per `discovery_agentic_phase1_plan.md` / `discovery_agentic_phase1_evaluation.md`
(settled architecture -- see those docs for the full review trail, not
re-litigated here), this module is **instrumentation only**:

- Side-channel only. Nothing here returns a value that any caller uses to
  make a routing/confidence/skeleton decision -- every public function
  either returns nothing (`None`) or, for `begin_turn`, a plain dict the
  caller threads through the rest of its own call (never inspected for
  anything except by these same hooks).
- Fail-soft, always. A tracing bug must never be able to break a real
  discovery run -- every public function catches and logs its own
  exceptions rather than propagating them.
- No new persistence. `context` is a plain dict; no ORM models, no new
  tables/columns, no writes to `SeriesSkeleton` or anything else. Any
  actual skeleton/world-model mutation still happens exactly where it
  always has (`services/skeleton_store.py`) -- this module only *observes*
  and logs a summary of it.
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
