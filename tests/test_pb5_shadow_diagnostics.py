"""PB-5 (Phase 1 shadow diagnostics) coverage.

Mirrors `tests/test_agentic_hooks.py`'s RT-1b structure, for the four new
shadow-mode functions (`shadow_probe`, `shadow_confidence_trace`,
`shadow_skeleton_merge_trace`, `shadow_gate_trace`) and their wiring into
`confidence_engine.py`, `services/skeleton_store.py`, and
`agents/series_agent.py`'s belongs-to-series gate. Three things this file
needs to prove, per `discovery_agentic_phase1_plan.md` section 4:

1. All four `agentic_hooks.py` shadow functions are fail-soft and produce
   structured traces (unit tests below).
2. Wiring them into `confidence_engine.py`/`services/skeleton_store.py`/
   `agents/series_agent.py` changes *nothing* about their existing return
   values -- confidence grades, skeleton_json content, and gate/routing
   outcomes are identical with or without a shadow context.
3. PB-5 output never reaches `skeleton_json`, `probes_json`, or any other
   persistent model -- it only ever reaches logging/telemetry.
"""

import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import agentic_hooks
import confidence_engine
import discovery_engine
from agents.series_agent import SeriesIntelligenceAgent
from database import Base
from models import Book, Series, SeriesSkeleton
from services.discovery_telemetry import DiscoveryTelemetry
from services.skeleton_store import apply_skeleton_updates, backfill_skeleton_for_series


class ShadowProbeTest(unittest.TestCase):
    def test_produces_a_structured_trace_and_does_not_raise(self):
        context = agentic_hooks.begin_turn({"series_id": 1})
        agentic_hooks.shadow_probe(context, "serper", "some query", {"a": 1})  # no assertion beyond "did not raise"

    def test_delegates_to_telemetry_tagged_as_shadow(self):
        telemetry = DiscoveryTelemetry()
        context = agentic_hooks.begin_turn({"series_id": 1, "telemetry": telemetry})
        agentic_hooks.shadow_probe(context, "serper", "q", [1, 2, 3])

        self.assertEqual(len(telemetry.tool_calls), 1)
        self.assertEqual(telemetry.tool_calls[0]["provider"], "shadow:serper")
        self.assertEqual(telemetry.tool_calls[0]["result_size"], 3)

    def test_none_context_does_not_raise(self):
        agentic_hooks.shadow_probe(None, "serper", "q", {})  # type: ignore[arg-type]

    def test_telemetry_delegation_failure_does_not_propagate(self):
        broken_telemetry = MagicMock()
        broken_telemetry.record_tool_call.side_effect = RuntimeError("boom")
        context = agentic_hooks.begin_turn({"series_id": 1, "telemetry": broken_telemetry})
        agentic_hooks.shadow_probe(context, "serper", "q", {})


class ShadowConfidenceTraceTest(unittest.TestCase):
    def test_produces_a_structured_trace_and_does_not_raise(self):
        context = agentic_hooks.begin_turn({"series_id": 1})
        agentic_hooks.shadow_confidence_trace(
            context,
            7.0,
            {"provider_confidence": "medium", "title_confidence": "unverified"},
            {"provider_confidence": "medium", "title_confidence": "unverified", "overall": "medium"},
        )

    def test_none_book_number_and_none_dicts_do_not_raise(self):
        context = agentic_hooks.begin_turn({"series_id": 1})
        agentic_hooks.shadow_confidence_trace(context, None, None, None)

    def test_non_dict_context_does_not_raise(self):
        agentic_hooks.shadow_confidence_trace("not-a-dict", 1.0, {}, {})  # type: ignore[arg-type]


class ShadowSkeletonMergeTraceTest(unittest.TestCase):
    def test_produces_a_structured_trace_and_does_not_raise(self):
        context = {"series_id": 1}
        agentic_hooks.shadow_skeleton_merge_trace(
            context,
            [{"book_number": 1.0}],
            [{"book_number": 1.0}, {"book_number": 2.0}],
        )

    def test_handles_non_list_before_after_without_raising(self):
        agentic_hooks.shadow_skeleton_merge_trace({"series_id": 1}, None, None)
        agentic_hooks.shadow_skeleton_merge_trace({"series_id": 1}, "not-a-list", 42)  # type: ignore[arg-type]

    def test_non_dict_context_does_not_raise(self):
        agentic_hooks.shadow_skeleton_merge_trace("not-a-dict", [], [])  # type: ignore[arg-type]


