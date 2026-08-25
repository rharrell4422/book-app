"""Phase 1, seventh/eighth implementation blocks: `services/agentic_drift_
detector.py` (`detect_skeleton_drift`) and `services/agentic_ttl_
validator.py` (`validate_ttl_behavior`), plus their integration into
`services/agentic_evaluation_harness.run_agentic_evaluation_for_series`.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here), this file needs to prove:

1. `detect_skeleton_drift` correctly flags no drift for identical entries,
   drift for changed titles/authors/metadata/confidence, and missing
   entries on either side.
2. `validate_ttl_behavior` correctly buckets discovered entries as
   expired/valid based on `last_confirmed_at` age, reusing `services.
   skeleton_store`'s own unmodified 90-day expiry check.
3. `run_agentic_evaluation_for_series` produces `drift_report`/`ttl_report`
   as part of its full diagnostic report.
4. Nothing in this stack writes anything.
"""

import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import Book, Series, SeriesSkeleton
from services.agentic_drift_detector import _preview_entries_by_number, detect_skeleton_drift
from services.agentic_evaluation_harness import run_agentic_evaluation_for_series
from services.agentic_ttl_validator import validate_ttl_behavior
from services.skeleton_store import DISCOVERED_ENTRY_TTL_DAYS, backfill_skeleton_for_series


class DetectSkeletonDriftTest(unittest.TestCase):
    def test_detect_skeleton_drift_basic(self):
        # Identical entries on both sides -> no drift.
        live = {"1.0": {"book_number": 1.0, "title": "Book One", "confidence": "high"}}
        preview = {"1.0": {"book_number": 1.0, "title": "Book One", "confidence": "high"}}

        report = detect_skeleton_drift(live, preview)

        self.assertEqual(report["missing_in_live"], [])
        self.assertEqual(report["missing_in_preview"], [])
        self.assertEqual(report["summary"], {"count_changed": 0, "count_missing_in_live": 0, "count_missing_in_preview": 0})
        entry = report["by_book_number"]["1.0"]
        self.assertFalse(any(entry["drift"].values()))
        self.assertEqual(entry["live"], live["1.0"])
        self.assertEqual(entry["preview"], preview["1.0"])

    def test_detect_skeleton_drift_missing_entries(self):
        live = {"1.0": {"book_number": 1.0, "title": "Book One"}}
        preview = {"2.0": {"book_number": 2.0, "title": "Book Two"}}

        report = detect_skeleton_drift(live, preview)

        self.assertEqual(report["missing_in_preview"], ["1.0"])
        self.assertEqual(report["missing_in_live"], ["2.0"])
        self.assertEqual(report["summary"]["count_missing_in_live"], 1)
        self.assertEqual(report["summary"]["count_missing_in_preview"], 1)
        self.assertIsNone(report["by_book_number"]["1.0"]["preview"])
        self.assertIsNone(report["by_book_number"]["2.0"]["live"])

    def test_detect_skeleton_drift_confidence_changes(self):
        live = {"1.0": {"book_number": 1.0, "title": "Book One", "confidence": "high"}}
        preview = {"1.0": {"book_number": 1.0, "title": "Book One", "confidence": "medium"}}

        report = detect_skeleton_drift(live, preview)

        entry = report["by_book_number"]["1.0"]
        self.assertTrue(entry["drift"]["confidence_changed"])
        self.assertFalse(entry["drift"]["title_changed"])
        self.assertEqual(report["summary"]["count_changed"], 1)

    def test_detect_skeleton_drift_title_author_metadata_changes(self):
        live = {
            "1.0": {
                "book_number": 1.0,
                "title": "Book One",
                "author": "Author A",
                "release_date": "2024-01-01",
                "status": "confirmed",
                "confidence": "high",
            }
        }
        preview = {
            "1.0": {
                "book_number": 1.0,
                "title": "Book One: Revised",
                "author": "Author B",
                "release_date": "2024-06-01",
                "status": "unconfirmed",
                "confidence": "high",
            }
        }

        report = detect_skeleton_drift(live, preview)

        entry = report["by_book_number"]["1.0"]
        self.assertTrue(entry["drift"]["title_changed"])
        self.assertTrue(entry["drift"]["author_changed"])
        self.assertTrue(entry["drift"]["metadata_changed"])
        self.assertFalse(entry["drift"]["confidence_changed"])
        self.assertEqual(report["summary"]["count_changed"], 1)

    def test_detect_skeleton_drift_handles_non_dict_inputs(self):
        report = detect_skeleton_drift(None, None)  # type: ignore[arg-type]
        self.assertEqual(report["by_book_number"], {})
        self.assertEqual(report["summary"], {"count_changed": 0, "count_missing_in_live": 0, "count_missing_in_preview": 0})

    def test_preview_entries_by_number_reshapes_skeleton_merge_previews(self):
        previews = [
            {
                "before": [],
                "after": [{"book_number": 1.0, "title": "Book One"}, {"book_number": 2.0, "title": "Book Two"}],
            }
        ]
        by_number = _preview_entries_by_number(previews)
        self.assertEqual(set(by_number.keys()), {"1.0", "2.0"})
        self.assertEqual(by_number["1.0"]["title"], "Book One")

    def test_preview_entries_by_number_handles_empty_or_malformed_input(self):
        self.assertEqual(_preview_entries_by_number([]), {})
        self.assertEqual(_preview_entries_by_number(None), {})  # type: ignore[arg-type]
        self.assertEqual(_preview_entries_by_number([{"before": []}]), {})


