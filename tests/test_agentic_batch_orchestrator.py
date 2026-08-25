"""Phase 1, fifth/sixth implementation blocks: `services/agentic_replay_
runner.py` (`replay_agentic_turn`/`replay_and_compare`) and `services/
agentic_batch_orchestrator.py` (`run_batch_agentic_evaluations`).

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here), this file needs to prove:

1. `replay_agentic_turn` returns the documented `{series_id, agentic_trace,
   timestamp}` shape.
2. `replay_and_compare` returns the same shape `services.agentic_
   evaluation_harness.run_agentic_evaluation_for_series` does.
3. `run_batch_agentic_evaluations` handles multiple series and aggregates
   correctly.
4. Nothing in this stack ever writes to persisted state, across a whole
   batch.
5. Deterministic replay for a single series.
6. The batch report's structure matches spec, and telemetry logs it via
   `record_agentic_batch`.
"""

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import Book, Series, SeriesSkeleton
from services.agentic_batch_orchestrator import run_batch_agentic_evaluations
from services.agentic_replay_runner import replay_agentic_turn, replay_and_compare
from services.skeleton_store import backfill_skeleton_for_series


class AgenticReplayAndBatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=cls.engine)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def _make_series(self, name, book_numbers):
        series = Series(name=name, author="Harmon Cooper", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        for number in book_numbers:
            self.db.add(
                Book(
                    title=f"{name} Book {number}",
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
        backfill_skeleton_for_series(self.db, series.id)
        return series

    def setUp(self):
        self.db = self.SessionLocal()
        self.series_a = self._make_series("Cherry Blossom Girls", [1, 2, 3])
        self.series_b = self._make_series("Safehold", [1, 2])

    def tearDown(self):
        self.db.close()

    def _row_counts(self):
        return {
            "series": self.db.query(Series).count(),
            "books": self.db.query(Book).count(),
            "skeletons": self.db.query(SeriesSkeleton).count(),
        }

    def _all_skeleton_json(self):
        return {
            row.series_id: list(row.skeleton_json)
            for row in self.db.query(SeriesSkeleton).all()
        }

    # -- 1: replay_agentic_turn shape ---------------------------------------

    def test_replay_agentic_turn_returns_expected_shape(self):
        result = replay_agentic_turn(self.series_a.id, db_session=self.db)

        self.assertEqual(set(result.keys()), {"series_id", "agentic_trace", "timestamp"})
        self.assertEqual(result["series_id"], self.series_a.id)
        agentic = result["agentic_trace"]
        for key in (
            "provider_calls",
            "probes",
            "confidence_traces",
            "gate_traces",
            "skeleton_merge_previews",
            "reasoning_steps",
        ):
            self.assertIn(key, agentic)
        self.assertEqual(len(agentic["provider_calls"]), 3)

    # -- 2: replay_and_compare shape ------------------------------------------

    def test_replay_and_compare_returns_expected_shape(self):
        result = replay_and_compare(self.series_a.id, db_session=self.db)

        self.assertEqual(
            set(result.keys()),
            {"series_id", "live_observation", "agentic_trace", "comparison", "timestamp"},
        )
        self.assertEqual(result["series_id"], self.series_a.id)
        self.assertEqual(len(result["live_observation"]["skeleton_snapshot"]), 3)
        self.assertEqual(len(result["comparison"]["by_book_number"]), 3)

    def test_replay_and_compare_opens_and_closes_its_own_session_when_none_supplied(self):
        with patch("services.agentic_replay_runner.SessionLocal", self.SessionLocal), patch(
            "services.agentic_evaluation_harness.SessionLocal", self.SessionLocal
        ):
            result = replay_and_compare(self.series_a.id)
        self.assertEqual(result["series_id"], self.series_a.id)
        self.assertEqual(len(result["live_observation"]["skeleton_snapshot"]), 3)

    # -- 3 & 6: batch runner + report structure -------------------------------

    def test_batch_runner_handles_multiple_series(self):
        batch_report = run_batch_agentic_evaluations(
            [self.series_a.id, self.series_b.id], db_session=self.db
        )

        self.assertEqual(batch_report["count"], 2)
        self.assertEqual(len(batch_report["results"]), 2)
        self.assertEqual(batch_report["results"][0]["series_id"], self.series_a.id)
        self.assertEqual(batch_report["results"][1]["series_id"], self.series_b.id)
        self.assertEqual(len(batch_report["results"][0]["comparison"]["by_book_number"]), 3)
        self.assertEqual(len(batch_report["results"][1]["comparison"]["by_book_number"]), 2)

    def test_batch_report_structure(self):
        batch_report = run_batch_agentic_evaluations([self.series_a.id], db_session=self.db)

        self.assertEqual(set(batch_report.keys()), {"count", "results", "batch_timestamp"})
        self.assertIsInstance(batch_report["results"], list)
        for result in batch_report["results"]:
            self.assertEqual(
                set(result.keys()), {"series_id", "live_observation", "agentic_trace", "comparison", "timestamp"}
            )

    def test_batch_runner_records_a_failure_without_aborting_the_batch(self):
        real_replay_and_compare = replay_and_compare

        def _flaky(series_id, **kwargs):
            if series_id == self.series_b.id:
                raise RuntimeError("boom")
            return real_replay_and_compare(series_id, **kwargs)

        with patch("services.agentic_batch_orchestrator.replay_and_compare", side_effect=_flaky):
            batch_report = run_batch_agentic_evaluations(
                [self.series_a.id, self.series_b.id], db_session=self.db
            )

        self.assertEqual(batch_report["count"], 2)
        self.assertNotIn("error", batch_report["results"][0])
        self.assertEqual(batch_report["results"][1]["series_id"], self.series_b.id)
        self.assertEqual(batch_report["results"][1]["error"], "replay_and_compare_failed")

    def test_batch_runner_logs_via_record_agentic_batch(self):
        with patch("services.agentic_batch_orchestrator.record_agentic_batch") as spy:
            run_batch_agentic_evaluations([self.series_a.id, self.series_b.id], db_session=self.db)
        spy.assert_called_once()
        call_args = spy.call_args[0]
        self.assertEqual(call_args[0], [self.series_a.id, self.series_b.id])
        self.assertEqual(call_args[1]["count"], 2)

    def test_record_agentic_batch_failure_does_not_break_the_batch_result(self):
        with patch(
            "services.agentic_batch_orchestrator.record_agentic_batch", side_effect=RuntimeError("boom")
        ):
            batch_report = run_batch_agentic_evaluations([self.series_a.id], db_session=self.db)
        self.assertEqual(batch_report["count"], 1)

    # -- 4: no state changes across a whole batch -----------------------------

    def test_no_state_changes_across_batch(self):
        before_counts = self._row_counts()
        before_skeletons = self._all_skeleton_json()

        run_batch_agentic_evaluations([self.series_a.id, self.series_b.id], db_session=self.db)

        after_counts = self._row_counts()
        after_skeletons = self._all_skeleton_json()
        self.assertEqual(before_counts, after_counts)
        self.assertEqual(before_skeletons, after_skeletons)

    # -- 5: deterministic replay for a single series --------------------------

    def test_deterministic_replay_for_single_series(self):
        first = replay_agentic_turn(self.series_a.id, db_session=self.db)
        second = replay_agentic_turn(self.series_a.id, db_session=self.db)

        def _strip_volatile(result):
            trace = dict(result["agentic_trace"])
            trace.pop("turn_timestamp", None)
            trace["reasoning_steps"] = [
                {k: v for k, v in step.items() if k not in ("turn_id", "recorded_at")}
                for step in trace.get("reasoning_steps") or []
            ]
            return {"series_id": result["series_id"], "agentic_trace": trace}

        self.assertEqual(_strip_volatile(first), _strip_volatile(second))


if __name__ == "__main__":
    unittest.main()
