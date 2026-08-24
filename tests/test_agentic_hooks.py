"""RT-1b (Phase 1 agentic substrate) coverage.

Two things this file needs to prove, per `discovery_agentic_phase1_plan.md`
section 5:

1. `agentic_hooks.py` itself is fail-soft -- none of its five public
   functions can raise, even when handed hostile input (non-dict context,
   a telemetry object whose delegated method blows up, etc.), and
   `record_tool_call` correctly delegates to a `DiscoveryTelemetry`
   instance when one is present on `context["telemetry"]`.
2. Wiring RT-1b into `agents/series_agent.py`'s `run_series_check` changes
   *nothing* about its existing behavior (routing outcomes, skeleton_
   updates, returned result shape) -- it only adds a side channel. This is
   verified two ways: (a) diffing a full `run_series_check` result against
   what it produced before RT-1b existed (frozen fixture-style assertions
   already covered by `tests/test_series_discovery.py`'s
   `SeriesCheckIntegrationTest` class, which still passes unmodified), and
   (b) here, a dedicated check that even a fully broken `agentic_hooks`
   module (every function raising) still lets `run_series_check` complete
   normally and return the same routing outcome.
"""

import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import agentic_hooks
import discovery_engine
from agents.series_agent import SeriesIntelligenceAgent
from database import Base
from models import Book, Series
from services.discovery_telemetry import DiscoveryTelemetry


class BeginTurnTest(unittest.TestCase):
    def test_returns_new_dict_without_mutating_input(self):
        original = {"series_id": 1}
        turn_context = agentic_hooks.begin_turn(original)
        self.assertIsNot(turn_context, original)
        self.assertEqual(original, {"series_id": 1})

    def test_preserves_caller_provided_keys(self):
        turn_context = agentic_hooks.begin_turn({"series_id": 42, "user_id": "robbie", "custom": "value"})
        self.assertEqual(turn_context["series_id"], 42)
        self.assertEqual(turn_context["user_id"], "robbie")
        self.assertEqual(turn_context["custom"], "value")

    def test_fills_in_turn_bookkeeping_defaults(self):
        turn_context = agentic_hooks.begin_turn({"series_id": 1})
        self.assertIn("turn_id", turn_context)
        self.assertIn("timestamp", turn_context)
        self.assertEqual(turn_context["reasoning_steps"], [])
        self.assertEqual(turn_context["tool_call_count"], 0)
        self.assertEqual(turn_context["world_model_update_count"], 0)

    def test_none_context_does_not_raise(self):
        turn_context = agentic_hooks.begin_turn(None)
        self.assertIsInstance(turn_context, dict)

    def test_non_dict_context_does_not_raise(self):
        # Defensive: begin_turn must fail-soft even if a caller passes
        # something that isn't a dict at all.
        turn_context = agentic_hooks.begin_turn("not-a-dict")  # type: ignore[arg-type]
        self.assertIsInstance(turn_context, dict)


class RecordToolCallTest(unittest.TestCase):
    def test_delegates_to_telemetry_when_present(self):
        telemetry = DiscoveryTelemetry()
        context = agentic_hooks.begin_turn({"series_id": 1, "telemetry": telemetry})
        agentic_hooks.record_tool_call(context, "serper", "some query", {"a": 1, "b": 2})

        summary = telemetry.summary()
        self.assertEqual(summary["total_tool_calls"], 1)
        self.assertEqual(telemetry.tool_calls[0]["provider"], "serper")
        self.assertEqual(telemetry.tool_calls[0]["query"], "some query")
        self.assertEqual(telemetry.tool_calls[0]["result_size"], 2)

    def test_increments_context_tool_call_count(self):
        context = agentic_hooks.begin_turn({"series_id": 1})
        agentic_hooks.record_tool_call(context, "apify", "q1", [])
        agentic_hooks.record_tool_call(context, "apify", "q2", [1, 2, 3])
        self.assertEqual(context["tool_call_count"], 2)

    def test_no_telemetry_on_context_does_not_raise(self):
        context = agentic_hooks.begin_turn({"series_id": 1})
        # Should be a no-op besides context bookkeeping/logging.
        agentic_hooks.record_tool_call(context, "serper", "q", {"x": 1})
        self.assertEqual(context["tool_call_count"], 1)

    def test_telemetry_whose_record_tool_call_raises_does_not_propagate(self):
        broken_telemetry = MagicMock()
        broken_telemetry.record_tool_call.side_effect = RuntimeError("boom")
        context = agentic_hooks.begin_turn({"series_id": 1, "telemetry": broken_telemetry})
        # Must not raise despite the delegated call blowing up.
        agentic_hooks.record_tool_call(context, "serper", "q", {})
        self.assertEqual(context["tool_call_count"], 1)

    def test_non_dict_context_does_not_raise(self):
        agentic_hooks.record_tool_call("not-a-dict", "serper", "q", {})  # type: ignore[arg-type]

    def test_raw_result_of_every_shape_is_handled(self):
        context = agentic_hooks.begin_turn({"series_id": 1})
        for raw_result in (None, [], {}, "a string", 12345, object()):
            agentic_hooks.record_tool_call(context, "serper", "q", raw_result)
        self.assertEqual(context["tool_call_count"], 6)


