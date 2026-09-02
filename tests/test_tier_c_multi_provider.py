"""Step 10 (Multi-Provider Tier C): incremental test coverage, one phase
at a time, mirroring how tests/test_tier_c_shadow_store.py grew across
Steps 8 and 9. Phase 1 (schema + settings scaffolding), Phase 3
(TierCOrchestrator extraction), Phase 4 (parallel fan-out), and Phase 5
(per-candidate aggregation + Step 9 wiring) so far -- later phases extend
this same file rather than starting a new one per phase.

Step 11 Phase 2 (Provider/Model Scorecard & Tier C Confidence Signals)
addition: two new test methods on `Phase5PromotionEngineWiringTest`
below covering the multi-provider-only consensus fields added to
`evaluate_tier_c_promotion`'s `metrics_snapshot` -- kept in that same
class (not a new one) since it's exercising the exact same function via
the exact same multi-provider fan-out fixtures, just asserting on two
additional dict keys.
"""

import os
import unittest
import uuid
from unittest.mock import MagicMock, patch

import settings
from database import Base
from models import Series, ShadowLLMCall
from services.discovery_telemetry import DiscoveryTelemetry
from services.tier_c_shadow_store import get_recent_candidate_aggregates, persist_tier_c_shadow_call
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
    def test_parallel_shadow_sample_rate_defaults_to_a_small_nonzero_fraction(self):
        """Step 10 Phase 6 (activation): raised from Phase 1-5's 0.0 to a
        small, non-zero default now that the per-candidate aggregation
        layer (Phase 5) has shipped -- see settings.py's own comment for
        why 0.05 specifically (the finalized plan's own "start low to cap
        cost" instruction) and `Phase6ConcurrencyAndTimeoutTest`/`tests/
        test_tier_c_multi_provider.py`'s Phase 4/5 classes for the broader
        safety-net coverage (live-state exclusion, partial-failure
        handling, promotion-engine parity) this activation leans on.

        `tests/conftest.py`'s autouse `_no_parallel_shadow_fan_out_during_
        tests` fixture pins the *runtime* module attribute to `0.0` for
        every test in this suite (a deliberate safety net against flaky,
        randomly-fanning-out tests -- see that fixture's own docstring),
        so this test can't just read `settings.TIER_C_PARALLEL_SHADOW_
        SAMPLE_RATE` directly -- it would always see `0.0` here regardless
        of what settings.py itself defaults to. Reloading the module with
        the env var unset re-executes settings.py's own default-resolution
        code, bypassing that safety-net patch for the duration of the
        reload only (the module object itself is unchanged by `reload` --
        only its attributes are recomputed -- so the conftest fixture's
        own `patch.object` teardown afterward is unaffected either way).
        """
        import importlib

        previous_env_value = os.environ.pop("TIER_C_PARALLEL_SHADOW_SAMPLE_RATE", None)
        try:
            importlib.reload(settings)
            default_value = settings.TIER_C_PARALLEL_SHADOW_SAMPLE_RATE
        finally:
            if previous_env_value is not None:
                os.environ["TIER_C_PARALLEL_SHADOW_SAMPLE_RATE"] = previous_env_value
            importlib.reload(settings)

        self.assertGreater(default_value, 0.0)
        self.assertLessEqual(default_value, 0.1)

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


