"""Phase 1, fourth implementation block: `services/agentic_evaluation_
harness.py`'s shadow-mode evaluation harness.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here), this file needs to prove:

1. `run_agentic_evaluation_for_series` returns the documented report
   shape.
2. It never changes persistent state (no new/changed rows anywhere).
3. `_observe_live_pipeline` reads through the real, unmodified
   `agents/series_agent.py`-adjacent models/`services/skeleton_store.py`
   read paths -- not a reimplementation.
4. `_compare_live_vs_agentic` handles a series with no owned books/no
   skeleton entries (missing-books) gracefully, without raising.
5. Deterministic replay: same DB state -> same report, modulo timestamps.
"""

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agents import agentic_series_agent
from database import Base
from models import Book, Series, SeriesSkeleton
from services.agentic_evaluation_harness import (
    _compare_live_vs_agentic,
    _observe_live_pipeline,
    run_agentic_evaluation_for_series,
)
from services.skeleton_store import backfill_skeleton_for_series


class AgenticEvaluationHarnessTest(unittest.TestCase):
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
        for number in [1, 2, 3]:
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
        backfill_skeleton_for_series(self.db, self.series.id)

    def tearDown(self):
        self.db.close()

    def _row_counts(self):
        return {
            "series": self.db.query(Series).count(),
            "books": self.db.query(Book).count(),
            "skeletons": self.db.query(SeriesSkeleton).count(),
        }

    def _skeleton_json(self):
        row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        return list(row.skeleton_json) if row else None

    # -- 1: documented report shape ---------------------------------------

    def test_run_agentic_evaluation_for_series_returns_expected_shape(self):
        report = run_agentic_evaluation_for_series(self.series.id, db_session=self.db)

        self.assertEqual(report["series_id"], self.series.id)
        self.assertIn("timestamp", report)
        for key in ("live_observation", "agentic_trace", "comparison", "drift_report", "ttl_report"):
            self.assertIn(key, report)
            self.assertIsInstance(report[key], dict)

        live = report["live_observation"]
        for key in ("skeleton_snapshot", "confidence_snapshot", "gate_snapshot"):
            self.assertIn(key, live)
            self.assertIsInstance(live[key], dict)
        self.assertEqual(len(live["skeleton_snapshot"]), 3)

        agentic = report["agentic_trace"]
        for key in (
            "provider_calls",
            "probes",
            "confidence_traces",
            "gate_traces",
            "skeleton_merge_previews",
            "reasoning_steps",
        ):
            self.assertIn(key, agentic)

        comparison = report["comparison"]
        self.assertIn("by_book_number", comparison)
        self.assertEqual(len(comparison["by_book_number"]), 3)
        entry = comparison["by_book_number"]["1.0"]
        for key in (
            "live_confidence",
            "agentic_confidence",
            "live_gate",
            "agentic_gate",
            "live_skeleton_entry",
            "agentic_preview_entry",
        ):
            self.assertIn(key, entry)
        self.assertTrue(entry["present_in_live"])
        self.assertTrue(entry["present_in_agentic"])

    # -- 2: no state changes -----------------------------------------------

    def test_no_state_changes(self):
        before_counts = self._row_counts()
        before_skeleton = self._skeleton_json()

        run_agentic_evaluation_for_series(self.series.id, db_session=self.db)

        after_counts = self._row_counts()
        after_skeleton = self._skeleton_json()
        self.assertEqual(before_counts, after_counts)
        self.assertEqual(before_skeleton, after_skeleton)

    def test_opens_and_closes_its_own_session_when_none_supplied(self):
        with patch("services.agentic_evaluation_harness.SessionLocal", self.SessionLocal):
            report = run_agentic_evaluation_for_series(self.series.id)
        self.assertEqual(report["series_id"], self.series.id)
        self.assertEqual(len(report["live_observation"]["skeleton_snapshot"]), 3)

        row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        self.assertEqual(len(row.skeleton_json), 3)

    # -- 3: live observation uses the real read paths ----------------------

    def test_live_observation_uses_real_series_agent_and_skeleton_store_read_paths(self):
        live = _observe_live_pipeline(self.series.id, self.db)
        self.assertEqual(len(live["skeleton_snapshot"]), 3)
        self.assertEqual(live["skeleton_snapshot"]["1.0"]["book_number"], 1.0)
        self.assertEqual(live["confidence_snapshot"]["1.0"]["confidence"], "high")
        self.assertTrue(live["gate_snapshot"]["1.0"]["belongs_to_series"])
        self.assertEqual(live["gate_snapshot"]["1.0"]["source_class"], "library")

    def test_live_observation_never_writes_anything(self):
        before = self._skeleton_json()
        _observe_live_pipeline(self.series.id, self.db)
        after = self._skeleton_json()
        self.assertEqual(before, after)

    def test_live_observation_handles_series_not_found(self):
        live = _observe_live_pipeline(999999, self.db)
        self.assertEqual(live, {"skeleton_snapshot": {}, "confidence_snapshot": {}, "gate_snapshot": {}})

    def test_run_agentic_evaluation_calls_the_real_shadow_loop(self):
        with patch(
            "services.agentic_evaluation_harness.agentic_series_agent.run_agentic_turn",
            wraps=agentic_series_agent.run_agentic_turn,
        ) as spy:
            run_agentic_evaluation_for_series(self.series.id, db_session=self.db)
        spy.assert_called_once()
        self.assertEqual(spy.call_args[0][0], self.series.id)

    # -- 4: comparison handles missing books gracefully ---------------------

    def test_compare_live_vs_agentic_handles_missing_books_gracefully(self):
        comparison = _compare_live_vs_agentic({}, {})
        self.assertEqual(comparison, {"by_book_number": {}})

        comparison = _compare_live_vs_agentic(
            {"skeleton_snapshot": {}, "confidence_snapshot": {}, "gate_snapshot": {}},
            {"confidence_traces": [], "gate_traces": [], "skeleton_merge_previews": []},
        )
        self.assertEqual(comparison, {"by_book_number": {}})

    def test_compare_live_vs_agentic_handles_none_inputs_gracefully(self):
        comparison = _compare_live_vs_agentic(None, None)  # type: ignore[arg-type]
        self.assertEqual(comparison, {"by_book_number": {}})

    def test_compare_live_vs_agentic_handles_one_sided_data(self):
        # A book_number only the agentic side saw (e.g. a fresh series with
        # no skeleton yet, but the shadow loop still produced traces) must
        # not raise and must be reported with live_* fields as None.
        live = {"skeleton_snapshot": {}, "confidence_snapshot": {}, "gate_snapshot": {}}
        agentic = {
            "confidence_traces": [{"book_number": 4.0, "before": {}, "after": {"overall": "medium"}}],
            "gate_traces": [{"book_number": 4.0, "gate_input": {}, "gate_output": {"belongs_to_series": True}}],
            "skeleton_merge_previews": [{"before": [], "after": [{"book_number": 4.0, "title": "New Book"}]}],
        }
        comparison = _compare_live_vs_agentic(live, agentic)
        entry = comparison["by_book_number"]["4.0"]
        self.assertIsNone(entry["live_confidence"])
        self.assertIsNone(entry["live_gate"])
        self.assertFalse(entry["present_in_live"])
        self.assertTrue(entry["present_in_agentic"])
        self.assertEqual(entry["agentic_confidence"], {"overall": "medium"})
        self.assertTrue(entry["agentic_gate"]["belongs_to_series"])
        self.assertEqual(entry["agentic_preview_entry"]["title"], "New Book")

    def test_handles_series_not_found_end_to_end_without_throwing(self):
        report = run_agentic_evaluation_for_series(999999, db_session=self.db)
        self.assertEqual(report["series_id"], 999999)
        self.assertEqual(report["comparison"], {"by_book_number": {}})

    # -- 5: deterministic report, modulo timestamps -------------------------

    def test_report_is_stable_given_same_db_state(self):
        first = run_agentic_evaluation_for_series(self.series.id, db_session=self.db)
        second = run_agentic_evaluation_for_series(self.series.id, db_session=self.db)

        def _strip_volatile(report):
            stripped = dict(report)
            stripped.pop("timestamp", None)
            agentic = dict(stripped["agentic_trace"])
            agentic.pop("turn_timestamp", None)
            agentic["reasoning_steps"] = [
                {k: v for k, v in step.items() if k not in ("turn_id", "recorded_at")}
                for step in agentic.get("reasoning_steps") or []
            ]
            stripped["agentic_trace"] = agentic
            ttl_report = dict(stripped["ttl_report"])
            ttl_report.pop("timestamp", None)
            stripped["ttl_report"] = ttl_report
            return stripped

        self.assertEqual(_strip_volatile(first), _strip_volatile(second))


if __name__ == "__main__":
    unittest.main()