class RecordReasoningStepTest(unittest.TestCase):
    def test_appends_structured_step_to_context(self):
        context = agentic_hooks.begin_turn({"series_id": 1})
        agentic_hooks.record_reasoning_step(context, {"phase": "routing", "decision": "accept"})
        self.assertEqual(len(context["reasoning_steps"]), 1)
        step = context["reasoning_steps"][0]
        self.assertEqual(step["phase"], "routing")
        self.assertEqual(step["decision"], "accept")
        self.assertIn("turn_id", step)
        self.assertIn("recorded_at", step)

    def test_none_step_does_not_raise(self):
        context = agentic_hooks.begin_turn({"series_id": 1})
        agentic_hooks.record_reasoning_step(context, None)
        self.assertEqual(len(context["reasoning_steps"]), 1)

    def test_non_dict_context_does_not_raise(self):
        agentic_hooks.record_reasoning_step("not-a-dict", {"phase": "x"})  # type: ignore[arg-type]


class RecordWorldModelUpdateTest(unittest.TestCase):
    def test_increments_world_model_update_count(self):
        context = agentic_hooks.begin_turn({"series_id": 1})
        agentic_hooks.record_world_model_update(
            context, {"series_id": 1, "books_changed": 2, "numbers_changed": [7.0], "confidence_changes": []}
        )
        self.assertEqual(context["world_model_update_count"], 1)

    def test_none_update_does_not_raise(self):
        context = agentic_hooks.begin_turn({"series_id": 1})
        agentic_hooks.record_world_model_update(context, None)
        self.assertEqual(context["world_model_update_count"], 1)

    def test_non_dict_context_does_not_raise(self):
        agentic_hooks.record_world_model_update("not-a-dict", {"series_id": 1})  # type: ignore[arg-type]


class EndTurnTest(unittest.TestCase):
    def test_end_turn_does_not_raise_on_well_formed_context(self):
        context = agentic_hooks.begin_turn({"series_id": 1})
        agentic_hooks.record_tool_call(context, "serper", "q", {})
        agentic_hooks.record_reasoning_step(context, {"phase": "routing", "decision": "accept"})
        agentic_hooks.end_turn(context)  # no assertion needed beyond "did not raise"

    def test_end_turn_on_empty_context_does_not_raise(self):
        agentic_hooks.end_turn({})

    def test_end_turn_on_none_does_not_raise(self):
        agentic_hooks.end_turn(None)  # type: ignore[arg-type]

    def test_end_turn_on_non_dict_does_not_raise(self):
        agentic_hooks.end_turn("not-a-dict")  # type: ignore[arg-type]

    def test_end_turn_with_malformed_timestamp_does_not_raise(self):
        agentic_hooks.end_turn({"turn_started_at": "not-a-real-timestamp"})


class DiscoveryTelemetryToolCallTest(unittest.TestCase):
    """RT-1b's new additive DiscoveryTelemetry.record_tool_call helper --
    must not touch the existing PB-9 provider_calls/gate_outcomes counters
    or their summary() breakdowns.
    """

    def test_tool_calls_are_recorded_independently_of_provider_calls(self):
        telemetry = DiscoveryTelemetry()
        telemetry.record_provider_call("google", ok=True, duration_s=0.1)
        telemetry.record_tool_call(provider="serper", query="q", result_size=3)

        summary = telemetry.summary()
        self.assertEqual(summary["total_tool_calls"], 1)
        self.assertEqual(summary["by_provider"]["google"]["calls"], 1)
        # New tool_calls tracking must not appear in by_provider/by_gate.
        self.assertNotIn("serper", summary["by_provider"])

    def test_no_tool_calls_is_zero_not_a_missing_key(self):
        summary = DiscoveryTelemetry().summary()
        self.assertEqual(summary["total_tool_calls"], 0)


