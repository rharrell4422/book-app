"""Step 8 ("Tier C Shadow Scoring Persistence + Promotion Path"): unit
coverage for services/tier_c_shadow_store.py in isolation from the full
run_series_check integration path (see tests/test_series_discovery.py's
TierCPromotionPathTest for that).
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import settings
from database import Base
from models import Series, ShadowLLMCall, TierCPromotionState
from services.tier_c_shadow_store import (
    check_tier_c_shadow_budget,
    get_tier_c_promotion_state,
    persist_tier_c_shadow_call,
)


class TierCShadowStoreTest(unittest.TestCase):
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

    def tearDown(self):
        self.db.close()

    def _make_call(self, *, total_cost_usd: float, created_at: datetime | None = None, db_session=None) -> None:
        persist_tier_c_shadow_call(
            series_id=self.series.id,
            run_id="job-1",
            gate_belongs_to_series=False,
            gate_inferred_number=7,
            gate_confidence=None,
            shadow_provider="anthropic",
            shadow_model_id="claude-haiku-4-5-20251001",
            shadow_belongs_to_series=True,
            shadow_inferred_number=7,
            shadow_confidence="high",
            shadow_is_alternate_title_of_known_book=False,
            parsed_ok=True,
            belongs_to_series_agreement=False,
            inferred_number_agreement=True,
            confidence_aligned=True,
            prompt_tokens=100,
            completion_tokens=50,
            total_cost_usd=total_cost_usd,
            db_session=db_session,
        )
        if created_at is not None:
            row = (
                self.db.query(ShadowLLMCall)
                .filter(ShadowLLMCall.series_id == self.series.id)
                .order_by(ShadowLLMCall.id.desc())
                .first()
            )
            row.created_at = created_at
            self.db.commit()

    def test_persist_writes_a_row_reusing_the_caller_supplied_session(self):
        self._make_call(total_cost_usd=0.01, db_session=self.db)
        rows = self.db.query(ShadowLLMCall).filter(ShadowLLMCall.series_id == self.series.id).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].total_cost_usd, 0.01)

    def test_persist_is_fail_soft_on_a_db_error(self):
        with patch("services.tier_c_shadow_store.models.ShadowLLMCall", side_effect=RuntimeError("boom")):
            # Must not raise.
            self._make_call(total_cost_usd=0.01, db_session=self.db)
        rows = self.db.query(ShadowLLMCall).filter(ShadowLLMCall.series_id == self.series.id).all()
        self.assertEqual(rows, [])

    def test_get_promotion_state_defaults_to_shadow_only_when_absent(self):
        state = get_tier_c_promotion_state(self.db, self.series.id)
        self.assertEqual(state["tier_c_state"], "shadow_only")
        self.assertIsNone(state["tier_c_provider"])

    def test_get_promotion_state_reads_an_explicit_row(self):
        self.db.add(
            TierCPromotionState(
                series_id=self.series.id,
                tier_c_state="live",
                tier_c_provider="anthropic",
                tier_c_model_id="claude-haiku-4-5-20251001",
            )
        )
        self.db.commit()
        state = get_tier_c_promotion_state(self.db, self.series.id)
        self.assertEqual(state["tier_c_state"], "live")
        self.assertEqual(state["tier_c_provider"], "anthropic")

    def test_budget_check_allows_when_no_ceilings_configured(self):
        with patch.object(settings, "TIER_C_SHADOW_MAX_DAILY_COST_USD", None), patch.object(
            settings, "TIER_C_SHADOW_MAX_MONTHLY_COST_USD", None
        ):
            self.assertTrue(check_tier_c_shadow_budget(self.db, self.series.id))

    def test_budget_check_blocks_once_daily_ceiling_is_met(self):
        self._make_call(total_cost_usd=0.6, db_session=self.db)
        self._make_call(total_cost_usd=0.5, db_session=self.db)
        with patch.object(settings, "TIER_C_SHADOW_MAX_DAILY_COST_USD", 1.0), patch.object(
            settings, "TIER_C_SHADOW_MAX_MONTHLY_COST_USD", None
        ):
            self.assertFalse(check_tier_c_shadow_budget(self.db, self.series.id))

    def test_budget_check_ignores_cost_outside_the_daily_window(self):
        stale = datetime.utcnow() - timedelta(days=2)
        self._make_call(total_cost_usd=5.0, created_at=stale, db_session=self.db)
        with patch.object(settings, "TIER_C_SHADOW_MAX_DAILY_COST_USD", 1.0), patch.object(
            settings, "TIER_C_SHADOW_MAX_MONTHLY_COST_USD", None
        ):
            self.assertTrue(check_tier_c_shadow_budget(self.db, self.series.id))

    def test_budget_check_is_fail_open_on_a_query_error(self):
        self._make_call(total_cost_usd=0.01, db_session=self.db)
        with patch.object(settings, "TIER_C_SHADOW_MAX_DAILY_COST_USD", 0.0001), patch(
            "services.tier_c_shadow_store._cost_since", side_effect=RuntimeError("boom")
        ):
            self.assertTrue(check_tier_c_shadow_budget(self.db, self.series.id))


if __name__ == "__main__":
    unittest.main()
