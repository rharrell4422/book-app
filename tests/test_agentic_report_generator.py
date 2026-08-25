"""Phase 1, ninth implementation block: `services/agentic_report_
generator.py` (`generate_agentic_report`/`generate_agentic_html_report`)
and its integration into `services/agentic_evaluation_harness.py`
(`generate_full_agentic_report`/`generate_full_agentic_html`).

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here), this file proves:

1. `generate_agentic_report` produces the documented consolidated shape,
   with every section present even when the input is empty/partial.
2. `generate_agentic_html_report` produces safe, escaped, script/CSS-free
   HTML-style markup for the same input.
3. The evaluation-harness wrappers compose the real pipeline end-to-end
   and never write anything.
"""

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import Book, Series, SeriesSkeleton
from services.agentic_evaluation_harness import (
    generate_full_agentic_html,
    generate_full_agentic_report,
    run_agentic_evaluation_for_series,
)
from services.agentic_report_generator import generate_agentic_html_report, generate_agentic_report
from services.skeleton_store import backfill_skeleton_for_series


class GenerateAgenticReportTest(unittest.TestCase):
    def _sample_evaluation(self):
        return {
            "series_id": 42,
            "timestamp": "2026-08-24T00:00:00+00:00",
            "live_observation": {
                "skeleton_snapshot": {"1.0": {"book_number": 1.0, "title": "Book One"}},
                "confidence_snapshot": {"1.0": {"confidence": "high", "status": "confirmed"}},
                "gate_snapshot": {"1.0": {"belongs_to_series": True, "source_class": "library"}},
            },
            "agentic_trace": {
                "provider_calls": [{"book_number": 1.0, "provider": "serper", "query": "q"}],
                "probes": [{"book_number": 1.0, "provider": "serper", "query": "q", "result_size": 0}],
                "confidence_traces": [{"book_number": 1.0, "before": {}, "after": {"overall": "high"}}],
                "gate_traces": [{"book_number": 1.0, "gate_input": {}, "gate_output": {"belongs_to_series": True}}],
                "skeleton_merge_previews": [{"before": [], "after": [{"book_number": 1.0}]}],
                "reasoning_steps": [{"phase": "probe", "book_number": 1.0}],
            },
            "comparison": {"by_book_number": {"1.0": {"live_confidence": {}, "agentic_confidence": {}}}},
            "drift_report": {"by_book_number": {}, "summary": {"count_changed": 0}},
            "ttl_report": {"series_id": 42, "discovered_ttl": {"expired": [], "valid": []}},
        }

    # -- 1: consolidated structure -----------------------------------------

    def test_generate_agentic_report_structure(self):
        report = generate_agentic_report(self._sample_evaluation())

        self.assertEqual(report["series_id"], 42)
        self.assertEqual(report["timestamp"], "2026-08-24T00:00:00+00:00")

        self.assertEqual(set(report["live"].keys()), {"skeleton", "confidence", "gate"})
        self.assertEqual(report["live"]["skeleton"], {"1.0": {"book_number": 1.0, "title": "Book One"}})
        self.assertEqual(report["live"]["confidence"]["1.0"]["confidence"], "high")
        self.assertTrue(report["live"]["gate"]["1.0"]["belongs_to_series"])

        self.assertEqual(
            set(report["agentic"].keys()),
            {"provider_calls", "probes", "confidence_traces", "gate_traces", "skeleton_merge_previews", "reasoning_steps"},
        )
        self.assertEqual(len(report["agentic"]["provider_calls"]), 1)
        self.assertEqual(len(report["agentic"]["reasoning_steps"]), 1)

        self.assertEqual(report["comparison"], self._sample_evaluation()["comparison"])
        self.assertEqual(report["drift_report"], self._sample_evaluation()["drift_report"])
        self.assertEqual(report["ttl_report"], self._sample_evaluation()["ttl_report"])

    def test_generate_agentic_report_all_sections_present_when_input_is_empty(self):
        report = generate_agentic_report({})

        self.assertIsNone(report["series_id"])
        self.assertIsNone(report["timestamp"])
        self.assertEqual(report["live"], {"skeleton": {}, "confidence": {}, "gate": {}})
        self.assertEqual(
            report["agentic"],
            {
                "provider_calls": [],
                "probes": [],
                "confidence_traces": [],
                "gate_traces": [],
                "skeleton_merge_previews": [],
                "reasoning_steps": [],
            },
        )
        self.assertEqual(report["comparison"], {"by_book_number": {}})
        self.assertEqual(report["drift_report"], {})
        self.assertEqual(report["ttl_report"], {})

    def test_generate_agentic_report_handles_non_dict_input(self):
        report = generate_agentic_report(None)  # type: ignore[arg-type]
        self.assertIsNone(report["series_id"])
        self.assertEqual(report["live"], {"skeleton": {}, "confidence": {}, "gate": {}})

    def test_generate_agentic_report_handles_partial_input(self):
        report = generate_agentic_report({"series_id": 7, "live_observation": {"skeleton_snapshot": {"1.0": {}}}})
        self.assertEqual(report["series_id"], 7)
        self.assertEqual(report["live"]["skeleton"], {"1.0": {}})
        self.assertEqual(report["live"]["confidence"], {})
        self.assertEqual(report["agentic"]["provider_calls"], [])

    # -- 2: HTML-style rendering --------------------------------------------

    def test_generate_agentic_html_report_contains_expected_sections(self):
        html_report = generate_agentic_html_report(self._sample_evaluation())

        self.assertIsInstance(html_report, str)
        self.assertIn("<h2>Series ID: 42</h2>", html_report)
        for title in ("Live Snapshot", "Agentic Trace", "Comparison", "Drift Report", "TTL Report"):
            self.assertIn(f"<h3>{title}</h3>", html_report)
        self.assertIn("<pre>", html_report)
        self.assertIn("</pre>", html_report)
        self.assertIn("<div>", html_report)

    def test_generate_agentic_html_report_has_no_script_or_css(self):
        html_report = generate_agentic_html_report(self._sample_evaluation())
        self.assertNotIn("<script", html_report.lower())
        self.assertNotIn("<style", html_report.lower())
        self.assertNotIn("javascript:", html_report.lower())

    def test_generate_agentic_html_report_escapes_dangerous_content(self):
        evaluation = self._sample_evaluation()
        evaluation["live_observation"]["skeleton_snapshot"]["1.0"]["title"] = "<script>alert(1)</script>"

        html_report = generate_agentic_html_report(evaluation)

        self.assertNotIn("<script>alert(1)</script>", html_report)
        self.assertIn("&lt;script&gt;", html_report)

    def test_generate_agentic_html_report_handles_empty_input(self):
        html_report = generate_agentic_html_report({})
        self.assertIn("<h2>Series ID: None</h2>", html_report)
        for title in ("Live Snapshot", "Agentic Trace", "Comparison", "Drift Report", "TTL Report"):
            self.assertIn(f"<h3>{title}</h3>", html_report)


