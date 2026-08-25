"""Phase 3, candidate promotion (feature-flagged live routing) --
`services/agentic_promotion_evaluator.py`'s `evaluate_promotion`/
`store_promotion_decision`/`get_promotion_history`, the new
`AgenticPromotionDecision` model/`agentic_promotion_decisions` table,
`settings.AGENTIC_ROUTING_ENABLED`, `agents/series_agent.py`'s new
feature-flagged promotion layer, and the read-only `/admin/agentic/
promotion-history/{series_id}` endpoint.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here), this file needs to prove:

1. `evaluate_promotion` implements the plan's rules deterministically:
   an agentic decision that strictly improves confidence with a
   consistent gate wins ("use_agentic"), a decision that neither
   improves nor violates anything defers to live ("use_live"), and a
   decision that violates a required field or contradicts the live gate
   is rejected outright ("reject_agentic").
2. `store_promotion_decision`/`get_promotion_history` write/read the new
   shadow table correctly.
3. `agents/series_agent.py`'s live routing path: with the flag off,
   behaves byte-for-byte as before (no agentic call, no shadow-table
   write); with the flag on, actually calls the promotion machinery and
   reflects `"use_agentic"` outcomes in `result["agentic_promotion"]`;
   and never lets a failure inside this layer propagate or affect the
   live result.
4. The new admin endpoint is owner-only and returns the stored history.
"""

import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import settings
from agents.series_agent import SeriesIntelligenceAgent
from database import Base
from models import AgenticPromotionDecision, Book, Series, SeriesSkeleton
from routers.deps import create_owner_token
from services.agentic_promotion_evaluator import (
    evaluate_promotion,
    get_promotion_history,
    store_promotion_decision,
)


class EvaluatePromotionTest(unittest.TestCase):
    """Pure-function tests for `evaluate_promotion` -- no DB involved."""

    def test_promotion_evaluator_use_agentic(self):
        live_conf = {"confidence": "medium"}
        agentic_conf = {
            "overall": "high",
            "provider_confidence": "high",
            "title_confidence": "high",
            "number_confidence": "high",
            "series_alignment_confidence": "high",
        }
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"gate_output": {"belongs_to_series": True}}

        outcome = evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate)
        self.assertEqual(outcome, "use_agentic")

    def test_promotion_evaluator_use_live(self):
        # Agentic matches live exactly -- no improvement, no violation.
        live_conf = {"confidence": "high"}
        agentic_conf = {"overall": "high"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        outcome = evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate)
        self.assertEqual(outcome, "use_live")

    def test_promotion_evaluator_reject_agentic_on_lower_confidence(self):
        live_conf = {"confidence": "high"}
        agentic_conf = {"overall": "low"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        outcome = evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate)
        self.assertEqual(outcome, "reject_agentic")

    def test_promotion_evaluator_reject_agentic_on_gate_contradiction(self):
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"gate_output": {"belongs_to_series": False}}

        outcome = evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate)
        self.assertEqual(outcome, "reject_agentic")

    def test_promotion_evaluator_reject_agentic_on_degenerate_agentic_decision(self):
        # Agentic side offers literally no opinion at all while live has one --
        # a deterministic-invariant violation, not just "no improvement".
        live_conf = {"confidence": "medium"}
        agentic_conf = {}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {}

        outcome = evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate)
        self.assertEqual(outcome, "reject_agentic")

    def test_promotion_evaluator_ignores_dimensions_only_one_side_reports(self):
        # live has no "provider_confidence" at all -- agentic reporting one
        # (regardless of grade) must not count as a violation or a win on
        # that dimension since there's nothing to compare it against.
        live_conf = {"confidence": "high"}
        agentic_conf = {"overall": "high", "provider_confidence": "zero"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        outcome = evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate)
        self.assertEqual(outcome, "use_live")