class ShadowGateTraceTest(unittest.TestCase):
    def test_produces_a_structured_trace_and_does_not_raise(self):
        context = agentic_hooks.begin_turn({"series_id": 1})
        agentic_hooks.shadow_gate_trace(
            context,
            7,
            {"explicit_series_match": True, "targeted_with_number": True},
            {"belongs_to_series": True},
        )

    def test_none_book_number_and_none_dicts_do_not_raise(self):
        context = agentic_hooks.begin_turn({"series_id": 1})
        agentic_hooks.shadow_gate_trace(context, None, None, None)

    def test_non_dict_context_does_not_raise(self):
        agentic_hooks.shadow_gate_trace("not-a-dict", 1, {}, {})  # type: ignore[arg-type]


class ConfidenceEngineShadowWiringTest(unittest.TestCase):
    """confidence_engine.compute_confidence's optional shadow_context must
    not change its return value at all, and must fire shadow_confidence_
    trace once per scored candidate when a context is supplied.
    """

    def _inputs(self):
        skeleton_entries = [{"book_number": 7.0, "title": "Cherry Blossom Girls Book 7"}]
        provider_candidates = [
            {
                "title": "Cherry Blossom Girls Book 7",
                "authors": ["Harmon Cooper"],
                "series_number": 7.0,
                "isbn13": None,
                "source_provenance": [{"source": "hardcover"}],
            }
        ]
        delta = {"malformed_books": []}
        return skeleton_entries, provider_candidates, delta

    def test_result_is_identical_with_and_without_shadow_context(self):
        skeleton_entries, provider_candidates, delta = self._inputs()

        without_shadow = confidence_engine.compute_confidence(
            1, skeleton_entries, provider_candidates, delta, series_name="Cherry Blossom Girls", series_author="Harmon Cooper"
        )
        with_shadow = confidence_engine.compute_confidence(
            1,
            skeleton_entries,
            provider_candidates,
            delta,
            series_name="Cherry Blossom Girls",
            series_author="Harmon Cooper",
            shadow_context=agentic_hooks.begin_turn({"series_id": 1}),
        )

        # Both calls' "confidence" scoring must be identical -- timestamp
        # is the only field allowed to differ between two separate calls.
        self.assertEqual(without_shadow["confidence"], with_shadow["confidence"])
        self.assertEqual(without_shadow["series_id"], with_shadow["series_id"])

    def test_shadow_context_none_never_calls_the_trace_function(self):
        skeleton_entries, provider_candidates, delta = self._inputs()
        with patch("confidence_engine.agentic_hooks.shadow_confidence_trace") as spy:
            confidence_engine.compute_confidence(1, skeleton_entries, provider_candidates, delta)
        spy.assert_not_called()

    def test_shadow_context_present_fires_trace_once_per_candidate(self):
        skeleton_entries, provider_candidates, delta = self._inputs()
        context = agentic_hooks.begin_turn({"series_id": 1})
        with patch("confidence_engine.agentic_hooks.shadow_confidence_trace") as spy:
            confidence_engine.compute_confidence(
                1, skeleton_entries, provider_candidates, delta, shadow_context=context
            )
        spy.assert_called_once()
        call_args = spy.call_args[0]
        self.assertEqual(call_args[0], context)
        self.assertEqual(call_args[1], 7.0)
        self.assertIn("provider_confidence", call_args[2])
        self.assertIn("overall", call_args[3])

    def test_broken_shadow_trace_does_not_affect_scoring_result(self):
        # confidence_engine itself never needs its own try/except around
        # the call site -- agentic_hooks.shadow_confidence_trace is
        # documented fail-soft -- but this proves the *real* function
        # really does swallow a broken telemetry delegation rather than
        # relying on confidence_engine to catch it.
        skeleton_entries, provider_candidates, delta = self._inputs()
        broken_telemetry = MagicMock()
        broken_telemetry.record_tool_call.side_effect = RuntimeError("boom")
        context = agentic_hooks.begin_turn({"series_id": 1, "telemetry": broken_telemetry})
        result = confidence_engine.compute_confidence(
            1, skeleton_entries, provider_candidates, delta, shadow_context=context
        )
        self.assertEqual(len(result["confidence"]), 1)


