"""Phase 10 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_
evaluation.md`, not re-litigated here): the finalization & hardening
layer's per-series readiness report, backing `GET /admin/agentic/
readiness/{series_id}`.

`compute_agentic_readiness` is a read-only pre-flight/health snapshot --
it never calls a live provider, never calls `run_agentic_turn`, never
writes anything, and never itself flips `settings.AGENTIC_SERIES_
ACTIVATION`/`settings.AGENTIC_ROUTING_ENABLED`. It answers "given
everything this process can currently observe about this series (and,
for a couple of process-wide signals, about the agentic system as a
whole), is it safe for agentic routing to be live for this series right
now" -- built entirely on top of Phases 6-9's already-existing
diagnostics (`agentic.promotion_evaluator.get_latest_promotion_
decisions`, `agentic.health.compute_agentic_health`, `services.
discovery_telemetry.get_agentic_metrics`, `settings.is_agentic_
activated`), plus one small new self-check of its own (`agentic.cache`'s
`AgenticTurnCache`, exercised against throwaway data only -- never a
real series' cache).

`ready` is deliberately strict: `True` only when every one of the five
boolean fields below is `True` *and* `safety_violations_recent == 0`.
Because `safety_violations_recent` mirrors `services.discovery_
telemetry`'s process-wide, lifetime `agentic_safety_violations` counter
(there is no per-series, per-time-window violation store anywhere in
this codebase -- see `agentic.health`'s own docstring for the identical
caveat on its own `safety_violations` field), a process that has ever
logged a single safety violation for *any* series will show `ready=
False` for *every* series afterward, until the process restarts. That
is intentionally conservative for this phase (a real production
deployment would eventually want a per-window/rolling counter instead)
rather than a bug: "no known-unsafe agentic decision has ever been
vetoed on this process" is a strictly stronger, honestly-computable bar
than "no *recent* one has", and Phase 10 does not add new persistence to
make the sharper claim.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_EXPECTED_METRIC_KEYS = frozenset(
    {
        "agentic_promotion_attempts",
        "agentic_promotion_use_agentic",
        "agentic_promotion_use_live",
        "agentic_promotion_rejected",
        "agentic_safety_violations",
        "agentic_cache_hits",
        "agentic_cache_misses",
        "agentic_turn_invocations",
        "agentic_turn_failures",
    }
)

_FAIL_SOFT_READINESS: dict = {
    "promotion_history_ok": False,
    "safety_violations_recent": 0,
    "determinism_ok": False,
    "activation_state": False,
    "metrics_ok": False,
    "cache_ok": False,
    "ready": False,
}


def compute_agentic_readiness(series_id: int, *, db_session=None) -> dict:
    """Returns a readiness report indicating whether the agentic system
    is safe to enable/keep enabled for this series:

        {"promotion_history_ok": bool,       # the promotion-history read path itself works
         "safety_violations_recent": int,    # process-wide lifetime count -- see module docstring
         "determinism_ok": bool,             # agentic.health's own determinism flag for this series
         "activation_state": bool,           # settings.is_agentic_activated(series_id) right now
         "metrics_ok": bool,                 # observability counters are present and well-formed
         "cache_ok": bool,                   # agentic.cache.AgenticTurnCache self-check passed
         "ready": bool}                      # every field above True AND zero violations

    Fail-soft: any exception (a broken `db_session`, a broken dependency,
    etc.) yields the same shape with every boolean `False`, the count at
    `0`, and `ready` `False`, rather than raising.
    """
    try:
        from agentic.health import compute_agentic_health
        from agentic.promotion_evaluator import get_latest_promotion_decisions
        from services.discovery_telemetry import get_agentic_metrics
        from settings import is_agentic_activated

        promotion_history_ok = True
        try:
            latest_decisions = get_latest_promotion_decisions(series_id, db_session=db_session)
            if not isinstance(latest_decisions, dict):
                promotion_history_ok = False
        except Exception:
            logger.exception(
                "compute_agentic_readiness: promotion-history read failed for series_id=%s", series_id
            )
            promotion_history_ok = False

        health = compute_agentic_health(series_id, db_session=db_session)
        determinism_ok = bool(health.get("determinism_ok", False))

        metrics = get_agentic_metrics()
        metrics_ok = (
            isinstance(metrics, dict)
            and _EXPECTED_METRIC_KEYS.issubset(metrics.keys())
            and all(isinstance(metrics.get(key), int) for key in _EXPECTED_METRIC_KEYS)
        )
        safety_violations_recent = int(metrics.get("agentic_safety_violations", 0)) if isinstance(metrics, dict) else 0

        activation_state = bool(is_agentic_activated(series_id))
        cache_ok = _self_check_cache()

        ready = bool(
            promotion_history_ok
            and determinism_ok
            and activation_state
            and metrics_ok
            and cache_ok
            and safety_violations_recent == 0
        )

        return {
            "promotion_history_ok": promotion_history_ok,
            "safety_violations_recent": safety_violations_recent,
            "determinism_ok": determinism_ok,
            "activation_state": activation_state,
            "metrics_ok": metrics_ok,
            "cache_ok": cache_ok,
            "ready": ready,
        }
    except Exception:
        logger.exception(
            "compute_agentic_readiness failed for series_id=%s; returning fail-soft report", series_id
        )
        return dict(_FAIL_SOFT_READINESS)


def _self_check_cache() -> bool:
    """A cheap, self-contained smoke test of `agentic.cache.
    AgenticTurnCache` against throwaway probe data -- never a real
    series' cache, never persisted, never shared with any live caller.
    `True` only if a fresh cache instance memoizes correctly (a second
    lookup for the same key returns the first call's value without
    invoking `compute_fn` again); `False` on any mismatch or exception
    ("cache malfunction detected", per this module's own spec).
    """
    try:
        from agentic.cache import AgenticTurnCache

        cache = AgenticTurnCache()
        call_count = {"n": 0}

        def _compute():
            call_count["n"] += 1
            return "agentic-readiness-probe-value"

        first = cache.get_or_set_promotion("__agentic_readiness_probe__", _compute)
        second = cache.get_or_set_promotion("__agentic_readiness_probe__", _compute)
        return first == "agentic-readiness-probe-value" and second == first and call_count["n"] == 1
    except Exception:
        logger.exception("compute_agentic_readiness: AgenticTurnCache self-check failed")
        return False