class ValidateTtlBehaviorTest(unittest.TestCase):
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
        self.db.add(
            Book(
                title="Cherry Blossom Girls Book 1",
                author="Harmon Cooper",
                series_id=series.id,
                profile_id=series.profile_id,
                series_order=1,
                book_number=1.0,
                record_status="active",
                is_read=False,
            )
        )
        self.db.commit()
        backfill_skeleton_for_series(self.db, self.series.id)

    def tearDown(self):
        self.db.close()

    def _set_skeleton_json(self, entries):
        row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        row.skeleton_json = entries
        self.db.commit()

    def test_validate_ttl_behavior_expired_and_valid(self):
        fresh_iso = datetime.utcnow().isoformat()
        stale_iso = (datetime.utcnow() - timedelta(days=DISCOVERED_ENTRY_TTL_DAYS + 5)).isoformat()

        skeleton_entries = [
            {"book_number": 1.0, "title": "Book One", "source_class": "library"},
            {
                "book_number": 5.0,
                "title": "Discovered Fresh",
                "source_class": "discovered",
                "first_seen_at": fresh_iso,
                "last_confirmed_at": fresh_iso,
            },
            {
                "book_number": 9.0,
                "title": "Discovered Stale",
                "source_class": "discovered",
                "first_seen_at": stale_iso,
                "last_confirmed_at": stale_iso,
            },
        ]
        self._set_skeleton_json(skeleton_entries)

        report = validate_ttl_behavior(self.series.id, db_session=self.db)

        self.assertEqual(report["series_id"], self.series.id)
        self.assertIn("timestamp", report)

        expired_numbers = {entry["book_number"] for entry in report["discovered_ttl"]["expired"]}
        valid_numbers = {entry["book_number"] for entry in report["discovered_ttl"]["valid"]}
        self.assertEqual(expired_numbers, {9.0})
        self.assertEqual(valid_numbers, {5.0})
        # The library entry (book 1) is never a "discovered" entry, so it
        # must not appear in either TTL bucket.
        self.assertNotIn(1.0, expired_numbers | valid_numbers)

        self.assertEqual(report["probes_ttl"], {"expired": [], "valid": [], "note": report["probes_ttl"]["note"]})
        self.assertIn("no probes_json storage exists yet", report["probes_ttl"]["note"])

    def test_validate_ttl_behavior_handles_no_skeleton_row(self):
        other = Series(name="No Skeleton Series", author="Someone", profile_id="robbie")
        self.db.add(other)
        self.db.commit()
        self.db.refresh(other)

        report = validate_ttl_behavior(other.id, db_session=self.db)
        self.assertEqual(report["discovered_ttl"], {"expired": [], "valid": []})

    def test_validate_ttl_behavior_never_writes_anything(self):
        before = list(
            self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first().skeleton_json
        )
        validate_ttl_behavior(self.series.id, db_session=self.db)
        after = list(
            self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first().skeleton_json
        )
        self.assertEqual(before, after)

    def test_validate_ttl_behavior_opens_and_closes_its_own_session_when_none_supplied(self):
        from unittest.mock import patch

        with patch("services.agentic_ttl_validator.SessionLocal", self.SessionLocal):
            report = validate_ttl_behavior(self.series.id)
        self.assertEqual(report["series_id"], self.series.id)


class EvaluationHarnessDriftAndTtlIntegrationTest(unittest.TestCase):
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

    def test_integration_with_evaluation_harness(self):
        report = run_agentic_evaluation_for_series(self.series.id, db_session=self.db)

        self.assertIn("drift_report", report)
        self.assertIn("ttl_report", report)

        drift_report = report["drift_report"]
        self.assertIn("by_book_number", drift_report)
        self.assertIn("summary", drift_report)
        self.assertEqual(len(drift_report["by_book_number"]), 3)

        ttl_report = report["ttl_report"]
        self.assertEqual(ttl_report["series_id"], self.series.id)
        self.assertIn("discovered_ttl", ttl_report)
        self.assertIn("probes_ttl", ttl_report)
        # All three books here are library-sourced (owned), never
        # "discovered" -- so the TTL buckets should both be empty.
        self.assertEqual(ttl_report["discovered_ttl"], {"expired": [], "valid": []})

    def test_no_state_changes(self):
        before_counts = self._row_counts()
        before_skeleton = self._skeleton_json()

        run_agentic_evaluation_for_series(self.series.id, db_session=self.db)

        after_counts = self._row_counts()
        after_skeleton = self._skeleton_json()
        self.assertEqual(before_counts, after_counts)
        self.assertEqual(before_skeleton, after_skeleton)


if __name__ == "__main__":
    unittest.main()