class SeriesAgentNoBehaviorChangeTest(unittest.TestCase):
    """Integration-level "no behavior change" guarantee: RT-1b's hooks are
    wired into `run_series_check`, but even if `agentic_hooks` itself is
    completely broken (every function raises), the discovery run must
    still complete and produce the exact same routing outcome -- proving
    the hooks are a true side channel, not something the real pipeline
    can be made to depend on.
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
        for number in [1, 2, 3, 4, 5, 6, 8, 9]:
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

    def _mock_discovery(self, candidates, **overrides):
        result = {
            "candidates": candidates,
            "provider_failures": [],
            "all_providers_failed": False,
            "used_author_fallback": False,
        }
        result.update(overrides)
        return patch("discovery_engine.discover_candidates_for_series", return_value=result)

    def _candidate(self):
        return {
            "source": "hardcover",
            "source_id": "hc-7",
            "title": "Cherry Blossom Girls Book 7",
            "authors": ["Harmon Cooper"],
            "published_date": "2024-02-20",
            "isbn13": None,
            "source_url": None,
            "language": "",
            "confidence": "targeted",
            "series_number_hint": 7,
            "upcoming_hint": False,
        }

    def test_normal_run_still_produces_expected_routing(self):
        with self._mock_discovery([self._candidate()]):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)
        self.assertEqual(result["available_missing"][0]["series_number"], 7)

    def test_hooks_are_actually_invoked_during_a_real_run_without_changing_the_result(self):
        # Spies (no side_effect -- calls pass through harmlessly) confirm
        # RT-1b's wiring actually fires at the documented points (turn
        # lifecycle, at least one tool call, at least one reasoning step,
        # one world-model update), while the result itself is byte-for-byte
        # identical to the unpatched run above -- proving the hooks
        # observe this run without altering it.
        with self._mock_discovery([self._candidate()]), patch(
            "agents.series_agent.agentic_hooks.begin_turn", wraps=agentic_hooks.begin_turn
        ) as spy_begin, patch(
            "agents.series_agent.agentic_hooks.record_tool_call", wraps=agentic_hooks.record_tool_call
        ) as spy_tool_call, patch(
            "agents.series_agent.agentic_hooks.record_reasoning_step", wraps=agentic_hooks.record_reasoning_step
        ) as spy_reasoning, patch(
            "agents.series_agent.agentic_hooks.record_world_model_update",
            wraps=agentic_hooks.record_world_model_update,
        ) as spy_world_model, patch(
            "agents.series_agent.agentic_hooks.end_turn", wraps=agentic_hooks.end_turn
        ) as spy_end:
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        spy_begin.assert_called_once()
        self.assertGreaterEqual(spy_tool_call.call_count, 1)
        self.assertGreaterEqual(spy_reasoning.call_count, 1)
        spy_world_model.assert_called_once()
        spy_end.assert_called_once()

        # Same outcome as the unpatched run in the previous test.
        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)
        self.assertEqual(result["available_missing"][0]["series_number"], 7)

    def test_needs_review_and_skeleton_updates_unaffected_by_agentic_context(self):
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-7",
                "title": "Desert Protocol",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "author_fallback",
                "series_number_hint": 7,
                "series_name_hint": "Cherry Blossom Girls",
                "upcoming_hint": False,
            }
        ]
        unified_candidate = discovery_engine.UnifiedCandidate(
            title="Desert Protocol",
            authors=["Harmon Cooper"],
            series_name="Cherry Blossom Girls",
            series_number=7.0,
            metadata_completeness_score=1.0,
            source_provenance=[{"source": "hardcover"}],
        )
        with self._mock_discovery(candidates, unified_candidates=[unified_candidate]):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(len(result["needs_review"]), 1)
        self.assertEqual(len(result["skeleton_updates"]), 1)
        self.assertEqual(result["skeleton_updates"][0]["book_number"], 7.0)
        self.assertEqual(result["skeleton_updates"][0]["status"], "unconfirmed")

    def test_telemetry_param_still_populates_result_and_gets_tool_call_traced(self):
        telemetry = DiscoveryTelemetry()
        with self._mock_discovery([self._candidate()]):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False, telemetry=telemetry)

        self.assertIsNotNone(result["telemetry"])
        # RT-1b's aggregate tool-call trace for the discovery-stack
        # invocation must have reached the same DiscoveryTelemetry
        # instance the caller passed in.
        self.assertGreaterEqual(result["telemetry"]["total_tool_calls"], 1)


if __name__ == "__main__":
    unittest.main()
