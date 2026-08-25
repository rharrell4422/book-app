"""Phase 10, finalization & hardening layer -- `discovery_agentic_
phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`'s settled
architecture (not re-litigated here). Adds per-series readiness
reports, a global invariant-enforcement check, and consolidates six
existing agentic modules (plus two new Phase 10 ones) into a dedicated
`agentic/` package. No routing behavior changes, no new agentic
capabilities.

Per the Phase 10 spec, this file needs to prove:

1. `agentic.readiness.compute_agentic_readiness` returns every
   documented field, and its `ready` flag reflects a genuinely healthy
   vs. genuinely violated snapshot.
2. `/admin/agentic/readiness/{series_id}` is owner-only.
3. `agentic.invariants.enforce_agentic_invariants` passes against this
   repo's real code, and is fail-soft when a check misbehaves.
4. `/admin/agentic/startup-check` is owner-only and reflects the same
   invariant check.
5. Every module the Phase 10 spec named actually moved -- the new
   `agentic.*` import paths work, and the old `services.agentic_*`
   paths for those same six modules no longer exist.
6. None of this phase's additions (the move itself, the two new
   modules, or exercising them) changes `run_series_check`'s actual
   routing behavior or mutates any series' stored state.
"""

import importlib
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import settings
from agentic.invariants import enforce_agentic_invariants
from agentic.promotion_evaluator import store_promotion_decision
from agentic.readiness import compute_agentic_readiness
from database import Base
from models import Series, SeriesSkeleton
from routers.deps import create_owner_token


class _InMemoryDbTestCase(unittest.TestCase):
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


def _clean_metrics(**overrides) -> dict:
    base = {
        "agentic_promotion_attempts": 0,
        "agentic_promotion_use_agentic": 0,
        "agentic_promotion_use_live": 0,
        "agentic_promotion_rejected": 0,
        "agentic_safety_violations": 0,
        "agentic_cache_hits": 0,
        "agentic_cache_misses": 0,
        "agentic_turn_invocations": 0,
        "agentic_turn_failures": 0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1-2. Readiness report
# ---------------------------------------------------------------------------


class ReadinessReportTest(_InMemoryDbTestCase):
    def test_readiness_report_fields(self):
        readiness = compute_agentic_readiness(self.series.id, db_session=self.db)

        expected_keys = {
            "promotion_history_ok",
            "safety_violations_recent",
            "determinism_ok",
            "activation_state",
            "metrics_ok",
            "cache_ok",
            "ready",
        }
        self.assertEqual(set(readiness.keys()), expected_keys)
        self.assertIsInstance(readiness["promotion_history_ok"], bool)
        self.assertIsInstance(readiness["safety_violations_recent"], int)
        self.assertIsInstance(readiness["determinism_ok"], bool)
        self.assertIsInstance(readiness["activation_state"], bool)
        self.assertIsInstance(readiness["metrics_ok"], bool)
        self.assertIsInstance(readiness["cache_ok"], bool)
        self.assertIsInstance(readiness["ready"], bool)

    def test_readiness_report_ready_true(self):
        # A genuinely healthy snapshot: activated, no stored violations,
        # well-formed metrics -- every field should be True/0 and
        # ready=True. Metrics are mocked to a known-clean value since
        # `agentic_safety_violations` is a process-wide, lifetime
        # counter this test's own process may have already bumped via
        # other test modules (see agentic/readiness.py's own docstring).
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", str(self.series.id)
        ), patch("services.discovery_telemetry.get_agentic_metrics", return_value=_clean_metrics()):
            readiness = compute_agentic_readiness(self.series.id, db_session=self.db)

        self.assertTrue(readiness["promotion_history_ok"])
        self.assertEqual(readiness["safety_violations_recent"], 0)
        self.assertTrue(readiness["determinism_ok"])
        self.assertTrue(readiness["activation_state"])
        self.assertTrue(readiness["metrics_ok"])
        self.assertTrue(readiness["cache_ok"])
        self.assertTrue(readiness["ready"])

    def test_readiness_report_ready_false_on_violation(self):
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", str(self.series.id)
        ), patch(
            "services.discovery_telemetry.get_agentic_metrics",
            return_value=_clean_metrics(agentic_safety_violations=3),
        ):
            readiness = compute_agentic_readiness(self.series.id, db_session=self.db)

        self.assertEqual(readiness["safety_violations_recent"], 3)
        self.assertFalse(readiness["ready"])
        # Every other field is still independently reported correctly --
        # only `ready` itself flips because of the violation count.
        self.assertTrue(readiness["activation_state"])
        self.assertTrue(readiness["metrics_ok"])

    def test_readiness_report_ready_false_when_not_activated(self):
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", ""
        ), patch("services.discovery_telemetry.get_agentic_metrics", return_value=_clean_metrics()):
            readiness = compute_agentic_readiness(self.series.id, db_session=self.db)

        self.assertFalse(readiness["activation_state"])
        self.assertFalse(readiness["ready"])

    def test_readiness_fails_soft_on_broken_db_session(self):
        broken_db = MagicMock()
        broken_db.query.side_effect = RuntimeError("db exploded")
        readiness = compute_agentic_readiness(self.series.id, db_session=broken_db)
        # get_latest_promotion_decisions is itself fail-soft, so this
        # alone does not force every field False -- but the report must
        # still come back well-shaped rather than raising.
        self.assertIn("ready", readiness)
        self.assertIsInstance(readiness["ready"], bool)


