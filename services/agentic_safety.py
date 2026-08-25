"""Phase 7 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_
evaluation.md`'s settled architecture, not re-litigated here): the
agentic safety & guardrail layer.

Phases 3-6 built the decision authority (`services/agentic_promotion_
evaluator.evaluate_promotion`), the resolution layer (`services/agentic_
resolution.resolve_routing_decisions`), and determinism hardening on top
of both. This module adds one more, independent check on top of all of
that: given a live/agentic confidence+gate pair, is it actually *safe* to
apply the agentic side at all, regardless of what `evaluate_promotion`'s
own improvement/violation rules already concluded?

`validate_agentic_decision` is deliberately self-contained -- it does
NOT import or call anything from `services/agentic_promotion_evaluator.py`
(despite substantial overlap with that module's own `evaluate_promotion`
rules 1-3), so that `services/agentic_resolution.py`'s defense-in-depth
re-check (Phase 7 section 3) never depends on the promotion evaluator
module at all, and so this module has zero risk of circular imports with
either of its two callers. Some duplication of `_confidence_dims`/
`_belongs_to_series`/`_grade_rank`-shaped logic versus `agentic_promotion_
evaluator.py` is intentional here, matching this codebase's established
convention (see e.g. `services/agentic_confidence_gate_store.py` vs.
`services/agentic_skeleton_preview_store.py`) of independent, mirrored
modules rather than shared private helpers.

Both functions here are pure: no DB access, no provider calls, no I/O,
deterministic (same inputs -> same output), and never raise -- any
unexpected internal error is treated as "unsafe" (`False`) rather than
propagating, since "I couldn't prove this is safe" and "this is unsafe"
should fail the same way for a guardrail.
"""

from __future__ import annotations

import math

# Same grade vocabulary/ranking as `services/agentic_promotion_evaluator.
# py`'s own `_GRADE_RANK` (see that module's docstring for the "unverified"
# placement rationale, not repeated here) -- duplicated per this module's
# own docstring above.
_GRADE_RANK = {"zero": 0, "low": 1, "unverified": 1, "medium": 2, "high": 3}

_CONFIDENCE_DIMENSIONS = (
    "overall",
    "provider_confidence",
    "title_confidence",
    "number_confidence",
    "series_alignment_confidence",
)

_VALID_PROMOTION_OUTCOMES = frozenset({"use_live", "use_agentic", "reject_agentic"})

# A book_number this large is never legitimate (the largest real series in
# this codebase's data is nowhere close) -- used only to catch a grossly
# malformed/corrupted "impossible jump" value, not to encode any real
# business limit.
_MAX_PLAUSIBLE_BOOK_NUMBER = 100_000


def _confidence_dims(conf) -> dict:
    """Canonicalizes one side's confidence dict onto `_CONFIDENCE_
    DIMENSIONS`' keys (see `services/agentic_promotion_evaluator.py`'s
    `_confidence_dims` for the shared rationale) -- `{}` for anything
    that isn't a dict at all.
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


def _belongs_to_series(gate) -> bool | None:
    """Reads `belongs_to_series` from either shape gate dicts show up in
    across this codebase -- `None` when neither shape provides one.
    """
    if not isinstance(gate, dict):
        return None
    if "belongs_to_series" in gate:
        return gate.get("belongs_to_series")
    gate_output = gate.get("gate_output")
    if isinstance(gate_output, dict):
        return gate_output.get("belongs_to_series")
    return None


def _grade_rank(value) -> int:
    return _GRADE_RANK.get(value, -1)


def _is_plausible_book_number(value) -> bool:
    """`True` only for a real, finite, non-negative, non-absurd number --
    used for the "impossible book_number jump" check below when an
    (opaque, optional) `"book_number"` key happens to be present in one
    of the agentic dicts. `bool` is deliberately excluded even though
    `isinstance(True, int)` is `True` in Python -- a boolean is never a
    legitimate book_number.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if not math.isfinite(value):
        return False
    return 0 <= value <= _MAX_PLAUSIBLE_BOOK_NUMBER


