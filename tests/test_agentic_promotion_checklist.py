"""Phase 1, eleventh implementation block (first half):
`services/agentic_promotion_checklist.py`'s `generate_promotion_readiness`.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here), this file needs to prove:

1. Each of the six pure check helpers correctly derives its verdict from
   `comparison`/`drift_report`/`ttl_report` shapes.
2. `generate_promotion_readiness` returns the documented shape, end to
   end, for a clean baseline series (everything passes).
3. It correctly reports "not ready" when the underlying evaluation shows
   a real problem (an expired discovered entry, or a series that doesn't
   exist at all).
4. It never writes anything.
5. It fails conservatively (never a false "ready") if the underlying
   evaluation call raises.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import Book, Series, SeriesSkeleton
from services.agentic_promotion_checklist import (
    _CHECK_LABELS,
    _build_notes,
    _confidence_stable,
    _drift_within_threshold,
    _gate_consistent,
    _has_recent_agentic_trace,
    _skeleton_preview_consistent,
    _ttl_clean,
    generate_promotion_readiness,
)
from services.skeleton_store import DISCOVERED_ENTRY_TTL_DAYS, backfill_skeleton_for_series


class CheckHelpersTest(unittest.TestCase):
    # -- has_recent_agentic_trace --------------------------------------------

    def test_has_recent_agentic_trace_true_with_confidence_traces(self):
        self.assertTrue(_has_recent_agentic_trace({"confidence_traces": [{"book_number": 1.0}]}))

    def test_has_recent_agentic_trace_false_when_empty_or_malformed(self):
        self.assertFalse(_has_recent_agentic_trace({"confidence_traces": []}))
        self.assertFalse(_has_recent_agentic_trace({}))
        self.assertFalse(_has_recent_agentic_trace(None))  # type: ignore[arg-type]

    # -- drift_within_threshold -----------------------------------------------

    def test_drift_within_threshold_true_when_no_changes(self):
        self.assertTrue(_drift_within_threshold({"summary": {"count_changed": 0}}))

    def test_drift_within_threshold_false_when_any_changed(self):
        self.assertFalse(_drift_within_threshold({"summary": {"count_changed": 1}}))

    def test_drift_within_threshold_defaults_true_when_summary_missing(self):
        # A missing "summary"/"count_changed" (an empty/absent
        # drift_report) has no evidence of drift either -- defaults to
        # "nothing found changed", same convention as _ttl_clean below.
        self.assertTrue(_drift_within_threshold({}))
        self.assertTrue(_drift_within_threshold(None))  # type: ignore[arg-type]

    def test_drift_within_threshold_false_when_count_changed_unparseable(self):
        self.assertFalse(_drift_within_threshold({"summary": {"count_changed": "not-a-number"}}))

    # -- ttl_clean -------------------------------------------------------------

    def test_ttl_clean_true_when_no_expired_entries(self):
        self.assertTrue(_ttl_clean({"discovered_ttl": {"expired": [], "valid": [{"book_number": 1.0}]}}))

    def test_ttl_clean_false_when_any_expired(self):
        self.assertFalse(_ttl_clean({"discovered_ttl": {"expired": [{"book_number": 9.0}], "valid": []}}))

    def test_ttl_clean_true_when_malformed(self):
        # Conservative here would arguably be False, but an entirely
        # missing/malformed ttl_report has no evidence of an expired
        # entry either -- treated as "nothing found expired".
        self.assertTrue(_ttl_clean({}))
        self.assertTrue(_ttl_clean(None))  # type: ignore[arg-type]

    # -- skeleton_preview_consistent -------------------------------------------

    def test_skeleton_preview_consistent_true_when_no_missing(self):
        self.assertTrue(_skeleton_preview_consistent({"missing_in_live": [], "missing_in_preview": []}))

    def test_skeleton_preview_consistent_false_when_missing_either_side(self):
        self.assertFalse(_skeleton_preview_consistent({"missing_in_live": ["1.0"], "missing_in_preview": []}))
        self.assertFalse(_skeleton_preview_consistent({"missing_in_live": [], "missing_in_preview": ["2.0"]}))

    # -- confidence_stable / gate_consistent -----------------------------------

    def test_confidence_stable_true_when_matching(self):
        comparison = {
            "by_book_number": {
                "1.0": {
                    "present_in_live": True,
                    "present_in_agentic": True,
                    "live_confidence": {"confidence": "high"},
                    "agentic_confidence": {"overall": "high"},
                }
            }
        }
        self.assertTrue(_confidence_stable(comparison))

    def test_confidence_stable_false_when_mismatched(self):
        comparison = {
            "by_book_number": {
                "1.0": {
                    "present_in_live": True,
                    "present_in_agentic": True,
                    "live_confidence": {"confidence": "high"},
                    "agentic_confidence": {"overall": "low"},
                }
            }
        }
        self.assertFalse(_confidence_stable(comparison))

    def test_confidence_stable_ignores_entries_not_present_on_both_sides(self):
        comparison = {
            "by_book_number": {
                "1.0": {
                    "present_in_live": False,
                    "present_in_agentic": True,
                    "live_confidence": None,
                    "agentic_confidence": {"overall": "low"},
                }
            }
        }
        self.assertTrue(_confidence_stable(comparison))

    def test_confidence_stable_vacuously_true_when_empty(self):
        self.assertTrue(_confidence_stable({"by_book_number": {}}))
        self.assertTrue(_confidence_stable({}))
        self.assertTrue(_confidence_stable(None))  # type: ignore[arg-type]

    def test_gate_consistent_true_when_matching(self):
        comparison = {
            "by_book_number": {
                "1.0": {
                    "present_in_live": True,
                    "present_in_agentic": True,
                    "live_gate": {"belongs_to_series": True},
                    "agentic_gate": {"belongs_to_series": True},
                }
            }
        }
        self.assertTrue(_gate_consistent(comparison))

    def test_gate_consistent_false_when_mismatched(self):
        comparison = {
            "by_book_number": {
                "1.0": {
                    "present_in_live": True,
                    "present_in_agentic": True,
                    "live_gate": {"belongs_to_series": True},
                    "agentic_gate": {"belongs_to_series": False},
                }
            }
        }
        self.assertFalse(_gate_consistent(comparison))

    # -- notes ------------------------------------------------------------------

    def test_build_notes_all_passed(self):
        checks = {name: True for name in _CHECK_LABELS}
        self.assertEqual(_build_notes(checks), "All promotion-readiness checks passed.")

    def test_build_notes_lists_failed_checks(self):
        checks = {name: True for name in _CHECK_LABELS}
        checks["ttl_clean"] = False
        notes = _build_notes(checks)
        self.assertIn("ttl_clean", notes)
        self.assertIn("Not ready", notes)


class GeneratePromotionReadinessIntegrationTest(unittest.TestCase):
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

    def _set_skeleton_json(self, entries):
        row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        row.skeleton_json = entries
        self.db.commit()

    # -- 2: documented shape against a real (unmocked) evaluation ------------

    def test_generate_promotion_readiness_returns_expected_shape(self):
        readiness = generate_promotion_readiness(self.series.id, db_session=self.db)

        self.assertEqual(readiness["series_id"], self.series.id)
        self.assertIn("timestamp", readiness)
        for key in (
            "has_recent_agentic_trace",
            "drift_within_threshold",
            "ttl_clean",
            "confidence_stable",
            "gate_consistent",
            "skeleton_preview_consistent",
        ):
            self.assertIn(key, readiness["checks"])
            self.assertIsInstance(readiness["checks"][key], bool)
        self.assertIn("ready_for_phase2", readiness["summary"])
        self.assertIn("notes", readiness["summary"])
        self.assertEqual(readiness["summary"]["ready_for_phase2"], all(readiness["checks"].values()))

        # This baseline series only has owned/library books, so there's
        # always at least real trace content to evaluate -- unlike
        # `confidence_stable` (see below), this doesn't depend on the
        # shadow loop's synthetic-candidate replay quirks.
        self.assertTrue(readiness["checks"]["has_recent_agentic_trace"])

    def test_generate_promotion_readiness_is_ready_when_every_check_passes(self):
        # `generate_promotion_readiness` only ever combines
        # comparison/drift_report/ttl_report -- fabricate a fully clean
        # evaluation dict (rather than relying on the real confidence
        # engine's synthetic-candidate replay, which currently grades a
        # "skeleton_replay"-sourced candidate's provider_confidence "low"
        # even for an otherwise-perfect library match -- see agents/
        # agentic_series_agent.py's `_synthetic_candidate_for_entry`;
        # that's a real, separate diagnostic finding about the shadow
        # loop's replay fidelity, not something this module should paper
        # over by loosening its own checks) to prove the "everything
        # passes" path end to end.
        clean_evaluation = {
            "series_id": self.series.id,
            "agentic_trace": {"confidence_traces": [{"book_number": 1.0, "after": {"overall": "high"}}]},
            "comparison": {
                "by_book_number": {
                    "1.0": {
                        "present_in_live": True,
                        "present_in_agentic": True,
                        "live_confidence": {"confidence": "high"},
                        "agentic_confidence": {"overall": "high"},
                        "live_gate": {"belongs_to_series": True},
                        "agentic_gate": {"belongs_to_series": True},
                    }
                }
            },
            "drift_report": {"missing_in_live": [], "missing_in_preview": [], "summary": {"count_changed": 0}},
            "ttl_report": {"discovered_ttl": {"expired": [], "valid": []}},
        }
        with patch(
            "services.agentic_promotion_checklist.run_agentic_evaluation_for_series", return_value=clean_evaluation
        ):
            readiness = generate_promotion_readiness(self.series.id, db_session=self.db)

        self.assertTrue(all(readiness["checks"].values()), readiness["checks"])
        self.assertTrue(readiness["summary"]["ready_for_phase2"])
        self.assertEqual(readiness["summary"]["notes"], "All promotion-readiness checks passed.")

    # -- 3: correctly reports not-ready on real problems ----------------------

    def test_series_not_found_is_not_ready_but_only_lacks_a_trace(self):
        readiness = generate_promotion_readiness(999999, db_session=self.db)

        self.assertEqual(readiness["series_id"], 999999)
        self.assertFalse(readiness["checks"]["has_recent_agentic_trace"])
        # Nothing to compare/drift/expire for a series that doesn't
        # exist, so every other check is vacuously true.
        for key in ("drift_within_threshold", "ttl_clean", "confidence_stable", "gate_consistent", "skeleton_preview_consistent"):
            self.assertTrue(readiness["checks"][key], key)
        self.assertFalse(readiness["summary"]["ready_for_phase2"])
        self.assertIn("has_recent_agentic_trace", readiness["summary"]["notes"])

    def test_expired_discovered_entry_makes_ttl_report_dirty_and_not_ready(self):
        stale_iso = (datetime.utcnow() - timedelta(days=DISCOVERED_ENTRY_TTL_DAYS + 5)).isoformat()
        entries = self._skeleton_json() + [
            {
                "book_number": 9.0,
                "title": "Discovered Stale",
                "status": "unconfirmed",
                "confidence": "medium",
                "source_class": "discovered",
                "first_seen_at": stale_iso,
                "last_confirmed_at": stale_iso,
            }
        ]
        self._set_skeleton_json(entries)

        readiness = generate_promotion_readiness(self.series.id, db_session=self.db)

        self.assertFalse(readiness["checks"]["ttl_clean"])
        self.assertFalse(readiness["summary"]["ready_for_phase2"])
        self.assertIn("Not ready", readiness["summary"]["notes"])

    # -- 4: no writes ----------------------------------------------------------

    def test_no_state_changes(self):
        before_counts = self._row_counts()
        before_skeleton = self._skeleton_json()

        generate_promotion_readiness(self.series.id, db_session=self.db)

        self.assertEqual(self._row_counts(), before_counts)
        self.assertEqual(self._skeleton_json(), before_skeleton)

    # -- 5: fails conservatively -----------------------------------------------

    def test_fails_conservatively_when_evaluation_harness_raises(self):
        with patch(
            "services.agentic_promotion_checklist.run_agentic_evaluation_for_series",
            side_effect=RuntimeError("boom"),
        ):
            readiness = generate_promotion_readiness(self.series.id, db_session=self.db)

        self.assertEqual(readiness["series_id"], self.series.id)
        self.assertTrue(all(value is False for value in readiness["checks"].values()))
        self.assertFalse(readiness["summary"]["ready_for_phase2"])
        self.assertIn("failed", readiness["summary"]["notes"].lower())


if __name__ == "__main__":
    unittest.main()
