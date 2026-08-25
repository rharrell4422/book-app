"""Phase 6, agentic stability & determinism layer -- `discovery_agentic_
phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`'s settled
architecture (not re-litigated here), hardening ordering guarantees
across every agentic layer built in Phases 1-5 without changing which
outcome/value wins for any book.

Per the Phase 6 spec, this file needs to prove:

1. `services/agentic_promotion_evaluator.get_promotion_history` sorts by
   `(book_number ASC, timestamp ASC)`, and its new `get_latest_promotion_
   decisions` helper returns the single latest row per book_number, with
   deterministic tie-breaking (by `promotion_outcome`, lexicographically)
   for rows sharing an identical timestamp.
2. `services/agentic_confidence_gate_store.get_agentic_confidence_history`/
   `get_agentic_gate_history` sort the same way, and their new `get_
   latest_confidence_decisions`/`get_latest_gate_decisions` helpers return
   the single latest row per book_number.
3. `agents/series_agent.py`'s `_sorted_agentic_trace_list` normalizes a
   `confidence_traces`/`gate_traces` list into book_number-ascending
   order, failing soft (to `[]`) on missing/malformed input.
4. `agents/series_agent.py`'s `_sorted_book_number_dict` normalizes a
   `{book_number: value}` mapping into `float`-keyed, book_number-
   ascending order, failing soft (dropping malformed keys, `{}` for
   non-dict input).
5. `services/agentic_resolution.resolve_routing_decisions` always returns
   `(resolved_confidence, resolved_gate)` dicts with book_number-ascending
   key order, on every code path (flag off, not activated, activated).
6. `agents/series_agent.py`'s live routing path returns `result[
   "agentic_promotion"]["promotions"]` sorted by book_number ASC.
7. All of the above are stable under randomized input order and under
   duplicate timestamps -- and every new/modified function stays
   fail-soft under malformed input, never raising.
"""

import random
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import settings
from agents.series_agent import SeriesIntelligenceAgent, _sorted_agentic_trace_list, _sorted_book_number_dict
from database import Base
from models import AgenticConfidenceDecision, AgenticGateDecision, AgenticPromotionDecision, Book, Series
from services.agentic_confidence_gate_store import (
    get_agentic_confidence_history,
    get_agentic_gate_history,
    get_latest_confidence_decisions,
    get_latest_gate_decisions,
)
from services.agentic_promotion_evaluator import get_latest_promotion_decisions, get_promotion_history
from services.agentic_resolution import resolve_routing_decisions


class _InMemoryDbTestCase(unittest.TestCase):
    """Shared in-memory-SQLite fixture, matching every other Phase 1-5
    agentic test file's convention (see e.g. tests/test_agentic_promotion.py).
    """

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()


# ---------------------------------------------------------------------------
# 1. get_promotion_history / get_latest_promotion_decisions
# ---------------------------------------------------------------------------


