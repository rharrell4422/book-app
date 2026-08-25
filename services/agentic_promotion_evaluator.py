"""Phase 3, candidate promotion (`discovery_agentic_phase1_plan.md`/
`discovery_agentic_phase1_evaluation.md`'s settled architecture, not
re-litigated here): the first module that decides, per book, whether an
agentic confidence/gate decision is *eligible* to be used in place of the
live one -- `evaluate_promotion` -- and persists that decision to the new
`AgenticPromotionDecision` shadow table (`models.py`) via
`store_promotion_decision`/`get_promotion_history`.

This module never calls a live provider, `confidence_engine.py`, or the
live `evaluate_belongs_to_series_gate` gate logic itself -- it only
compares the *outputs* of those, already computed by its caller
(`agents/series_agent.py`'s live routing path, gated by
`settings.AGENTIC_ROUTING_ENABLED`). `evaluate_promotion` is a pure
function: no DB, no I/O, no side effects -- same inputs always produce
the same outcome. `store_promotion_decision`/`get_promotion_history`
mirror `services/agentic_confidence_gate_store.py`'s shape and
guarantees exactly (same fail-soft write, same pure/fail-soft read; see
that module's own docstring, not repeated here) -- the only new table
here is `agentic_promotion_decisions`, which additionally records the
`promotion_outcome` string itself alongside the same live/agentic
confidence+gate pair those other tables already capture.

Contract for `evaluate_promotion`'s four dict arguments (deliberately
loose/duck-typed, matching what `agents/series_agent.py`'s live routing
block and `agents/agentic_series_agent.run_agentic_turn`'s
`confidence_traces`/`gate_traces` entries already produce):

- `live_conf`/`agentic_conf`: dicts that may contain any of
  `"overall"` (or, for `live_conf`, `"confidence"` as a synonym --
  `_observe_live_pipeline`'s `confidence_snapshot` entries use that key,
  since a `SeriesSkeleton` entry only ever stores one grade, not a
  per-dimension breakdown), `"provider_confidence"`, `"title_confidence"`,
  `"number_confidence"`, `"series_alignment_confidence"`. Only keys
  present in *both* dicts are compared -- a dimension only one side
  reports is never treated as a violation or an improvement on its own.
- `live_gate`/`agentic_gate`: dicts exposing a `"belongs_to_series"`
  boolean, either at the top level (`_observe_live_pipeline`'s
  `gate_snapshot` shape) or nested under `"gate_output"`
  (`agentic_series_agent`'s `gate_traces` entry shape) -- both are
  checked.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from database import SessionLocal
from models import AgenticPromotionDecision
from services.agentic_safety import validate_agentic_decision, validate_promotion_outcome

logger = logging.getLogger(__name__)

# "unverified" is confidence_engine.py's title_confidence-only grade
# (see that module's docstring) -- never emitted for the other three
# dimensions or for "overall". It folds into an overall "medium" ceiling
# there, but as a per-dimension grade in isolation it reflects "no
# corroborating skeleton entry to check against", not a confirmed
# positive signal -- ranked here just above "zero"/"low" and below
# "medium", a deliberately conservative placement for this comparison
# only (confidence_engine.py's own _LEVEL_RANK has no such entry at all,
# since it is only ever compared there after already folding into
# "medium"/"zero").
_GRADE_RANK = {"zero": 0, "low": 1, "unverified": 1, "medium": 2, "high": 3}

# Compared in this order for determinism only (dict iteration order in
# the returned "reasons" list, etc.) -- comparison itself is unordered
# (every shared key is checked).
_CONFIDENCE_DIMENSIONS = (
    "overall",
    "provider_confidence",
    "title_confidence",
    "number_confidence",
    "series_alignment_confidence",
)


def _grade_rank(value) -> int:
    """Unknown/`None`/malformed grades rank below every real grade
    (`-1`), so a side missing a value can never "win" a comparison
    against a side that has one -- see `evaluate_promotion`'s docstring
    for why that matters for the deterministic-invariant check.
    """
    return _GRADE_RANK.get(value, -1)


def _confidence_dims(conf: dict | None) -> dict:
    """Canonicalizes one side's confidence dict onto `_CONFIDENCE_
    DIMENSIONS`' keys, treating `live_conf`'s `"confidence"` key as a
    synonym for `"overall"` (see module docstring) and dropping anything
    not present at all (as opposed to synthesizing a `None`/`"zero"` --
    an absent key means "no opinion", not "worst possible grade").
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


def _belongs_to_series(gate: dict | None) -> bool | None:
    """Reads `belongs_to_series` from either shape gate dicts show up in
    across this codebase (see module docstring) -- `None` when neither
    shape provides one, meaning "no opinion" rather than "false".
    """
    if not isinstance(gate, dict):
        return None
    if "belongs_to_series" in gate:
        return gate.get("belongs_to_series")
    gate_output = gate.get("gate_output")
    if isinstance(gate_output, dict):
        return gate_output.get("belongs_to_series")
    return None


def evaluate_promotion(
    live_conf: dict,
    agentic_conf: dict,
    live_gate: dict,
    agentic_gate: dict,
    *,
    series_id: int | None = None,
    book_number=None,
    cache=None,
) -> str:
    """Pure decision function -- no DB, no provider calls, deterministic
    (`series_id`/`book_number` are optional and used for nothing but
    fail-soft safety-violation *logging* below -- see Phase 7 note).
    Returns one of `"use_live"`, `"use_agentic"`, `"reject_agentic"` per
    the Phase 1 plan's promotion rules (see module docstring for the
    input contract):

    Phase 8 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
    phase1_evaluation.md`, not re-litigated here) adds one more, entirely
    optional keyword: `cache`, a `services.agentic_cache.AgenticTurnCache`
    instance. When provided (and `book_number` is not `None`), the actual
    decision (steps 1-6 plus both Phase 7 safety gates below) is computed
    at most once per `book_number` for that cache's lifetime -- a second
    `evaluate_promotion(..., book_number=X, cache=same_cache)` call for
    the same `X` returns the first call's result without re-running any
    of the logic below (or re-logging a safety violation, if the first
    call triggered one). Omitting `cache` (the default) reproduces the
    exact pre-Phase-8 behavior: every call recomputes from scratch. This
    keyword changes nothing about *what* gets decided, only how many
    times the decision underneath gets computed.

    1. Deterministic-invariant check: an agentic decision that provides
       no usable confidence grade and no usable gate opinion at all --
       while the live side has at least one of those -- gives nothing to
       promote over live and is rejected outright, rather than silently
       falling through to "use_live" as if nothing were wrong.
    2. Required-fields check: for every confidence dimension *both*
       sides report, the agentic grade must rank `>=` the live grade
       (via `_GRADE_RANK`). Any single dimension ranking lower is a
       violation -- this is also where "must not reduce provider
       agreement" lives, since `"provider_confidence"` is just one of
       the dimensions this same check already covers; it is not a
       separate rule with different math.
    3. Gate-contradiction check: if both sides express an opinion on
       `belongs_to_series` and they disagree, that is a violation --
       "the agentic gate must not contradict the live gate".
    4. Any violation from 1-3 -> `"reject_agentic"`.
    5. Otherwise, if the agentic side ranks strictly higher on at least
       one shared confidence dimension, it's a *candidate* for
       `"use_agentic"` -- see Phase 7 step 6 below before that's final.
    6. Otherwise (no violation, no improvement) -> `"use_live"`.

    Phase 7 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
    phase1_evaluation.md`, not re-litigated here) adds two more gates on
    top of steps 1-6 above, neither of which changes this function's
    "pure, no DB, no provider calls" contract (`services.agentic_safety`'s
    two functions are themselves pure):

    - Before finalizing a `"use_agentic"` candidate from step 5,
      `services.agentic_safety.validate_agentic_decision` re-checks the
      same (live, agentic) pair against its own, independent safety
      rules (contradiction, malformed structures, negative values,
      missing fields, impossible book_number jumps, determinism
      invariants -- see that function's own docstring). Failing that
      check downgrades the outcome to `"reject_agentic"` -- a
      `"use_agentic"` candidate this function's own rules 1-3 already
      approved can still be vetoed here.
    - Immediately before returning, whatever outcome was decided (by
      steps 1-6 and the safety re-check above) is passed through
      `services.agentic_safety.validate_promotion_outcome` -- this
      function can only ever construct one of the three valid literal
      outcome strings itself, so that check can never actually fail in
      practice, but it's asserted explicitly per the Phase 7 spec rather
      than assumed.

    Either Phase 7 rejection logs a fail-soft `services.discovery_
    telemetry.record_agentic_safety_violation(series_id, book_number,
    reason)` call (a no-op, best-effort side-channel only -- a logging
    failure, or omitted `series_id`/`book_number`, never affects the
    returned decision).
    """
    if cache is not None and book_number is not None:
        try:
            return cache.get_or_set_promotion(
                book_number,
                lambda: _evaluate_once(live_conf, agentic_conf, live_gate, agentic_gate, series_id, book_number),
            )
        except Exception:
            # Phase 8 fail-soft: a broken/misbehaving cache must never
            # prevent a real decision from being computed -- fall back
            # to the uncached path below rather than raising.
            logger.exception(
                "evaluate_promotion: cache lookup failed for book_number=%s; recomputing without it", book_number
            )
    return _evaluate_once(live_conf, agentic_conf, live_gate, agentic_gate, series_id, book_number)


def _evaluate_once(
    live_conf: dict,
    agentic_conf: dict,
    live_gate: dict,
    agentic_gate: dict,
    series_id: int | None,
    book_number,
) -> str:
    """The actual, pure promotion decision (`evaluate_promotion`'s steps
    1-6 plus both Phase 7 safety gates) -- factored out so Phase 8's
    optional `cache` wrapping in `evaluate_promotion` above has a plain
    zero-argument closure to memoize. Not part of this module's public
    contract; call `evaluate_promotion` instead.
    """
    live_dims = _confidence_dims(live_conf)
    agentic_dims = _confidence_dims(agentic_conf)
    live_belongs = _belongs_to_series(live_gate)
    agentic_belongs = _belongs_to_series(agentic_gate)

    # Rule 1: a degenerate agentic decision (no confidence opinion AND no
    # gate opinion) can't be evaluated for improvement at all, but the
    # live side actually has something -- reject rather than silently
    # defer to "use_live" as if this were just "no improvement found".
    agentic_has_no_opinion = not agentic_dims and agentic_belongs is None
    live_has_an_opinion = bool(live_dims) or live_belongs is not None
    if agentic_has_no_opinion and live_has_an_opinion:
        outcome = "reject_agentic"
    else:
        # Rule 2 (+ "must not reduce provider agreement", same math):
        # every shared confidence dimension must rank agentic >= live.
        shared_confidence_keys = set(live_dims) & set(agentic_dims)
        required_fields_violation = any(
            _grade_rank(agentic_dims[key]) < _grade_rank(live_dims[key]) for key in shared_confidence_keys
        )

        # Rule 3: an explicit disagreement on series membership.
        gate_contradiction = (
            live_belongs is not None and agentic_belongs is not None and bool(live_belongs) != bool(agentic_belongs)
        )

        if required_fields_violation or gate_contradiction:
            outcome = "reject_agentic"
        else:
            improves_confidence = any(
                _grade_rank(agentic_dims[key]) > _grade_rank(live_dims[key]) for key in shared_confidence_keys
            )
            outcome = "use_agentic" if improves_confidence else "use_live"

    # Phase 7 step 1: an outcome of "use_agentic" is only final if the
    # independent safety guardrail agrees it's safe to apply.
    if outcome == "use_agentic" and not validate_agentic_decision(live_conf, agentic_conf, live_gate, agentic_gate):
        _log_safety_violation(series_id, book_number, "validate_agentic_decision rejected use_agentic candidate")
        outcome = "reject_agentic"

    # Phase 7 step 2: the outcome itself must be one of the three valid
    # literals -- can't actually fail given this function's own control
    # flow above, but asserted explicitly per the Phase 7 spec.
    if not validate_promotion_outcome(outcome):
        _log_safety_violation(series_id, book_number, f"invalid promotion outcome: {outcome!r}")
        outcome = "reject_agentic"

    # Phase 9 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
    # phase1_evaluation.md`, not re-litigated here): counted here, at
    # the end of `_evaluate_once`, rather than in the public
    # `evaluate_promotion` wrapper -- this is the one place that always
    # runs exactly once per book_number actually *computed* (a Phase 8
    # cache hit skips straight to the cached return value without ever
    # reaching this function again), so "agentic_promotion_attempts"
    # means "a decision was computed", matching this counter's own
    # docstring, not "evaluate_promotion was called" (that would double-
    # count every cache hit as a fresh attempt).
    _record_promotion_metric(outcome)

    return outcome


