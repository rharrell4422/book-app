"""Phase 2 kickoff, first implementation block:
`services/agentic_promotion_plan.py`'s `build_phase2_promotion_plan`,
plus its `/admin/agentic/promotion-plan/{series_id}` wiring.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here; this is Phase 2 scaffolding on top of that
settled Phase 1 architecture), this file needs to prove:

1. `build_phase2_promotion_plan` returns the documented shape.
2. Alignment flags (`confidence_alignment`/`gate_alignment`/`skeleton_
   alignment`) are correctly derived from a real evaluation, including
   for a series that doesn't exist (vacuously aligned, nothing to
   compare).
3. `risk_assessment.risk_level` is correctly derived from how many
   requirement signals are aligned.
4. `promotion_steps` is always the same fixed, static list -- never
   conditioned on the current risk level (this block does not decide to
   promote anything).
5. The admin endpoint requires owner auth.
6. Nothing here writes anything.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
from database import Base
from models import Book, Series, SeriesSkeleton
from routers.deps import create_owner_token
from services.agentic_promotion_plan import (
    PROMOTION_STEPS,
    _no_recent_errors,
    _provider_stability_verified,
    _risk_level_and_notes,
    build_phase2_promotion_plan,
)
from services.skeleton_store import backfill_skeleton_for_series


class RiskAndSignalHelpersTest(unittest.TestCase):
    # -- risk level derivation --------------------------------------------

    def test_risk_level_low_when_all_signals_aligned(self):
        signals = {name: True for name in ("a", "b", "c", "d", "e", "f", "g")}
        risk_level, notes = _risk_level_and_notes(signals)
        self.assertEqual(risk_level, "low")
        self.assertIn("All promotion requirement signals aligned", notes)

    def test_risk_level_medium_when_one_or_two_signals_fail(self):
        signals = {name: True for name in ("a", "b", "c", "d", "e", "f", "g")}
        signals["a"] = False
        risk_level, notes = _risk_level_and_notes(signals)
        self.assertEqual(risk_level, "medium")
        self.assertIn("a", notes)

        signals["b"] = False
        risk_level, _ = _risk_level_and_notes(signals)
        self.assertEqual(risk_level, "medium")

    def test_risk_level_high_when_three_or_more_signals_fail(self):
        signals = {name: True for name in ("a", "b", "c", "d", "e", "f", "g")}
        signals["a"] = False
        signals["b"] = False
        signals["c"] = False
        risk_level, notes = _risk_level_and_notes(signals)
        self.assertEqual(risk_level, "high")
        self.assertIn("3 of 7", notes)

    # -- provider_stability_verified ---------------------------------------

    def test_provider_stability_verified_true_when_all_calls_use_serper(self):
        trace = {"provider_calls": [{"provider": "serper"}, {"provider": "serper"}]}
        self.assertTrue(_provider_stability_verified(trace))

    def test_provider_stability_verified_false_when_empty(self):
        self.assertFalse(_provider_stability_verified({"provider_calls": []}))
        self.assertFalse(_provider_stability_verified({}))
        self.assertFalse(_provider_stability_verified(None))  # type: ignore[arg-type]

    def test_provider_stability_verified_false_when_unexpected_provider(self):
        trace = {"provider_calls": [{"provider": "serper"}, {"provider": "apify"}]}
        self.assertFalse(_provider_stability_verified(trace))

    # -- no_recent_errors ----------------------------------------------------

    def test_no_recent_errors_true_when_no_stop_step(self):
        trace = {"reasoning_steps": [{"phase": "probe", "decision": "continue"}]}
        self.assertTrue(_no_recent_errors(trace))
        self.assertTrue(_no_recent_errors({}))
        self.assertTrue(_no_recent_errors(None))  # type: ignore[arg-type]

    def test_no_recent_errors_false_when_stop_step_present(self):
        trace = {"reasoning_steps": [{"phase": "precheck", "decision": "stop", "reason": "series-not-found"}]}
        self.assertFalse(_no_recent_errors(trace))


class BuildPhase2PromotionPlanIntegrationTest(unittest.TestCase):
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

    # -- 1: documented shape --------------------------------------------------

    def test_promotion_plan_structure(self):
        plan = build_phase2_promotion_plan(self.series.id, db_session=self.db)

        self.assertEqual(plan["series_id"], self.series.id)
        self.assertIn("timestamp", plan)

        requirements = plan["requirements"]
        for key in (
            "confidence_alignment",
            "gate_alignment",
            "skeleton_alignment",
            "ttl_clean",
            "drift_within_threshold",
            "provider_stability_verified",
            "no_recent_errors",
        ):
            self.assertIn(key, requirements)

        for alignment_key, expected_keys in (
            ("confidence_alignment", {"live", "agentic", "aligned"}),
            ("gate_alignment", {"live", "agentic", "aligned"}),
            ("skeleton_alignment", {"live", "preview", "aligned"}),
        ):
            self.assertEqual(set(requirements[alignment_key].keys()), expected_keys)
            self.assertIsInstance(requirements[alignment_key]["aligned"], bool)

        self.assertIn("risk_level", plan["risk_assessment"])
        self.assertIn(plan["risk_assessment"]["risk_level"], ("low", "medium", "high"))
        self.assertIn("notes", plan["risk_assessment"])
        self.assertIsInstance(plan["promotion_steps"], list)

    # -- 2: alignment flags derived from a real evaluation --------------------

    def test_promotion_plan_alignment_flags(self):
        plan = build_phase2_promotion_plan(self.series.id, db_session=self.db)
        requirements = plan["requirements"]

        # Real book_numbers appear on both sides of the confidence/gate
        # alignment summaries for this library-owned baseline series.
        self.assertEqual(set(requirements["confidence_alignment"]["live"].keys()), {"1.0", "2.0", "3.0"})
        self.assertEqual(set(requirements["gate_alignment"]["live"].keys()), {"1.0", "2.0", "3.0"})
        self.assertEqual(set(requirements["skeleton_alignment"]["live"].keys()), {"1.0", "2.0", "3.0"})
        # Every live gate entry for an owned book is True.
        self.assertTrue(all(requirements["gate_alignment"]["live"].values()))
        self.assertTrue(requirements["gate_alignment"]["aligned"])
        self.assertTrue(requirements["skeleton_alignment"]["aligned"])
        # Deliberately not asserting confidence_alignment["aligned"] here
        # -- see test_promotion_plan_risk_assessment's comment for why
        # it's currently False even for this clean baseline (a real,
        # separate finding about the shadow loop's synthetic-candidate
        # replay, not a bug in this check).

        # No discovered entries in this baseline -> clean TTL/drift.
        self.assertTrue(requirements["ttl_clean"])
        self.assertTrue(requirements["drift_within_threshold"])

        # The shadow loop actually probed all three candidates via Serper.
        self.assertTrue(requirements["provider_stability_verified"])
        self.assertTrue(requirements["no_recent_errors"])

    def test_promotion_plan_alignment_flags_for_missing_series(self):
        plan = build_phase2_promotion_plan(999999, db_session=self.db)
        requirements = plan["requirements"]

        # Nothing to compare -> vacuously aligned/clean.
        self.assertTrue(requirements["confidence_alignment"]["aligned"])
        self.assertTrue(requirements["gate_alignment"]["aligned"])
        self.assertTrue(requirements["skeleton_alignment"]["aligned"])
        self.assertTrue(requirements["ttl_clean"])
        self.assertTrue(requirements["drift_within_threshold"])
        # But no real trace was ever produced for a series that doesn't
        # exist -- both single-run signals correctly flag that.
        self.assertFalse(requirements["provider_stability_verified"])
        self.assertFalse(requirements["no_recent_errors"])

    # -- 3: risk assessment ----------------------------------------------------

    def test_promotion_plan_risk_assessment(self):
        plan = build_phase2_promotion_plan(self.series.id, db_session=self.db)
        # Clean baseline series: six of seven signals aligned. The one
        # exception -- confidence_alignment -- fails even here because
        # the shadow loop's synthetic-candidate replay (`agents/
        # agentic_series_agent.py`'s `_synthetic_candidate_for_entry`,
        # `source: "skeleton_replay"`) always grades provider_confidence
        # "low", dragging its `overall` below the live entry's "high" --
        # a real, separate diagnostic finding about replay fidelity (see
        # `services/agentic_promotion_checklist.py`'s test suite for the
        # identical finding), not something this module should paper
        # over. One signal failing -> "medium", not "low".
        self.assertEqual(plan["risk_assessment"]["risk_level"], "medium")
        self.assertEqual(plan["risk_assessment"]["notes"].count(","), 0)  # exactly one failed signal listed

        plan_missing = build_phase2_promotion_plan(999999, db_session=self.db)
        # Two signals fail for a nonexistent series (provider stability,
        # no_recent_errors) -> also medium risk.
        self.assertEqual(plan_missing["risk_assessment"]["risk_level"], "medium")

    def test_promotion_plan_risk_assessment_is_low_when_every_signal_aligns(self):
        # Fabricate a fully clean evaluation (same technique as
        # services/agentic_promotion_checklist.py's test suite) to prove
        # the "low risk" path end to end, independent of the shadow
        # loop's current synthetic-candidate replay quirk above.
        clean_evaluation = {
            "series_id": self.series.id,
            "agentic_trace": {
                "provider_calls": [{"provider": "serper"}],
                "reasoning_steps": [{"phase": "probe", "decision": "continue"}],
            },
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
            "drift_report": {
                "missing_in_live": [],
                "missing_in_preview": [],
                "summary": {"count_changed": 0},
                "by_book_number": {},
            },
            "ttl_report": {"discovered_ttl": {"expired": [], "valid": []}},
        }
        with patch(
            "services.agentic_promotion_plan.run_agentic_evaluation_for_series", return_value=clean_evaluation
        ):
            plan = build_phase2_promotion_plan(self.series.id, db_session=self.db)

        self.assertEqual(plan["risk_assessment"]["risk_level"], "low")
        self.assertTrue(all(plan["requirements"][key] for key in ("ttl_clean", "drift_within_threshold", "provider_stability_verified", "no_recent_errors")))
        self.assertTrue(plan["requirements"]["confidence_alignment"]["aligned"])
        self.assertTrue(plan["requirements"]["gate_alignment"]["aligned"])
        self.assertTrue(plan["requirements"]["skeleton_alignment"]["aligned"])

    # -- 4: promotion_steps is fixed and always present ------------------------

    def test_promotion_plan_steps_present(self):
        plan_ready = build_phase2_promotion_plan(self.series.id, db_session=self.db)
        plan_missing = build_phase2_promotion_plan(999999, db_session=self.db)

        self.assertEqual(plan_ready["promotion_steps"], PROMOTION_STEPS)
        self.assertEqual(plan_missing["promotion_steps"], PROMOTION_STEPS)
        self.assertIn("Manual approval required", plan_ready["promotion_steps"])
        self.assertEqual(plan_ready["promotion_steps"][-1], "Manual approval required")

    # -- 5: owner-only admin endpoint --------------------------------------------

    def test_admin_endpoint_owner_only(self):
        client = TestClient(main.app)

        anonymous_response = client.get(f"/admin/agentic/promotion-plan/{self.series.id}")
        self.assertEqual(anonymous_response.status_code, 403)

        owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}
        owner_response = client.get(f"/admin/agentic/promotion-plan/{self.series.id}", headers=owner_headers)
        self.assertEqual(owner_response.status_code, 200)
        body = owner_response.json()
        self.assertEqual(body["series_id"], self.series.id)
        self.assertIn("requirements", body)
        self.assertIn("risk_assessment", body)
        self.assertIn("promotion_steps", body)

    # -- 6: no writes ----------------------------------------------------------

    def test_no_state_changes(self):
        before_counts = self._row_counts()
        before_skeleton = self._skeleton_json()

        build_phase2_promotion_plan(self.series.id, db_session=self.db)

        self.assertEqual(self._row_counts(), before_counts)
        self.assertEqual(self._skeleton_json(), before_skeleton)

    def test_logs_plan_via_telemetry(self):
        with patch("services.agentic_promotion_plan.record_agentic_promotion_plan") as mock_record:
            plan = build_phase2_promotion_plan(self.series.id, db_session=self.db)

        mock_record.assert_called_once()
        call_series_id, call_plan = mock_record.call_args[0]
        self.assertEqual(call_series_id, self.series.id)
        self.assertEqual(call_plan, plan)

    def test_fails_conservatively_when_evaluation_harness_raises(self):
        with patch(
            "services.agentic_promotion_plan.run_agentic_evaluation_for_series",
            side_effect=RuntimeError("boom"),
        ):
            plan = build_phase2_promotion_plan(self.series.id, db_session=self.db)

        self.assertEqual(plan["risk_assessment"]["risk_level"], "high")
        self.assertFalse(plan["requirements"]["ttl_clean"])
        self.assertFalse(plan["requirements"]["confidence_alignment"]["aligned"])
        self.assertEqual(plan["promotion_steps"], PROMOTION_STEPS)


if __name__ == "__main__":
    unittest.main()
