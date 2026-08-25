"""Phase 2 dual-write shadow storage, final Phase 2 scaffolding block
(`discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`'s
settled architecture, not re-litigated here): the only read/write path for
the `AgenticConfidenceDecision`/`AgenticGateDecision` shadow tables
(`models.py`; `agentic_confidence_decisions`/`agentic_gate_decisions`).

Mirrors `services/agentic_skeleton_preview_store.py`'s shape and
guarantees exactly (see that module's own docstring for the shared
rationale): each `store_agentic_*` function performs a real INSERT +
commit, but scoped entirely to its own new, dedicated shadow table --
never reading from or writing to `SeriesSkeleton.skeleton_json` (the live
table), `probes_json` (not implemented anywhere yet), `confidence_
engine.py`, or the live `evaluate_belongs_to_series_gate` gate logic in
`agents/series_agent.py`. Both writers are fail-soft: neither ever raises
back to its caller (`agents/series_agent.py`'s dry-run block, itself
already wrapped in its own fail-soft try/except -- this is defense in
depth, not the only guard), logging any failure via `services.discovery_
telemetry.record_agentic_confidence_gate_error` instead. Both readers are
pure, fail-soft (`[]` on error).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from database import SessionLocal
from models import AgenticConfidenceDecision, AgenticGateDecision

logger = logging.getLogger(__name__)


def _serialize_timestamp(value) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat()


def _safe_sort_key(value) -> float:
    """Phase 6 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
    phase1_evaluation.md`, not re-litigated here): a book_number that
    isn't a real `int`/`float` sorts to `+inf` -- last, deterministically
    -- rather than raising `TypeError` when compared against a genuine
    number, or against another malformed value of a different type.
    """
    return value if isinstance(value, (int, float)) else float("inf")


def _book_number_timestamp_sort_key(entry: dict) -> tuple:
    """Phase 6: shared, fail-soft sort key for `get_agentic_confidence_
    history`/`get_agentic_gate_history`'s stability guarantee --
    `(book_number ASC, timestamp ASC, id ASC)`. `id` (each table's real,
    always-unique primary key) is an intentional addition beyond the
    letter of the Phase 6 spec's "book_number ASC, timestamp ASC": it's
    what actually makes ordering fully deterministic even when two rows
    share an identical `book_number`/`timestamp` pair, rather than
    leaving that case to depend on Python's merely-stable (not
    independently reproducible) sort against whatever order the
    database happened to return rows in.
    """
    book_number = entry.get("book_number")
    timestamp = entry.get("timestamp")
    return (
        _safe_sort_key(book_number),
        timestamp if isinstance(timestamp, str) else "",
        entry.get("id") or 0,
    )


def _latest_by_book_number(history: list[dict]) -> dict:
    """Phase 6: shared reduction behind `get_latest_confidence_decisions`/
    `get_latest_gate_decisions` -- given a history list already sorted
    by `_book_number_timestamp_sort_key` (i.e. each book's own rows are
    in ascending `(timestamp, id)` order), returns `{book_number:
    latest_entry}` in ascending book_number order via a running-max
    reduction per book. Ties are resolved by the already-baked-in `id`
    tiebreak in that sort key, not re-litigated here.
    """
    latest: dict = {}
    for entry in history:
        book_number = entry.get("book_number")
        if book_number is None:
            continue
        # history is already (book_number, timestamp, id) ascending, so
        # the last entry seen for a given book_number is always its most
        # recent -- an unconditional overwrite is sufficient (no need to
        # re-compare keys the way get_latest_promotion_decisions does,
        # since that one also breaks ties on a field -- promotion_
        # outcome -- that isn't part of this sort key at all).
        latest[book_number] = entry
    return {book_number: latest[book_number] for book_number in sorted(latest, key=_safe_sort_key)}


def store_agentic_confidence(
    series_id: int,
    book_number,
    live_conf,
    agentic_conf,
    *,
    db_session: Session | None = None,
) -> None:
    """Inserts one row into the `agentic_confidence_decisions` shadow
    table, pairing `live_conf` (typically `_observe_live_pipeline`'s
    `confidence_snapshot` entry for `book_number`) against `agentic_conf`
    (typically the dry-run turn's matching `agentic_trace["confidence_
    traces"]` entry). Both are stored as-is, opaquely, in `live_
    confidence`/`agentic_confidence`.

    Pure write to the shadow table only. Never touches live `skeleton_
    json`/`probes_json`, never calls `confidence_engine.py`. Fail-soft:
    any exception is caught, logged via `services.discovery_telemetry.
    record_agentic_confidence_gate_error`, and swallowed -- this function
    never raises.

    `db_session`, when provided, is reused as-is; this function commits
    on it (the one write this makes) but never closes it -- same
    convention as every other Phase 1/2 harness function that accepts an
    optional session. When omitted, a session is opened internally,
    committed, and always closed before returning.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        row = AgenticConfidenceDecision(
            series_id=series_id,
            book_number=book_number,
            timestamp=datetime.utcnow(),
            live_confidence=live_conf if live_conf is not None else {},
            agentic_confidence=agentic_conf if agentic_conf is not None else {},
        )
        db.add(row)
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            from services.discovery_telemetry import record_agentic_confidence_gate_error

            record_agentic_confidence_gate_error(series_id, "confidence", str(exc))
        except Exception:
            logger.exception(
                "store_agentic_confidence: failed to store decision for series_id=%s "
                "and failed to log the error too; continuing",
                series_id,
            )
    finally:
        if not caller_supplied_db:
            db.close()


def store_agentic_gate(
    series_id: int,
    book_number,
    live_gate,
    agentic_gate,
    *,
    db_session: Session | None = None,
) -> None:
    """Inserts one row into the `agentic_gate_decisions` shadow table,
    pairing `live_gate` (typically `_observe_live_pipeline`'s `gate_
    snapshot` entry for `book_number`) against `agentic_gate` (typically
    the dry-run turn's matching `agentic_trace["gate_traces"]` entry).
    Both are stored as-is, opaquely, in `live_gate`/`agentic_gate`.

    Pure write to the shadow table only. Never touches live `skeleton_
    json`/`probes_json`, never calls the live `evaluate_belongs_to_
    series_gate` logic. Fail-soft: any exception is caught, logged via
    `services.discovery_telemetry.record_agentic_confidence_gate_error`,
    and swallowed -- this function never raises.

    `db_session`, when provided, is reused as-is; this function commits
    on it (the one write this makes) but never closes it -- same
    convention as every other Phase 1/2 harness function that accepts an
    optional session. When omitted, a session is opened internally,
    committed, and always closed before returning.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        row = AgenticGateDecision(
            series_id=series_id,
            book_number=book_number,
            timestamp=datetime.utcnow(),
            live_gate=live_gate if live_gate is not None else {},
            agentic_gate=agentic_gate if agentic_gate is not None else {},
        )
        db.add(row)
        db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            from services.discovery_telemetry import record_agentic_confidence_gate_error

            record_agentic_confidence_gate_error(series_id, "gate", str(exc))
        except Exception:
            logger.exception(
                "store_agentic_gate: failed to store decision for series_id=%s "
                "and failed to log the error too; continuing",
                series_id,
            )
    finally:
        if not caller_supplied_db:
            db.close()


def get_agentic_confidence_history(series_id: int, *, db_session: Session | None = None) -> list[dict]:
    """Read-only: returns every stored `agentic_confidence_decisions` row
    for `series_id`, sorted deterministically by `(book_number ASC,
    timestamp ASC, id ASC)` (Phase 6 -- see `_book_number_timestamp_
    sort_key`), as plain dicts:

        [{"id": ..., "series_id": ..., "book_number": ..., "timestamp": iso8601 | None,
          "live_confidence": ..., "agentic_confidence": ...}, ...]

    Pure read. Fails soft -- returns `[]` (logging the failure) rather
    than raising, matching every other Phase 1/2 read-only harness
    function's convention. `db_session`, when provided, is reused as-is
    and never closed by this function; when omitted, a session is opened
    internally and always closed before returning.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        rows = db.query(AgenticConfidenceDecision).filter(AgenticConfidenceDecision.series_id == series_id).all()
        entries = [
            {
                "id": row.id,
                "series_id": row.series_id,
                "book_number": row.book_number,
                "timestamp": _serialize_timestamp(row.timestamp),
                "live_confidence": row.live_confidence,
                "agentic_confidence": row.agentic_confidence,
            }
            for row in rows
        ]
        return sorted(entries, key=_book_number_timestamp_sort_key)
    except Exception:
        logger.exception("get_agentic_confidence_history failed for series_id=%s; returning empty list", series_id)
        return []
    finally:
        if not caller_supplied_db:
            db.close()


def get_latest_confidence_decisions(series_id: int, *, db_session: Session | None = None) -> dict:
    """Phase 6: `{book_number: latest_confidence_decision_dict}` -- the
    single most recent `get_agentic_confidence_history` row per
    `book_number`, keyed by `float` book_number, returned in ascending-
    book_number order. Fail-soft: any exception yields `{}`.
    """
    try:
        history = get_agentic_confidence_history(series_id, db_session=db_session)
        return _latest_by_book_number(history)
    except Exception:
        logger.exception(
            "get_latest_confidence_decisions failed for series_id=%s; returning empty dict", series_id
        )
        return {}


def get_agentic_gate_history(series_id: int, *, db_session: Session | None = None) -> list[dict]:
    """Read-only: returns every stored `agentic_gate_decisions` row for
    `series_id`, sorted deterministically by `(book_number ASC,
    timestamp ASC, id ASC)` (Phase 6 -- see `_book_number_timestamp_
    sort_key`), as plain dicts:

        [{"id": ..., "series_id": ..., "book_number": ..., "timestamp": iso8601 | None,
          "live_gate": ..., "agentic_gate": ...}, ...]

    Pure read. Fails soft -- returns `[]` (logging the failure) rather
    than raising. `db_session`, when provided, is reused as-is and never
    closed by this function; when omitted, a session is opened internally
    and always closed before returning.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        rows = db.query(AgenticGateDecision).filter(AgenticGateDecision.series_id == series_id).all()
        entries = [
            {
                "id": row.id,
                "series_id": row.series_id,
                "book_number": row.book_number,
                "timestamp": _serialize_timestamp(row.timestamp),
                "live_gate": row.live_gate,
                "agentic_gate": row.agentic_gate,
            }
            for row in rows
        ]
        return sorted(entries, key=_book_number_timestamp_sort_key)
    except Exception:
        logger.exception("get_agentic_gate_history failed for series_id=%s; returning empty list", series_id)
        return []
    finally:
        if not caller_supplied_db:
            db.close()


def get_latest_gate_decisions(series_id: int, *, db_session: Session | None = None) -> dict:
    """Phase 6: `{book_number: latest_gate_decision_dict}` -- the single
    most recent `get_agentic_gate_history` row per `book_number`, keyed
    by `float` book_number, returned in ascending-book_number order.
    Fail-soft: any exception yields `{}`.
    """
    try:
        history = get_agentic_gate_history(series_id, db_session=db_session)
        return _latest_by_book_number(history)
    except Exception:
        logger.exception("get_latest_gate_decisions failed for series_id=%s; returning empty dict", series_id)
        return {}
