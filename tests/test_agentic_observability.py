"""Phase 9, agentic observability & telemetry layer -- `discovery_
agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`'s
settled architecture (not re-litigated here). Adds metrics/counters,
per-series health, and three read-only admin endpoints on top of
Phases 1-8; no routing behavior changes.

Per the Phase 9 spec, this file needs to prove:

1. `services/discovery_telemetry.get_agentic_metrics` exposes the nine
   named counters, and each of the four integration points
   (`evaluate_promotion`, `record_agentic_safety_violation`, `resolve_
   routing_decisions`' cache usage, `_run_agentic_turn_guarded`)
   increments the right one(s).
2. `agentic/health.compute_agentic_health` returns every
   documented field, reflects a series' real activation state, and its
   `determinism_ok` flag flips to `False` on malformed stored history.
3. `/admin/agentic/metrics`, `/admin/agentic/health/{series_id}`, and
   `/admin/agentic/summary` are owner-only and return the expected
   shape.
4. Every metrics-recording call site is fail-soft -- a broken counter/
   lock never raises back into its caller.

Tests read counters via before/after deltas (`_agentic_metrics` is a
process-wide, in-memory dict shared across this whole test run, exactly
per its own module docstring -- so a test must never assert an absolute
count, only how much *this test's own* code changed it by).
"""

import threading
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import services.discovery_telemetry as discovery_telemetry
import settings
from agents.series_agent import _run_agentic_turn_guarded
from database import Base
from models import Series
from routers.deps import create_owner_token
from agentic.cache import AgenticTurnCache
from agentic.health import compute_agentic_health
from agentic.promotion_evaluator import evaluate_promotion, store_promotion_decision
from agentic.resolution import resolve_routing_decisions
from services.discovery_telemetry import get_agentic_metrics, record_agentic_safety_violation


def _metric_delta(before: dict, after: dict, name: str) -> int:
    return after.get(name, 0) - before.get(name, 0)


