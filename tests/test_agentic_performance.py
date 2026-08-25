"""Phase 8, agentic performance & efficiency layer -- `discovery_agentic_
phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`'s settled
architecture (not re-litigated here). Pure performance: reduces
redundant agentic work and DB round-trips without changing any decision
this system already makes.

Per the Phase 8 spec, this file needs to prove:

1. `services/agentic_cache.AgenticTurnCache` memoizes per-`book_number`,
   and `services/agentic_promotion_evaluator.evaluate_promotion` computes
   its decision at most once per `book_number` when a shared `cache` is
   passed across repeated calls.
2. `services/agentic_resolution.resolve_routing_decisions` never re-reads
   `promotion_decisions[book_number]` more than once per book across
   repeated calls sharing one `cache`.
3. `services/agentic_promotion_evaluator.build_activation_preview` never
   reconstructs a book's preview entry more than once across repeated
   calls sharing one `cache`.
4. `services/agentic_confidence_gate_store.get_latest_confidence_
   decisions`/`get_latest_gate_decisions` and `services/agentic_
   promotion_evaluator.get_latest_promotion_decisions` are each a single
   bulk query per `series_id` (never one query per book), and skip the
   query entirely when a caller passes an already-fetched `history`.
5. None of the above ever calls a live provider.
6. `agents/series_agent.py`'s `_run_agentic_turn_guarded` invokes
   `run_agentic_turn` at most once per shared turn/state, end-to-end
   through `run_series_check`, regardless of how many of its own call
   sites ask for a trace.
7. A broken/misbehaving `cache` fails soft (falls back to uncached
   computation) rather than raising, in every integration point above.
8. None of this changes any resolved routing value/outcome versus the
   exact same inputs run without a cache.
"""

import unittest
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import settings
import services.agentic_promotion_evaluator as agentic_promotion_evaluator_module
from agents.series_agent import SeriesIntelligenceAgent, _run_agentic_turn_guarded
from database import Base
from models import Book, Series
from services.agentic_cache import AgenticTurnCache
from services.agentic_confidence_gate_store import (
    get_agentic_confidence_history,
    get_agentic_gate_history,
    get_latest_confidence_decisions,
    get_latest_gate_decisions,
    store_agentic_confidence,
    store_agentic_gate,
)
from services.agentic_promotion_evaluator import (
    build_activation_preview,
    evaluate_promotion,
    get_latest_promotion_decisions,
    get_promotion_history,
    store_promotion_decision,
)
from services.agentic_resolution import resolve_routing_decisions


class _InMemoryDbTestCase(unittest.TestCase):
    """Shared in-memory-SQLite fixture, matching every other Phase 1-7
    agentic test file's convention (see e.g. tests/test_agentic_
    determinism.py).
    """

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()


# ---------------------------------------------------------------------------
# 1. AgenticTurnCache + evaluate_promotion memoization
# ---------------------------------------------------------------------------


class AgenticTurnCacheTest(unittest.TestCase):
    """Pure unit tests on the cache primitive itself -- no DB, no agentic
    modules involved at all.
    """

    def test_promotion_namespace_memoizes_per_book_number(self):
        cache = AgenticTurnCache()
        calls = []

        def compute():
            calls.append(1)
            return "use_agentic"

        first = cache.get_or_set_promotion(1.0, compute)
        second = cache.get_or_set_promotion(1.0, compute)
        self.assertEqual(first, "use_agentic")
        self.assertEqual(second, "use_agentic")
        self.assertEqual(len(calls), 1)

    def test_confidence_and_gate_namespaces_are_independent(self):
        cache = AgenticTurnCache()
        cache.get_or_set_confidence(1.0, lambda: {"overall": "high"})
        cache.get_or_set_gate(1.0, lambda: {"belongs_to_series": True})
        self.assertEqual(cache.confidence, {1.0: {"overall": "high"}})
        self.assertEqual(cache.gate, {1.0: {"belongs_to_series": True}})
        self.assertEqual(cache.promotion, {})

    def test_different_book_numbers_are_cached_independently(self):
        cache = AgenticTurnCache()
        cache.get_or_set_promotion(1.0, lambda: "use_live")
        cache.get_or_set_promotion(2.0, lambda: "use_agentic")
        self.assertEqual(cache.promotion, {1.0: "use_live", 2.0: "use_agentic"})