class ReadinessEndpointTest(_InMemoryDbTestCase):
    def test_readiness_endpoint_owner_only(self):
        client = TestClient(main.app)
        series_id = -999999994

        anon = client.get(f"/admin/agentic/readiness/{series_id}")
        self.assertEqual(anon.status_code, 403)

        owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}
        owner = client.get(f"/admin/agentic/readiness/{series_id}", headers=owner_headers)
        self.assertEqual(owner.status_code, 200)
        body = owner.json()
        self.assertEqual(body["series_id"], series_id)
        self.assertIn("readiness", body)
        self.assertIn("ready", body["readiness"])


# ---------------------------------------------------------------------------
# 3-4. Invariants / startup check
# ---------------------------------------------------------------------------


class InvariantsTest(unittest.TestCase):
    def test_startup_check_invariants_ok(self):
        self.assertTrue(enforce_agentic_invariants())

    def test_startup_check_invariants_fail_soft(self):
        # A broken individual check must not raise back into the
        # caller -- it should just count as one failed invariant.
        with patch("agentic.invariants._check_modules_import_cleanly", side_effect=RuntimeError("boom")):
            self.assertFalse(enforce_agentic_invariants())

        with patch("agentic.invariants._check_metrics_initialized", side_effect=RuntimeError("boom")):
            self.assertFalse(enforce_agentic_invariants())

        with patch("agentic.invariants._check_safety_validator_callable", return_value=False):
            self.assertFalse(enforce_agentic_invariants())

        with patch("agentic.invariants._check_resolution_sorted_keys", side_effect=RuntimeError("boom")):
            self.assertFalse(enforce_agentic_invariants())

        with patch("agentic.invariants._check_promotion_evaluator_valid_outcome", return_value=False):
            self.assertFalse(enforce_agentic_invariants())

    def test_individual_invariant_checks_pass_independently(self):
        from agentic.invariants import (
            _check_metrics_initialized,
            _check_modules_import_cleanly,
            _check_promotion_evaluator_valid_outcome,
            _check_resolution_sorted_keys,
            _check_safety_validator_callable,
        )

        self.assertTrue(_check_modules_import_cleanly())
        self.assertTrue(_check_metrics_initialized())
        self.assertTrue(_check_safety_validator_callable())
        self.assertTrue(_check_resolution_sorted_keys())
        self.assertTrue(_check_promotion_evaluator_valid_outcome())


class StartupCheckEndpointTest(unittest.TestCase):
    def test_startup_check_endpoint_owner_only(self):
        client = TestClient(main.app)

        anon = client.get("/admin/agentic/startup-check")
        self.assertEqual(anon.status_code, 403)

        owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}
        owner = client.get("/admin/agentic/startup-check", headers=owner_headers)
        self.assertEqual(owner.status_code, 200)
        self.assertEqual(owner.json(), {"invariants_ok": True})


# ---------------------------------------------------------------------------
# 5. Namespace consolidation
# ---------------------------------------------------------------------------


