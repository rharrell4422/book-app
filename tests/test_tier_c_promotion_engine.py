"""Step 9 ("Tier C Promotion Policy Engine"): unit coverage for
services/tier_c_promotion_engine.py -- the pure `_decide_transition` rule
table in isolation, plus `evaluate_tier_c_promotion`'s DB read/write
behavior against an in-memory engine (same pattern as tests/test_tier_c_
shadow_store.py and tests/test_series_discovery.py's
TierCPromotionPathTest).
"""

import unittest
import uuid
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import settings
from database import Base
from models import Series, ShadowLLMCall, TierCPromotionHistory, TierCPromotionState
from services.tier_c_promotion_engine import (
    REASON_AGREEMENT_HIGH,
    REASON_CONSENSUS_HOLD,
    REASON_DISAGREEMENT_HIGH,
    REASON_INSUFFICIENT_EVIDENCE,
    REASON_MANUAL_OVERRIDE_ACTIVE,
    REASON_STABLE,
    _decide_transition,
    _has_sustained_low_consensus,
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

    def test_consensus_hold_blocks_promotion_from_shadow_only(self):
        """Step 11 Phase 4: a would-be promotion (agreement_rate clears
        the threshold) is held, not made, when consensus_hold=True.
        """
        new_state, reason = _decide_transition(
            current_state="shadow_only",
            shadow_calls_considered=10,
            agreement_rate=0.95,
            disagreement_rate=0.05,
            consensus_hold=True,
            **self.COMMON,
        )
        self.assertEqual(new_state, "shadow_only")
        self.assertEqual(reason, REASON_CONSENSUS_HOLD)

    def test_consensus_hold_blocks_promotion_from_shadow_advisory_to_live(self):
        new_state, reason = _decide_transition(
            current_state="shadow_advisory",
            shadow_calls_considered=10,
            agreement_rate=0.95,
            disagreement_rate=0.05,
            consensus_hold=True,
            **self.COMMON,
        )
        self.assertEqual(new_state, "shadow_advisory")
        self.assertEqual(reason, REASON_CONSENSUS_HOLD)

    def test_consensus_hold_never_blocks_a_demotion(self):
        """The whole point of the hold shape (vs. a soft demotion vote):
        consensus_hold=True must have ZERO effect on the demotion branch
        -- disagreement-driven demotion is untouched by this signal.
        """
        new_state, reason = _decide_transition(
            current_state="shadow_advisory",
            shadow_calls_considered=10,
            agreement_rate=0.4,
            disagreement_rate=0.6,
            consensus_hold=True,
            **self.COMMON,
        )
        self.assertEqual(new_state, "shadow_only")
        self.assertEqual(reason, REASON_DISAGREEMENT_HIGH)

    def test_consensus_hold_has_no_effect_on_live_state(self):
        """"live" has no promotion branch for a hold to ever block --
        consensus_hold=True must be a complete no-op here, in both
        directions (stays live when stable, still demotes on
        disagreement, identically to consensus_hold=False).
        """
        stable_state, stable_reason = _decide_transition(
            current_state="live",
            shadow_calls_considered=10,
            agreement_rate=0.8,
            disagreement_rate=0.2,
            consensus_hold=True,
            **self.COMMON,
        )
        self.assertEqual(stable_state, "live")
        self.assertEqual(stable_reason, REASON_STABLE)

        demoted_state, demoted_reason = _decide_transition(
            current_state="live",
            shadow_calls_considered=10,
            agreement_rate=0.4,
            disagreement_rate=0.6,
            consensus_hold=True,
            **self.COMMON,
        )
        self.assertEqual(demoted_state, "shadow_advisory")
        self.assertEqual(demoted_reason, REASON_DISAGREEMENT_HIGH)

    def test_consensus_hold_false_is_identical_to_omitting_it(self):
        """Default value (False) must be indistinguishable from every
        pre-Phase-4 call site/test that never passes this parameter at
        all -- confirms backward compatibility explicitly, not just by
        the rest of this file's tests happening to still pass.
        """
        with_default = _decide_transition(
            current_state="shadow_only",
            shadow_calls_considered=10,
            agreement_rate=0.95,
            disagreement_rate=0.05,
            **self.COMMON,
        )
        with_explicit_false = _decide_transition(
            current_state="shadow_only",
            shadow_calls_considered=10,
            agreement_rate=0.95,
            disagreement_rate=0.05,
            consensus_hold=False,
            **self.COMMON,
        )
        self.assertEqual(with_default, with_explicit_false)
        self.assertEqual(with_default, ("shadow_advisory", REASON_AGREEMENT_HIGH))


class HasSustainedLowConsensusTest(unittest.TestCase):
    """Step 11 Phase 4: unit coverage for `_has_sustained_low_consensus`
    against a real in-memory `TierCPromotionHistory` table -- the
    history-scanning logic (skip non-qualifying entries, require ALL of
    the last LOOKBACK qualifying ones to be low, respect the enabled
    flag) is significant enough to warrant its own direct tests, separate
    from the full `evaluate_tier_c_promotion` integration tests below.
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
        series = Series(name="Test Series", author="Test Author", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

        self._settings_patches = [
            patch.object(settings, "TIER_C_CONSENSUS_SIGNAL_ENABLED", True),
            patch.object(settings, "TIER_C_CONSENSUS_SIGNAL_LOOKBACK", 3),
            patch.object(settings, "TIER_C_CONSENSUS_LOW_THRESHOLD", 0.7),
        ]
        for p in self._settings_patches:
            p.start()

    def tearDown(self):
        for p in self._settings_patches:
            p.stop()
        self.db.close()

    def _add_history_row(
        self,
        *,
        multi_provider_candidate_count: int,
        avg_consensus_score_multi_provider_only: float | None,
    ) -> None:
        self.db.add(
            TierCPromotionHistory(
                series_id=self.series.id,
                evaluated_at=datetime.utcnow(),
                previous_state="shadow_only",
                new_state="shadow_only",
                evaluation_reason=REASON_STABLE,
                shadow_calls_considered=5,
                agreement_rate=1.0,
                manual_override_active=False,
                metrics_snapshot={
                    "cross_provider_multi_provider_candidate_count": multi_provider_candidate_count,
                    "cross_provider_avg_consensus_score_multi_provider_only": (
                        avg_consensus_score_multi_provider_only
                    ),
                },
            )
        )
        self.db.commit()

    def test_returns_false_when_disabled(self):
        with patch.object(settings, "TIER_C_CONSENSUS_SIGNAL_ENABLED", False):
            for _ in range(3):
                self._add_history_row(
                    multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=0.5
                )
            self.assertFalse(_has_sustained_low_consensus(self.db, self.series.id))

    def test_returns_false_with_no_history(self):
        self.assertFalse(_has_sustained_low_consensus(self.db, self.series.id))

    def test_returns_false_when_fewer_than_lookback_qualifying_entries_exist(self):
        # Only 2 qualifying entries, lookback is 3.
        self._add_history_row(multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=0.5)
        self._add_history_row(multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=0.5)

        self.assertFalse(_has_sustained_low_consensus(self.db, self.series.id))

    def test_skips_entries_with_no_multi_provider_evidence(self):
        # 3 low, qualifying entries, interleaved with several
        # zero-multi-provider entries that must be skipped over entirely
        # rather than breaking the "sustained" streak.
        self._add_history_row(multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=0.5)
        self._add_history_row(multi_provider_candidate_count=0, avg_consensus_score_multi_provider_only=None)
        self._add_history_row(multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=0.6)
        self._add_history_row(multi_provider_candidate_count=0, avg_consensus_score_multi_provider_only=None)
        self._add_history_row(multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=0.4)

        self.assertTrue(_has_sustained_low_consensus(self.db, self.series.id))

    def test_returns_false_when_any_of_the_lookback_entries_is_high(self):
        # Most recent 3 qualifying: 0.5 (low), 0.6 (low), 0.9 (high) --
        # NOT sustained, must return False.
        self._add_history_row(multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=0.9)
        self._add_history_row(multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=0.6)
        self._add_history_row(multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=0.5)

        self.assertFalse(_has_sustained_low_consensus(self.db, self.series.id))

    def test_only_considers_the_most_recent_lookback_qualifying_entries(self):
        # 2 old, high-consensus qualifying entries, followed by 3 recent
        # low ones -- only the most recent 3 (lookback=3) matter.
        self._add_history_row(multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=1.0)
        self._add_history_row(multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=1.0)
        self._add_history_row(multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=0.5)
        self._add_history_row(multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=0.6)
        self._add_history_row(multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=0.4)

        self.assertTrue(_has_sustained_low_consensus(self.db, self.series.id))

    def test_exactly_at_threshold_does_not_count_as_low(self):
        # Strict "<", not "<=" -- 0.7 == threshold must not count as low.
        for _ in range(3):
            self._add_history_row(multi_provider_candidate_count=1, avg_consensus_score_multi_provider_only=0.7)

        self.assertFalse(_has_sustained_low_consensus(self.db, self.series.id))


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
        # Step 10 Phase 5: each call below is its own Tier C *candidate*
        # (a distinct candidate_request_id) -- matching every real row
        # `run_tier_c_shadow_call` persists from Phase 4 onward, single-
        # provider included (see that function's docstring). One row per
        # candidate here means get_recent_candidate_aggregates' per-
        # candidate vote count stays numerically identical to this
        # helper's pre-Phase-5 raw-row count, so every existing assertion
        # in this file (`shadow_calls_considered == len(agreements)`,
        # etc.) keeps meaning exactly what it always meant.
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
                    candidate_request_id=uuid.uuid4().hex,
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

    def test_consensus_hold_blocks_a_would_be_promotion_end_to_end(self):
        """Step 11 Phase 4, full wiring: seeds 3 sustained-low-consensus
        history rows, then adds enough high-agreement shadow calls that
        this evaluation would otherwise promote shadow_only ->
        shadow_advisory -- confirms the hold actually reaches
        _decide_transition through evaluate_tier_c_promotion, not just
        in the unit tests above.
        """
        for _ in range(3):
            self.db.add(
                TierCPromotionHistory(
                    series_id=self.series.id,
                    evaluated_at=datetime.utcnow(),
                    previous_state="shadow_only",
                    new_state="shadow_only",
                    evaluation_reason=REASON_STABLE,
                    shadow_calls_considered=5,
                    agreement_rate=1.0,
                    manual_override_active=False,
                    metrics_snapshot={
                        "cross_provider_multi_provider_candidate_count": 1,
                        "cross_provider_avg_consensus_score_multi_provider_only": 0.5,
                    },
                )
            )
        self.db.commit()
        self._add_shadow_calls(agreements=[True, True, True, True, True])

        with patch.object(settings, "TIER_C_CONSENSUS_SIGNAL_ENABLED", True), patch.object(
            settings, "TIER_C_CONSENSUS_SIGNAL_LOOKBACK", 3
        ), patch.object(settings, "TIER_C_CONSENSUS_LOW_THRESHOLD", 0.7):
            evaluate_tier_c_promotion(self.series.id)

        state = self._state_row()
        self.assertEqual(state.tier_c_state, "shadow_only")  # held, not promoted
        history = self._history_rows()
        self.assertEqual(history[-1].evaluation_reason, REASON_CONSENSUS_HOLD)
        self.assertEqual(history[-1].previous_state, history[-1].new_state)
        self.assertTrue(history[-1].metrics_snapshot["consensus_hold_active"])
        self.assertTrue(history[-1].metrics_snapshot["consensus_signal_enabled"])

    def test_consensus_signal_disabled_by_default_does_not_hold(self):
        """Same setup as the test above, MINUS enabling the flag -- must
        promote normally, proving the hold is genuinely opt-in.
        """
        for _ in range(3):
            self.db.add(
                TierCPromotionHistory(
                    series_id=self.series.id,
                    evaluated_at=datetime.utcnow(),
                    previous_state="shadow_only",
                    new_state="shadow_only",
                    evaluation_reason=REASON_STABLE,
                    shadow_calls_considered=5,
                    agreement_rate=1.0,
                    manual_override_active=False,
                    metrics_snapshot={
                        "cross_provider_multi_provider_candidate_count": 1,
                        "cross_provider_avg_consensus_score_multi_provider_only": 0.5,
                    },
                )
            )
        self.db.commit()
        self._add_shadow_calls(agreements=[True, True, True, True, True])

        evaluate_tier_c_promotion(self.series.id)

        state = self._state_row()
        self.assertEqual(state.tier_c_state, "shadow_advisory")

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