class PromotionCachePreventsRecomputeTest(unittest.TestCase):
    """`evaluate_promotion(..., book_number=X, cache=shared_cache)` must
    compute the decision at most once per book_number for that cache's
    lifetime.
    """

    def test_promotion_cache_prevents_recompute(self):
        cache = AgenticTurnCache()
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        with patch(
            "services.agentic_promotion_evaluator._evaluate_once",
            wraps=agentic_promotion_evaluator_module._evaluate_once,
        ) as spy_evaluate_once:
            first = evaluate_promotion(
                live_conf, agentic_conf, live_gate, agentic_gate, book_number=1.0, cache=cache
            )
            second = evaluate_promotion(
                live_conf, agentic_conf, live_gate, agentic_gate, book_number=1.0, cache=cache
            )
            third = evaluate_promotion(
                # Different book_number -- must still be computed (cache
                # is per-book_number, not global).
                live_conf,
                agentic_conf,
                live_gate,
                agentic_gate,
                book_number=2.0,
                cache=cache,
            )

        self.assertEqual(first, second)
        self.assertEqual(spy_evaluate_once.call_count, 2)  # book 1.0 once, book 2.0 once -- never book 1.0 twice
        self.assertEqual(first, third)  # same inputs -> same outcome either way

    def test_no_cache_recomputes_every_call(self):
        # Baseline/contrast: omitting `cache` (the default) reproduces
        # pre-Phase-8 behavior exactly -- every call recomputes.
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}
        with patch(
            "services.agentic_promotion_evaluator._evaluate_once",
            wraps=agentic_promotion_evaluator_module._evaluate_once,
        ) as spy_evaluate_once:
            evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate, book_number=1.0)
            evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate, book_number=1.0)
        self.assertEqual(spy_evaluate_once.call_count, 2)

    def test_cache_without_book_number_is_a_no_op(self):
        # book_number is required to key the cache -- omitting it (as
        # every pre-Phase-8 caller that doesn't pass one still can) must
        # never cache under a shared `None` key across unrelated books.
        cache = AgenticTurnCache()
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}
        with patch(
            "services.agentic_promotion_evaluator._evaluate_once",
            wraps=agentic_promotion_evaluator_module._evaluate_once,
        ) as spy_evaluate_once:
            evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate, cache=cache)
            evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate, cache=cache)
        self.assertEqual(spy_evaluate_once.call_count, 2)
        self.assertEqual(cache.promotion, {})


# ---------------------------------------------------------------------------
# 2. resolve_routing_decisions cache
# ---------------------------------------------------------------------------