class PromotionHistoryDeterminismTest(_InMemoryDbTestCase):
    def _add_promotion_row(self, book_number, timestamp, outcome="use_live"):
        row = AgenticPromotionDecision(
            series_id=self.series.id,
            book_number=book_number,
            timestamp=timestamp,
            live_confidence={},
            agentic_confidence={},
            live_gate={},
            agentic_gate={},
            promotion_outcome=outcome,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def test_promotion_history_sorted(self):
        # Inserted out of book_number order, and out of timestamp order
        # within a book -- get_promotion_history must still return
        # (book_number ASC, timestamp ASC).
        self._add_promotion_row(3.0, datetime(2026, 1, 1))
        self._add_promotion_row(1.0, datetime(2026, 1, 3))
        self._add_promotion_row(2.0, datetime(2026, 1, 2))
        self._add_promotion_row(1.0, datetime(2026, 1, 1))  # earlier row for book 1, added later

        history = get_promotion_history(self.series.id, db_session=self.db)
        book_numbers = [entry["book_number"] for entry in history]
        self.assertEqual(book_numbers, [1.0, 1.0, 2.0, 3.0])
        # Within book 1's two rows, timestamp ASC.
        book_one_timestamps = [entry["timestamp"] for entry in history if entry["book_number"] == 1.0]
        self.assertEqual(book_one_timestamps, sorted(book_one_timestamps))

    def test_latest_promotion_decisions(self):
        self._add_promotion_row(2.0, datetime(2026, 1, 1), outcome="use_live")
        self._add_promotion_row(2.0, datetime(2026, 1, 5), outcome="use_agentic")  # latest for book 2
        self._add_promotion_row(1.0, datetime(2026, 1, 2), outcome="reject_agentic")

        latest = get_latest_promotion_decisions(self.series.id, db_session=self.db)
        self.assertEqual(list(latest.keys()), [1.0, 2.0])  # ascending book_number
        self.assertEqual(latest[2.0]["promotion_outcome"], "use_agentic")
        self.assertEqual(latest[1.0]["promotion_outcome"], "reject_agentic")

    def test_latest_promotion_decisions_tie_break_by_outcome_lexicographically(self):
        # Two rows for the same book, identical timestamp, different
        # outcomes -- deterministic tie-break: the lexicographically
        # greater outcome string wins ("use_agentic" > "reject_agentic").
        tied_timestamp = datetime(2026, 3, 1, 12, 0, 0)
        self._add_promotion_row(1.0, tied_timestamp, outcome="reject_agentic")
        self._add_promotion_row(1.0, tied_timestamp, outcome="use_agentic")

        latest = get_latest_promotion_decisions(self.series.id, db_session=self.db)
        self.assertEqual(latest[1.0]["promotion_outcome"], "use_agentic")

    def test_latest_promotion_decisions_tie_break_independent_of_insertion_order(self):
        tied_timestamp = datetime(2026, 3, 1, 12, 0, 0)
        # Reverse insertion order versus the test above -- result must
        # be identical (the tie-break depends on the outcome string, not
        # on which row happened to be written first).
        self._add_promotion_row(1.0, tied_timestamp, outcome="use_agentic")
        self._add_promotion_row(1.0, tied_timestamp, outcome="reject_agentic")

        latest = get_latest_promotion_decisions(self.series.id, db_session=self.db)
        self.assertEqual(latest[1.0]["promotion_outcome"], "use_agentic")

    def test_promotion_history_empty_and_fail_soft(self):
        self.assertEqual(get_promotion_history(999999, db_session=self.db), [])
        self.assertEqual(get_latest_promotion_decisions(999999, db_session=self.db), {})

        broken_db = MagicMock()
        broken_db.query.side_effect = RuntimeError("query exploded")
        self.assertEqual(get_promotion_history(self.series.id, db_session=broken_db), [])
        self.assertEqual(get_latest_promotion_decisions(self.series.id, db_session=broken_db), {})


# ---------------------------------------------------------------------------
# 2. get_agentic_confidence_history / get_agentic_gate_history + latest_*
# ---------------------------------------------------------------------------


class ConfidenceGateHistoryDeterminismTest(_InMemoryDbTestCase):
    def _add_confidence_row(self, book_number, timestamp):
        row = AgenticConfidenceDecision(
            series_id=self.series.id,
            book_number=book_number,
            timestamp=timestamp,
            live_confidence={},
            agentic_confidence={"timestamp": timestamp.isoformat()},
        )
        self.db.add(row)
        self.db.commit()
        return row

    def _add_gate_row(self, book_number, timestamp):
        row = AgenticGateDecision(
            series_id=self.series.id,
            book_number=book_number,
            timestamp=timestamp,
            live_gate={},
            agentic_gate={"timestamp": timestamp.isoformat()},
        )
        self.db.add(row)
        self.db.commit()
        return row

    def test_confidence_history_sorted(self):
        self._add_confidence_row(3.0, datetime(2026, 1, 1))
        self._add_confidence_row(1.0, datetime(2026, 1, 2))
        self._add_confidence_row(2.0, datetime(2026, 1, 1))

        history = get_agentic_confidence_history(self.series.id, db_session=self.db)
        self.assertEqual([entry["book_number"] for entry in history], [1.0, 2.0, 3.0])

    def test_gate_history_sorted(self):
        self._add_gate_row(3.0, datetime(2026, 1, 1))
        self._add_gate_row(1.0, datetime(2026, 1, 2))
        self._add_gate_row(2.0, datetime(2026, 1, 1))

        history = get_agentic_gate_history(self.series.id, db_session=self.db)
        self.assertEqual([entry["book_number"] for entry in history], [1.0, 2.0, 3.0])

    def test_latest_confidence_decisions(self):
        self._add_confidence_row(1.0, datetime(2026, 1, 1))
        self._add_confidence_row(1.0, datetime(2026, 1, 5))  # latest for book 1
        self._add_confidence_row(2.0, datetime(2026, 1, 3))

        latest = get_latest_confidence_decisions(self.series.id, db_session=self.db)
        self.assertEqual(list(latest.keys()), [1.0, 2.0])
        self.assertEqual(latest[1.0]["agentic_confidence"]["timestamp"], datetime(2026, 1, 5).isoformat())

    def test_latest_gate_decisions(self):
        self._add_gate_row(1.0, datetime(2026, 1, 1))
        self._add_gate_row(1.0, datetime(2026, 1, 5))  # latest for book 1
        self._add_gate_row(2.0, datetime(2026, 1, 3))

        latest = get_latest_gate_decisions(self.series.id, db_session=self.db)
        self.assertEqual(list(latest.keys()), [1.0, 2.0])
        self.assertEqual(latest[1.0]["agentic_gate"]["timestamp"], datetime(2026, 1, 5).isoformat())

    def test_latest_confidence_and_gate_decisions_repeatable_under_duplicate_timestamps(self):
        # Two rows for the same book_number, identical timestamp -- no
        # promotion_outcome-like field exists here to break the tie on,
        # so the tie is broken by the underlying row id (insertion
        # order); the important determinism property is that repeated
        # calls always agree with each other, never flipping between
        # runs.
        tied_timestamp = datetime(2026, 2, 1)
        self._add_confidence_row(1.0, tied_timestamp)
        self._add_confidence_row(1.0, tied_timestamp)

        first_call = get_latest_confidence_decisions(self.series.id, db_session=self.db)
        second_call = get_latest_confidence_decisions(self.series.id, db_session=self.db)
        self.assertEqual(first_call, second_call)

    def test_history_and_latest_empty_and_fail_soft(self):
        self.assertEqual(get_agentic_confidence_history(999999, db_session=self.db), [])
        self.assertEqual(get_agentic_gate_history(999999, db_session=self.db), [])
        self.assertEqual(get_latest_confidence_decisions(999999, db_session=self.db), {})
        self.assertEqual(get_latest_gate_decisions(999999, db_session=self.db), {})

        broken_db = MagicMock()
        broken_db.query.side_effect = RuntimeError("query exploded")
        self.assertEqual(get_agentic_confidence_history(self.series.id, db_session=broken_db), [])
        self.assertEqual(get_agentic_gate_history(self.series.id, db_session=broken_db), [])
        self.assertEqual(get_latest_confidence_decisions(self.series.id, db_session=broken_db), {})
        self.assertEqual(get_latest_gate_decisions(self.series.id, db_session=broken_db), {})


# ---------------------------------------------------------------------------
# 3 & 4. agents/series_agent.py's normalization helpers
# ---------------------------------------------------------------------------


class AgenticTraceNormalizationTest(unittest.TestCase):
    def test_agentic_trace_normalization(self):
        raw = [
            {"book_number": 3.0, "tag": "c"},
            {"book_number": 1.0, "tag": "a"},
            {"book_number": 2.0, "tag": "b"},
        ]
        result = _sorted_agentic_trace_list(raw)
        self.assertEqual([entry["book_number"] for entry in result], [1.0, 2.0, 3.0])
        self.assertEqual([entry["tag"] for entry in result], ["a", "b", "c"])

    def test_agentic_trace_normalization_fails_soft_on_missing_or_malformed(self):
        self.assertEqual(_sorted_agentic_trace_list(None), [])
        self.assertEqual(_sorted_agentic_trace_list("not-a-list"), [])
        self.assertEqual(_sorted_agentic_trace_list(42), [])
        # Non-dict entries within an otherwise-valid list are dropped,
        # never raise (a malformed entry can't have .get() called on it).
        mixed = [{"book_number": 2.0}, "bad-entry", None, {"book_number": 1.0}]
        result = _sorted_agentic_trace_list(mixed)
        self.assertEqual([entry["book_number"] for entry in result], [1.0, 2.0])
        # An entry missing book_number entirely sorts last, not dropped.
        missing_key = [{"book_number": 1.0}, {"other": "x"}]
        result2 = _sorted_agentic_trace_list(missing_key)
        self.assertEqual(result2[0]["book_number"], 1.0)
        self.assertEqual(result2[1], {"other": "x"})

    def test_determinism_under_randomized_input_order(self):
        entries = [{"book_number": float(n)} for n in range(20)]
        expected = [float(n) for n in range(20)]
        for _ in range(10):
            shuffled = list(entries)
            random.shuffle(shuffled)
            result = _sorted_agentic_trace_list(shuffled)
            self.assertEqual([entry["book_number"] for entry in result], expected)


class LiveSnapshotNormalizationTest(unittest.TestCase):
    def test_live_snapshot_normalization(self):
        raw = {3: "c", "1": "a", 2.0: "b"}
        result = _sorted_book_number_dict(raw)
        self.assertEqual(list(result.keys()), [1.0, 2.0, 3.0])
        self.assertEqual(result[1.0], "a")
        self.assertEqual(result[2.0], "b")
        self.assertEqual(result[3.0], "c")
        # Distinct object -- never the same dict reference back.
        self.assertIsNot(result, raw)

    def test_live_snapshot_normalization_fails_soft_on_malformed(self):
        self.assertEqual(_sorted_book_number_dict(None), {})
        self.assertEqual(_sorted_book_number_dict("not-a-dict"), {})
        self.assertEqual(_sorted_book_number_dict([1, 2, 3]), {})
        # A malformed (non-numeric) key is dropped, real keys survive.
        raw = {"abc": "x", 1: "y", None: "z"}
        result = _sorted_book_number_dict(raw)
        self.assertEqual(result, {1.0: "y"})

    def test_determinism_under_randomized_input_order(self):
        keys = list(range(20))
        for _ in range(10):
            shuffled_keys = list(keys)
            random.shuffle(shuffled_keys)
            raw = {key: f"value-{key}" for key in shuffled_keys}
            result = _sorted_book_number_dict(raw)
            self.assertEqual(list(result.keys()), [float(k) for k in range(20)])


# ---------------------------------------------------------------------------
# 5. resolve_routing_decisions -- sorted output
# ---------------------------------------------------------------------------


class ResolutionSortedOutputTest(unittest.TestCase):
    def setUp(self):
        # Built in descending order on purpose -- resolve_routing_
        # decisions must not depend on caller-supplied ordering.
        self.live_confidence = {
            3.0: {"confidence": "high"},
            1.0: {"confidence": "medium"},
            2.0: {"confidence": "low"},
        }
        self.live_gate = {
            3.0: {"belongs_to_series": True},
            1.0: {"belongs_to_series": True},
            2.0: {"belongs_to_series": True},
        }
        self.promotion_decisions = {
            2.0: {"outcome": "use_live", "live_confidence": {}, "live_gate": {}},
            3.0: {"outcome": "use_agentic", "agentic_confidence": {"overall": "high"}, "agentic_gate": {}},
            1.0: {"outcome": "reject_agentic", "live_confidence": {}, "live_gate": {}},
        }

    def test_resolution_sorted_output_flag_off(self):
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", False):
            resolved_conf, resolved_gate = resolve_routing_decisions(
                1, self.live_confidence, self.live_gate, self.promotion_decisions
            )
        self.assertEqual(list(resolved_conf.keys()), [1.0, 2.0, 3.0])
        self.assertEqual(list(resolved_gate.keys()), [1.0, 2.0, 3.0])

    def test_resolution_sorted_output_not_activated(self):
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", ""
        ):
            resolved_conf, resolved_gate = resolve_routing_decisions(
                1, self.live_confidence, self.live_gate, self.promotion_decisions
            )
        self.assertEqual(list(resolved_conf.keys()), [1.0, 2.0, 3.0])
        self.assertEqual(list(resolved_gate.keys()), [1.0, 2.0, 3.0])

    def test_resolution_sorted_output_activated(self):
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            resolved_conf, resolved_gate = resolve_routing_decisions(
                1, self.live_confidence, self.live_gate, self.promotion_decisions
            )
        self.assertEqual(list(resolved_conf.keys()), [1.0, 2.0, 3.0])
        self.assertEqual(list(resolved_gate.keys()), [1.0, 2.0, 3.0])
        # Values themselves are unaffected by Phase 6 -- only ordering changed.
        self.assertEqual(resolved_conf[3.0], {"overall": "high"})
        self.assertEqual(resolved_conf[2.0], self.live_confidence[2.0])
        self.assertEqual(resolved_conf[1.0], self.live_confidence[1.0])

    def test_determinism_under_randomized_input_order(self):
        book_numbers = [float(n) for n in range(10)]
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            for _ in range(10):
                shuffled = list(book_numbers)
                random.shuffle(shuffled)
                live_conf = {n: {"confidence": "medium"} for n in shuffled}
                live_gate = {n: {"belongs_to_series": True} for n in shuffled}
                decisions = {n: {"outcome": "use_live"} for n in shuffled}
                resolved_conf, resolved_gate = resolve_routing_decisions(1, live_conf, live_gate, decisions)
                self.assertEqual(list(resolved_conf.keys()), book_numbers)
                self.assertEqual(list(resolved_gate.keys()), book_numbers)

    def test_fail_soft_on_malformed_inputs(self):
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            resolved_conf, resolved_gate = resolve_routing_decisions(
                1, self.live_confidence, self.live_gate, "not-a-dict"  # type: ignore[arg-type]
            )
        # Falls back to the (still-sorted) live snapshots rather than raising.
        self.assertEqual(list(resolved_conf.keys()), [1.0, 2.0, 3.0])
        self.assertEqual(list(resolved_gate.keys()), [1.0, 2.0, 3.0])

        resolved_conf2, resolved_gate2 = resolve_routing_decisions(1, None, None, None)
        self.assertEqual(resolved_conf2, {})
        self.assertEqual(resolved_gate2, {})


