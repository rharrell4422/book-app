"""Step 10 (Multi-Provider Tier C): incremental test coverage, one phase
at a time, mirroring how tests/test_tier_c_shadow_store.py grew across
Steps 8 and 9. Phase 1 (schema + settings scaffolding) only -- later
phases extend this same file rather than starting a new one per phase.
"""

import unittest

import settings
from database import Base
from models import Series, ShadowLLMCall
from services.tier_c_shadow_store import persist_tier_c_shadow_call
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class Phase1SchemaAndSettingsTest(unittest.TestCase):
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

    def _persist(self, *, candidate_request_id: str | None) -> None:
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
            total_cost_usd=0.01,
            candidate_request_id=candidate_request_id,
            db_session=self.db,
        )

    def test_candidate_request_id_defaults_to_none_when_omitted(self):
        """No caller passes this yet (agents/series_agent.py's Tier C
        shadow call site is unchanged in Phase 1) -- confirms the column
        is purely additive and never NOT NULL-constrained.
        """
        self._persist(candidate_request_id=None)
        row = self.db.query(ShadowLLMCall).filter(ShadowLLMCall.series_id == self.series.id).first()
        self.assertIsNone(row.candidate_request_id)

    def test_candidate_request_id_round_trips_when_provided(self):
        self._persist(candidate_request_id="cand-req-abc123")
        row = self.db.query(ShadowLLMCall).filter(ShadowLLMCall.series_id == self.series.id).first()
        self.assertEqual(row.candidate_request_id, "cand-req-abc123")

    def test_candidate_request_id_is_independent_per_row_not_shared_with_run_id(self):
        """Two rows in the same Check Now job (same run_id) but different
        Tier C invocations must be able to carry distinct
        candidate_request_id values -- this is the whole reason the
        column exists separately from run_id (see models.ShadowLLMCall's
        docstring).
        """
        self._persist(candidate_request_id="cand-req-1")
        self._persist(candidate_request_id="cand-req-2")
        rows = (
            self.db.query(ShadowLLMCall)
            .filter(ShadowLLMCall.series_id == self.series.id)
            .order_by(ShadowLLMCall.id.asc())
            .all()
        )
        self.assertEqual([r.run_id for r in rows], ["job-1", "job-1"])
        self.assertEqual([r.candidate_request_id for r in rows], ["cand-req-1", "cand-req-2"])


class Phase1SettingsDefaultsTest(unittest.TestCase):
    def test_parallel_shadow_sample_rate_defaults_to_zero(self):
        """Must default to fully inactive -- Step 10's activation rule
        (Phase 6) requires this to stay 0.0 until the per-candidate
        aggregation layer (Phase 5) has shipped, so no code merge before
        then can accidentally turn on multi-provider fan-out in
        production.
        """
        self.assertEqual(settings.TIER_C_PARALLEL_SHADOW_SAMPLE_RATE, 0.0)

    def test_parallel_call_timeout_has_a_sane_positive_default(self):
        self.assertGreater(settings.TIER_C_PARALLEL_CALL_TIMEOUT_SECONDS, 0.0)


if __name__ == "__main__":
    unittest.main()