class _CountingDecisionsDict(dict):
    """Plain dict subclass that counts `.get()` calls -- lets tests prove
    `resolve_routing_decisions` never re-reads a book's promotion
    decision more than once across repeated calls sharing one cache.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.get_calls = 0

    def get(self, *args, **kwargs):
        self.get_calls += 1
        return super().get(*args, **kwargs)


class ResolutionCachePreventsRecomputeTest(unittest.TestCase):
    def test_resolution_cache_prevents_recompute(self):
        live_confidence = {1.0: {"confidence": "medium"}}
        live_gate = {1.0: {"belongs_to_series": True}}
        promotion_decisions = _CountingDecisionsDict(
            {1.0: {"outcome": "use_live", "live_confidence": {"confidence": "medium"}, "live_gate": {"belongs_to_series": True}}}
        )
        cache = AgenticTurnCache()

        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            resolve_routing_decisions(1, live_confidence, live_gate, promotion_decisions, cache=cache)
            resolve_routing_decisions(1, live_confidence, live_gate, promotion_decisions, cache=cache)

        # Only the first call actually reads promotion_decisions[1.0];
        # the second call is served entirely from `cache`.
        self.assertEqual(promotion_decisions.get_calls, 1)

    def test_no_cache_reads_promotion_decisions_every_call(self):
        live_confidence = {1.0: {"confidence": "medium"}}
        live_gate = {1.0: {"belongs_to_series": True}}
        promotion_decisions = _CountingDecisionsDict({1.0: {"outcome": "use_live"}})

        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            resolve_routing_decisions(1, live_confidence, live_gate, promotion_decisions)
            resolve_routing_decisions(1, live_confidence, live_gate, promotion_decisions)

        self.assertEqual(promotion_decisions.get_calls, 2)

    def test_resolution_cache_does_not_change_resolved_values(self):
        # Same inputs, with vs. without a cache, must resolve to the
        # exact same values -- Phase 8 changes *how many times* work
        # happens, never *what* gets decided.
        live_confidence = {1.0: {"confidence": "medium"}, 2.0: {"confidence": "low"}}
        live_gate = {1.0: {"belongs_to_series": True}, 2.0: {"belongs_to_series": True}}
        promotion_decisions = {
            1.0: {
                "outcome": "use_agentic",
                "agentic_confidence": {"overall": "high"},
                "agentic_gate": {"belongs_to_series": True},
            },
            2.0: {"outcome": "use_live"},
        }

        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ), patch("services.agentic_resolution.validate_agentic_decision", return_value=True):
            uncached_conf, uncached_gate = resolve_routing_decisions(
                1, live_confidence, live_gate, promotion_decisions
            )
            cached_conf, cached_gate = resolve_routing_decisions(
                1, live_confidence, live_gate, promotion_decisions, cache=AgenticTurnCache()
            )

        self.assertEqual(uncached_conf, cached_conf)
        self.assertEqual(uncached_gate, cached_gate)


# ---------------------------------------------------------------------------
# 3. build_activation_preview cache
# ---------------------------------------------------------------------------


class PreviewCachePreventsRecomputeTest(_InMemoryDbTestCase):
    def setUp(self):
        super().setUp()
        for book_number, outcome in ((1.0, "use_live"), (2.0, "use_agentic")):
            store_promotion_decision(
                self.series.id,
                book_number,
                {"confidence": "medium"},
                {"overall": "high"},
                {"belongs_to_series": True},
                {"belongs_to_series": True},
                outcome,
                db_session=self.db,
            )

    def test_preview_cache_prevents_recompute(self):
        cache = AgenticTurnCache()
        with patch(
            "services.agentic_promotion_evaluator._build_preview_entry",
            wraps=agentic_promotion_evaluator_module._build_preview_entry,
        ) as spy_build_entry:
            first = build_activation_preview(self.series.id, db_session=self.db, cache=cache)
            second = build_activation_preview(self.series.id, db_session=self.db, cache=cache)

        self.assertEqual(first, second)
        # Two distinct book_numbers -- constructed once each, never twice
        # for the same book across the two build_activation_preview calls.
        self.assertEqual(spy_build_entry.call_count, 2)

    def test_no_cache_reconstructs_every_call(self):
        with patch(
            "services.agentic_promotion_evaluator._build_preview_entry",
            wraps=agentic_promotion_evaluator_module._build_preview_entry,
        ) as spy_build_entry:
            build_activation_preview(self.series.id, db_session=self.db)
            build_activation_preview(self.series.id, db_session=self.db)
        self.assertEqual(spy_build_entry.call_count, 4)


# ---------------------------------------------------------------------------
# 4. Bulk shadow reads
# ---------------------------------------------------------------------------


class BulkShadowReadsTest(_InMemoryDbTestCase):
    def setUp(self):
        super().setUp()
        for book_number in (1.0, 2.0, 3.0):
            store_agentic_confidence(
                self.series.id, book_number, {"confidence": "medium"}, {"overall": "high"}, db_session=self.db
            )
            store_agentic_gate(
                self.series.id,
                book_number,
                {"belongs_to_series": True},
                {"belongs_to_series": True},
                db_session=self.db,
            )
            store_promotion_decision(
                self.series.id,
                book_number,
                {"confidence": "medium"},
                {"overall": "high"},
                {"belongs_to_series": True},
                {"belongs_to_series": True},
                "use_live",
                db_session=self.db,
            )

    def _spy_on_queries(self):
        """Wraps `self.db.query` so a test can assert exactly how many
        real queries a call issued.
        """
        return patch.object(self.db, "query", wraps=self.db.query)

    def test_get_latest_confidence_decisions_is_a_single_query(self):
        with self._spy_on_queries() as spy_query:
            latest = get_latest_confidence_decisions(self.series.id, db_session=self.db)
        self.assertEqual(spy_query.call_count, 1)
        self.assertEqual(set(latest.keys()), {1.0, 2.0, 3.0})

    def test_get_latest_gate_decisions_is_a_single_query(self):
        with self._spy_on_queries() as spy_query:
            latest = get_latest_gate_decisions(self.series.id, db_session=self.db)
        self.assertEqual(spy_query.call_count, 1)
        self.assertEqual(set(latest.keys()), {1.0, 2.0, 3.0})

    def test_get_latest_promotion_decisions_is_a_single_query(self):
        with self._spy_on_queries() as spy_query:
            latest = get_latest_promotion_decisions(self.series.id, db_session=self.db)
        self.assertEqual(spy_query.call_count, 1)
        self.assertEqual(set(latest.keys()), {1.0, 2.0, 3.0})

    def test_bulk_shadow_reads_via_prefetched_history_skip_the_query_entirely(self):
        confidence_history = get_agentic_confidence_history(self.series.id, db_session=self.db)
        gate_history = get_agentic_gate_history(self.series.id, db_session=self.db)
        promotion_history = get_promotion_history(self.series.id, db_session=self.db)

        broken_db = MagicMock()
        broken_db.query.side_effect = AssertionError("must not query when history is pre-fetched")

        latest_confidence = get_latest_confidence_decisions(
            self.series.id, db_session=broken_db, history=confidence_history
        )
        latest_gate = get_latest_gate_decisions(self.series.id, db_session=broken_db, history=gate_history)
        latest_promotion = get_latest_promotion_decisions(
            self.series.id, db_session=broken_db, history=promotion_history
        )

        self.assertEqual(set(latest_confidence.keys()), {1.0, 2.0, 3.0})
        self.assertEqual(set(latest_gate.keys()), {1.0, 2.0, 3.0})
        self.assertEqual(set(latest_promotion.keys()), {1.0, 2.0, 3.0})
        broken_db.query.assert_not_called()


# ---------------------------------------------------------------------------
# 5. No provider calls during cache use
# ---------------------------------------------------------------------------


class NoProviderCallsDuringCacheUseTest(unittest.TestCase):
    def test_no_provider_calls_during_cache_use(self):
        with patch(
            "discovery_engine.discover_candidates_for_series",
            side_effect=AssertionError("must never call a live provider"),
        ):
            promotion_cache = AgenticTurnCache()
            live_conf = {"confidence": "medium"}
            agentic_conf = {"overall": "medium"}
            live_gate = {"belongs_to_series": True}
            agentic_gate = {"belongs_to_series": True}
            evaluate_promotion(
                live_conf, agentic_conf, live_gate, agentic_gate, book_number=1.0, cache=promotion_cache
            )
            evaluate_promotion(
                live_conf, agentic_conf, live_gate, agentic_gate, book_number=1.0, cache=promotion_cache
            )

            resolution_cache = AgenticTurnCache()
            promotion_decisions = {1.0: {"outcome": "use_live", "live_confidence": live_conf, "live_gate": live_gate}}
            resolve_routing_decisions(1, {1.0: live_conf}, {1.0: live_gate}, promotion_decisions, cache=resolution_cache)
            resolve_routing_decisions(1, {1.0: live_conf}, {1.0: live_gate}, promotion_decisions, cache=resolution_cache)
        # Reaching this point without the patched provider raising is the
        # assertion -- nothing above ever touches it.


# ---------------------------------------------------------------------------
# 6. run_agentic_turn invoked exactly once per turn
# ---------------------------------------------------------------------------


class AgenticTurnGuardTest(unittest.TestCase):
    """Direct unit tests on `_run_agentic_turn_guarded` -- no DB, no
    run_series_check involved.
    """

    def test_agentic_turn_invoked_once_per_turn(self):
        mock_run = MagicMock(return_value={"trace": "value"})
        shared_state: dict = {}
        first = _run_agentic_turn_guarded(mock_run, 1, {"series_id": 1}, shared_state=shared_state)
        second = _run_agentic_turn_guarded(mock_run, 1, {"series_id": 1, "other": "context"}, shared_state=shared_state)
        self.assertEqual(first, {"trace": "value"})
        self.assertEqual(second, {"trace": "value"})
        mock_run.assert_called_once()

    def test_guard_without_shared_state_falls_back_to_context_itself(self):
        mock_run = MagicMock(return_value={"trace": "value"})
        context = {"series_id": 1}
        _run_agentic_turn_guarded(mock_run, 1, context)
        _run_agentic_turn_guarded(mock_run, 1, context)
        mock_run.assert_called_once()

    def test_two_independent_shared_states_each_get_their_own_call(self):
        mock_run = MagicMock(return_value={"trace": "value"})
        _run_agentic_turn_guarded(mock_run, 1, {"series_id": 1}, shared_state={})
        _run_agentic_turn_guarded(mock_run, 1, {"series_id": 1}, shared_state={})
        self.assertEqual(mock_run.call_count, 2)

    def test_guard_reraises_and_caches_an_exception_for_subsequent_calls(self):
        mock_run = MagicMock(side_effect=RuntimeError("boom"))
        shared_state: dict = {}
        with self.assertRaises(RuntimeError):
            _run_agentic_turn_guarded(mock_run, 1, {"series_id": 1}, shared_state=shared_state)
        # Second call must re-raise the same failure rather than
        # invoking run_agentic_turn a second time or silently succeeding.
        with self.assertRaises(RuntimeError):
            _run_agentic_turn_guarded(mock_run, 1, {"series_id": 1}, shared_state=shared_state)
        mock_run.assert_called_once()


class AgenticTurnInvokedOnceEndToEndTest(_InMemoryDbTestCase):
    """End-to-end proof through the real `run_series_check` promotion +
    dry-run blocks: with AGENTIC_ROUTING_ENABLED on, both blocks ask for
    an agentic trace during the same call, but the real `run_agentic_
    turn` underneath must fire only once.
    """

    def setUp(self):
        super().setUp()
        for number in [1, 2, 3]:
            self.db.add(
                Book(
                    title=f"Cherry Blossom Girls Book {number}",
                    author="Harmon Cooper",
                    series_id=self.series.id,
                    profile_id=self.series.profile_id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=False,
                )
            )
        self.db.commit()

    def _mock_discovery(self, **overrides):
        result = {
            "candidates": [],
            "provider_failures": [],
            "all_providers_failed": False,
            "used_author_fallback": False,
        }
        result.update(overrides)
        return patch("discovery_engine.discover_candidates_for_series", return_value=result)

    def test_agentic_turn_invoked_once_per_turn_end_to_end(self):
        from agents import agentic_series_agent

        real_run_agentic_turn = agentic_series_agent.run_agentic_turn
        spy = MagicMock(side_effect=real_run_agentic_turn)

        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", str(self.series.id)
        ), patch("agents.agentic_series_agent.SessionLocal", self.SessionLocal), patch(
            "agents.agentic_series_agent.run_agentic_turn", spy
        ):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertIn("found", result)
        spy.assert_called_once()

    def test_agentic_turn_invoked_once_per_turn_when_flag_off(self):
        # Flag off -- only the Phase 2 dry-run block ever asks for a
        # trace at all; still exactly one real invocation.
        from agents import agentic_series_agent

        real_run_agentic_turn = agentic_series_agent.run_agentic_turn
        spy = MagicMock(side_effect=real_run_agentic_turn)

        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", False), patch(
            "agents.agentic_series_agent.SessionLocal", self.SessionLocal
        ), patch("agents.agentic_series_agent.run_agentic_turn", spy):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False)

        spy.assert_called_once()


# ---------------------------------------------------------------------------
# 7. Fail-soft on cache errors
# ---------------------------------------------------------------------------


class _BrokenCache:
    """A cache-shaped object whose `get_or_set_*` methods always raise --
    used to prove every Phase 8 integration point falls back to
    uncached computation rather than propagating a cache bug.
    """

    def get_or_set_promotion(self, book_number, compute_fn):
        raise RuntimeError("cache is broken")

    def get_or_set_confidence(self, book_number, compute_fn):
        raise RuntimeError("cache is broken")

    def get_or_set_gate(self, book_number, compute_fn):
        raise RuntimeError("cache is broken")


class FailSoftOnCacheErrorsTest(_InMemoryDbTestCase):
    def test_evaluate_promotion_fails_soft_on_broken_cache(self):
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}
        outcome = evaluate_promotion(
            live_conf, agentic_conf, live_gate, agentic_gate, book_number=1.0, cache=_BrokenCache()
        )
        self.assertIn(outcome, ("use_live", "use_agentic", "reject_agentic"))

    def test_resolve_routing_decisions_fails_soft_on_broken_cache(self):
        live_confidence = {1.0: {"confidence": "medium"}}
        live_gate = {1.0: {"belongs_to_series": True}}
        promotion_decisions = {1.0: {"outcome": "use_live"}}
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            resolved_conf, resolved_gate = resolve_routing_decisions(
                1, live_confidence, live_gate, promotion_decisions, cache=_BrokenCache()
            )
        self.assertEqual(resolved_conf, live_confidence)
        self.assertEqual(resolved_gate, live_gate)

    def test_build_activation_preview_fails_soft_on_broken_cache(self):
        store_promotion_decision(
            self.series.id,
            1.0,
            {"confidence": "medium"},
            {"overall": "high"},
            {"belongs_to_series": True},
            {"belongs_to_series": True},
            "use_live",
            db_session=self.db,
        )
        preview = build_activation_preview(self.series.id, db_session=self.db, cache=_BrokenCache())
        self.assertIn("1.0", preview["preview"])
        self.assertEqual(preview["preview"]["1.0"]["outcome"], "use_live")


# ---------------------------------------------------------------------------
# 8. No routing behavior change
# ---------------------------------------------------------------------------


class NoRoutingBehaviorChangeTest(_InMemoryDbTestCase):
    """Same fixture/assertions style as tests/test_agentic_promotion.py --
    proves the Phase 8 changes (cache plumbing, bulk-read plumbing, the
    run_agentic_turn guard) don't alter what run_series_check returns.
    """

    def setUp(self):
        super().setUp()
        for number in [1, 2, 3]:
            self.db.add(
                Book(
                    title=f"Cherry Blossom Girls Book {number}",
                    author="Harmon Cooper",
                    series_id=self.series.id,
                    profile_id=self.series.profile_id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=False,
                )
            )
        self.db.commit()

    def _mock_discovery(self, **overrides):
        result = {
            "candidates": [],
            "provider_failures": [],
            "all_providers_failed": False,
            "used_author_fallback": False,
        }
        result.update(overrides)
        return patch("discovery_engine.discover_candidates_for_series", return_value=result)

    def test_no_routing_behavior_change_flag_off(self):
        with self._mock_discovery():
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)
        self.assertEqual(result["agentic_promotion"], {"enabled": False, "activated": False, "promotions": []})

    def test_no_routing_behavior_change_flag_on_not_activated(self):
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch(
            "agents.agentic_series_agent.SessionLocal", self.SessionLocal
        ), patch("services.agentic_promotion_evaluator.evaluate_promotion", return_value="use_agentic"):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        payload = result["agentic_promotion"]
        self.assertTrue(payload["enabled"])
        self.assertFalse(payload["activated"])
        # "record, don't apply": resolved_confidence stays the *live*
        # shape (has a "confidence" key) even though evaluate_promotion
        # was forced to "use_agentic" -- not activated, so it's recorded
        # but never applied.
        for promotion in payload["promotions"]:
            self.assertEqual(promotion["outcome"], "use_agentic")
            self.assertIn("confidence", promotion["resolved_confidence"])

    def test_no_routing_behavior_change_flag_on_activated(self):
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", str(self.series.id)
        ), patch("agents.agentic_series_agent.SessionLocal", self.SessionLocal), patch(
            "services.agentic_promotion_evaluator.evaluate_promotion", return_value="use_agentic"
        ), patch("services.agentic_resolution.validate_agentic_decision", return_value=True):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        payload = result["agentic_promotion"]
        self.assertTrue(payload["enabled"])
        self.assertTrue(payload["activated"])
        self.assertGreater(len(payload["promotions"]), 0)
        for promotion in payload["promotions"]:
            self.assertEqual(promotion["outcome"], "use_agentic")
            self.assertNotEqual(promotion["resolved_confidence"], {})

    def test_no_skeleton_or_probes_mutation(self):
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", str(self.series.id)
        ), patch("agents.agentic_series_agent.SessionLocal", self.SessionLocal):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)
        self.assertEqual(result["probes"], [])


if __name__ == "__main__":
    unittest.main()