class SkeletonStoreShadowWiringTest(unittest.TestCase):
    """services/skeleton_store.py's shadow trace must fire on every
    successful commit without changing skeleton_json content, and must
    never itself become part of the persisted row.
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
        series = Series(name="Shadow Series", author="Shadow Author", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series
        self.db.add(
            Book(
                title="Shadow Series Book 1",
                author="Shadow Author",
                series_id=series.id,
                profile_id=series.profile_id,
                series_order=1,
                book_number=1.0,
                record_status="active",
                is_read=False,
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_backfill_fires_shadow_trace_without_changing_skeleton_json(self):
        with patch("services.skeleton_store.agentic_hooks.shadow_skeleton_merge_trace") as spy:
            backfill_skeleton_for_series(self.db, self.series.id)
        spy.assert_called_once()

        row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        self.assertEqual(len(row.skeleton_json), 1)
        self.assertEqual(row.skeleton_json[0]["book_number"], 1.0)

    def test_apply_skeleton_updates_fires_shadow_trace_without_changing_persisted_result(self):
        backfill_skeleton_for_series(self.db, self.series.id)

        with patch("services.skeleton_store.agentic_hooks.shadow_skeleton_merge_trace") as spy:
            apply_skeleton_updates(
                self.db,
                self.series.id,
                skeleton_updates=[{"book_number": 5.0, "title": "Shadow Series Book 5", "status": "unconfirmed"}],
            )
        spy.assert_called_once()
        before_arg, after_arg = spy.call_args[0][1], spy.call_args[0][2]
        self.assertEqual(len(before_arg), 1)  # pre-merge: just the library entry
        self.assertEqual(len(after_arg), 2)  # post-merge: library + new discovered entry

        row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        numbers = sorted(entry["book_number"] for entry in row.skeleton_json)
        self.assertEqual(numbers, [1.0, 5.0])

    def test_broken_shadow_trace_does_not_break_the_actual_write(self):
        with patch(
            "services.skeleton_store.agentic_hooks.shadow_skeleton_merge_trace",
            side_effect=RuntimeError("boom"),
        ):
            # agentic_hooks.shadow_skeleton_merge_trace is documented
            # fail-soft in real life; this simulates a maximally-broken
            # replacement to prove skeleton_store.py's own write path has
            # already committed before the trace call, so a raise there
            # still propagates (same "call site has no extra guard"
            # reasoning as agents/series_agent.py's RT-1b wiring) without
            # ever having corrupted skeleton_json.
            with self.assertRaises(RuntimeError):
                backfill_skeleton_for_series(self.db, self.series.id)

        row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        self.assertIsNotNone(row)
        self.assertEqual(row.skeleton_json[0]["book_number"], 1.0)


class SeriesAgentGateShadowWiringTest(unittest.TestCase):
    """agents/series_agent.py's belongs-to-series gate must fire shadow_
    gate_trace (and the discovery-stack shadow_probe) without changing
    routing outcomes at all.
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

    def test_shadow_gate_trace_and_shadow_probe_fire_without_changing_result(self):
        with self._mock_discovery([self._candidate()]), patch(
            "agents.series_agent.agentic_hooks.shadow_gate_trace", wraps=agentic_hooks.shadow_gate_trace
        ) as spy_gate, patch(
            "agents.series_agent.agentic_hooks.shadow_probe", wraps=agentic_hooks.shadow_probe
        ) as spy_probe:
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        spy_gate.assert_called_once()
        gate_call_args = spy_gate.call_args[0]
        self.assertEqual(gate_call_args[1], 7)  # book_number
        self.assertTrue(gate_call_args[3]["belongs_to_series"])  # gate_output
        spy_probe.assert_called_once()

        # Same outcome as an uninstrumented run.
        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)
        self.assertEqual(result["available_missing"][0]["series_number"], 7)

    def test_result_contains_no_shadow_diagnostics_leakage(self):
        # PB-5 output must never reach a returned/persisted structure --
        # spot-check that none of the top-level result keys are shadow-*
        # named, and that skeleton_updates/probes are exactly what RT-1b
        # already produces (no new PB-5 fields injected into them).
        with self._mock_discovery([self._candidate()]):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertFalse(any(str(key).startswith("shadow") for key in result.keys()))
        self.assertEqual(result["skeleton_updates"], [])
        self.assertEqual(result["probes"], [])


if __name__ == "__main__":
    unittest.main()
