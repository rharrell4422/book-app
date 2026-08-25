"""Phase 7, agentic safety & guardrail layer -- `services/agentic_safety.
py`'s `validate_agentic_decision`/`validate_promotion_outcome`, their
integration into `services/agentic_promotion_evaluator.evaluate_
promotion` (before it would return `"use_agentic"`) and `services/
agentic_resolution.resolve_routing_decisions` (defense-in-depth
re-check), and `services/discovery_telemetry.record_agentic_safety_
violation`.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here), this file needs to prove:

1. `validate_agentic_decision` rejects (`False`) on every named safety
   rule: confidence contradiction, gate contradiction, missing required
   fields, negative confidence values, malformed structures, impossible
   book_number jumps -- and accepts (`True`) a genuinely safe decision.
2. `validate_promotion_outcome` accepts only the three valid outcome
   literals.
3. `evaluate_promotion` downgrades an otherwise-"use_agentic" candidate
   to `"reject_agentic"` when `validate_agentic_decision` disagrees, and
   logs the rejection via `record_agentic_safety_violation` (fail-soft --
   a logging failure never propagates).
4. `resolve_routing_decisions` independently re-validates a `"use_
   agentic"` outcome before applying it (defense-in-depth) and falls
   back to the live value when unsafe, also logging the rejection.
5. None of this touches the database or calls a provider.
"""

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import settings
from agents.series_agent import SeriesIntelligenceAgent
from database import Base
from models import Book, Series, SeriesSkeleton
from services.agentic_promotion_evaluator import evaluate_promotion
from services.agentic_resolution import resolve_routing_decisions
from services.agentic_safety import validate_agentic_decision, validate_promotion_outcome


class ValidateAgenticDecisionTest(unittest.TestCase):
    """Pure-function tests -- no DB, no provider, no mocking needed."""

    def test_accepts_a_genuinely_safe_decision(self):
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}
        self.assertTrue(validate_agentic_decision(live_conf, agentic_conf, live_gate, agentic_gate))

    def test_reject_agentic_on_confidence_contradiction(self):
        # Agentic ranks lower than live on a shared dimension -- a
        # contradiction/reduction in provider agreement.
        live_conf = {"confidence": "high"}
        agentic_conf = {"overall": "low"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}
        self.assertFalse(validate_agentic_decision(live_conf, agentic_conf, live_gate, agentic_gate))

    def test_reject_agentic_on_gate_contradiction(self):
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": False}
        self.assertFalse(validate_agentic_decision(live_conf, agentic_conf, live_gate, agentic_gate))

    def test_reject_agentic_on_missing_fields(self):
        # agentic_conf/agentic_gate are dicts, but offer literally no
        # usable opinion at all -- unsafe independent of what live says.
        self.assertFalse(validate_agentic_decision(None, {}, None, {}))
        self.assertFalse(
            validate_agentic_decision({"confidence": "medium"}, {}, {"belongs_to_series": True}, {})
        )

    def test_reject_agentic_on_negative_values(self):
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high", "raw_score": -1}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}
        self.assertFalse(validate_agentic_decision(live_conf, agentic_conf, live_gate, agentic_gate))

    def test_reject_agentic_on_malformed_structures(self):
        live_conf = {"confidence": "high"}
        live_gate = {"belongs_to_series": True}

        # Not a dict at all.
        self.assertFalse(validate_agentic_decision(live_conf, "not-a-dict", live_gate, {"belongs_to_series": True}))
        self.assertFalse(validate_agentic_decision(live_conf, {"overall": "high"}, live_gate, ["not", "a", "dict"]))

        # Unrecognized/garbage confidence grade string.
        self.assertFalse(
            validate_agentic_decision(live_conf, {"overall": "extremely-confident"}, live_gate, {"belongs_to_series": True})
        )
        # Non-string confidence grade.
        self.assertFalse(
            validate_agentic_decision(live_conf, {"overall": 42}, live_gate, {"belongs_to_series": True})
        )
        # Gate opinion that isn't a real bool.
        self.assertFalse(
            validate_agentic_decision(live_conf, {"overall": "high"}, live_gate, {"belongs_to_series": "yes"})
        )

    def test_reject_agentic_on_impossible_book_number_jump(self):
        # An (opaque, optional) "book_number" field showing up inside
        # agentic_conf/agentic_gate must be a real, finite, non-negative,
        # plausible number.
        self.assertFalse(validate_agentic_decision(None, {"book_number": -5, "overall": "high"}, None, None))
        self.assertFalse(validate_agentic_decision(None, {"book_number": float("nan"), "overall": "high"}, None, None))
        self.assertFalse(validate_agentic_decision(None, {"book_number": float("inf"), "overall": "high"}, None, None))
        self.assertFalse(
            validate_agentic_decision(None, None, None, {"book_number": 999_999_999, "belongs_to_series": True})
        )
        # A plausible book_number alongside an otherwise-safe decision is fine.
        self.assertTrue(
            validate_agentic_decision(
                {"confidence": "medium"}, {"book_number": 7.0, "overall": "high"}, None, None
            )
        )

    def test_reject_agentic_on_determinism_invariant(self):
        # Agentic offers no opinion at all while live has one.
        live_conf = {"confidence": "medium"}
        self.assertFalse(validate_agentic_decision(live_conf, None, None, None))

    def test_never_raises_on_unexpected_input_shapes(self):
        # Deeply malformed (non-dict, non-None) input should fail soft
        # to False, never raise.
        self.assertFalse(validate_agentic_decision(object(), object(), object(), object()))
        # All-None input is vacuously safe -- no opinion on either side
        # means nothing to contradict (evaluate_promotion's own rules
        # already handle "nothing to promote" as a separate concern).
        self.assertTrue(validate_agentic_decision(None, None, None, None))