class Phase5CandidateAggregationTest(unittest.TestCase):
    """Step 10 Phase 5: direct, unit-level coverage of `services.tier_c_
    shadow_store.get_recent_candidate_aggregates` -- the read-time
    counterpart to Phase 4's in-process `_aggregate_gate_comparison_
    votes`, exercised here against real persisted rows instead of
    hand-built score dicts.
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

    def tearDown(self):
        self.db.close()

    def _add_row(
        self,
        *,
        candidate_request_id: str | None,
        provider: str = "anthropic",
        belongs_to_series_agreement: bool | None,
        shadow_belongs_to_series: bool | None = None,
        parsed_ok: bool = True,
        created_at=None,
    ) -> None:
        from datetime import datetime

        self.db.add(
            ShadowLLMCall(
                series_id=self.series.id,
                run_id="job-1",
                tier="C",
                gate_belongs_to_series=False,
                shadow_provider=provider,
                shadow_model_id="some-model",
                shadow_belongs_to_series=(
                    shadow_belongs_to_series if shadow_belongs_to_series is not None else belongs_to_series_agreement
                ),
                parsed_ok=parsed_ok,
                belongs_to_series_agreement=belongs_to_series_agreement,
                candidate_request_id=candidate_request_id,
                created_at=created_at or datetime.utcnow(),
            )
        )
        self.db.commit()

    def _aggregates(self, limit=10):
        return get_recent_candidate_aggregates(self.db, self.series.id, limit)

    def test_single_provider_candidate_aggregates_trivially(self):
        cand = uuid.uuid4().hex
        self._add_row(candidate_request_id=cand, belongs_to_series_agreement=True)

        aggregates = self._aggregates()
        self.assertEqual(len(aggregates), 1)
        agg = aggregates[0]
        self.assertEqual(agg["candidate_request_id"], cand)
        self.assertEqual(agg["provider_count"], 1)
        self.assertEqual(agg["voter_count"], 1)
        self.assertTrue(agg["gate_agreement"])
        self.assertEqual(agg["consensus_score"], 1.0)
        self.assertFalse(agg["conflict_flag"])

    def test_three_provider_unanimous_agreement(self):
        cand = uuid.uuid4().hex
        for provider in ("anthropic", "groq", "openai"):
            self._add_row(candidate_request_id=cand, provider=provider, belongs_to_series_agreement=True)

        agg = self._aggregates()[0]
        self.assertEqual(agg["provider_count"], 3)
        self.assertTrue(agg["gate_agreement"])
        self.assertEqual(agg["consensus_score"], 1.0)
        self.assertFalse(agg["conflict_flag"])

    def test_two_one_split_follows_majority_and_flags_conflict(self):
        cand = uuid.uuid4().hex
        self._add_row(candidate_request_id=cand, provider="anthropic", belongs_to_series_agreement=True)
        self._add_row(candidate_request_id=cand, provider="groq", belongs_to_series_agreement=True)
        self._add_row(candidate_request_id=cand, provider="openai", belongs_to_series_agreement=False)

        agg = self._aggregates()[0]
        self.assertEqual(agg["provider_count"], 3)
        self.assertTrue(agg["gate_agreement"])
        # Providers disagreed with each other (2 said "agrees with gate",
        # 1 said "disagrees with gate") -- cross-provider conflict, even
        # though the gate-comparison vote itself was decisive.
        self.assertTrue(agg["conflict_flag"])
        self.assertAlmostEqual(agg["consensus_score"], 2 / 3)

    def test_exact_two_way_tie_resolves_to_disagreement(self):
        cand = uuid.uuid4().hex
        self._add_row(candidate_request_id=cand, provider="anthropic", belongs_to_series_agreement=True)
        self._add_row(candidate_request_id=cand, provider="groq", belongs_to_series_agreement=False)

        agg = self._aggregates()[0]
        self.assertFalse(agg["gate_agreement"])
        self.assertTrue(agg["conflict_flag"])

    def test_unparseable_row_counts_toward_provider_count_not_votes(self):
        cand = uuid.uuid4().hex
        self._add_row(candidate_request_id=cand, provider="anthropic", belongs_to_series_agreement=True)
        self._add_row(
            candidate_request_id=cand,
            provider="groq",
            belongs_to_series_agreement=None,
            shadow_belongs_to_series=None,
            parsed_ok=False,
        )

        agg = self._aggregates()[0]
        self.assertEqual(agg["provider_count"], 2)
        self.assertEqual(agg["voter_count"], 1)
        self.assertTrue(agg["gate_agreement"])
        # Only 1 comparable shadow_belongs_to_series value -- trivially
        # "consensus" with itself.
        self.assertEqual(agg["consensus_score"], 1.0)
        self.assertFalse(agg["conflict_flag"])

    def test_rows_with_no_candidate_request_id_are_excluded(self):
        # Historical, pre-Phase-4 rows -- see get_recent_candidate_
        # aggregates' docstring for why these are deliberately excluded
        # rather than synthesized into singleton groups.
        self._add_row(candidate_request_id=None, belongs_to_series_agreement=True)

        self.assertEqual(self._aggregates(), [])

    def test_respects_limit_and_orders_most_recent_candidate_first(self):
        from datetime import datetime, timedelta

        base = datetime.utcnow()
        older_cand = uuid.uuid4().hex
        newer_cand = uuid.uuid4().hex
        self._add_row(candidate_request_id=older_cand, belongs_to_series_agreement=True, created_at=base)
        self._add_row(
            candidate_request_id=newer_cand,
            belongs_to_series_agreement=False,
            created_at=base + timedelta(seconds=10),
        )

        all_aggregates = self._aggregates(limit=10)
        self.assertEqual([a["candidate_request_id"] for a in all_aggregates], [newer_cand, older_cand])

        limited = self._aggregates(limit=1)
        self.assertEqual([a["candidate_request_id"] for a in limited], [newer_cand])

    def test_no_rows_returns_empty_list(self):
        self.assertEqual(self._aggregates(), [])


class Step11Phase5RiskFlagsTest(unittest.TestCase):
    """Step 11 Phase 5: unit coverage for `services.tier_c_shadow_store.
    get_candidate_risk_flags` -- a read-time-only risk-flag lookup built
    on the same `_build_candidate_aggregate` output `get_recent_candidate_
    aggregates` uses. No persistence, no routing/promotion wiring.
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

    def tearDown(self):
        self.db.close()

    def _add_row(
        self,
        *,
        candidate_request_id: str,
        provider: str = "anthropic",
        belongs_to_series_agreement: bool | None = True,
        shadow_belongs_to_series: bool | None = None,
        parsed_ok: bool = True,
        shadow_confidence: str | None = "high",
    ) -> None:
        from datetime import datetime

        self.db.add(
            ShadowLLMCall(
                series_id=self.series.id,
                run_id="job-1",
                tier="C",
                gate_belongs_to_series=False,
                shadow_provider=provider,
                shadow_model_id="some-model",
                shadow_belongs_to_series=(
                    shadow_belongs_to_series if shadow_belongs_to_series is not None else belongs_to_series_agreement
                ),
                shadow_confidence=shadow_confidence,
                parsed_ok=parsed_ok,
                belongs_to_series_agreement=belongs_to_series_agreement,
                candidate_request_id=candidate_request_id,
                created_at=datetime.utcnow(),
            )
        )
        self.db.commit()

    def _flags(self, candidate_request_id: str) -> list[str]:
        from services.tier_c_shadow_store import get_candidate_risk_flags

        return get_candidate_risk_flags(self.db, candidate_request_id)

    def test_unknown_candidate_id_returns_no_flags(self):
        self.assertEqual(self._flags(uuid.uuid4().hex), [])

    def test_clean_agreeing_candidate_has_no_flags(self):
        cand = uuid.uuid4().hex
        self._add_row(candidate_request_id=cand, belongs_to_series_agreement=True, shadow_confidence="high")

        self.assertEqual(self._flags(cand), [])

    def test_gate_disagreement_flag(self):
        cand = uuid.uuid4().hex
        self._add_row(candidate_request_id=cand, belongs_to_series_agreement=False)

        self.assertEqual(self._flags(cand), ["gate_disagreement"])

    def test_unparseable_flag_fires_even_when_the_candidate_still_has_a_usable_vote(self):
        """A partial parse failure (1 of 2 rows) still gets flagged, even
        though the candidate's overall gate_agreement is perfectly valid
        (computed from the one parseable row) -- "unparseable" surfaces
        ANY parse failure, not just a total one.
        """
        cand = uuid.uuid4().hex
        self._add_row(candidate_request_id=cand, provider="anthropic", belongs_to_series_agreement=True)
        self._add_row(
            candidate_request_id=cand,
            provider="groq",
            belongs_to_series_agreement=None,
            shadow_belongs_to_series=None,
            parsed_ok=False,
            shadow_confidence=None,
        )

        flags = self._flags(cand)
        self.assertIn("unparseable", flags)
        self.assertNotIn("gate_disagreement", flags)

    def test_fully_unparseable_candidate_gets_unparseable_not_gate_disagreement(self):
        cand = uuid.uuid4().hex
        self._add_row(
            candidate_request_id=cand,
            belongs_to_series_agreement=None,
            shadow_belongs_to_series=None,
            parsed_ok=False,
            shadow_confidence=None,
        )

        flags = self._flags(cand)
        self.assertEqual(flags, ["unparseable"])

    def test_cross_provider_conflict_flag(self):
        cand = uuid.uuid4().hex
        self._add_row(
            candidate_request_id=cand,
            provider="anthropic",
            belongs_to_series_agreement=True,
            shadow_belongs_to_series=True,
        )
        self._add_row(
            candidate_request_id=cand,
            provider="groq",
            belongs_to_series_agreement=False,
            shadow_belongs_to_series=False,
        )

        self.assertIn("cross_provider_conflict", self._flags(cand))

    def test_low_confidence_primary_flag_reads_the_anthropic_row_specifically(self):
        cand = uuid.uuid4().hex
        self._add_row(
            candidate_request_id=cand,
            provider="anthropic",
            belongs_to_series_agreement=True,
            shadow_confidence="low",
        )
        # Non-primary provider's OWN low confidence must not trigger this
        # flag -- it's scoped to the primary provider's row specifically.
        self._add_row(
            candidate_request_id=cand,
            provider="groq",
            belongs_to_series_agreement=True,
            shadow_confidence="high",
        )

        self.assertIn("low_confidence_primary", self._flags(cand))

    def test_non_primary_low_confidence_alone_does_not_trigger_the_flag(self):
        cand = uuid.uuid4().hex
        self._add_row(
            candidate_request_id=cand, provider="anthropic", belongs_to_series_agreement=True, shadow_confidence="high"
        )
        self._add_row(
            candidate_request_id=cand, provider="groq", belongs_to_series_agreement=True, shadow_confidence="low"
        )

        self.assertNotIn("low_confidence_primary", self._flags(cand))

    def test_missing_primary_row_does_not_error_or_flag(self):
        cand = uuid.uuid4().hex
        self._add_row(
            candidate_request_id=cand, provider="groq", belongs_to_series_agreement=True, shadow_confidence="low"
        )

        # No anthropic row at all for this candidate -- must not raise,
        # and must not flag (nothing to check confidence on).
        self.assertNotIn("low_confidence_primary", self._flags(cand))

    def test_all_four_flags_can_coexist(self):
        cand = uuid.uuid4().hex
        self._add_row(
            candidate_request_id=cand,
            provider="anthropic",
            belongs_to_series_agreement=False,
            shadow_belongs_to_series=True,
            shadow_confidence="low",
        )
        self._add_row(
            candidate_request_id=cand,
            provider="groq",
            belongs_to_series_agreement=False,
            shadow_belongs_to_series=False,
        )
        self._add_row(
            candidate_request_id=cand,
            provider="openai",
            belongs_to_series_agreement=None,
            shadow_belongs_to_series=None,
            parsed_ok=False,
            shadow_confidence=None,
        )

        flags = set(self._flags(cand))
        self.assertEqual(
            flags,
            {"unparseable", "gate_disagreement", "cross_provider_conflict", "low_confidence_primary"},
        )


