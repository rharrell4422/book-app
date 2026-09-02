"""Step 10 (Multi-Provider Tier C): incremental test coverage, one phase
at a time, mirroring how tests/test_tier_c_shadow_store.py grew across
Steps 8 and 9. Phase 1 (schema + settings scaffolding) and Phase 3
(TierCOrchestrator extraction) so far -- later phases extend this same
file rather than starting a new one per phase.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

import settings
from database import Base
from models import Series, ShadowLLMCall
from services.discovery_telemetry import DiscoveryTelemetry
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


def _mock_anthropic_client(response_text, *, input_tokens=10, output_tokens=20):
    # Same shape as tests/test_series_discovery.py's own helper of the
    # same name -- duplicated rather than imported since that module
    # doesn't export it, matching this file's existing "self-contained
    # per phase" convention.
    mock_text_block = MagicMock()
    mock_text_block.type = "text"
    mock_text_block.text = response_text
    mock_usage = MagicMock()
    mock_usage.input_tokens = input_tokens
    mock_usage.output_tokens = output_tokens
    mock_message = MagicMock()
    mock_message.content = [mock_text_block]
    mock_message.usage = mock_usage
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message
    return mock_client


class Phase3OrchestratorExtractionTest(unittest.TestCase):
    """Step 10 Phase 3: direct, unit-level coverage of `services.tier_c_
    orchestrator.run_tier_c_shadow_call` -- the whole point of extracting
    it out of `agents/series_agent.py`'s classification loop is that it
    becomes independently testable like this, rather than only reachable
    through a full `run_series_check` integration test (see `tests/
    test_series_discovery.py`'s `TierCShadowLlmTest`/`TierCPromotionPathTest`
    for that existing end-to-end coverage, which this phase's acceptance
    bar requires to keep passing completely unmodified -- confirmed
    separately, not re-asserted here).
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

        # persist_tier_c_shadow_call opens its OWN independent session
        # when the caller doesn't pass one -- same pattern tests/test_
        # series_discovery.py's TierCPromotionPathTest uses -- so the
        # module-level SessionLocal is patched to land writes in this
        # test's in-memory engine.
        self._session_local_patch = patch("services.tier_c_shadow_store.SessionLocal", self.SessionLocal)
        self._session_local_patch.start()

    def tearDown(self):
        self._session_local_patch.stop()
        self.db.close()

    def _call(self, *, tier_c_state="shadow_only", telemetry=None):
        from services.tier_c_orchestrator import run_tier_c_shadow_call

        return run_tier_c_shadow_call(
            series_id=self.series.id,
            run_id="job-1",
            tier_c_state=tier_c_state,
            prompt="does this belong to the series?",
            candidate_id="isbn-123",
            gate_belongs_to_series=False,
            gate_inferred_number_int=7,
            gate_confidence="medium",
            telemetry=telemetry,
        )

    def test_successful_call_returns_score_and_persists_a_row(self):
        response_text = (
            '{"belongs_to_series": true, "confidence": "high", "inferred_number": 7, '
            '"is_alternate_title_of_known_book": false}'
        )
        telemetry = DiscoveryTelemetry()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=_mock_anthropic_client(response_text)
        ):
            tier_c_score = self._call(telemetry=telemetry)

        self.assertIsNotNone(tier_c_score)
        self.assertTrue(tier_c_score["parsed_ok"])
        self.assertFalse(tier_c_score["belongs_to_series_agreement"])
        self.assertTrue(tier_c_score["tier_c_belongs_to_series"])

        row = self.db.query(ShadowLLMCall).filter(ShadowLLMCall.series_id == self.series.id).one()
        self.assertEqual(row.run_id, "job-1")
        self.assertEqual(row.shadow_provider, "anthropic")
        self.assertTrue(row.shadow_belongs_to_series)
        self.assertEqual(row.tier_c_state_at_call, "shadow_only")

        summary = telemetry.summary()
        self.assertEqual(summary["shadow"]["total_llm_calls"], 1)
        self.assertEqual(summary["tier_c_shadow"]["total_scored"], 1)

    def test_failed_call_returns_none_and_persists_nothing(self):
        # No ANTHROPIC_API_KEY patched -- conftest.py's autouse fixture
        # blanks it, so call_llm raises LLMCallError immediately, same
        # fail-soft path TierCShadowLlmTest's failure test exercises
        # end-to-end.
        telemetry = DiscoveryTelemetry()
        tier_c_score = self._call(telemetry=telemetry)

        self.assertIsNone(tier_c_score)
        self.assertEqual(
            self.db.query(ShadowLLMCall).filter(ShadowLLMCall.series_id == self.series.id).count(), 0
        )
        summary = telemetry.summary()
        self.assertEqual(summary["shadow"]["total_llm_calls"], 1)
        self.assertEqual(summary["shadow"]["total_tokens_in"], 0)
        self.assertEqual(summary["tier_c_shadow"]["total_scored"], 0)

    def test_live_state_gets_an_explicit_timeout_other_states_do_not(self):
        # Regression guard for the Step 8, section 5.1 timeout policy
        # this extraction moved verbatim -- only "live" passes an
        # explicit `timeout` through to `call_llm`.
        captured_timeouts = []

        def fake_anthropic(api_key):
            client = _mock_anthropic_client('{"belongs_to_series": true}')

            def capture_create(**kwargs):
                captured_timeouts.append(kwargs.get("timeout"))
                return client.messages.create.return_value

            client.messages.create.side_effect = capture_create
            return client

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", side_effect=fake_anthropic
        ):
            self._call(tier_c_state="shadow_only")
            self._call(tier_c_state="live")

        self.assertIsNone(captured_timeouts[0])
        self.assertEqual(captured_timeouts[1], settings.TIER_C_LIVE_TIMEOUT_SECONDS)

    def test_works_without_a_telemetry_instance(self):
        # maybe_pass_scope's documented no-op contract: a caller that
        # doesn't pass telemetry must see no behavior change, including
        # here where telemetry is genuinely optional (`None` default).
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=_mock_anthropic_client('{"belongs_to_series": true}')
        ):
            tier_c_score = self._call(telemetry=None)

        self.assertIsNotNone(tier_c_score)
        self.assertTrue(tier_c_score["parsed_ok"])


class Phase3ScoreFunctionReExportTest(unittest.TestCase):
    """Step 10 Phase 3: `_score_tier_c_shadow_response` moved to `services.
    tier_c_orchestrator` but must remain importable from `agents.series_
    agent` (re-exported there) so `tests/test_series_discovery.py`'s
    existing `ScoreTierCShadowResponseTest` import needs zero changes --
    this is the literal mechanism that makes that true, asserted directly
    rather than only implied by that other test file still passing.
    """

    def test_reexported_function_is_the_same_object(self):
        from agents.series_agent import _score_tier_c_shadow_response as reexported
        from services.tier_c_orchestrator import _score_tier_c_shadow_response as original

        self.assertIs(reexported, original)


if __name__ == "__main__":
    unittest.main()