class StorePromotionDecisionTest(unittest.TestCase):
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

    def test_store_promotion_decision_writes_shadow_table(self):
        store_promotion_decision(
            self.series.id,
            7.0,
            {"confidence": "medium"},
            {"overall": "high"},
            {"belongs_to_series": True},
            {"belongs_to_series": True},
            "use_agentic",
            db_session=self.db,
        )

        rows = (
            self.db.query(AgenticPromotionDecision)
            .filter(AgenticPromotionDecision.series_id == self.series.id)
            .all()
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].book_number, 7.0)
        self.assertEqual(rows[0].live_confidence, {"confidence": "medium"})
        self.assertEqual(rows[0].agentic_confidence, {"overall": "high"})
        self.assertEqual(rows[0].live_gate, {"belongs_to_series": True})
        self.assertEqual(rows[0].agentic_gate, {"belongs_to_series": True})
        self.assertEqual(rows[0].promotion_outcome, "use_agentic")
        self.assertIsNotNone(rows[0].timestamp)

    def test_get_promotion_history_returns_all(self):
        store_promotion_decision(
            self.series.id, 7.0, {"a": 1}, {"b": 1}, {"c": 1}, {"d": 1}, "use_live", db_session=self.db
        )
        store_promotion_decision(
            self.series.id, 8.0, {"a": 2}, {"b": 2}, {"c": 2}, {"d": 2}, "reject_agentic", db_session=self.db
        )

        history = get_promotion_history(self.series.id, db_session=self.db)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["book_number"], 7.0)
        self.assertEqual(history[0]["promotion_outcome"], "use_live")
        self.assertEqual(history[1]["book_number"], 8.0)
        self.assertEqual(history[1]["promotion_outcome"], "reject_agentic")
        for entry in history:
            self.assertIn("id", entry)
            self.assertIn("timestamp", entry)
            self.assertIn("live_confidence", entry)
            self.assertIn("agentic_confidence", entry)
            self.assertIn("live_gate", entry)
            self.assertIn("agentic_gate", entry)

    def test_get_promotion_history_returns_empty_list_for_unknown_series(self):
        history = get_promotion_history(999999, db_session=self.db)
        self.assertEqual(history, [])

    def test_store_write_failure_is_fail_soft_and_logged(self):
        broken_db = MagicMock()
        broken_db.commit.side_effect = RuntimeError("commit exploded")

        with patch("services.discovery_telemetry.record_agentic_promotion_error") as mock_record_error:
            store_promotion_decision(
                self.series.id, 7.0, {}, {}, {}, {}, "use_live", db_session=broken_db
            )

        mock_record_error.assert_called_once()
        call_series_id, call_error = mock_record_error.call_args[0]
        self.assertEqual(call_series_id, self.series.id)
        self.assertTrue(call_error)

    def test_get_read_failure_is_fail_soft(self):
        broken_db = MagicMock()
        broken_db.query.side_effect = RuntimeError("query exploded")

        history = get_promotion_history(self.series.id, db_session=broken_db)
        self.assertEqual(history, [])

    def test_never_touches_live_skeleton(self):
        self.db.add(
            SeriesSkeleton(
                series_id=self.series.id,
                skeleton_json=[{"book_number": 7, "confidence": "high", "status": "confirmed"}],
                schema_version=2,
            )
        )
        self.db.commit()

        before = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        before_json = list(before.skeleton_json)

        store_promotion_decision(
            self.series.id, 7.0, {"confidence": "high"}, {"overall": "low"}, {}, {}, "reject_agentic",
            db_session=self.db,
        )

        after = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        self.assertEqual(list(after.skeleton_json), before_json)


