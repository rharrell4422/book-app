"""Phase 10 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_
evaluation.md`, not re-litigated here): the finalization & hardening
layer's global invariant-enforcement check, run once at process startup
(`main.py`, fail-soft -- a violation is logged, never raised, and never
prevents the app from starting) and re-runnable on demand via `GET
/admin/agentic/startup-check`.

`enforce_agentic_invariants` is a cheap, pure sanity probe -- it never
touches a real series' data, never calls a live provider, and (with one
documented, deliberate exception below) never mutates process-wide
observability counters. It checks five independent invariants; each
runs in its own try/except so one broken invariant never prevents the
others from being checked, and every individual failure is logged by
name before the overall function returns `False`.

Deliberate exception to "never mutates counters": invariant 5
("promotion evaluator returns valid outcomes") calls the real `agentic.
promotion_evaluator.evaluate_promotion` once, with fixed inputs chosen
to land on the cheapest, safest outcome (`"use_live"` -- no candidate
"use_agentic" decision is ever formed, so `agentic.safety.validate_
agentic_decision`/`services.discovery_telemetry.record_agentic_safety_
violation` are never reached). That one real call does increment
`services.discovery_telemetry`'s process-wide `agentic_promotion_
attempts`/`agentic_promotion_use_live` counters by exactly 1 each, same
as any other real `evaluate_promotion` call would -- a negligible,
explainable blip in a lifetime counter, not a materially misleading one,
and the only way to genuinely verify "the promotion evaluator returns
one of the three valid outcome literals" rather than merely "the
function object is importable and callable".
"""

from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)

# Phase 10 spec's "all agentic modules import cleanly" -- the seven
# modules this package hosts (six moved from `services/` unchanged,
# `readiness.py` new in this same phase). Deliberately does NOT include
# this module itself (`invariants.py`) -- if this file failed to import,
# nothing calling `enforce_agentic_invariants` would even exist to run.
_AGENTIC_SUBMODULES = (
    "agentic.cache",
    "agentic.confidence_gate_store",
    "agentic.promotion_evaluator",
    "agentic.resolution",
    "agentic.safety",
    "agentic.health",
    "agentic.readiness",
)

_VALID_PROMOTION_OUTCOMES = frozenset({"use_live", "use_agentic", "reject_agentic"})


def enforce_agentic_invariants() -> bool:
    """Checks, in order (every check independently fail-soft):

    1. All seven `agentic.*` submodules listed above import cleanly.
    2. `services.discovery_telemetry.get_agentic_metrics()` returns a
       dict containing every one of the nine Phase 9 counter keys, each
       an `int` -- i.e. "metrics counters are initialized".
    3. `agentic.safety.validate_agentic_decision`/`validate_promotion_
       outcome` are both callable, and each produces its own documented
       result for a fixed, known-safe input (`validate_agentic_decision
       (None, None, None, None)` -> `True`; `validate_promotion_outcome
       ("use_live")` -> `True`; `validate_promotion_outcome("not-a-real
       -outcome")` -> `False`).
    4. `agentic.resolution.resolve_routing_decisions`, called against an
       out-of-order `book_number` input (and a series_id chosen to never
       collide with a real activation allowlist entry, so this check's
       result never depends on this process's real `settings.AGENTIC_
       SERIES_ACTIVATION`/`AGENTIC_ROUTING_ENABLED` values), returns both
       dicts with their keys already in ascending order.
    5. `agentic.promotion_evaluator.evaluate_promotion`, called with a
       fixed live/agentic pair designed to resolve to `"use_live"` (see
       module docstring for why that specific outcome was chosen),
       returns one of the three valid promotion-outcome literals.

    Returns `True` only if every one of the five checks above passed.
    Fail-soft: a violation (or an unexpected exception while checking)
    is logged by name and counted as that one check failing -- this
    function itself never raises, no matter what it finds broken.
    """
    checks = (
        ("agentic_modules_import_cleanly", _check_modules_import_cleanly),
        ("metrics_counters_initialized", _check_metrics_initialized),
        ("safety_validator_callable", _check_safety_validator_callable),
        ("resolution_layer_returns_sorted_keys", _check_resolution_sorted_keys),
        ("promotion_evaluator_returns_valid_outcomes", _check_promotion_evaluator_valid_outcome),
    )

    all_ok = True
    for name, check_fn in checks:
        try:
            passed = bool(check_fn())
        except Exception:
            logger.exception("enforce_agentic_invariants: check %r raised unexpectedly; treating as failed", name)
            passed = False
        if not passed:
            logger.error("enforce_agentic_invariants: invariant violated -- %s", name)
            all_ok = False
    return all_ok


def _check_modules_import_cleanly() -> bool:
    for module_name in _AGENTIC_SUBMODULES:
        importlib.import_module(module_name)
    return True


def _check_metrics_initialized() -> bool:
    from services.discovery_telemetry import get_agentic_metrics

    metrics = get_agentic_metrics()
    if not isinstance(metrics, dict):
        return False
    expected_keys = {
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
    if not expected_keys.issubset(metrics.keys()):
        return False
    return all(isinstance(metrics.get(key), int) for key in expected_keys)


def _check_safety_validator_callable() -> bool:
    from agentic.safety import validate_agentic_decision, validate_promotion_outcome

    if not callable(validate_agentic_decision) or not callable(validate_promotion_outcome):
        return False
    if validate_agentic_decision(None, None, None, None) is not True:
        return False
    if validate_promotion_outcome("use_live") is not True:
        return False
    if validate_promotion_outcome("not-a-real-outcome") is not False:
        return False
    return True


def _check_resolution_sorted_keys() -> bool:
    from agentic.resolution import resolve_routing_decisions

    # A series_id this large/negative is never a real one, so this
    # check's outcome never depends on the real activation allowlist --
    # see this module's own docstring.
    sentinel_series_id = -999_999_999
    out_of_order_confidence = {3.0: {"confidence": "medium"}, 1.0: {"confidence": "medium"}, 2.0: {"confidence": "medium"}}
    out_of_order_gate = {3.0: {}, 1.0: {}, 2.0: {}}

    resolved_confidence, resolved_gate = resolve_routing_decisions(
        sentinel_series_id, out_of_order_confidence, out_of_order_gate, {}
    )

    confidence_keys = list(resolved_confidence.keys())
    gate_keys = list(resolved_gate.keys())
    return confidence_keys == sorted(confidence_keys) and gate_keys == sorted(gate_keys)


def _check_promotion_evaluator_valid_outcome() -> bool:
    from agentic.promotion_evaluator import evaluate_promotion

    # Identical live/agentic confidence+gate -> no improvement, no
    # violation -> "use_live", the cheapest/safest outcome to provoke
    # (see module docstring for why this specific outcome matters here).
    outcome = evaluate_promotion(
        {"confidence": "medium"},
        {"overall": "medium"},
        {"belongs_to_series": True},
        {"belongs_to_series": True},
    )
    return outcome in _VALID_PROMOTION_OUTCOMES
