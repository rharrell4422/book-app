"""Step 9 ("Tier C Promotion Policy Engine"): unit coverage for
services/tier_c_promotion_engine.py -- the pure `_decide_transition` rule
table in isolation, plus `evaluate_tier_c_promotion`'s DB read/write
behavior against an in-memory engine (same pattern as tests/test_tier_c_
shadow_store.py and tests/test_series_discovery.py's
TierCPromotionPathTest).
"""

import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import settings
from database import Base
from models import Series, ShadowLLMCall, TierCPromotionHistory, TierCPromotionState
from services.tier_c_promotion_engine import (
    REASON_AGREEMENT_HIGH,
    REASON_DISAGREEMENT_HIGH,
    REASON_INSUFFICIENT_EVIDENCE,
    REASON_MANUAL_OVERRIDE_ACTIVE,
    REASON_STABLE,
    _decide_transition,
    evaluate_tier_c_promotion,
)


class DecideTransitionTest(unittest.TestCase):
    """Pure function -- no DB, no mocking, every branch of the rule table."""

    COMMON = dict(min_calls=10, agreement_threshold=0.9, disagreement_threshold=0.3)

    def test_holds_on_insufficient_evidence_regardless_of_state(self):
        for state in ("shadow_only", "shadow_advisory", "live"):
            new_state, reason = _decide_transition(
                current_state=state,
                shadow_calls_considered=3,
                agreement_rate=1.0,
                disagreement_rate=0.0,
                **self.COMMON,
            )
            self.assertEqual(new_state, state)
            self.assertEqual(reason, REASON_INSUFFICIENT_EVIDENCE)

    def test_promotes_shadow_only_to_shadow_advisory_on_high_agreement(self):
        new_state, reason = _decide_transition(
            current_state="shadow_only",
            shadow_calls_considered=10,
            agreement_rate=0.95,
            disagreement_rate=0.05,
            **self.COMMON,
        )
        self.assertEqual(new_state, "shadow_advisory")
        self.assertEqual(reason, REASON_AGREEMENT_HIGH)

    def test_holds_shadow_only_below_agreement_threshold(self):
        new_state, reason = _decide_transition(
            current_state="shadow_only",
            shadow_calls_considered=10,
            agreement_rate=0.5,
            disagreement_rate=0.5,
            **self.COMMON,
        )
        self.assertEqual(new_state, "shadow_only")
        self.assertEqual(reason, REASON_STABLE)

    def test_promotes_shadow_advisory_to_live_on_high_agreement(self):
        new_state, reason = _decide_transition(
            current_state="shadow_advisory",
            shadow_calls_considered=10,
            agreement_rate=0.95,
            disagreement_rate=0.05,
            **self.COMMON,
        )
        self.assertEqual(new_state, "live")
        self.assertEqual(reason, REASON_AGREEMENT_HIGH)

    def test_demotes_shadow_advisory_to_shadow_only_on_high_disagreement(self):
        new_state, reason = _decide_transition(
            current_state="shadow_advisory",
            shadow_calls_considered=10,
            agreement_rate=0.4,
            disagreement_rate=0.6,
            **self.COMMON,
        )
        self.assertEqual(new_state, "shadow_only")
        self.assertEqual(reason, REASON_DISAGREEMENT_HIGH)

    def test_demotes_live_to_shadow_advisory_on_high_disagreement(self):
        new_state, reason = _decide_transition(
            current_state="live",
            shadow_calls_considered=10,
            agreement_rate=0.4,
            disagreement_rate=0.6,
            **self.COMMON,
        )
        self.assertEqual(new_state, "shadow_advisory")
        self.assertEqual(reason, REASON_DISAGREEMENT_HIGH)

    def test_holds_live_below_disagreement_threshold(self):
        new_state, reason = _decide_transition(
            current_state="live",
            shadow_calls_considered=10,
            agreement_rate=0.8,
            disagreement_rate=0.2,
            **self.COMMON,
        )
        self.assertEqual(new_state, "live")
        self.assertEqual(reason, REASON_STABLE)

    def test_never_skips_a_state_shadow_only_cannot_jump_to_live(self):
        # shadow_only's own branch never even considers "live" -- this
        # documents that a single evaluation can only ever move one step.
        new_state, _ = _decide_transition(
            current_state="shadow_only",
            shadow_calls_considered=10,
            agreement_rate=1.0,
            disagreement_rate=0.0,
            **self.COMMON,
        )
        self.assertNotEqual(new_state, "live")

    def test_unknown_state_holds_defensively(self):
        new_state, reason = _decide_transition(
            current_state="bogus",
            shadow_calls_considered=10,
            agreement_rate=1.0,
            disagreement_rate=0.0,
            **self.COMMON,
        )
        self.assertEqual(new_state, "bogus")
        self.assertEqual(reason, "unknown_state")


