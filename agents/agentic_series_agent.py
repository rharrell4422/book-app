"""Phase 1 agentic discovery loop -- deterministic, shadow-mode ONLY.

Per `discovery_agentic_phase1_plan.md` / `discovery_agentic_phase1_evaluation.md`
(settled architecture -- see those docs for the full review trail, not
re-litigated here; in particular Round 1's "shadow mode first" consensus and
Round 2/3 item 13's "the live call site keeps reading from
`agents/series_agent.py`; this loop only populates diagnostics for the
eval harness in the meantime"), this module is the third and final Phase-1
implementation block, after RT-1b (`agentic_hooks.py`'s turn lifecycle) and
PB-5 (`agentic_hooks.py`'s shadow diagnostics, wired into
`confidence_engine.py`/`services/skeleton_store.py`/`agents/series_agent.py`).

`run_agentic_turn` runs entirely in parallel with the live pipeline:

- It is **never called** from `agents/series_agent.py`, `routers/books.py`,
  or anywhere else in the live request path. Nothing currently invokes this
  module; it exists as scaffolding for the Phase-1 evaluation harness the
  plan's later steps build on top of. Wiring it into anything live is
  explicitly out of scope here ("Do NOT proceed to Phase-2 promotion").
- It opens its own read-only DB session (or reuses one passed in
  `context["db"]`, e.g. from a test) and never calls `db.add`/`db.commit`/
  `db.flush` -- every query is a plain read. See the `finally` block below:
  a session this function opened itself is always closed, never committed.
- It never writes `SeriesSkeleton.skeleton_json`/`probes_json` or any other
  column on any model. The "Skeleton Merge Preview Phase" computes what a
  merge *would* produce via `services.skeleton_store.
  compute_skeleton_updates_merge` -- a pure function with no `db` argument
  at all, extracted from `apply_skeleton_updates` for exactly this purpose
  (see that function's docstring) -- and never calls `apply_skeleton_updates`
  or `_upsert_skeleton_row` itself.
- It never calls a live network provider (Serper/Apify/etc.). Real
  provider calls are inherently non-deterministic across time (ranking
  drift, quota/rate limits, result-set changes) and this loop's own tests
  require "same inputs -> same trace" (see `tests/test_agentic_series_agent.py`).
  The "Provider Probe Phase" therefore only exercises deterministic
  *query construction* -- the same query shape `discovery_engine`'s
  targeted/lookahead passes build -- and records it via the usual hooks
  with an empty/`None` result, rather than making a live HTTP call. Wiring
  a real (or fixture-recorded) provider into this phase is explicit future
  work, not this ticket.
- It never touches confidence/gate/merge *logic* -- every phase below
  calls the exact same, unmodified functions the live pipeline uses
  (`confidence_engine.compute_confidence`, `agents.series_agent.
  evaluate_belongs_to_series_gate`, `services.skeleton_store.
  compute_skeleton_updates_merge`), so there is exactly one implementation
  of each, not a shadow-loop copy that could silently drift from the real
  one.

`run_agentic_turn`'s only output is a plain trace dict (see its docstring
for the schema) -- nothing here is read back by anything routing/confidence/
skeleton-related; it exists purely for logging/diagnostics/evaluation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

import agentic_hooks
import confidence_engine
from agents.series_agent import (
    _build_owned_core_title_texts,
    _build_series_identity_sets,
    evaluate_belongs_to_series_gate,
)
from database import SessionLocal
from models import Book, Series, SeriesSkeleton
from services.skeleton_store import compute_skeleton_updates_merge

logger = logging.getLogger(__name__)


def _build_deterministic_probe_query(series_name: str, author: str, book_number) -> str:
    """Same deterministic query shape `discovery_engine`'s targeted/
    missing-volume-lookahead passes build ("<series> <author> book <N>")
    -- reused here for query-construction determinism only. See module
    docstring for why this phase never issues a live network call with
    this query.
    """
    try:
        number_str = str(int(book_number)) if float(book_number).is_integer() else str(book_number)
    except Exception:
        number_str = str(book_number)
    parts = [part for part in (series_name, author) if part]
    return f"{' '.join(parts)} book {number_str}".strip()


def _synthetic_candidate_for_entry(entry: dict, series_author: str) -> dict:
    """Builds a provider-candidate-shaped dict out of an existing skeleton
    entry, for the confidence/gate replay phases below. This is a shadow-
    mode-only construction, not a live discovery result -- there is no
    live candidate to replay against without a real network call (see
    module docstring), so this loop instead exercises the exact same
    scoring/gating functions against the series' own current world model
    to prove they behave deterministically. `confidence`/`series_number_hint`
    are chosen to mirror how a library-sourced vs. agent-discovered
    skeleton entry would typically have been tagged.
    """
    is_library = entry.get("source_class", "library") == "library"
    return {
        "title": entry.get("title") or "",
        "authors": [series_author] if series_author else [],
        "isbn13": None,
        "series_number": entry.get("book_number"),
        "series_number_hint": entry.get("book_number"),
        "confidence": "targeted" if is_library else "author_fallback",
        "source": "skeleton_replay",
    }


def run_agentic_turn(series_id: int, context: dict) -> dict:
    """Executes one deterministic agentic turn in shadow mode for
    `series_id`. Returns a structured trace dict:

        {
          "series_id": int,
          "turn_timestamp": iso8601 str,
          "provider_calls": [...],
          "probes": [...],
          "confidence_traces": [...],
          "gate_traces": [...],
          "skeleton_merge_previews": [...],
          "reasoning_steps": [...],
        }

    Every list entry is a plain dict -- no freeform strings. Does NOT
    modify any persistent state: no DB writes, no skeleton_json/
    probes_json changes, no live provider calls, no effect on
    `agents/series_agent.py`'s routing/confidence/gate/merge behavior.
    Deterministic: given the same DB state, calling this twice for the
    same `series_id` produces the same trace (see
    `tests/test_agentic_series_agent.py`).

    `context` accepts the same optional keys `agentic_hooks.begin_turn`
    already understands (`telemetry`, `user_id`, etc.), plus an optional
    `db` (a `Session` to reuse instead of opening a new one -- primarily
    for tests; a session passed this way is never closed or committed by
    this function, matching the "never write" contract above).
    """
    context = agentic_hooks.begin_turn({**(context or {}), "series_id": series_id})

    trace: dict = {
        "series_id": series_id,
        "turn_timestamp": context.get("timestamp"),
        "provider_calls": [],
        "probes": [],
        "confidence_traces": [],
        "gate_traces": [],
        "skeleton_merge_previews": [],
        "reasoning_steps": [],
    }

    caller_supplied_db = context.get("db")
    db: Session = caller_supplied_db if caller_supplied_db is not None else SessionLocal()
    try:
        series = db.query(Series).filter(Series.id == series_id).first()
        if series is None:
            agentic_hooks.record_reasoning_step(
                context, {"phase": "precheck", "decision": "stop", "reason": "series-not-found"}
            )
            trace["reasoning_steps"] = list(context.get("reasoning_steps") or [])
            agentic_hooks.end_turn(context)
            return trace

        series_name = str(series.name or "")
        series_author = str(series.author or "").strip()

        active_books = [
            book
            for book in db.query(Book).filter(Book.series_id == series_id).all()
            if (book.record_status or "") != "deleted"
        ]
        highest_owned_book_number = max(
            (
                int(float(book.book_number))
                for book in active_books
                if book.book_number is not None and not bool(book.is_missing)
            ),
            default=None,
        )
        known_series_titles, _known_series_numbers, _known_bare_titles = _build_series_identity_sets(active_books)
        owned_core_title_texts = _build_owned_core_title_texts(active_books)

        skeleton_row = db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == series_id).first()
        skeleton_entries = (
            list(skeleton_row.skeleton_json) if skeleton_row is not None and isinstance(skeleton_row.skeleton_json, list) else []
        )

        candidate_numbers = sorted(
            {
                entry.get("book_number")
                for entry in skeleton_entries
                if isinstance(entry, dict) and entry.get("book_number") is not None
            }
        )

        agentic_hooks.record_reasoning_step(
            context,
            {
                "phase": "provider_selection",
                "chosen": "serper_then_apify",
                "reason": "matches_live_escalation_order",
                "candidate_count": len(candidate_numbers),
            },
        )

        skeleton_updates_preview: list[dict] = []

        for book_number in candidate_numbers:
            entry = next(
                (e for e in skeleton_entries if isinstance(e, dict) and e.get("book_number") == book_number), {}
            )
            title = entry.get("title") or f"{series_name} Book {book_number}".strip()

            # ---- 2.2 Provider Probe Phase ----
            # Serper first, Apify only as a fallback -- same escalation
            # order as the live pipeline (see discovery_engine.py) -- but
            # see module docstring for why this is query-construction only,
            # not a live call.
            query = _build_deterministic_probe_query(series_name, series_author, book_number)
            agentic_hooks.record_tool_call(context, "serper", query, None)
            agentic_hooks.shadow_probe(context, "serper", query, None)
            trace["provider_calls"].append({"book_number": book_number, "provider": "serper", "query": query})
            trace["probes"].append({"book_number": book_number, "provider": "serper", "query": query, "result_size": 0})
            agentic_hooks.record_reasoning_step(
                context, {"phase": "probe", "book_number": book_number, "provider": "serper", "query": query}
            )

            synthetic_candidate = _synthetic_candidate_for_entry(entry, series_author)

            # ---- 2.3 Confidence Evaluation Phase ----
            # Deterministic replay: the exact same inputs fed to
            # confidence_engine.compute_confidence twice, proving that
            # function's output is a pure function of its inputs (see
            # confidence_engine.py -- it makes no LLM/network/DB calls).
            before_conf = confidence_engine.compute_confidence(
                series_id,
                skeleton_entries,
                [synthetic_candidate],
                {"malformed_books": []},
                series_name=series_name,
                series_author=series_author,
            )
            after_conf = confidence_engine.compute_confidence(
                series_id,
                skeleton_entries,
                [synthetic_candidate],
                {"malformed_books": []},
                series_name=series_name,
                series_author=series_author,
            )
            before_scored = (before_conf.get("confidence") or [{}])[0]
            after_scored = (after_conf.get("confidence") or [{}])[0]
            before_dims = {
                "provider_confidence": before_scored.get("provider_confidence"),
                "title_confidence": before_scored.get("title_confidence"),
                "number_confidence": before_scored.get("number_confidence"),
                "series_alignment_confidence": before_scored.get("series_alignment_confidence"),
            }
            after_dims = dict(before_dims)
            after_dims.update(
                {
                    "provider_confidence": after_scored.get("provider_confidence"),
                    "title_confidence": after_scored.get("title_confidence"),
                    "number_confidence": after_scored.get("number_confidence"),
                    "series_alignment_confidence": after_scored.get("series_alignment_confidence"),
                    "overall": after_scored.get("overall"),
                }
            )
            agentic_hooks.shadow_confidence_trace(context, book_number, before_dims, after_dims)
            trace["confidence_traces"].append({"book_number": book_number, "before": before_dims, "after": after_dims})

            # ---- 2.4 Gate Evaluation Phase ----
            # Exact same, unmodified gate function the live loop calls
            # (see agents/series_agent.py) -- no reimplementation here.
            gate_result = evaluate_belongs_to_series_gate(
                title=title,
                inferred_number=book_number,
                candidate_confidence=synthetic_candidate.get("confidence"),
                series_name=series_name,
                known_series_titles=known_series_titles,
                owned_core_title_texts=owned_core_title_texts,
                highest_owned_book_number=highest_owned_book_number,
            )
            gate_input = {
                "title": title,
                "explicit_series_match": gate_result["explicit_series_match"],
                "partial_match": gate_result["partial_match"],
                "continues_numbering": gate_result["continues_numbering"],
                "targeted_with_number": gate_result["targeted_with_number"],
                "is_universe_tie_in": gate_result["is_universe_tie_in"],
                "is_compilation_of_owned_titles": gate_result["is_compilation_of_owned_titles"],
            }
            gate_output = {"belongs_to_series": gate_result["belongs_to_series"]}
            agentic_hooks.shadow_gate_trace(context, book_number, gate_input, gate_output)
            trace["gate_traces"].append({"book_number": book_number, "gate_input": gate_input, "gate_output": gate_output})

            # Only entries that both clear the gate and score at least
            # "medium" overall are fed into the merge preview below --
            # mirrors the live loop's own bar for touching a skeleton row
            # (see agents/series_agent.py's needs_review/accept routing),
            # without reimplementing that routing itself.
            if gate_result["belongs_to_series"] and after_scored.get("overall") in ("high", "medium"):
                skeleton_updates_preview.append(
                    {
                        "book_number": book_number,
                        "title": title,
                        "status": entry.get("status") or "unconfirmed",
                        "confidence": after_scored.get("overall"),
                    }
                )

        # ---- 2.5 Skeleton Merge Preview Phase ----
        # Pure computation, no `db` argument at all -- see
        # compute_skeleton_updates_merge's own docstring. Never calls
        # apply_skeleton_updates/_upsert_skeleton_row, so this can never
        # write skeleton_json.
        preview_entries = compute_skeleton_updates_merge(
            skeleton_entries, skeleton_updates_preview, now=datetime.now(timezone.utc), series_id=series_id
        )
        agentic_hooks.shadow_skeleton_merge_trace(context, skeleton_entries, preview_entries)
        trace["skeleton_merge_previews"].append(
            {
                "before": skeleton_entries,
                "after": preview_entries,
                "before_count": len(skeleton_entries),
                "after_count": len(preview_entries),
            }
        )

        agentic_hooks.record_world_model_update(
            context,
            {
                "series_id": series_id,
                "books_changed": 0,
                "numbers_changed": [],
                "confidence_changes": [],
                "note": "shadow-preview-only; nothing written",
            },
        )
    finally:
        if caller_supplied_db is None:
            db.close()

    trace["reasoning_steps"] = list(context.get("reasoning_steps") or [])
    agentic_hooks.end_turn(context)
    return trace