def _record_promotion_metric(outcome: str) -> None:
    """Fail-soft telemetry side-channel for the Phase 9 counter above --
    never raises. Deliberately a local helper (matching `_log_safety_
    violation`'s own convention just below) rather than importing
    `services.discovery_telemetry` at module load time, for the same
    reason every other telemetry call in this module is a function-
    scoped import: avoids any risk of a circular import at import time.
    """
    try:
        from services.discovery_telemetry import record_agentic_promotion_metric

        record_agentic_promotion_metric(outcome)
    except Exception:
        logger.exception("_record_promotion_metric: failed to record metric for outcome=%r", outcome)


def _log_safety_violation(series_id, book_number, reason: str) -> None:
    """Fail-soft telemetry side-channel shared by both Phase 7 rejection
    points in `evaluate_promotion` above -- never raises, and a missing
    `series_id`/`book_number` (the caller didn't opt into logging) is
    still logged as `None` rather than skipped, so a real bug that
    somehow produces an invalid outcome is never silently dropped just
    because the caller omitted the optional logging context.
    """
    try:
        from services.discovery_telemetry import record_agentic_safety_violation

        record_agentic_safety_violation(series_id, book_number, reason)
    except Exception:
        logger.exception(
            "evaluate_promotion: failed to log safety violation for series_id=%s book_number=%s", series_id,
            book_number,
        )