class ValidatePromotionOutcomeTest(unittest.TestCase):
    def test_reject_agentic_on_invalid_outcome(self):
        self.assertFalse(validate_promotion_outcome("something_else"))
        self.assertFalse(validate_promotion_outcome(None))
        self.assertFalse(validate_promotion_outcome(123))
        self.assertFalse(validate_promotion_outcome(""))

    def test_accepts_only_the_three_valid_outcomes(self):
        self.assertTrue(validate_promotion_outcome("use_live"))
        self.assertTrue(validate_promotion_outcome("use_agentic"))
        self.assertTrue(validate_promotion_outcome("reject_agentic"))


class EvaluatePromotionSafetyIntegrationTest(unittest.TestCase):
    """`evaluate_promotion`'s own rules 1-3 would otherwise approve these
    as "use_agentic" candidates, but the Phase 7 safety re-check vetoes
    them.
    """

    def test_safety_veto_downgrades_use_agentic_to_reject_agentic(self):
        # Shared dim "overall": agentic (high) ranks above live (medium)
        # -- rules 1-3 alone would say "use_agentic" -- but the extra
        # "raw_score" field is negative, which validate_agentic_decision
        # treats as a malformed/corrupted value.
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high", "raw_score": -1}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        outcome = evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate)
        self.assertEqual(outcome, "reject_agentic")

    def test_safe_use_agentic_candidate_is_not_vetoed(self):
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        outcome = evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate)
        self.assertEqual(outcome, "use_agentic")

    def test_safety_logging_called(self):
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high", "raw_score": -1}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        with patch("services.discovery_telemetry.record_agentic_safety_violation") as mock_log:
            outcome = evaluate_promotion(
                live_conf, agentic_conf, live_gate, agentic_gate, series_id=1, book_number=7.0
            )

        self.assertEqual(outcome, "reject_agentic")
        mock_log.assert_called_once()
        call_series_id, call_book_number, call_reason = mock_log.call_args[0]
        self.assertEqual(call_series_id, 1)
        self.assertEqual(call_book_number, 7.0)
        self.assertTrue(call_reason)

    def test_safety_logging_not_called_for_safe_decisions(self):
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high"}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        with patch("services.discovery_telemetry.record_agentic_safety_violation") as mock_log:
            evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate, series_id=1, book_number=7.0)

        mock_log.assert_not_called()

    def test_fail_soft_on_safety_logging_error(self):
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high", "raw_score": -1}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        with patch(
            "services.discovery_telemetry.record_agentic_safety_violation",
            side_effect=RuntimeError("logging exploded"),
        ):
            outcome = evaluate_promotion(
                live_conf, agentic_conf, live_gate, agentic_gate, series_id=1, book_number=7.0
            )

        # The veto itself must still take effect even though logging it failed.
        self.assertEqual(outcome, "reject_agentic")

    def test_logging_works_without_series_id_or_book_number(self):
        # series_id/book_number are optional -- omitting them (existing
        # callers/tests predating Phase 7) must not raise.
        live_conf = {"confidence": "medium"}
        agentic_conf = {"overall": "high", "raw_score": -1}
        live_gate = {"belongs_to_series": True}
        agentic_gate = {"belongs_to_series": True}

        outcome = evaluate_promotion(live_conf, agentic_conf, live_gate, agentic_gate)
        self.assertEqual(outcome, "reject_agentic")