class PromotionLayerInSeriesAgentTest(unittest.TestCase):
    """`agents/series_agent.py`'s live routing path -- feature-flagged
    promotion layer, gated by `settings.AGENTIC_ROUTING_ENABLED`.
    """

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
        # A traced book needs a SeriesSkeleton entry to exist -- see
        # agents/agentic_series_agent.run_agentic_turn's own docstring:
        # its candidate_numbers come from the skeleton (backfilled from
        # owned Book rows below), not from newly-discovered candidates.
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

    def test_promotion_layer_respects_feature_flag_off(self):
        # Note: agents/series_agent.py's *separate*, pre-existing Phase 2
        # dry-run block always calls run_agentic_turn regardless of this
        # flag (that block predates AGENTIC_ROUTING_ENABLED and is not
        # gated by it) -- so this test asserts on the promotion layer's
        # own, distinct calls (evaluate_promotion/store_promotion_decision)
        # rather than on run_agentic_turn, which is not a reliable signal
        # of whether *this* layer ran.
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", False), patch(
            "services.agentic_promotion_evaluator.evaluate_promotion"
        ) as mock_evaluate, patch(
            "services.agentic_promotion_evaluator.store_promotion_decision"
        ) as mock_store:
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(result["agentic_promotion"], {"enabled": False, "activated": False, "promotions": []})
        mock_evaluate.assert_not_called()
        mock_store.assert_not_called()

        history = get_promotion_history(self.series.id, db_session=self.db)
        self.assertEqual(history, [])

    def test_promotion_layer_uses_agentic_when_flag_on(self):
        # Phase 4 note: recording ("use_agentic" outcome + shadow-table
        # write) happens whenever AGENTIC_ROUTING_ENABLED is on, but
        # *applying* it to resolved_confidence/resolved_gate additionally
        # requires this series to be activated (settings.
        # is_agentic_activated) -- see tests/test_agentic_activation.py
        # for the flag-on-but-not-activated ("record, don't apply") case
        # this test used to (incorrectly, post-Phase-4) also cover.
        #
        # Phase 7 note: evaluate_promotion is mocked here to force
        # "use_agentic" regardless of this fixture's real confidence/gate
        # data, but services/agentic_resolution.py now independently
        # re-validates that same data via services.agentic_safety.
        # validate_agentic_decision (defense-in-depth) before actually
        # applying it -- and this fixture's real agentic confidence
        # (a synthetic replay, not real corroborating provider evidence)
        # may not itself pass that real check. That re-check is also
        # mocked here so this test can keep isolating "does the wiring
        # apply the agentic side for a use_agentic outcome" from "is this
        # particular fixture's data actually safe" -- the latter is
        # covered by tests/test_agentic_safety.py instead.
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", str(self.series.id)
        ), patch("agents.agentic_series_agent.SessionLocal", self.SessionLocal), patch(
            "services.agentic_promotion_evaluator.evaluate_promotion", return_value="use_agentic"
        ), patch("services.agentic_resolution.validate_agentic_decision", return_value=True):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        payload = result["agentic_promotion"]
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["activated"])
        self.assertGreater(len(payload["promotions"]), 0)
        for promotion in payload["promotions"]:
            self.assertEqual(promotion["outcome"], "use_agentic")
            # resolved_confidence/resolved_gate must be the *agentic* side
            # when the outcome says so and the series is activated.
            self.assertNotEqual(promotion["resolved_confidence"], {})
            self.assertNotIn("confidence", promotion["resolved_confidence"])  # live shape uses "confidence", not "overall"

        history = get_promotion_history(self.series.id, db_session=self.db)
        self.assertEqual(len(history), len(payload["promotions"]))
        self.assertTrue(all(entry["promotion_outcome"] == "use_agentic" for entry in history))

    def test_promotion_layer_defers_to_live_when_evaluator_says_use_live(self):
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch(
            "agents.agentic_series_agent.SessionLocal", self.SessionLocal
        ), patch(
            "services.agentic_promotion_evaluator.evaluate_promotion", return_value="use_live"
        ):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        payload = result["agentic_promotion"]
        for promotion in payload["promotions"]:
            self.assertEqual(promotion["outcome"], "use_live")

    def test_promotion_layer_fail_soft(self):
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch(
            "agents.agentic_series_agent.run_agentic_turn", side_effect=RuntimeError("shadow loop exploded")
        ):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        # Live result still returned normally, never raised.
        self.assertIn("found", result)
        self.assertEqual(result["agentic_promotion"]["promotions"], [])
        self.assertTrue(result["agentic_promotion"]["enabled"])
        self.assertTrue(result["agentic_promotion"].get("error"))

    def test_promotion_layer_never_writes_skeleton_json_or_probes_json(self):
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch(
            "agents.agentic_series_agent.SessionLocal", self.SessionLocal
        ):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(result["probes"], [])
        skeleton_row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        if skeleton_row is not None:
            for entry in skeleton_row.skeleton_json:
                # Every entry is still library-owned/confirmed exactly as
                # backfill_skeleton_for_series alone would have produced --
                # the promotion layer never touched it.
                if entry.get("source_class", "library") == "library":
                    self.assertEqual(entry.get("status"), "confirmed")


class AdminAgenticPromotionEndpointTest(unittest.TestCase):
    """Same reasoning as the equivalent endpoint tests in
    tests/test_agentic_confidence_gate_store.py: TestClient dispatches
    through `main.app`'s real `database.SessionLocal`/engine, so this
    class writes/reads through that same real SessionLocal directly,
    against a series_id chosen not to collide with real data, cleaning
    up afterward.
    """

    SERIES_ID = -999999995

    def setUp(self):
        from database import SessionLocal as RealSessionLocal

        self.RealSessionLocal = RealSessionLocal
        store_promotion_decision(
            self.SERIES_ID,
            7.0,
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

    def test_admin_endpoint_owner_only(self):
        client = TestClient(main.app)

        anon = client.get(f"/admin/agentic/promotion-history/{self.SERIES_ID}")
        self.assertEqual(anon.status_code, 403)

        owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}
        owner = client.get(f"/admin/agentic/promotion-history/{self.SERIES_ID}", headers=owner_headers)
        self.assertEqual(owner.status_code, 200)
        body = owner.json()
        self.assertEqual(body["series_id"], self.SERIES_ID)
        self.assertEqual(len(body["promotion_history"]), 1)
        self.assertEqual(body["promotion_history"][0]["promotion_outcome"], "use_agentic")


if __name__ == "__main__":
    unittest.main()