def store_promotion_decision(
    series_id: int,
    book_number,
    live_conf: dict,
    agentic_conf: dict,
    live_gate: dict,
    agentic_gate: dict,
    outcome: str,
    *,
    db_session: Session | None = None,
) -> None:
    """Inserts one row into `agentic_promotion_decisions`, recording the
    full live/agentic confidence+gate pair alongside `outcome` (whatever
    `evaluate_promotion` returned -- this function does not itself call
    or validate against that function, so a caller-supplied outcome
    string is stored verbatim).

    Pure write to the shadow table only. Never touches live `skeleton_
    json`/`probes_json`, never calls `confidence_engine.py` or the live
    gate logic. Fail-soft: any exception is caught, logged via
    `services.discovery_telemetry.record_agentic_promotion_error`, and
    swallowed -- this function never raises.

    `db_session`, when provided, is reused as-is; this function commits
    on it (the one write this makes) but never closes it -- same
    convention as every other Phase 1/2/3 store that accepts an optional
    session. When omitted, a session is opened internally, committed,
    and always closed before returning.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        row = AgenticPromotionDecision(
            series_id=series_id,
            book_number=book_number,
            timestamp=datetime.utcnow(),
            live_confidence=live_conf if live_conf is not None else {},
            agentic_confidence=agentic_conf if agentic_conf is not None else {},
            live_gate=live_gate if live_gate is not None else {},
            agentic_gate=agentic_gate if agentic_gate is not None else {},
            promotion_outcome=str(outcome or "use_live"),
        )
        db.add(row)
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            from services.discovery_telemetry import record_agentic_promotion_error

            record_agentic_promotion_error(series_id, str(exc))
        except Exception:
            logger.exception(
                "store_promotion_decision: failed to store decision for series_id=%s "
                "and failed to log the error too; continuing",
                series_id,
            )
    finally:
        if not caller_supplied_db:
            db.close()


def _build_preview_entry(entry: dict) -> dict:
    """Pure per-book construction step behind `build_activation_preview`
    below -- factored out (Phase 8) purely so that function's optional
    `cache` wrapping has a plain, spy-able zero-argument closure to
    memoize; the logic itself is unchanged from pre-Phase-8.
    """
    outcome = entry.get("promotion_outcome")
    use_agentic = outcome == "use_agentic"
    return {
        "outcome": outcome,
        "resolved_confidence": entry.get("agentic_confidence") if use_agentic else entry.get("live_confidence"),
        "resolved_gate": entry.get("agentic_gate") if use_agentic else entry.get("live_gate"),
    }


def build_activation_preview(series_id: int, *, db_session: Session | None = None, cache=None) -> dict:
    """Phase 4 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
    phase1_evaluation.md`'s settled architecture, not re-litigated
    here): read-only "what would routing look like if this series were
    activated" preview, backing `GET /admin/agentic/activation-preview/
    {series_id}`.

    Never calls `run_agentic_turn`/a live provider and never writes
    anything -- it works entirely off `get_latest_promotion_decisions`
    (Phase 6: the most recent stored row per `book_number`, itself built
    on `get_promotion_history`'s now-deterministic `(book_number ASC,
    timestamp ASC, id ASC)` ordering -- see that function's own
    docstring). For each of those, it recomputes what `agents/series_
    agent.py`'s live routing path would resolve to *if* `settings.
    is_agentic_activated(series_id)` were `True` -- i.e. the agentic
    side exactly when that row's own `promotion_outcome` was
    `"use_agentic"`, live otherwise -- regardless of whether this series
    is actually activated right now. Returns:

        {"activated": bool,  # the series' REAL current activation state
         "preview": {"<book_number>": {"outcome": ..., "resolved_confidence": ...,
                                         "resolved_gate": ...}, ...}}
         # preview keys are inserted in ascending book_number order (Phase 6).

    Fail-soft: any exception (e.g. a broken `db_session`) yields
    `{"activated": False, "preview": {}}` rather than raising.

    Phase 8: `get_latest_promotion_decisions` below already does the one
    bulk `get_promotion_history` query itself needs (a single query per
    `series_id`, not one per book -- see that function's own docstring),
    so this function is already O(n) in the number of distinct
    book_numbers, not O(n^2). The optional `cache` keyword (a `services.
    agentic_cache.AgenticTurnCache`) additionally memoizes each book's
    constructed preview entry (`_build_preview_entry` above) by
    `book_number`, so a caller that calls this function more than once
    for the same series within one turn/request, sharing one `cache`
    instance, skips re-constructing entries for book_numbers it already
    built. Omitting `cache` (the default) reproduces the exact
    pre-Phase-8 behavior.
    """
    try:
        from settings import is_agentic_activated

        latest_by_book_number = get_latest_promotion_decisions(series_id, db_session=db_session)

        preview: dict = {}
        for book_number, entry in latest_by_book_number.items():
            if cache is not None:
                try:
                    preview[str(book_number)] = cache.get_or_set_promotion(
                        book_number, lambda entry=entry: _build_preview_entry(entry)
                    )
                    continue
                except Exception:
                    # Phase 8 fail-soft: a broken cache must not drop this
                    # book_number's preview entry -- fall back below.
                    logger.exception(
                        "build_activation_preview: cache lookup failed for book_number=%s; "
                        "constructing without it",
                        book_number,
                    )
            preview[str(book_number)] = _build_preview_entry(entry)

        return {"activated": is_agentic_activated(series_id), "preview": preview}
    except Exception:
        logger.exception("build_activation_preview failed for series_id=%s; returning empty preview", series_id)
        return {"activated": False, "preview": {}}


def _safe_sort_key(value) -> float:
    """Phase 6 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
    phase1_evaluation.md`, not re-litigated here): a book_number that
    isn't a real `int`/`float` sorts to `+inf` -- last, deterministically
    -- rather than raising `TypeError` when compared against a genuine
    number, or against another malformed value of a different type.
    """
    return value if isinstance(value, (int, float)) else float("inf")


def _book_number_timestamp_sort_key(entry: dict) -> tuple:
    """Phase 6: shared, fail-soft sort key for `get_promotion_history`'s
    stability guarantee -- `(book_number ASC, timestamp ASC, id ASC)`.
    `id` (this table's real, always-unique primary key) is an
    intentional addition beyond the letter of the Phase 6 spec's
    "book_number ASC, timestamp ASC": it's what actually makes ordering
    fully deterministic even when two rows share an identical
    `book_number`/`timestamp` pair (e.g. two writes in the same
    transaction), rather than leaving that case to depend on Python's
    merely-stable (not independently reproducible) sort against
    whatever order the DB happened to return rows in.
    """
    book_number = entry.get("book_number")
    timestamp = entry.get("timestamp")
    return (
        _safe_sort_key(book_number),
        timestamp if isinstance(timestamp, str) else "",
        entry.get("id") or 0,
    )


def get_promotion_history(series_id: int, *, db_session: Session | None = None) -> list[dict]:
    """Read-only: returns every stored `agentic_promotion_decisions` row
    for `series_id`, sorted deterministically by
    `(book_number ASC, timestamp ASC, id ASC)` (see
    `_book_number_timestamp_sort_key`), as plain dicts:

        [{"id": ..., "series_id": ..., "book_number": ..., "timestamp": iso8601 | None,
          "live_confidence": ..., "agentic_confidence": ...,
          "live_gate": ..., "agentic_gate": ..., "promotion_outcome": ...}, ...]

    Phase 6 note: prior to this, rows came back ordered by `id ASC` only
    (i.e. insertion/chronological order across the whole series, not
    grouped by book) -- callers that group by `book_number` (`build_
    activation_preview`, `get_latest_promotion_decisions` below) now see
    a stable, reproducible order regardless of which book's turn wrote a
    decision first. Sorting happens in Python (after an unordered DB
    fetch) rather than via `ORDER BY` so the exact same fail-soft
    coercion in `_safe_sort_key` -- a malformed `book_number` sorts last
    instead of raising -- applies uniformly regardless of database
    backend.

    Pure read. Fails soft -- returns `[]` (logging the failure) rather
    than raising. `db_session`, when provided, is reused as-is and never
    closed by this function; when omitted, a session is opened internally
    and always closed before returning.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        rows = db.query(AgenticPromotionDecision).filter(AgenticPromotionDecision.series_id == series_id).all()
        entries = [
            {
                "id": row.id,
                "series_id": row.series_id,
                "book_number": row.book_number,
                "timestamp": (row.timestamp.replace(tzinfo=timezone.utc).isoformat() if row.timestamp else None),
                "live_confidence": row.live_confidence,
                "agentic_confidence": row.agentic_confidence,
                "live_gate": row.live_gate,
                "agentic_gate": row.agentic_gate,
                "promotion_outcome": row.promotion_outcome,
            }
            for row in rows
        ]
        return sorted(entries, key=_book_number_timestamp_sort_key)
    except Exception:
        logger.exception("get_promotion_history failed for series_id=%s; returning empty list", series_id)
        return []
    finally:
        if not caller_supplied_db:
            db.close()


def get_latest_promotion_decisions(
    series_id: int, *, db_session: Session | None = None, history: list[dict] | None = None
) -> dict:
    """Phase 6: `{book_number: latest_decision_dict}` -- the single most
    recent `get_promotion_history` row per `book_number`, keyed by
    `float` book_number and returned in ascending-book_number order.

    "Most recent" means the row with the greatest `(timestamp,
    promotion_outcome)` pair, per book -- `timestamp` is the primary
    ordering (a strictly later write is always "more recent"
    regardless of its outcome string), and `promotion_outcome`
    (lexicographic) only breaks an exact `timestamp` tie, per the Phase
    6 spec. Since `get_promotion_history` already sorts each book's own
    rows by `(timestamp ASC, id ASC)`, a plain running-max reduction
    over that per-book sequence is enough to land on the correct
    "latest" row deterministically, independent of whatever order the
    database itself returned rows in.

    Fail-soft: any exception (including one raised by `get_promotion_
    history` itself, though that function is already fail-soft) yields
    `{}` rather than raising.

    Phase 8: `get_promotion_history` is already a single bulk query per
    `series_id` (never one query per book -- see that function's own
    docstring), so this function was already a single DB round-trip.
    The optional `history` keyword lets a caller that has *already*
    fetched that same series' history (e.g. via its own `get_promotion_
    history` call, for some other reason, earlier in the same turn) pass
    it straight through and skip the query here entirely -- `db_session`
    is then never touched. Omitting `history` (the default, `None`)
    reproduces the exact pre-Phase-8 behavior: fetch fresh via
    `get_promotion_history`.
    """
    try:
        if history is None:
            history = get_promotion_history(series_id, db_session=db_session)
        latest: dict = {}
        for entry in history:
            book_number = entry.get("book_number")
            if book_number is None:
                continue
            candidate_key = (entry.get("timestamp") or "", str(entry.get("promotion_outcome") or ""))
            existing = latest.get(book_number)
            if existing is None or candidate_key >= (
                existing.get("timestamp") or "",
                str(existing.get("promotion_outcome") or ""),
            ):
                latest[book_number] = entry
        return {book_number: latest[book_number] for book_number in sorted(latest, key=_safe_sort_key)}
    except Exception:
        logger.exception("get_latest_promotion_decisions failed for series_id=%s; returning empty dict", series_id)
        return {}