class _InMemoryDbTestCase(unittest.TestCase):
    """Shared in-memory-SQLite fixture, matching every other Phase 1-8
    agentic test file's convention.
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
# 1. get_agentic_metrics shape
# ---------------------------------------------------------------------------


class AgenticMetricsShapeTest(unittest.TestCase):
    def test_get_agentic_metrics_exposes_every_named_counter(self):
        metrics = get_agentic_metrics()
        expected_keys = {
            "agentic_promotion_attempts",
            "agentic_promotion_use_agentic",
            "agentic_promotion_use_live",
            "agentic_promotion_rejected",
            "agentic_safety_violations",
            "agentic_cache_hits",
            "agentic_cache_misses",
            "agentic_turn_invocations",
            "agentic_turn_failures",
        }
        self.assertEqual(set(metrics.keys()), expected_keys)
        for value in metrics.values():
            self.assertIsInstance(value, int)

    def test_get_agentic_metrics_is_sorted_and_a_stable_snapshot(self):
        metrics = get_agentic_metrics()
        self.assertEqual(list(metrics.keys()), sorted(metrics.keys()))
        # Mutating the returned dict must never affect the live counters.
        metrics["agentic_promotion_attempts"] = 999999
        self.assertNotEqual(get_agentic_metrics()["agentic_promotion_attempts"], 999999)


# ---------------------------------------------------------------------------
# 2. evaluate_promotion increments promotion metrics
# ---------------------------------------------------------------------------


class PromotionMetricsTest(unittest.TestCase):
    def test_metrics_increment_on_promotion(self):
        # A genuinely-improving agentic side -> "use_agentic".
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        before = get_agentic_metrics()
        outcome = evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate, book_number=1.0)
        after = get_agentic_metrics()

        self.assertEqual(outcome, "use_agentic")
        self.assertEqual(_metric_delta(before, after, "agentic_promotion_attempts"), 1)
        self.assertEqual(_metric_delta(before, after, "agentic_promotion_use_agentic"), 1)
        self.assertEqual(_metric_delta(before, after, "agentic_promotion_use_live"), 0)
        self.assertEqual(_metric_delta(before, after, "agentic_promotion_rejected"), 0)

    def test_metrics_increment_on_use_live(self):
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "medium"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        before = get_agentic_metrics()
        outcome = evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate, book_number=1.0)
        after = get_agentic_metrics()

        self.assertEqual(outcome, "use_live")
        self.assertEqual(_metric_delta(before, after, "agentic_promotion_attempts"), 1)
        self.assertEqual(_metric_delta(before, after, "agentic_promotion_use_live"), 1)

    def test_metrics_increment_on_reject(self):
        # Agentic ranks lower than live on a shared dimension -> reject.
        live_conf = {"confidence": "high"}
        agentic_conf = {"overall": "low"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        before = get_agentic_metrics()
        outcome = evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate, book_number=1.0)
        after = get_agentic_metrics()

        self.assertEqual(outcome, "reject_agentic")
        self.assertEqual(_metric_delta(before, after, "agentic_promotion_attempts"), 1)
        self.assertEqual(_metric_delta(before, after, "agentic_promotion_rejected"), 1)

    def test_cache_hit_does_not_double_count_promotion_attempts(self):
        # Phase 8's cache means a second evaluate_promotion call for the
        # same book_number+cache never re-enters _evaluate_once -- so it
        # must not bump agentic_promotion_attempts a second time either.
        cache = AgenticTurnCache()
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "medium"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        before = get_agentic_metrics()
        evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate, book_number=1.0, cache=cache)
        evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate, book_number=1.0, cache=cache)
        after = get_agentic_metrics()

        self.assertEqual(_metric_delta(before, after, "agentic_promotion_attempts"), 1)


# ---------------------------------------------------------------------------
# 3. Safety violations
# ---------------------------------------------------------------------------


class SafetyViolationMetricsTest(unittest.TestCase):
    def test_metrics_increment_on_safety_violation(self):
        before = get_agentic_metrics()
        record_agentic_safety_violation(1, 7.0, "test violation")
        after = get_agentic_metrics()
        self.assertEqual(_metric_delta(before, after, "agentic_safety_violations"), 1)

    def test_metrics_increment_on_safety_violation_via_evaluate_promotion_veto(self):
        # A "use_agentic" candidate per evaluate_promotion's own rules,
        # but validate_agentic_decision vetoes it -> reject_agentic, and
        # exactly one safety-violation log/metric.
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        before = get_agentic_metrics()
        with patch("agentic.promotion_evaluator.validate_agentic_decision", return_value=False):
            outcome = evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate, book_number=1.0)
        after = get_agentic_metrics()

        self.assertEqual(outcome, "reject_agentic")
        self.assertEqual(_metric_delta(before, after, "agentic_safety_violations"), 1)
        self.assertEqual(_metric_delta(before, after, "agentic_promotion_rejected"), 1)


# ---------------------------------------------------------------------------
# 4. Cache hit/miss metrics (resolve_routing_decisions)
# ---------------------------------------------------------------------------


class CacheHitMissMetricsTest(unittest.TestCase):
    def test_metrics_increment_on_cache_hit_miss(self):
        live_confidence = {1.0: {"confidence": "medium"}}
        live_gate = {1.0: {"belongs_to_series": True}}
        promotion_decisions = {1.0: {"outcome": "use_live"}}
        cache = AgenticTurnCache()

        before = get_agentic_metrics()
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            resolve_routing_decisions(1, live_confidence, live_gate, promotion_decisions, cache=cache)
            resolve_routing_decisions(1, live_confidence, live_gate, promotion_decisions, cache=cache)
        after = get_agentic_metrics()

        self.assertEqual(_metric_delta(before, after, "agentic_cache_misses"), 1)
        self.assertEqual(_metric_delta(before, after, "agentic_cache_hits"), 1)

    def test_no_cache_metrics_when_no_cache_supplied(self):
        live_confidence = {1.0: {"confidence": "medium"}}
        live_gate = {1.0: {"belongs_to_series": True}}
        promotion_decisions = {1.0: {"outcome": "use_live"}}

        before = get_agentic_metrics()
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            resolve_routing_decisions(1, live_confidence, live_gate, promotion_decisions)
        after = get_agentic_metrics()

        self.assertEqual(_metric_delta(before, after, "agentic_cache_misses"), 0)
        self.assertEqual(_metric_delta(before, after, "agentic_cache_hits"), 0)


# ---------------------------------------------------------------------------
# 5. run_agentic_turn invocation metrics
# ---------------------------------------------------------------------------


class AgenticTurnMetricsTest(unittest.TestCase):
    def test_metrics_increment_on_agentic_turn_invocation(self):
        mock_run = MagicMock(return_value={"trace": "value"})
        shared_state: dict = {}

        before = get_agentic_metrics()
        _run_agentic_turn_guarded(mock_run, 1, {"series_id": 1}, shared_state=shared_state)
        _run_agentic_turn_guarded(mock_run, 1, {"series_id": 1}, shared_state=shared_state)  # cache hit
        after = get_agentic_metrics()

        # Two guarded calls, one real invocation.
        mock_run.assert_called_once()
        self.assertEqual(_metric_delta(before, after, "agentic_turn_invocations"), 1)
        self.assertEqual(_metric_delta(before, after, "agentic_turn_failures"), 0)

    def test_metrics_increment_on_agentic_turn_failure(self):
        mock_run = MagicMock(side_effect=RuntimeError("boom"))
        shared_state: dict = {}

        before = get_agentic_metrics()
        with self.assertRaises(RuntimeError):
            _run_agentic_turn_guarded(mock_run, 1, {"series_id": 1}, shared_state=shared_state)
        after = get_agentic_metrics()

        self.assertEqual(_metric_delta(before, after, "agentic_turn_invocations"), 1)
        self.assertEqual(_metric_delta(before, after, "agentic_turn_failures"), 1)


# ---------------------------------------------------------------------------
# 6. compute_agentic_health
# ---------------------------------------------------------------------------


class AgenticHealthTest(_InMemoryDbTestCase):
    def _store(self, book_number, outcome):
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

    def test_health_summary_fields(self):
        self._store(1.0, "use_agentic")
        self._store(2.0, "use_live")
        self._store(3.0, "reject_agentic")

        health = compute_agentic_health(self.series.id, db_session=self.db)

        expected_keys = {
            "total_promotions",
            "use_agentic_count",
            "use_live_count",
            "rejected_count",
            "safety_violations",
            "last_promotion_timestamp",
            "activation_state",
            "determinism_ok",
        }
        self.assertEqual(set(health.keys()), expected_keys)
        self.assertEqual(health["total_promotions"], 3)
        self.assertEqual(health["use_agentic_count"], 1)
        self.assertEqual(health["use_live_count"], 1)
        self.assertEqual(health["rejected_count"], 1)
        self.assertIsInstance(health["safety_violations"], int)
        self.assertIsInstance(health["last_promotion_timestamp"], str)
        self.assertTrue(health["determinism_ok"])

    def test_health_summary_with_no_promotions(self):
        health = compute_agentic_health(self.series.id, db_session=self.db)
        self.assertEqual(health["total_promotions"], 0)
        self.assertIsNone(health["last_promotion_timestamp"])
        self.assertTrue(health["determinism_ok"])

    def test_health_summary_activation_state(self):
        self._store(1.0, "use_live")

        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", str(self.series.id)
        ):
            activated_health = compute_agentic_health(self.series.id, db_session=self.db)
        self.assertTrue(activated_health["activation_state"])

        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", ""
        ):
            not_activated_health = compute_agentic_health(self.series.id, db_session=self.db)
        self.assertFalse(not_activated_health["activation_state"])

    def test_health_summary_determinism_flag(self):
        self._store(1.0, "use_live")

        # Well-formed history -> determinism_ok stays True.
        self.assertTrue(compute_agentic_health(self.series.id, db_session=self.db)["determinism_ok"])

        # Malformed history (an unrecognized promotion_outcome) -> False.
        with patch(
            "agentic.promotion_evaluator.get_latest_promotion_decisions",
            return_value={1.0: {"promotion_outcome": "not-a-real-outcome", "timestamp": "2024-01-01T00:00:00+00:00"}},
        ):
            malformed_health = compute_agentic_health(self.series.id, db_session=self.db)
        self.assertFalse(malformed_health["determinism_ok"])

        # A non-dict entry is just as malformed.
        with patch(
            "agentic.promotion_evaluator.get_latest_promotion_decisions",
            return_value={1.0: "not-a-dict"},
        ):
            non_dict_health = compute_agentic_health(self.series.id, db_session=self.db)
        self.assertFalse(non_dict_health["determinism_ok"])

    def test_health_fails_soft_on_broken_db_session(self):
        # get_latest_promotion_decisions is itself fail-soft (returns {}
        # on a broken session), so this alone surfaces as an *empty*,
        # not malformed, history -- zero counts, determinism_ok True.
        broken_db = MagicMock()
        broken_db.query.side_effect = RuntimeError("db exploded")
        health = compute_agentic_health(self.series.id, db_session=broken_db)
        self.assertEqual(health["total_promotions"], 0)
        self.assertTrue(health["determinism_ok"])

    def test_health_fails_soft_when_activation_check_itself_raises(self):
        # A genuine internal failure inside compute_agentic_health's own
        # try block (not one of its fail-soft dependencies) must still
        # yield the documented fail-soft shape, not raise.
        with patch("settings.is_agentic_activated", side_effect=RuntimeError("boom")):
            health = compute_agentic_health(self.series.id, db_session=self.db)
        self.assertEqual(health["total_promotions"], 0)
        self.assertFalse(health["activation_state"])
        self.assertFalse(health["determinism_ok"])


# ---------------------------------------------------------------------------
# 7. Admin endpoints -- owner only
# ---------------------------------------------------------------------------


class AdminAgenticObservabilityEndpointsTest(_InMemoryDbTestCase):
    def setUp(self):
        super().setUp()
        from database import SessionLocal as RealSessionLocal

        self.RealSessionLocal = RealSessionLocal

    def test_metrics_endpoint_owner_only(self):
        client = TestClient(main.app)

        anon = client.get("/admin/agentic/metrics")
        self.assertEqual(anon.status_code, 403)

        owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}
        owner = client.get("/admin/agentic/metrics", headers=owner_headers)
        self.assertEqual(owner.status_code, 200)
        body = owner.json()
        self.assertIn("metrics", body)
        self.assertIn("agentic_promotion_attempts", body["metrics"])

    def test_health_endpoint_owner_only(self):
        client = TestClient(main.app)
        series_id = -999999995

        anon = client.get(f"/admin/agentic/health/{series_id}")
        self.assertEqual(anon.status_code, 403)

        owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}
        owner = client.get(f"/admin/agentic/health/{series_id}", headers=owner_headers)
        self.assertEqual(owner.status_code, 200)
        body = owner.json()
        self.assertEqual(body["series_id"], series_id)
        self.assertIn("health", body)
        self.assertIn("determinism_ok", body["health"])

    def test_summary_endpoint_owner_only(self):
        client = TestClient(main.app)

        anon = client.get("/admin/agentic/summary")
        self.assertEqual(anon.status_code, 403)

        owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}
        owner = client.get("/admin/agentic/summary", headers=owner_headers)
        self.assertEqual(owner.status_code, 200)
        body = owner.json()
        self.assertIn("activated_series", body)
        self.assertIn("total_promotions", body)
        self.assertIn("total_safety_violations", body)
        self.assertIn("agentic_turn_invocations", body)
        self.assertIsInstance(body["activated_series"], list)

    def test_summary_endpoint_reflects_activation_allowlist(self):
        client = TestClient(main.app)
        owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}

        with patch.object(settings, "AGENTIC_SERIES_ACTIVATION", "42, 7"):
            owner = client.get("/admin/agentic/summary", headers=owner_headers)
        self.assertEqual(owner.status_code, 200)
        self.assertEqual(owner.json()["activated_series"], [7, 42])


# ---------------------------------------------------------------------------
# 8. Fail-soft on metrics errors
# ---------------------------------------------------------------------------


class FailSoftOnMetricsErrorsTest(unittest.TestCase):
    def test_increment_never_raises_on_broken_lock(self):
        broken_lock = MagicMock()
        broken_lock.__enter__.side_effect = RuntimeError("lock exploded")
        with patch.object(discovery_telemetry, "_agentic_metrics_lock", broken_lock):
            discovery_telemetry._increment_agentic_metric("agentic_promotion_attempts")  # must not raise

    def test_get_agentic_metrics_never_raises_on_broken_lock(self):
        broken_lock = MagicMock()
        broken_lock.__enter__.side_effect = RuntimeError("lock exploded")
        with patch.object(discovery_telemetry, "_agentic_metrics_lock", broken_lock):
            self.assertEqual(get_agentic_metrics(), {})

    def test_record_agentic_safety_violation_never_raises_when_metric_increment_fails(self):
        with patch.object(
            discovery_telemetry, "_increment_agentic_metric", side_effect=RuntimeError("boom")
        ):
            record_agentic_safety_violation(1, 7.0, "reason")  # must not raise

    def test_evaluate_promotion_never_raises_when_metric_recording_fails(self):
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "medium"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}
        with patch(
            "services.discovery_telemetry.record_agentic_promotion_metric", side_effect=RuntimeError("boom")
        ):
            outcome = evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate, book_number=1.0)
        self.assertIn(outcome, ("use_live", "use_agentic", "reject_agentic"))

    def test_resolve_routing_decisions_never_raises_when_cache_metric_recording_fails(self):
        live_confidence = {1.0: {"confidence": "medium"}}
        live_gate = {1.0: {"belongs_to_series": True}}
        promotion_decisions = {1.0: {"outcome": "use_live"}}
        cache = AgenticTurnCache()
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ), patch(
            "services.discovery_telemetry.record_agentic_cache_miss", side_effect=RuntimeError("boom")
        ):
            resolved_conf, resolved_gate = resolve_routing_decisions(
                1, live_confidence, live_gate, promotion_decisions, cache=cache
            )
        self.assertEqual(resolved_conf, live_confidence)
        self.assertEqual(resolved_gate, live_gate)

    def test_run_agentic_turn_guarded_never_raises_when_metric_recording_fails(self):
        mock_run = MagicMock(return_value={"trace": "value"})
        with patch(
            "services.discovery_telemetry.record_agentic_turn_invocation", side_effect=RuntimeError("boom")
        ):
            result = _run_agentic_turn_guarded(mock_run, 1, {"series_id": 1}, shared_state={})
        self.assertEqual(result, {"trace": "value"})

    def test_counters_are_thread_safe_under_concurrent_increments(self):
        before = get_agentic_metrics().get("agentic_promotion_attempts", 0)

        def _bump():
            for _ in range(200):
                discovery_telemetry._increment_agentic_metric("agentic_promotion_attempts")

        threads = [threading.Thread(target=_bump) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        after = get_agentic_metrics().get("agentic_promotion_attempts", 0)
        self.assertEqual(after - before, 1600)


if __name__ == "__main__":
    unittest.main()
