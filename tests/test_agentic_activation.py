"""Phase 4, controlled activation (guarded live agentic routing) --
`settings.AGENTIC_SERIES_ACTIVATION`/`settings.is_agentic_activated`,
`agents/series_agent.py`'s activation gate layered on top of Phase 3's
promotion layer, and the two new read-only admin endpoints
(`/admin/agentic/activation-preview/{series_id}`, `/admin/agentic/
activation-status`).

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here), this file needs to prove:

1. `is_agentic_activated` is a per-series AND-gate on top of
   `AGENTIC_ROUTING_ENABLED` -- off if the global flag is off regardless
   of the allowlist, and per-series otherwise.
2. With the global flag on but a series NOT activated: promotion
   decisions are still evaluated and recorded (Phase 3 behavior,
   unchanged), but `resolved_confidence`/`resolved_gate` always stay the
   *live* values -- "record, don't apply".
3. With a series activated: `resolved_confidence`/`resolved_gate`
   actually become the *agentic* values exactly when the promotion
   evaluator chose `"use_agentic"` -- "record AND apply".
4. Activation is genuinely per-series, not global once the allowlist has
   any entry.
5. The activation-preview endpoint's hypothetical resolution matches
   hand-computed expectations from stored promotion history.
6. The activation-status endpoint is owner-only and reflects the
   allowlist.
7. The whole layer is fail-soft, never calls a provider, and never
   writes `skeleton_json`/`probes_json`.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import discovery_engine
import main
import settings
from agents.series_agent import SeriesIntelligenceAgent
from database import Base
from models import AgenticPromotionDecision, Book, Series, SeriesSkeleton
from routers.deps import create_owner_token
from services.agentic_promotion_evaluator import build_activation_preview, store_promotion_decision


class IsAgenticActivatedTest(unittest.TestCase):
    def test_false_when_global_flag_off_regardless_of_allowlist(self):
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", False), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1,2,3"
        ):
            self.assertFalse(settings.is_agentic_activated(1))
            self.assertFalse(settings.is_agentic_activated(2))

    def test_true_only_for_series_in_allowlist(self):
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1, 3"
        ):
            self.assertTrue(settings.is_agentic_activated(1))
            self.assertFalse(settings.is_agentic_activated(2))
            self.assertTrue(settings.is_agentic_activated(3))

    def test_empty_allowlist_activates_nothing(self):
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", ""
        ):
            self.assertFalse(settings.is_agentic_activated(1))

    def test_malformed_allowlist_entries_are_skipped_not_raised(self):
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1,not-an-int, 3"
        ):
            self.assertTrue(settings.is_agentic_activated(1))
            self.assertTrue(settings.is_agentic_activated(3))
            self.assertFalse(settings.is_agentic_activated(999))


class ActivationLayerInSeriesAgentTest(unittest.TestCase):
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
        self.series_a = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
        self.series_b = Series(name="Frostbound Chronicles", author="Harmon Cooper", profile_id="robbie")
        self.db.add_all([self.series_a, self.series_b])
        self.db.commit()
        self.db.refresh(self.series_a)
        self.db.refresh(self.series_b)
        for series in (self.series_a, self.series_b):
            for number in [1, 2, 3]:
                self.db.add(
                    Book(
                        title=f"{series.name} Book {number}",
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

    def _run(self, series):
        agent = SeriesIntelligenceAgent()
        return agent.run_series_check(self.db, series.id, emit_summary=False)

    def test_activation_flag_off_preserves_live_behavior(self):
        # Global flag on, but nothing in the allowlist -- Phase 3's
        # "record, don't apply" behavior: outcome recorded as
        # "use_agentic", but resolved_* must stay live.
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", ""
        ), patch("agents.agentic_series_agent.SessionLocal", self.SessionLocal), patch(
            "services.agentic_promotion_evaluator.evaluate_promotion", return_value="use_agentic"
        ):
            result = self._run(self.series_a)

        payload = result["agentic_promotion"]
        self.assertTrue(payload["enabled"])
        self.assertFalse(payload["activated"])
        self.assertGreater(len(payload["promotions"]), 0)
        for promotion in payload["promotions"]:
            self.assertEqual(promotion["outcome"], "use_agentic")
            # Live confidence snapshot shape always has "confidence"/"status".
            self.assertIn("confidence", promotion["resolved_confidence"])
            self.assertEqual(promotion["resolved_gate"], {"belongs_to_series": True, "source_class": "library"})

    def test_activation_flag_on_applies_agentic_decisions(self):
        # Phase 7 note: evaluate_promotion is mocked to force
        # "use_agentic", but services/agentic_resolution.py now
        # independently re-validates that against the fixture's real
        # confidence/gate data (defense-in-depth) before applying it --
        # mocked here too so this test can keep isolating "does
        # activation apply the agentic side" from "is this fixture's
        # data actually safe" (covered separately by
        # tests/test_agentic_safety.py).
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", str(self.series_a.id)
        ), patch("agents.agentic_series_agent.SessionLocal", self.SessionLocal), patch(
            "services.agentic_promotion_evaluator.evaluate_promotion", return_value="use_agentic"
        ), patch("services.agentic_resolution.validate_agentic_decision", return_value=True):
            result = self._run(self.series_a)

        payload = result["agentic_promotion"]
        self.assertTrue(payload["activated"])
        self.assertGreater(len(payload["promotions"]), 0)
        for promotion in payload["promotions"]:
            self.assertEqual(promotion["outcome"], "use_agentic")
            # Agentic confidence shape has "overall"/dimension keys, not "confidence".
            self.assertNotIn("confidence", promotion["resolved_confidence"])
            self.assertIn("overall", promotion["resolved_confidence"])

    def test_activation_is_per_series(self):
        # Only series_a is activated -- series_b must still resolve to
        # live even with the exact same global flag/mocked outcome.
        # Phase 7 note: see test_activation_flag_on_applies_agentic_
        # decisions above for why validate_agentic_decision is also
        # mocked here.
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", str(self.series_a.id)
        ), patch("agents.agentic_series_agent.SessionLocal", self.SessionLocal), patch(
            "services.agentic_promotion_evaluator.evaluate_promotion", return_value="use_agentic"
        ), patch("services.agentic_resolution.validate_agentic_decision", return_value=True):
            result_a = self._run(self.series_a)
            result_b = self._run(self.series_b)

        self.assertTrue(result_a["agentic_promotion"]["activated"])
        self.assertFalse(result_b["agentic_promotion"]["activated"])

        for promotion in result_a["agentic_promotion"]["promotions"]:
            self.assertNotIn("confidence", promotion["resolved_confidence"])
        for promotion in result_b["agentic_promotion"]["promotions"]:
            self.assertIn("confidence", promotion["resolved_confidence"])

    def test_activation_fail_soft(self):
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", str(self.series_a.id)
        ), patch(
            "agents.agentic_series_agent.run_agentic_turn", side_effect=RuntimeError("shadow loop exploded")
        ):
            result = self._run(self.series_a)

        self.assertIn("found", result)
        self.assertTrue(result["agentic_promotion"]["enabled"])
        self.assertFalse(result["agentic_promotion"]["activated"])
        self.assertEqual(result["agentic_promotion"]["promotions"], [])
        self.assertTrue(result["agentic_promotion"].get("error"))

    def test_activation_fail_soft_when_is_agentic_activated_itself_raises(self):
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch(
            "settings.is_agentic_activated", side_effect=RuntimeError("allowlist parsing exploded")
        ):
            result = self._run(self.series_a)

        self.assertIn("found", result)
        self.assertTrue(result["agentic_promotion"].get("error"))

    def test_no_skeleton_or_probes_mutation(self):
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", str(self.series_a.id)
        ), patch("agents.agentic_series_agent.SessionLocal", self.SessionLocal), patch(
            "services.agentic_promotion_evaluator.evaluate_promotion", return_value="use_agentic"
        ):
            result = self._run(self.series_a)

        self.assertEqual(result["probes"], [])
        skeleton_row = (
            self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series_a.id).first()
        )
        self.assertIsNotNone(skeleton_row)
        for entry in skeleton_row.skeleton_json:
            if entry.get("source_class", "library") == "library":
                # Untouched by activation -- still exactly what
                # backfill_skeleton_for_series alone produces.
                self.assertEqual(entry.get("status"), "confirmed")
                self.assertEqual(entry.get("confidence"), "high")

    def test_no_provider_calls_during_activation(self):
        with self._mock_discovery() as mock_discover, patch.object(
            settings, "AGENTIC_ROUTING_ENABLED", True
        ), patch.object(settings, "AGENTIC_SERIES_ACTIVATION", str(self.series_a.id)), patch(
            "agents.agentic_series_agent.SessionLocal", self.SessionLocal
        ), patch("services.agentic_promotion_evaluator.evaluate_promotion", return_value="use_agentic"):
            self._run(self.series_a)

        # discover_candidates_for_series is the live pipeline's own,
        # single provider call -- the activation layer must not add any
        # additional provider calls of its own.
        mock_discover.assert_called_once()


class ActivationPreviewTest(unittest.TestCase):
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

    def test_activation_preview_matches_expected_resolution(self):
        store_promotion_decision(
            self.series.id,
            1.0,
            {"confidence": "medium"},
            {"overall": "high"},
            {"belongs_to_series": True},
            {"belongs_to_series": True},
            "use_agentic",
            db_session=self.db,
        )
        store_promotion_decision(
            self.series.id,
            2.0,
            {"confidence": "high"},
            {"overall": "high"},
            {"belongs_to_series": True},
            {"belongs_to_series": True},
            "use_live",
            db_session=self.db,
        )
        # A second, later decision for book 1 -- the preview should use
        # only the most recent one per book_number.
        store_promotion_decision(
            self.series.id,
            1.0,
            {"confidence": "medium"},
            {"overall": "medium"},
            {"belongs_to_series": True},
            {"belongs_to_series": True},
            "use_live",
            db_session=self.db,
        )

        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", ""
        ):
            preview = build_activation_preview(self.series.id, db_session=self.db)

        self.assertFalse(preview["activated"])
        self.assertEqual(
            preview["preview"]["1.0"],
            {"outcome": "use_live", "resolved_confidence": {"confidence": "medium"}, "resolved_gate": {"belongs_to_series": True}},
        )
        self.assertEqual(
            preview["preview"]["2.0"],
            {"outcome": "use_live", "resolved_confidence": {"confidence": "high"}, "resolved_gate": {"belongs_to_series": True}},
        )

    def test_activation_preview_shows_agentic_resolution_for_use_agentic_outcome(self):
        store_promotion_decision(
            self.series.id,
            5.0,
            {"confidence": "medium"},
            {"overall": "high"},
            {"belongs_to_series": True},
            {"belongs_to_series": True},
            "use_agentic",
            db_session=self.db,
        )

        preview = build_activation_preview(self.series.id, db_session=self.db)
        self.assertEqual(
            preview["preview"]["5.0"],
            {"outcome": "use_agentic", "resolved_confidence": {"overall": "high"}, "resolved_gate": {"belongs_to_series": True}},
        )

    def test_activation_preview_empty_for_series_with_no_history(self):
        preview = build_activation_preview(999999, db_session=self.db)
        self.assertEqual(preview, {"activated": False, "preview": {}})

    def test_activation_preview_reflects_real_activation_state(self):
        store_promotion_decision(
            self.series.id, 1.0, {}, {}, {}, {}, "use_live", db_session=self.db
        )
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", str(self.series.id)
        ):
            preview = build_activation_preview(self.series.id, db_session=self.db)
        self.assertTrue(preview["activated"])

    def test_activation_preview_fail_soft(self):
        from unittest.mock import MagicMock

        broken_db = MagicMock()
        broken_db.query.side_effect = RuntimeError("query exploded")
        preview = build_activation_preview(self.series.id, db_session=broken_db)
        self.assertEqual(preview, {"activated": False, "preview": {}})


class AdminAgenticActivationEndpointsTest(unittest.TestCase):
    """Same reasoning as the other admin-endpoint tests in this suite:
    TestClient dispatches through `main.app`'s real `database.
    SessionLocal`, so this class writes/reads through that same real
    session directly, against a series_id chosen not to collide with
    real data, cleaning up afterward.
    """

    SERIES_ID = -999999994

    def setUp(self):
        from database import SessionLocal as RealSessionLocal

        self.RealSessionLocal = RealSessionLocal
        store_promotion_decision(
            self.SERIES_ID,
            3.0,
            {"confidence": "medium"},
            {"overall": "high"},
            {"belongs_to_series": True},
            {"belongs_to_series": True},
            "use_agentic",
        )

    def tearDown(self):
        db = self.RealSessionLocal()
        try:
            db.query(AgenticPromotionDecision).filter(
                AgenticPromotionDecision.series_id == self.SERIES_ID
            ).delete()
            db.commit()
        finally:
            db.close()

    def test_activation_preview_endpoint_owner_only(self):
        client = TestClient(main.app)

        anon = client.get(f"/admin/agentic/activation-preview/{self.SERIES_ID}")
        self.assertEqual(anon.status_code, 403)

        owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}
        owner = client.get(f"/admin/agentic/activation-preview/{self.SERIES_ID}", headers=owner_headers)
        self.assertEqual(owner.status_code, 200)
        body = owner.json()
        self.assertEqual(body["series_id"], self.SERIES_ID)
        self.assertIn("3.0", body["preview"])
        self.assertEqual(body["preview"]["3.0"]["outcome"], "use_agentic")

    def test_activation_status_endpoint_owner_only(self):
        client = TestClient(main.app)

        anon = client.get("/admin/agentic/activation-status")
        self.assertEqual(anon.status_code, 403)

        owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}
        with patch.object(settings, "AGENTIC_SERIES_ACTIVATION", f"{self.SERIES_ID},42"):
            owner = client.get("/admin/agentic/activation-status", headers=owner_headers)
        self.assertEqual(owner.status_code, 200)
        body = owner.json()
        self.assertEqual(sorted(body["activated_series"]), sorted([self.SERIES_ID, 42]))


if __name__ == "__main__":
    unittest.main()