class ResolutionDefenseInDepthTest(unittest.TestCase):
    def test_resolution_defense_in_depth(self):
        # A stored "use_agentic" decision (as if evaluate_promotion had
        # already approved it) whose actual agentic_confidence is unsafe
        # (lower than live) -- resolve_routing_decisions must
        # independently re-check and fall back to live rather than
        # trusting the stored outcome string.
        live_confidence = {1.0: {"confidence": "high"}}
        live_gate = {1.0: {"belongs_to_series": True}}
        promotion_decisions = {
            1.0: {
                "outcome": "use_agentic",
                "live_confidence": live_confidence[1.0],
                "agentic_confidence": {"overall": "low"},
                "live_gate": live_gate[1.0],
                "agentic_gate": {"belongs_to_series": True},
            }
        }

        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            resolved_conf, resolved_gate = resolve_routing_decisions(1, live_confidence, live_gate, promotion_decisions)

        # Fell back to live, not the unsafe agentic value.
        self.assertEqual(resolved_conf[1.0], live_confidence[1.0])
        self.assertEqual(resolved_gate[1.0], live_gate[1.0])

    def test_resolution_applies_agentic_side_when_actually_safe(self):
        live_confidence = {1.0: {"confidence": "medium"}}
        live_gate = {1.0: {"belongs_to_series": True}}
        promotion_decisions = {
            1.0: {
                "outcome": "use_agentic",
                "live_confidence": live_confidence[1.0],
                "agentic_confidence": {"overall": "high"},
                "live_gate": live_gate[1.0],
                "agentic_gate": {"belongs_to_series": True},
            }
        }

        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            resolved_conf, _ = resolve_routing_decisions(1, live_confidence, live_gate, promotion_decisions)

        self.assertEqual(resolved_conf[1.0], {"overall": "high"})

    def test_resolution_safety_logging_called_on_veto(self):
        live_confidence = {1.0: {"confidence": "high"}}
        live_gate = {1.0: {"belongs_to_series": True}}
        promotion_decisions = {
            1.0: {
                "outcome": "use_agentic",
                "live_confidence": live_confidence[1.0],
                "agentic_confidence": {"overall": "low"},
                "live_gate": live_gate[1.0],
                "agentic_gate": {"belongs_to_series": True},
            }
        }

        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ), patch("services.discovery_telemetry.record_agentic_safety_violation") as mock_log:
            resolve_routing_decisions(1, live_confidence, live_gate, promotion_decisions)

        mock_log.assert_called_once()
        call_series_id, call_book_number, call_reason = mock_log.call_args[0]
        self.assertEqual(call_series_id, 1)
        self.assertEqual(call_book_number, 1.0)
        self.assertTrue(call_reason)

    def test_resolution_fail_soft_on_safety_logging_error(self):
        live_confidence = {1.0: {"confidence": "high"}}
        live_gate = {1.0: {"belongs_to_series": True}}
        promotion_decisions = {
            1.0: {
                "outcome": "use_agentic",
                "agentic_confidence": {"overall": "low"},
                "agentic_gate": {"belongs_to_series": True},
            }
        }

        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ), patch(
            "services.discovery_telemetry.record_agentic_safety_violation",
            side_effect=RuntimeError("logging exploded"),
        ):
            resolved_conf, _ = resolve_routing_decisions(1, live_confidence, live_gate, promotion_decisions)

        # Veto still took effect despite the logging failure.
        self.assertEqual(resolved_conf[1.0], live_confidence[1.0])


class NoSideEffectsTest(unittest.TestCase):
    """End-to-end proof (via `agents/series_agent.py`'s real promotion
    block) that the Phase 7 safety layer never writes to the database
    and never calls a provider of its own -- mirroring the equivalent
    tests in tests/test_agentic_promotion.py/test_agentic_activation.py.
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

    def tearDown(self):
        self.db.close()

    def _mock_discovery(self, **overrides):
        result = {
            "candidates": [],
            "provider_failures": [],
            "all_providers_failed": False,
            "used_author_fallback": False,
        }
        result.update(overrides)
        return patch("discovery_engine.discover_candidates_for_series", return_value=result)

    def test_no_skeleton_or_probes_mutation(self):
        with self._mock_discovery(), patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", str(self.series.id)
        ), patch("agents.agentic_series_agent.SessionLocal", self.SessionLocal):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(result["probes"], [])
        skeleton_row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        self.assertIsNotNone(skeleton_row)
        for entry in skeleton_row.skeleton_json:
            if entry.get("source_class", "library") == "library":
                self.assertEqual(entry.get("status"), "confirmed")
                self.assertEqual(entry.get("confidence"), "high")

    def test_no_provider_calls(self):
        with self._mock_discovery() as mock_discover, patch.object(
            settings, "AGENTIC_ROUTING_ENABLED", True
        ), patch.object(settings, "AGENTIC_SERIES_ACTIVATION", str(self.series.id)), patch(
            "agents.agentic_series_agent.SessionLocal", self.SessionLocal
        ):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False)

        # The live pipeline's own single provider call -- the safety
        # layer adds no provider calls of its own.
        mock_discover.assert_called_once()


if __name__ == "__main__":
    unittest.main()