class EvaluateTierCPromotionTest(unittest.TestCase):
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
        series = Series(name="Test Series", author="Test Author", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

        # evaluate_tier_c_promotion opens its OWN independent session (see
        # that function's docstring) -- patch the module-level SessionLocal
        # so its reads/writes land in this test's in-memory engine, same
        # convention as TierCPromotionPathTest in tests/test_series_
        # discovery.py.
        self._session_local_patch = patch("services.tier_c_promotion_engine.SessionLocal", self.SessionLocal)
        self._session_local_patch.start()

        self._settings_patches = [
            patch.object(settings, "TIER_C_PROMOTION_MIN_CALLS", 5),
            patch.object(settings, "TIER_C_PROMOTION_AGREEMENT_THRESHOLD", 0.9),
            patch.object(settings, "TIER_C_DEMOTION_DISAGREEMENT_THRESHOLD", 0.3),
            patch.object(settings, "TIER_C_MANUAL_OVERRIDE_HONORED", True),
        ]
        for p in self._settings_patches:
            p.start()

    def tearDown(self):
        for p in self._settings_patches:
            p.stop()
        self._session_local_patch.stop()
        self.db.close()

    def _add_shadow_calls(self, *, agreements: list[bool]) -> None:
        for agreement in agreements:
            self.db.add(
                ShadowLLMCall(
                    series_id=self.series.id,
                    run_id="job-1",
                    tier="C",
                    gate_belongs_to_series=False,
                    shadow_provider="anthropic",
                    shadow_model_id="claude-haiku-4-5-20251001",
                    shadow_belongs_to_series=agreement,
                    parsed_ok=True,
                    belongs_to_series_agreement=agreement,
                    created_at=datetime.utcnow(),
                )
            )
        self.db.commit()

    def _history_rows(self) -> list[TierCPromotionHistory]:
        return (
            self.db.query(TierCPromotionHistory)
            .filter(TierCPromotionHistory.series_id == self.series.id)
            .order_by(TierCPromotionHistory.id.asc())
            .all()
        )

    def _state_row(self) -> TierCPromotionState:
        return (
            self.db.query(TierCPromotionState)
            .filter(TierCPromotionState.series_id == self.series.id)
            .first()
        )

    def test_holds_and_creates_a_state_row_when_no_shadow_calls_exist(self):
        evaluate_tier_c_promotion(self.series.id)

        history = self._history_rows()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].previous_state, "shadow_only")
        self.assertEqual(history[0].new_state, "shadow_only")
        self.assertEqual(history[0].evaluation_reason, REASON_INSUFFICIENT_EVIDENCE)
        self.assertEqual(history[0].shadow_calls_considered, 0)

        state = self._state_row()
        self.assertIsNotNone(state)
        self.assertEqual(state.tier_c_state, "shadow_only")
        self.assertIsNotNone(state.last_evaluated_at)

    def test_promotes_shadow_only_to_shadow_advisory_on_enough_high_agreement_calls(self):
        self._add_shadow_calls(agreements=[True, True, True, True, True])

        evaluate_tier_c_promotion(self.series.id)

        state = self._state_row()
        self.assertEqual(state.tier_c_state, "shadow_advisory")
        history = self._history_rows()
        self.assertEqual(history[-1].new_state, "shadow_advisory")
        self.assertEqual(history[-1].evaluation_reason, REASON_AGREEMENT_HIGH)
        self.assertEqual(history[-1].shadow_calls_considered, 5)
        self.assertEqual(history[-1].agreement_rate, 1.0)

    def test_demotes_live_to_shadow_advisory_on_high_disagreement(self):
        self.db.add(TierCPromotionState(series_id=self.series.id, tier_c_state="live"))
        self.db.commit()
        self._add_shadow_calls(agreements=[False, False, True, False, True])  # 3/5 disagree

        evaluate_tier_c_promotion(self.series.id)

        state = self._state_row()
        self.assertEqual(state.tier_c_state, "shadow_advisory")
        history = self._history_rows()
        self.assertEqual(history[-1].previous_state, "live")
        self.assertEqual(history[-1].evaluation_reason, REASON_DISAGREEMENT_HIGH)

    def test_manual_override_skips_the_decision_and_is_audited(self):
        self.db.add(
            TierCPromotionState(series_id=self.series.id, tier_c_state="shadow_only", is_manual_override=True)
        )
        self.db.commit()
        # Would otherwise promote -- proves the freeze actually short-circuits.
        self._add_shadow_calls(agreements=[True, True, True, True, True])

        evaluate_tier_c_promotion(self.series.id)

        state = self._state_row()
        self.assertEqual(state.tier_c_state, "shadow_only")
        self.assertTrue(state.is_manual_override)
        self.assertIsNotNone(state.last_evaluated_at)

        history = self._history_rows()
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0].manual_override_active)
        self.assertEqual(history[0].evaluation_reason, REASON_MANUAL_OVERRIDE_ACTIVE)
        self.assertEqual(history[0].previous_state, history[0].new_state)

    def test_manual_override_flag_ignored_when_honoring_is_disabled(self):
        self.db.add(
            TierCPromotionState(series_id=self.series.id, tier_c_state="shadow_only", is_manual_override=True)
        )
        self.db.commit()
        self._add_shadow_calls(agreements=[True, True, True, True, True])

        with patch.object(settings, "TIER_C_MANUAL_OVERRIDE_HONORED", False):
            evaluate_tier_c_promotion(self.series.id)

        state = self._state_row()
        self.assertEqual(state.tier_c_state, "shadow_advisory")

    def test_budget_blocked_flag_is_appended_to_the_evaluation_reason(self):
        evaluate_tier_c_promotion(self.series.id, budget_blocked=True)

        history = self._history_rows()
        self.assertEqual(history[0].evaluation_reason, f"{REASON_INSUFFICIENT_EVIDENCE},budget_blocked")

    def test_preserves_provider_and_model_columns_across_a_hold(self):
        self.db.add(
            TierCPromotionState(
                series_id=self.series.id,
                tier_c_state="shadow_only",
                tier_c_provider="anthropic",
                tier_c_model_id="claude-haiku-4-5-20251001",
            )
        )
        self.db.commit()

        evaluate_tier_c_promotion(self.series.id)

        state = self._state_row()
        self.assertEqual(state.tier_c_provider, "anthropic")
        self.assertEqual(state.tier_c_model_id, "claude-haiku-4-5-20251001")

    def test_is_fail_soft_when_the_state_read_raises(self):
        with patch(
            "services.tier_c_promotion_engine.get_tier_c_promotion_state",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise.
            evaluate_tier_c_promotion(self.series.id)
        self.assertEqual(self._history_rows(), [])


if __name__ == "__main__":
    unittest.main()
