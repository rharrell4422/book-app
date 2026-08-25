"""Phase 1, third implementation block: `agents/agentic_series_agent.py`'s
deterministic shadow loop.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here), this file needs to prove:

1. `run_agentic_turn` executes without throwing, for a series with a real
   skeleton and for edge cases (no skeleton, series not found).
2. Its return value has every documented trace-dict section, all populated
   with plain dicts (no freeform strings).
3. It never writes anything -- no new/changed rows in `series`, `books`, or
   `series_skeletons` after a call.
4. It coexists with RT-1b/PB-5: this is provable because it calls the exact
   same `agentic_hooks`/`confidence_engine`/`evaluate_belongs_to_series_gate`/
   `compute_skeleton_updates_merge` functions those tickets already cover
   elsewhere, not a shadow-loop-only reimplementation.
5. Deterministic replay: two calls against the same DB state produce the
   same trace (module docstring's own "same inputs -> same trace" claim).
"""

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agents.agentic_series_agent import run_agentic_turn
from database import Base
from models import Book, Series, SeriesSkeleton
from services.skeleton_store import backfill_skeleton_for_series


class AgenticSeriesAgentTest(unittest.TestCase):
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
        # Real skeleton (via the existing, unmodified skeleton_store
        # machinery) rather than a hand-built row, so this test exercises
        # the same schema/shape run_series_check would see.
        backfill_skeleton_for_series(self.db, self.series.id)

    def tearDown(self):
        self.db.close()

    def _row_counts(self):
        return {
            "series": self.db.query(Series).count(),
            "books": self.db.query(Book).count(),
            "skeletons": self.db.query(SeriesSkeleton).count(),
        }

    def _skeleton_json(self):
        row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        return list(row.skeleton_json) if row else None

    # -- 1 & 2: executes without throwing, full trace shape --------------

    def test_returns_every_documented_trace_section(self):
        trace = run_agentic_turn(self.series.id, {"db": self.db})

        self.assertEqual(trace["series_id"], self.series.id)
        self.assertIn("turn_timestamp", trace)
        for key in (
            "provider_calls",
            "probes",
            "confidence_traces",
            "gate_traces",
            "skeleton_merge_previews",
            "reasoning_steps",
        ):
            self.assertIn(key, trace)
            self.assertIsInstance(trace[key], list)

        # One provider call/probe/confidence/gate entry per skeleton
        # book_number (3 owned books here, no discovered entries yet).
        self.assertEqual(len(trace["provider_calls"]), 3)
        self.assertEqual(len(trace["probes"]), 3)
        self.assertEqual(len(trace["confidence_traces"]), 3)
        self.assertEqual(len(trace["gate_traces"]), 3)
        self.assertEqual(len(trace["skeleton_merge_previews"]), 1)
        self.assertGreaterEqual(len(trace["reasoning_steps"]), 1)

        # Every entry in every section is a plain dict -- no freeform
        # strings anywhere in the trace's list sections.
        for key in ("provider_calls", "probes", "confidence_traces", "gate_traces", "reasoning_steps"):
            for entry in trace[key]:
                self.assertIsInstance(entry, dict)

    def test_handles_series_with_no_skeleton_row_without_throwing(self):
        other = Series(name="No Skeleton Series", author="Someone", profile_id="robbie")
        self.db.add(other)
        self.db.commit()
        self.db.refresh(other)

        trace = run_agentic_turn(other.id, {"db": self.db})
        self.assertEqual(trace["series_id"], other.id)
        self.assertEqual(trace["provider_calls"], [])
        self.assertEqual(trace["confidence_traces"], [])
        self.assertEqual(len(trace["skeleton_merge_previews"]), 1)
        self.assertEqual(trace["skeleton_merge_previews"][0]["before"], [])
        self.assertEqual(trace["skeleton_merge_previews"][0]["after"], [])

    def test_handles_series_not_found_without_throwing(self):
        trace = run_agentic_turn(999999, {"db": self.db})
        self.assertEqual(trace["series_id"], 999999)
        for key in (
            "provider_calls",
            "probes",
            "confidence_traces",
            "gate_traces",
            "skeleton_merge_previews",
        ):
            self.assertEqual(trace[key], [])
        self.assertGreaterEqual(len(trace["reasoning_steps"]), 1)
        self.assertEqual(trace["reasoning_steps"][0]["reason"], "series-not-found")

    # -- 3: never writes anything -----------------------------------------

    def test_never_changes_persisted_rows_or_skeleton_json(self):
        before_counts = self._row_counts()
        before_skeleton = self._skeleton_json()

        run_agentic_turn(self.series.id, {"db": self.db})

        after_counts = self._row_counts()
        after_skeleton = self._skeleton_json()
        self.assertEqual(before_counts, after_counts)
        self.assertEqual(before_skeleton, after_skeleton)

    def test_opens_and_closes_its_own_session_when_none_supplied(self):
        # No "db" key in context at all -- run_agentic_turn must open (and
        # close) its own session rather than raising or requiring a
        # caller-supplied one. Patches the module's SessionLocal to this
        # test's in-memory engine (rather than the real configured DB)
        # purely so the assertions below can see real data; the behavior
        # under test -- opening its own session when none is supplied,
        # then closing it -- is unaffected by which engine that session
        # is bound to.
        with patch("agents.agentic_series_agent.SessionLocal", self.SessionLocal):
            trace = run_agentic_turn(self.series.id, {})
        self.assertEqual(trace["series_id"], self.series.id)
        self.assertEqual(len(trace["provider_calls"]), 3)

        row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        self.assertEqual(len(row.skeleton_json), 3)

    # -- 4: coexists with RT-1b/PB-5 (calls the real shared functions) ---

    def test_calls_the_same_agentic_hooks_rt1b_and_pb5_already_use(self):
        with patch(
            "agents.agentic_series_agent.agentic_hooks.begin_turn", wraps=__import__("agentic_hooks").begin_turn
        ) as spy_begin, patch(
            "agents.agentic_series_agent.agentic_hooks.record_tool_call",
            wraps=__import__("agentic_hooks").record_tool_call,
        ) as spy_tool_call, patch(
            "agents.agentic_series_agent.agentic_hooks.shadow_probe", wraps=__import__("agentic_hooks").shadow_probe
        ) as spy_probe, patch(
            "agents.agentic_series_agent.agentic_hooks.shadow_confidence_trace",
            wraps=__import__("agentic_hooks").shadow_confidence_trace,
        ) as spy_conf, patch(
            "agents.agentic_series_agent.agentic_hooks.shadow_gate_trace",
            wraps=__import__("agentic_hooks").shadow_gate_trace,
        ) as spy_gate, patch(
            "agents.agentic_series_agent.agentic_hooks.shadow_skeleton_merge_trace",
            wraps=__import__("agentic_hooks").shadow_skeleton_merge_trace,
        ) as spy_merge, patch(
            "agents.agentic_series_agent.agentic_hooks.record_world_model_update",
            wraps=__import__("agentic_hooks").record_world_model_update,
        ) as spy_world, patch(
            "agents.agentic_series_agent.agentic_hooks.end_turn", wraps=__import__("agentic_hooks").end_turn
        ) as spy_end:
            run_agentic_turn(self.series.id, {"db": self.db})

        spy_begin.assert_called_once()
        self.assertEqual(spy_tool_call.call_count, 3)
        self.assertEqual(spy_probe.call_count, 3)
        self.assertEqual(spy_conf.call_count, 3)
        self.assertEqual(spy_gate.call_count, 3)
        spy_merge.assert_called_once()
        spy_world.assert_called_once()
        spy_end.assert_called_once()

    def test_gate_traces_use_the_real_evaluate_belongs_to_series_gate(self):
        # No reimplementation -- patching the shared gate function must be
        # observable from inside the shadow loop.
        with patch(
            "agents.agentic_series_agent.evaluate_belongs_to_series_gate",
            return_value={
                "explicit_series_match": False,
                "partial_match": False,
                "inferred_number_int": None,
                "continues_numbering": False,
                "targeted_with_number": False,
                "is_universe_tie_in": False,
                "referenced_owned_titles": 0,
                "is_compilation_of_owned_titles": False,
                "belongs_to_series": False,
            },
        ) as spy:
            trace = run_agentic_turn(self.series.id, {"db": self.db})

        self.assertEqual(spy.call_count, 3)
        for gate_trace in trace["gate_traces"]:
            self.assertFalse(gate_trace["gate_output"]["belongs_to_series"])

    def test_skeleton_merge_preview_uses_the_real_shared_merge_function(self):
        with patch(
            "agents.agentic_series_agent.compute_skeleton_updates_merge",
            wraps=__import__("services.skeleton_store", fromlist=["compute_skeleton_updates_merge"]).compute_skeleton_updates_merge,
        ) as spy:
            run_agentic_turn(self.series.id, {"db": self.db})
        spy.assert_called_once()

    # -- 5: deterministic replay ------------------------------------------

    def test_same_inputs_produce_the_same_trace_on_replay(self):
        first = run_agentic_turn(self.series.id, {"db": self.db})
        second = run_agentic_turn(self.series.id, {"db": self.db})

        def _strip_volatile(trace):
            stripped = dict(trace)
            stripped.pop("turn_timestamp", None)
            stripped["reasoning_steps"] = [
                {k: v for k, v in step.items() if k not in ("turn_id", "recorded_at")} for step in trace["reasoning_steps"]
            ]
            return stripped

        self.assertEqual(_strip_volatile(first), _strip_volatile(second))


if __name__ == "__main__":
    unittest.main()