def validate_agentic_decision(live_conf, agentic_conf, live_gate, agentic_gate) -> bool:
    """Returns `True` if the agentic (confidence, gate) pair is SAFE to
    apply in place of the live one; `False` if not. Called from two
    places: `services/agentic_promotion_evaluator.evaluate_promotion`
    (before it would otherwise return `"use_agentic"`) and `services/
    agentic_resolution.resolve_routing_decisions` (defense-in-depth,
    re-checked independently even for a decision `evaluate_promotion`
    already approved).

    Checks (`False`/unsafe on the first one that fails):

    1. Malformed structures: each of the four arguments must be either
       `None` ("no opinion") or a `dict` -- anything else (a string,
       list, number, ...) can't be a decision at all.
    2. Missing required fields: if `agentic_conf`/`agentic_gate` is a
       `dict` at all, the agentic side must provide *some* usable
       opinion -- at least one recognized confidence dimension or a
       `belongs_to_series` opinion. A dict that parses to nothing is not
       a decision, independent of what `live_conf`/`live_gate` say (a
       stricter, standalone version of the "degenerate decision" check
       below, which only fires when live has an opinion to compare
       against).
    3. Negative confidence values: no numeric field anywhere in
       `agentic_conf` may be negative -- a real confidence grade is
       always one of `_GRADE_RANK`'s known strings, so a negative number
       showing up at all is a corrupted/malformed value, not a low
       score.
    4. Impossible book_number jumps: if either agentic dict happens to
       carry an (opaque, optional) `"book_number"` key, per `services/
       agentic_confidence_gate_store.py`'s trace shapes, it must be a
       real, finite, non-negative, non-absurd number (see
       `_is_plausible_book_number`) -- this function's signature has no
       separate "current" book_number to diff against, so this is a
       sanity check on the value itself, not a delta.
    5. Unrecognized confidence grades: every value present in
       `agentic_conf`'s recognized dimensions must be one of
       `_GRADE_RANK`'s known strings -- an unrecognized grade is
       malformed, not merely "low".
    6. Malformed gate opinion: `agentic_gate`'s `belongs_to_series` (if
       present at all) must be a real `bool`, not a truthy/falsy stand-in.
    7. Determinism invariant: a degenerate agentic opinion (no confidence
       dims AND no gate opinion) while the live side has one of its own
       is unsafe to promote over live -- mirrors `evaluate_promotion`'s
       own rule 1, duplicated here (see module docstring) so this
       function needs no import from that module.
    8. Must not contradict / must not reduce provider agreement: for
       every confidence dimension both sides report, the agentic grade
       must rank `>=` the live grade.
    9. Gate contradiction: if both sides express an opinion on
       `belongs_to_series` and they disagree, that's unsafe.

    Pure function: no DB, no I/O, no provider calls, deterministic. Any
    unexpected internal error is treated as unsafe (`False`) rather than
    raising.
    """
    try:
        # 1. Malformed structures.
        for value in (live_conf, agentic_conf, live_gate, agentic_gate):
            if value is not None and not isinstance(value, dict):
                return False

        live_dims = _confidence_dims(live_conf)
        agentic_dims = _confidence_dims(agentic_conf)
        live_belongs = _belongs_to_series(live_gate)
        agentic_belongs = _belongs_to_series(agentic_gate)

        # 2. Missing required fields: the agentic side must offer
        # *something* to be a decision at all, whenever it was passed a
        # dict in the first place (as opposed to None, "no opinion").
        agentic_offered_a_structure = isinstance(agentic_conf, dict) or isinstance(agentic_gate, dict)
        agentic_has_no_opinion = not agentic_dims and agentic_belongs is None
        if agentic_offered_a_structure and agentic_has_no_opinion:
            return False

        # 3. Negative confidence values -- any numeric field anywhere in
        # agentic_conf, not just the recognized dimensions (a corrupted
        # score could show up under any key).
        if isinstance(agentic_conf, dict):
            for value in agentic_conf.values():
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0:
                    return False

        # 4. Impossible book_number jumps (opaque, optional field).
        for source in (agentic_conf, agentic_gate):
            if isinstance(source, dict) and "book_number" in source:
                if not _is_plausible_book_number(source["book_number"]):
                    return False

        # 5. Unrecognized confidence grades.
        for value in agentic_dims.values():
            if not isinstance(value, str) or value not in _GRADE_RANK:
                return False

        # 6. Malformed gate opinion.
        if agentic_belongs is not None and not isinstance(agentic_belongs, bool):
            return False

        # 7. Determinism invariant.
        live_has_an_opinion = bool(live_dims) or live_belongs is not None
        if agentic_has_no_opinion and live_has_an_opinion:
            return False

        # 8. Must not contradict / must not reduce provider agreement.
        shared_keys = set(live_dims) & set(agentic_dims)
        for key in shared_keys:
            if _grade_rank(agentic_dims[key]) < _grade_rank(live_dims[key]):
                return False

        # 9. Gate contradiction.
        if live_belongs is not None and agentic_belongs is not None and bool(live_belongs) != bool(agentic_belongs):
            return False

        return True
    except Exception:
        return False


def validate_promotion_outcome(outcome) -> bool:
    """Returns `True` only for the three outcome strings `evaluate_
    promotion` is allowed to return (`"use_live"`, `"use_agentic"`,
    `"reject_agentic"`); `False` for anything else -- `None`, an unknown
    string, a non-string, etc.

    Pure, deterministic, never raises.
    """
    try:
        return outcome in _VALID_PROMOTION_OUTCOMES
    except Exception:
        return False