# ---------------------------------------------------------------------------
# 6. Result payload sorted (agents/series_agent.py, end-to-end)
# ---------------------------------------------------------------------------


class ResultPayloadSortedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=cls.engine)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setUp(self):
        self.db = self.SessionLocal()
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series
        # Deliberately added out of ascending order -- proves the
        # eventual "promotions" list is sorted by the code itself, not
        # merely because setUp happened to add books in order.
        for number in [5, 1, 4, 2, 3]:
            self.db.add(
                Book(
                    title=f"Cherry Blossom Girls Book {number}",
                    author="Harmon Cooper",
                    series_id=series.id,
                    profile_id=series.profile_id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=False,
                )
            )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _mock_discovery(self, **overrides):
        result = {
            "candidates": [],
            "provider_failures": [],
            "all_providers_failed": False,
            "used_author_fallback": False,
        }
        result.update(overrides)
        return patch("discovery_engine.discover_candidates_for_series", return_value=result)

    def test_result_payload_sorted(self):
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", str(self.series.id)
        ), patch("agents.agentic_series_agent.SessionLocal", self.SessionLocal):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        promotions = result["agentic_promotion"]["promotions"]
        self.assertGreater(len(promotions), 0)
        book_numbers = [promotion["book_number"] for promotion in promotions]
        self.assertEqual(book_numbers, sorted(book_numbers))


if __name__ == "__main__":
    unittest.main()
