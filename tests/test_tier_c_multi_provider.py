"""Step 10 (Multi-Provider Tier C): incremental test coverage, one phase
at a time, mirroring how tests/test_tier_c_shadow_store.py grew across
Steps 8 and 9. Phase 1 (schema + settings scaffolding), Phase 3
(TierCOrchestrator extraction), and Phase 4 (parallel fan-out) so far --
later phases extend this same file rather than starting a new one per
phase.
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


def _mock_groq_or_openai_client(response_text, *, prompt_tokens=10, completion_tokens=20):
    # Same OpenAI-compatible shape tests/test_llm_client.py's own
    # `_mock_groq_client`/`_mock_openai_client` helpers use -- duplicated
    # rather than imported, matching this file's existing "self-contained
    # per phase" convention (see `_mock_anthropic_client` above).
    mock_message = MagicMock()
    mock_message.content = response_text
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_usage = MagicMock()
    mock_usage.prompt_tokens = prompt_tokens
    mock_usage.completion_tokens = completion_tokens
    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    mock_completion.usage = mock_usage
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_completion
    return mock_client


class Phase4SamplingGateTest(unittest.TestCase):
    """Step 10 Phase 4: `_should_fan_out`'s live-state short-circuit and
    sample-rate roll, in isolation from any actual LLM dispatch.
    """

    def test_live_state_never_fans_out_even_at_full_sample_rate(self):
        from services.tier_c_orchestrator import _should_fan_out

        with patch.object(settings, "TIER_C_PARALLEL_SHADOW_SAMPLE_RATE", 1.0), patch(
            "services.tier_c_orchestrator.random.random"
        ) as mock_random:
            self.assertFalse(_should_fan_out("live"))
            # The whole point of the short-circuit: a "live" candidate
            # must never even consume a random draw.
            mock_random.assert_not_called()

    def test_default_zero_sample_rate_never_fans_out_non_live_states(self):
        from services.tier_c_orchestrator import _should_fan_out

        for state in ("shadow_only", "shadow_advisory"):
            with self.subTest(state=state):
                self.assertFalse(_should_fan_out(state))

    def test_non_live_state_fans_out_when_the_roll_beats_the_sample_rate(self):
        from services.tier_c_orchestrator import _should_fan_out

        with patch.object(settings, "TIER_C_PARALLEL_SHADOW_SAMPLE_RATE", 0.5), patch(
            "services.tier_c_orchestrator.random.random", return_value=0.1
        ):
            self.assertTrue(_should_fan_out("shadow_only"))

        with patch.object(settings, "TIER_C_PARALLEL_SHADOW_SAMPLE_RATE", 0.5), patch(
            "services.tier_c_orchestrator.random.random", return_value=0.9
        ):
            self.assertFalse(_should_fan_out("shadow_advisory"))


class Phase4AggregationTest(unittest.TestCase):
    """Step 10 Phase 4: `_aggregate_gate_comparison_votes`'s partial-
    failure + majority-vote/tie-break rule, exercised directly against
    hand-built `_score_tier_c_shadow_response`-shaped dicts so each case
    from the finalized spec gets its own unambiguous assertion.
    """

    @staticmethod
    def _score(agreement, *, parsed_ok=True, tag=None):
        return {
            "parsed_ok": parsed_ok,
            "belongs_to_series_agreement": agreement,
            "inferred_number_agreement": True,
            "tier_c_confidence": "high",
            "confidence_aligned": None,
            "tier_c_alternate_title_flag": False,
            "tier_c_belongs_to_series": True,
            "tier_c_inferred_number": 7,
            "_tag": tag,
        }

    def test_zero_responses_returns_none(self):
        from services.tier_c_orchestrator import _aggregate_gate_comparison_votes

        self.assertIsNone(_aggregate_gate_comparison_votes([]))

    def test_one_response_is_returned_verbatim(self):
        from services.tier_c_orchestrator import _aggregate_gate_comparison_votes

        only = self._score(True, tag="solo")
        self.assertIs(_aggregate_gate_comparison_votes([only]), only)

    def test_three_zero_and_two_zero_are_unanimous_agreement(self):
        from services.tier_c_orchestrator import _aggregate_gate_comparison_votes

        result = _aggregate_gate_comparison_votes([self._score(True), self._score(True), self._score(True)])
        self.assertTrue(result["belongs_to_series_agreement"])

        result = _aggregate_gate_comparison_votes([self._score(True), self._score(True)])
        self.assertTrue(result["belongs_to_series_agreement"])

    def test_two_one_split_follows_the_majority(self):
        from services.tier_c_orchestrator import _aggregate_gate_comparison_votes

        result = _aggregate_gate_comparison_votes([self._score(True), self._score(True), self._score(False)])
        self.assertTrue(result["belongs_to_series_agreement"])

        result = _aggregate_gate_comparison_votes([self._score(False), self._score(False), self._score(True)])
        self.assertFalse(result["belongs_to_series_agreement"])

    def test_exact_two_way_tie_resolves_to_disagreement(self):
        from services.tier_c_orchestrator import _aggregate_gate_comparison_votes

        result = _aggregate_gate_comparison_votes([self._score(True), self._score(False)])
        self.assertFalse(result["belongs_to_series_agreement"])

    def test_unparseable_responses_do_not_get_a_vote(self):
        from services.tier_c_orchestrator import _aggregate_gate_comparison_votes

        # 2 raw responses, only 1 comparable (parsed) -- treated as a
        # single-provider result, not a tie.
        unparseable = self._score(None, parsed_ok=False, tag="unparseable")
        parseable = self._score(True, tag="parseable")
        result = _aggregate_gate_comparison_votes([unparseable, parseable])
        self.assertIs(result, parseable)

    def test_all_responses_unparseable_returns_first_as_a_no_op(self):
        from services.tier_c_orchestrator import _aggregate_gate_comparison_votes

        first = self._score(None, parsed_ok=False, tag="first")
        second = self._score(None, parsed_ok=False, tag="second")
        result = _aggregate_gate_comparison_votes([first, second])
        self.assertIs(result, first)
        self.assertFalse(result["parsed_ok"])


class Phase4ParallelFanOutTest(unittest.TestCase):
    """Step 10 Phase 4: end-to-end coverage of `run_tier_c_shadow_call`
    actually taking the fan-out branch -- sample rate forced to `1.0` so
    the roll always fans out, all three provider SDKs mocked so real
    dispatch happens through `call_llm` exactly as production would.
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

        self._session_local_patch = patch("services.tier_c_shadow_store.SessionLocal", self.SessionLocal)
        self._session_local_patch.start()
        self._sample_rate_patch = patch.object(settings, "TIER_C_PARALLEL_SHADOW_SAMPLE_RATE", 1.0)
        self._sample_rate_patch.start()

    def tearDown(self):
        self._sample_rate_patch.stop()
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

    def _rows(self):
        return (
            self.db.query(ShadowLLMCall)
            .filter(ShadowLLMCall.series_id == self.series.id)
            .order_by(ShadowLLMCall.id.asc())
            .all()
        )

    def test_all_three_providers_succeed_persists_three_rows_sharing_one_candidate_request_id(self):
        agree_text = '{"belongs_to_series": false, "confidence": "high", "inferred_number": 7}'
        env = {"ANTHROPIC_API_KEY": "test-key", "GROQ_API_KEY": "test-key", "OPENAI_API_KEY": "test-key"}
        with patch.dict(os.environ, env), patch(
            "anthropic.Anthropic", return_value=_mock_anthropic_client(agree_text)
        ), patch("groq.Groq", return_value=_mock_groq_or_openai_client(agree_text)), patch(
            "openai.OpenAI", return_value=_mock_groq_or_openai_client(agree_text)
        ):
            tier_c_score = self._call()

        rows = self._rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual({row.shadow_provider for row in rows}, {"anthropic", "groq", "openai"})

        candidate_request_ids = {row.candidate_request_id for row in rows}
        self.assertEqual(len(candidate_request_ids), 1)
        self.assertIsNotNone(next(iter(candidate_request_ids)))

        # All three agree with the gate (gate_belongs_to_series=False,
        # shadow reported false too) -> unanimous agreement.
        self.assertIsNotNone(tier_c_score)
        self.assertTrue(tier_c_score["belongs_to_series_agreement"])

    def test_one_provider_failing_still_persists_the_other_two_and_aggregates_them(self):
        agree_text = '{"belongs_to_series": false, "confidence": "high", "inferred_number": 7}'
        # OPENAI_API_KEY intentionally omitted -- that provider's call_llm
        # raises LLMCallError, exercising the partial-failure path.
        env = {"ANTHROPIC_API_KEY": "test-key", "GROQ_API_KEY": "test-key"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            with patch("anthropic.Anthropic", return_value=_mock_anthropic_client(agree_text)), patch(
                "groq.Groq", return_value=_mock_groq_or_openai_client(agree_text)
            ):
                tier_c_score = self._call()

        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.shadow_provider for row in rows}, {"anthropic", "groq"})
        self.assertIsNotNone(tier_c_score)
        self.assertTrue(tier_c_score["belongs_to_series_agreement"])

    def test_all_providers_failing_returns_none_and_persists_nothing(self):
        # No provider API keys patched -- conftest.py blanks
        # ANTHROPIC_API_KEY already; GROQ_API_KEY/OPENAI_API_KEY are
        # simply unset in this sandboxed test environment.
        for key in ("GROQ_API_KEY", "OPENAI_API_KEY"):
            os.environ.pop(key, None)

        tier_c_score = self._call()

        self.assertIsNone(tier_c_score)
        self.assertEqual(len(self._rows()), 0)

    def test_live_state_stays_single_provider_even_at_full_sample_rate(self):
        # settings.TIER_C_PARALLEL_SHADOW_SAMPLE_RATE is patched to 1.0 in
        # setUp -- this asserts "live" is still forced single-provider
        # regardless, per Step 10's finalized live-state exclusion rule.
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=_mock_anthropic_client('{"belongs_to_series": true}')
        ):
            self._call(tier_c_state="live")

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].shadow_provider, "anthropic")
        self.assertEqual(rows[0].tier_c_state_at_call, "live")


if __name__ == "__main__":
    unittest.main()