class EvaluationHarnessReportIntegrationTest(unittest.TestCase):
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

    def test_generate_full_agentic_report_matches_generate_agentic_report_on_a_real_evaluation(self):
        evaluation = run_agentic_evaluation_for_series(self.series.id, db_session=self.db)
        expected = generate_agentic_report(evaluation)

        report = generate_full_agentic_report(self.series.id, db_session=self.db)

        # Same shape/content modulo the two calls' own independent
        # timestamps (each run_agentic_evaluation_for_series call re-runs
        # the agentic turn, so turn_timestamp/reasoning recorded_at can
        # differ too) -- spot-check structure and stable content instead.
        self.assertEqual(report["series_id"], expected["series_id"])
        self.assertEqual(set(report.keys()), set(expected.keys()))
        self.assertEqual(report["live"]["skeleton"].keys(), expected["live"]["skeleton"].keys())
        self.assertEqual(len(report["agentic"]["provider_calls"]), len(expected["agentic"]["provider_calls"]))

    def test_generate_full_agentic_html_returns_a_string_and_writes_nothing(self):
        before_counts = self._row_counts()

        html_report = generate_full_agentic_html(self.series.id, db_session=self.db)

        self.assertIsInstance(html_report, str)
        self.assertIn(f"<h2>Series ID: {self.series.id}</h2>", html_report)
        self.assertEqual(before_counts, self._row_counts())

    def test_generate_full_agentic_report_logs_via_telemetry(self):
        with patch("services.agentic_evaluation_harness.record_agentic_full_report") as spy:
            report = generate_full_agentic_report(self.series.id, db_session=self.db)
        spy.assert_called_once()
        self.assertEqual(spy.call_args[0][0], self.series.id)
        self.assertEqual(spy.call_args[0][1], report)

    def test_generate_full_agentic_html_logs_via_telemetry(self):
        with patch("services.agentic_evaluation_harness.record_agentic_full_html") as spy:
            html_report = generate_full_agentic_html(self.series.id, db_session=self.db)
        spy.assert_called_once()
        self.assertEqual(spy.call_args[0][0], self.series.id)
        self.assertEqual(spy.call_args[0][1], html_report)

    def test_broken_telemetry_does_not_break_the_returned_report(self):
        with patch(
            "services.agentic_evaluation_harness.record_agentic_full_report", side_effect=RuntimeError("boom")
        ):
            report = generate_full_agentic_report(self.series.id, db_session=self.db)
        self.assertEqual(report["series_id"], self.series.id)

    def test_no_state_changes(self):
        before_counts = self._row_counts()
        generate_full_agentic_report(self.series.id, db_session=self.db)
        generate_full_agentic_html(self.series.id, db_session=self.db)
        self.assertEqual(before_counts, self._row_counts())


if __name__ == "__main__":
    unittest.main()