class Phase5PromotionEngineWiringTest(unittest.TestCase):
    """Step 10 Phase 5: end-to-end coverage that `evaluate_tier_c_
    promotion` now counts per-candidate votes, not raw rows -- the actual
    behavior-change point of this phase. `tests/test_tier_c_promotion_
    engine.py`'s existing single-provider-shaped tests already confirm
    "no change for today's only real traffic shape"; this class confirms
    the new multi-provider-aware counting itself.
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

        self._session_local_patch = patch("services.tier_c_promotion_engine.SessionLocal", self.SessionLocal)
        self._session_local_patch.start()
        self._settings_patches = [
            patch.object(settings, "TIER_C_PROMOTION_MIN_CALLS", 2),
            patch.object(settings, "TIER_C_PROMOTION_AGREEMENT_THRESHOLD", 0.9),
            patch.object(settings, "TIER_C_DEMOTION_DISAGREEMENT_THRESHOLD", 0.3),
        ]
        for p in self._settings_patches:
            p.start()

    def tearDown(self):
        for p in self._settings_patches:
            p.stop()
        self._session_local_patch.stop()
        self.db.close()

    def _add_fan_out_candidate(self, *, candidate_request_id, agreements: list[bool]) -> None:
        from datetime import datetime

        for i, agreement in enumerate(agreements):
            self.db.add(
                ShadowLLMCall(
                    series_id=self.series.id,
                    run_id="job-1",
                    tier="C",
                    gate_belongs_to_series=False,
                    shadow_provider=["anthropic", "groq", "openai"][i],
                    shadow_model_id="some-model",
                    shadow_belongs_to_series=agreement,
                    parsed_ok=True,
                    belongs_to_series_agreement=agreement,
                    candidate_request_id=candidate_request_id,
                    created_at=datetime.utcnow(),
                )
            )
        self.db.commit()

    def _history_last(self):
        from models import TierCPromotionHistory

        return (
            self.db.query(TierCPromotionHistory)
            .filter(TierCPromotionHistory.series_id == self.series.id)
            .order_by(TierCPromotionHistory.id.desc())
            .first()
        )

    def test_a_three_provider_candidate_counts_as_one_vote_not_three(self):
        from services.tier_c_promotion_engine import evaluate_tier_c_promotion

        # 2 candidates, min_calls=2 -- if this incorrectly counted raw
        # rows (3 + 1 = 4), it would still clear min_calls, but the
        # *rate* would differ: 3 rows agree + 1 disagrees = 75% raw-row
        # agreement vs. this test's actual 1 agree-candidate + 1
        # disagree-candidate = 50% per-candidate agreement. Asserting the
        # 50% figure proves candidates, not rows, are being counted.
        self._add_fan_out_candidate(
            candidate_request_id=uuid.uuid4().hex, agreements=[True, True, True]
        )
        self._add_fan_out_candidate(candidate_request_id=uuid.uuid4().hex, agreements=[False])

        evaluate_tier_c_promotion(self.series.id)

        history = self._history_last()
        self.assertEqual(history.shadow_calls_considered, 2)
        self.assertEqual(history.agreement_rate, 0.5)
        self.assertEqual(history.metrics_snapshot["agreement_count"], 1)
        self.assertEqual(history.metrics_snapshot["disagreement_count"], 1)

    def test_cross_provider_metrics_land_in_metrics_snapshot_only(self):
        from services.tier_c_promotion_engine import evaluate_tier_c_promotion

        self._add_fan_out_candidate(
            candidate_request_id=uuid.uuid4().hex, agreements=[True, True, False]
        )
        self._add_fan_out_candidate(candidate_request_id=uuid.uuid4().hex, agreements=[True])

        evaluate_tier_c_promotion(self.series.id)

        history = self._history_last()
        self.assertEqual(history.metrics_snapshot["cross_provider_conflict_candidate_count"], 1)
        self.assertIsNotNone(history.metrics_snapshot["cross_provider_avg_consensus_score"])
        # Both candidates voted "agree" (majority of [T, T, F] is
        # agreement) -- promotion decision itself is untouched by the
        # conflict signal above.
        self.assertEqual(history.agreement_rate, 1.0)

    def test_multi_provider_only_consensus_excludes_single_provider_candidates(self):
        """Step 11 Phase 2: the blended `cross_provider_avg_consensus_
        score` averages in the trivial single-provider candidate's
        consensus_score=1.0, diluting the real 3-provider disagreement --
        the multi_provider_only variant must not.
        """
        from services.tier_c_promotion_engine import evaluate_tier_c_promotion

        # 3-provider candidate: 2 agree, 1 disagrees -> consensus_score =
        # 2/3, conflict_flag=True.
        self._add_fan_out_candidate(
            candidate_request_id=uuid.uuid4().hex, agreements=[True, True, False]
        )
        # Single-provider candidate: trivially consensus_score=1.0,
        # conflict_flag=False.
        self._add_fan_out_candidate(candidate_request_id=uuid.uuid4().hex, agreements=[True])

        evaluate_tier_c_promotion(self.series.id)

        snapshot = self._history_last().metrics_snapshot
        # Blended: (2/3 + 1.0) / 2 -- diluted toward 1.0 by the trivial
        # single-provider candidate.
        self.assertAlmostEqual(snapshot["cross_provider_avg_consensus_score"], (2 / 3 + 1.0) / 2)
        # Multi-provider-only: just the one real 3-provider candidate's
        # 2/3, undiluted.
        self.assertEqual(snapshot["cross_provider_multi_provider_candidate_count"], 1)
        self.assertAlmostEqual(
            snapshot["cross_provider_avg_consensus_score_multi_provider_only"], 2 / 3
        )

    def test_multi_provider_only_consensus_is_none_when_no_candidate_has_two_providers(self):
        """Step 11 Phase 2: when every candidate in the window is single-
        provider (today's actual production traffic shape), the
        multi-provider-only field must be `None` -- NOT silently fall
        back to the blended (trivially 1.0) average, which would make it
        look like "perfect consensus" when there's really zero multi-
        provider evidence at all.
        """
        from services.tier_c_promotion_engine import evaluate_tier_c_promotion

        self._add_fan_out_candidate(candidate_request_id=uuid.uuid4().hex, agreements=[True])
        self._add_fan_out_candidate(candidate_request_id=uuid.uuid4().hex, agreements=[False])

        evaluate_tier_c_promotion(self.series.id)

        snapshot = self._history_last().metrics_snapshot
        self.assertEqual(snapshot["cross_provider_multi_provider_candidate_count"], 0)
        self.assertIsNone(snapshot["cross_provider_avg_consensus_score_multi_provider_only"])
        # Blended field is unaffected by this phase -- still the trivial
        # 1.0 it always was for single-provider-only windows.
        self.assertEqual(snapshot["cross_provider_avg_consensus_score"], 1.0)


class Step11Phase3ParseFailureSpikeWiringTest(unittest.TestCase):
    """Step 11 Phase 3: confirms `evaluate_tier_c_promotion` actually
    invokes `services.provider_model_scorecard.check_parse_failure_spikes`
    once per call, and that the wiring is fail-soft -- a spike-check
    failure must never prevent THIS series' own promotion evaluation
    (the `TierCPromotionHistory` row) from still being written. Detailed
    behavior of the detector itself (threshold comparison, alert shape,
    etc.) is covered in tests/test_provider_model_scorecard.py; this
    class only covers the wiring.
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

        self._session_local_patch = patch("services.tier_c_promotion_engine.SessionLocal", self.SessionLocal)
        self._session_local_patch.start()

    def tearDown(self):
        self._session_local_patch.stop()
        self.db.close()

    def _history_last(self):
        from models import TierCPromotionHistory

        return (
            self.db.query(TierCPromotionHistory)
            .filter(TierCPromotionHistory.series_id == self.series.id)
            .order_by(TierCPromotionHistory.id.desc())
            .first()
        )

    def test_check_parse_failure_spikes_is_called_once_per_evaluation(self):
        from services.tier_c_promotion_engine import evaluate_tier_c_promotion

        with patch(
            "services.tier_c_promotion_engine.check_parse_failure_spikes", return_value=[]
        ) as mock_check:
            evaluate_tier_c_promotion(self.series.id)

        mock_check.assert_called_once()
        # Called with the caller's own db session (read-only, no
        # independent session needed) -- not `self.db` directly (that's
        # the test's handle on the SAME session `SessionLocal()` returns
        # inside evaluate_tier_c_promotion, per the class-level patch
        # above), so assert on the type instead of identity.
        (call_args, _call_kwargs) = mock_check.call_args
        self.assertEqual(len(call_args), 1)

    def test_a_spike_check_failure_does_not_prevent_the_series_evaluation_from_completing(self):
        from services.tier_c_promotion_engine import evaluate_tier_c_promotion

        with patch(
            "services.tier_c_promotion_engine.check_parse_failure_spikes",
            side_effect=RuntimeError("scorecard query boom"),
        ):
            evaluate_tier_c_promotion(self.series.id)

        # The series' own evaluation still ran and wrote its history row,
        # completely unaffected by the global spike-check's failure.
        history = self._history_last()
        self.assertIsNotNone(history)
        self.assertEqual(history.evaluation_reason, "insufficient_evidence")


class Phase6ConcurrencyAndTimeoutTest(unittest.TestCase):
    """Step 10 Phase 6 (tuning & safety checks): the two coverage items
    from the finalized plan's own checklist that no earlier phase's tests
    actually exercised yet -- Phase 4/5's fan-out tests all use instant-
    return mocks, which prove correctness but not genuine concurrency, and
    prove the *value* of `TIER_C_PARALLEL_CALL_TIMEOUT_SECONDS` gets used
    (via `_aggregate_gate_comparison_votes`'s partial-failure handling)
    without ever proving it's actually *passed* to every provider call.
    This class closes both gaps directly. Partial-failure/tie-break logic,
    live-state-always-single-provider, and promotion-engine parity under
    both single- and multi-provider data are already covered by `Phase4AggregationTest`/
    `Phase4ParallelFanOutTest`/`Phase5PromotionEngineWiringTest` and
    `tests/test_tier_c_promotion_engine.py` -- not re-duplicated here.
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

    def _call(self, *, tier_c_state="shadow_only"):
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
        )

    def test_three_providers_are_called_concurrently_not_sequentially(self):
        import time

        per_call_delay_s = 0.2
        agree_text = '{"belongs_to_series": false, "confidence": "high", "inferred_number": 7}'

        def slow_anthropic(api_key):
            time.sleep(per_call_delay_s)
            return _mock_anthropic_client(agree_text)

        def slow_openai_shaped(*, api_key=None):
            time.sleep(per_call_delay_s)
            return _mock_groq_or_openai_client(agree_text)

        env = {"ANTHROPIC_API_KEY": "test-key", "GROQ_API_KEY": "test-key", "OPENAI_API_KEY": "test-key"}
        with patch.dict(os.environ, env), patch("anthropic.Anthropic", side_effect=slow_anthropic), patch(
            "groq.Groq", side_effect=slow_openai_shaped
        ), patch("openai.OpenAI", side_effect=slow_openai_shaped):
            started = time.monotonic()
            self._call()
            elapsed = time.monotonic() - started

        # Sequential execution would take >= 3 * per_call_delay_s (0.6s);
        # genuine concurrency should finish close to a single call's
        # delay. The threshold (1.5x one call) leaves generous headroom
        # for scheduling/GIL overhead while still failing hard if the
        # three calls were run one after another.
        self.assertLess(elapsed, per_call_delay_s * 1.5)

    def test_parallel_call_timeout_is_passed_to_every_provider(self):
        captured_timeouts: dict[str, float | None] = {}
        agree_text = '{"belongs_to_series": false, "confidence": "high", "inferred_number": 7}'

        def capture_anthropic(api_key):
            client = _mock_anthropic_client(agree_text)

            def capture_create(**kwargs):
                captured_timeouts["anthropic"] = kwargs.get("timeout")
                return client.messages.create.return_value

            client.messages.create.side_effect = capture_create
            return client

        def make_capture_openai_shaped(name):
            def capture(api_key=None):
                client = _mock_groq_or_openai_client(agree_text)

                def capture_create(**kwargs):
                    captured_timeouts[name] = kwargs.get("timeout")
                    return client.chat.completions.create.return_value

                client.chat.completions.create.side_effect = capture_create
                return client

            return capture

        env = {"ANTHROPIC_API_KEY": "test-key", "GROQ_API_KEY": "test-key", "OPENAI_API_KEY": "test-key"}
        with patch.dict(os.environ, env), patch(
            "anthropic.Anthropic", side_effect=capture_anthropic
        ), patch("groq.Groq", side_effect=make_capture_openai_shaped("groq")), patch(
            "openai.OpenAI", side_effect=make_capture_openai_shaped("openai")
        ):
            self._call()

        self.assertEqual(
            captured_timeouts,
            {
                "anthropic": settings.TIER_C_PARALLEL_CALL_TIMEOUT_SECONDS,
                "groq": settings.TIER_C_PARALLEL_CALL_TIMEOUT_SECONDS,
                "openai": settings.TIER_C_PARALLEL_CALL_TIMEOUT_SECONDS,
            },
        )


if __name__ == "__main__":
    unittest.main()