class NamespaceConsolidationTest(unittest.TestCase):
    NEW_MODULES = (
        "agentic.cache",
        "agentic.confidence_gate_store",
        "agentic.promotion_evaluator",
        "agentic.resolution",
        "agentic.safety",
        "agentic.health",
        "agentic.readiness",
        "agentic.invariants",
    )

    OLD_MODULES = (
        "services.agentic_cache",
        "services.agentic_confidence_gate_store",
        "services.agentic_promotion_evaluator",
        "services.agentic_resolution",
        "services.agentic_safety",
        "services.agentic_health",
    )

    def test_imports_after_namespace_consolidation(self):
        for module_name in self.NEW_MODULES:
            module = importlib.import_module(module_name)
            self.assertIsNotNone(module)

        for module_name in self.OLD_MODULES:
            with self.assertRaises(ModuleNotFoundError):
                importlib.import_module(module_name)

    def test_moved_modules_expose_expected_public_functions(self):
        from agentic.cache import AgenticTurnCache
        from agentic.confidence_gate_store import get_agentic_confidence_history, store_agentic_confidence
        from agentic.health import compute_agentic_health
        from agentic.promotion_evaluator import evaluate_promotion, store_promotion_decision
        from agentic.readiness import compute_agentic_readiness as readiness_fn
        from agentic.resolution import resolve_routing_decisions
        from agentic.safety import validate_agentic_decision, validate_promotion_outcome

        for obj in (
            AgenticTurnCache,
            get_agentic_confidence_history,
            store_agentic_confidence,
            compute_agentic_health,
            evaluate_promotion,
            store_promotion_decision,
            readiness_fn,
            resolve_routing_decisions,
            validate_agentic_decision,
            validate_promotion_outcome,
        ):
            self.assertTrue(callable(obj))

    def test_router_and_agent_modules_import_cleanly_after_move(self):
        # Reload rather than a plain import_module -- these modules were
        # already imported earlier in the test run (before this test's
        # own assertions matter here is just "does this still work"),
        # and a reload re-executes every module-level import statement,
        # which is exactly what would fail if any import path referencing
        # a moved module were left stale.
        import agents.series_agent
        import routers.admin_agentic

        importlib.reload(routers.admin_agentic)
        importlib.reload(agents.series_agent)


# ---------------------------------------------------------------------------
# 6. No routing behavior change
# ---------------------------------------------------------------------------


class NoRoutingBehaviorChangeTest(_InMemoryDbTestCase):
    def test_readiness_and_invariants_do_not_mutate_series_state(self):
        skeleton = SeriesSkeleton(
            series_id=self.series.id,
            skeleton_json=[{"number": 1, "status": "confirmed", "confidence": "high", "source_class": "library"}],
            schema_version=2,
        )
        self.db.add(skeleton)
        self.db.commit()

        store_promotion_decision(
            self.series.id,
            1.0,
            {"confidence": "high"},
            {"overall": "high"},
            {"belongs_to_series": True},
            {"belongs_to_series": True},
            "use_live",
            db_session=self.db,
        )

        before_skeleton = list(
            self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).one().skeleton_json
        )

        compute_agentic_readiness(self.series.id, db_session=self.db)
        enforce_agentic_invariants()

        after_row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).one()
        self.assertEqual(list(after_row.skeleton_json), before_skeleton)

    def test_evaluate_promotion_outcomes_unchanged_by_move(self):
        # Same fixed inputs/expected outcomes this codebase's own Phase
        # 3/7 test suites already assert against `evaluate_promotion` --
        # re-asserted here post-move as a smoke check that moving the
        # module changed nothing about its actual decision logic.
        from agentic.promotion_evaluator import evaluate_promotion

        self.assertEqual(
            evaluate_promotion(
                {"confidence": "medium"}, {"overall": "high"}, {"belongs_to_series": True}, {"belongs_to_series": True}
            ),
            "use_agentic",
        )
        self.assertEqual(
            evaluate_promotion(
                {"confidence": "high"}, {"overall": "low"}, {"belongs_to_series": True}, {"belongs_to_series": True}
            ),
            "reject_agentic",
        )
        self.assertEqual(
            evaluate_promotion(
                {"confidence": "medium"},
                {"overall": "medium"},
                {"belongs_to_series": True},
                {"belongs_to_series": True},
            ),
            "use_live",
        )


if __name__ == "__main__":
    unittest.main()
