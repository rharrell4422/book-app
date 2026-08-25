"""Phase 5, unified resolution layer -- `services/agentic_resolution.
resolve_routing_decisions`.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here), this file needs to prove:

1. Flag off -> live snapshots returned unchanged (as copies).
2. Flag on but series not activated -> live snapshots returned unchanged
   ("record, don't apply" -- same as Phase 3/4).
3. Flag on and activated -> per-book resolution follows each decision's
   own "outcome": agentic side for "use_agentic", live side for
   anything else ("use_live", "reject_agentic", missing).
4. A book_number with no promotion decision at all still resolves to its
   own live value (no decision != "use agentic").
5. Fail-soft: an exception anywhere inside (e.g. settings.
   is_agentic_activated itself raising) falls back to the live
   snapshots, never raises.
6. Never touches skeleton_json/probes_json or calls a provider (this
   module has no DB/provider access at all -- verified by the absence of
   any such import/call, and every test here uses plain dicts).
7. Returns new dict objects, not the same object references passed in
   (mutation-safety for callers).
"""

import unittest
from unittest.mock import patch

import settings
from services.agentic_resolution import resolve_routing_decisions


class ResolveRoutingDecisionsTest(unittest.TestCase):
    def setUp(self):
        self.live_confidence = {
            1.0: {"confidence": "high", "status": "confirmed"},
            2.0: {"confidence": "medium", "status": "confirmed"},
        }
        self.live_gate = {
            1.0: {"belongs_to_series": True, "source_class": "library"},
            2.0: {"belongs_to_series": True, "source_class": "library"},
        }

    def test_flag_off_returns_live_snapshots_unchanged(self):
        promotion_decisions = {
            1.0: {
                "outcome": "use_agentic",
                "live_confidence": self.live_confidence[1.0],
                "agentic_confidence": {"overall": "high"},
                "live_gate": self.live_gate[1.0],
                "agentic_gate": {"belongs_to_series": True},
            }
        }
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", False):
            resolved_conf, resolved_gate = resolve_routing_decisions(
                1, self.live_confidence, self.live_gate, promotion_decisions
            )

        self.assertEqual(resolved_conf, self.live_confidence)
        self.assertEqual(resolved_gate, self.live_gate)
        # Copies, not the same objects.
        self.assertIsNot(resolved_conf, self.live_confidence)
        self.assertIsNot(resolved_gate, self.live_gate)

    def test_flag_on_but_not_activated_records_without_applying(self):
        promotion_decisions = {
            1.0: {
                "outcome": "use_agentic",
                "live_confidence": self.live_confidence[1.0],
                "agentic_confidence": {"overall": "high"},
                "live_gate": self.live_gate[1.0],
                "agentic_gate": {"belongs_to_series": True},
            }
        }
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", ""
        ):
            resolved_conf, resolved_gate = resolve_routing_decisions(
                1, self.live_confidence, self.live_gate, promotion_decisions
            )

        self.assertEqual(resolved_conf, self.live_confidence)
        self.assertEqual(resolved_gate, self.live_gate)

    def test_activated_applies_agentic_side_only_for_use_agentic_outcome(self):
        promotion_decisions = {
            1.0: {
                "outcome": "use_agentic",
                "live_confidence": self.live_confidence[1.0],
                "agentic_confidence": {"overall": "high"},
                "live_gate": self.live_gate[1.0],
                "agentic_gate": {"belongs_to_series": True, "source": "agentic"},
            },
            2.0: {
                "outcome": "use_live",
                "live_confidence": self.live_confidence[2.0],
                "agentic_confidence": {"overall": "medium"},
                "live_gate": self.live_gate[2.0],
                "agentic_gate": {"belongs_to_series": True, "source": "agentic"},
            },
        }
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            resolved_conf, resolved_gate = resolve_routing_decisions(
                1, self.live_confidence, self.live_gate, promotion_decisions
            )

        self.assertEqual(resolved_conf[1.0], {"overall": "high"})
        self.assertEqual(resolved_gate[1.0], {"belongs_to_series": True, "source": "agentic"})
        # outcome "use_live" -> live side, even though it's activated.
        self.assertEqual(resolved_conf[2.0], self.live_confidence[2.0])
        self.assertEqual(resolved_gate[2.0], self.live_gate[2.0])

    def test_reject_agentic_outcome_resolves_to_live(self):
        promotion_decisions = {
            1.0: {
                "outcome": "reject_agentic",
                "live_confidence": self.live_confidence[1.0],
                "agentic_confidence": {"overall": "low"},
                "live_gate": self.live_gate[1.0],
                "agentic_gate": {"belongs_to_series": False},
            }
        }
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            resolved_conf, resolved_gate = resolve_routing_decisions(
                1, self.live_confidence, self.live_gate, promotion_decisions
            )

        self.assertEqual(resolved_conf[1.0], self.live_confidence[1.0])
        self.assertEqual(resolved_gate[1.0], self.live_gate[1.0])

    def test_book_with_no_promotion_decision_resolves_to_its_own_live_value(self):
        # Only book 1 has a decision; book 2 has none at all -- must
        # still appear in the result, resolved to its own live value,
        # never treated as "use agentic".
        promotion_decisions = {
            1.0: {
                "outcome": "use_agentic",
                "live_confidence": self.live_confidence[1.0],
                "agentic_confidence": {"overall": "high"},
                "live_gate": self.live_gate[1.0],
                "agentic_gate": {"belongs_to_series": True},
            }
        }
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            resolved_conf, resolved_gate = resolve_routing_decisions(
                1, self.live_confidence, self.live_gate, promotion_decisions
            )

        self.assertEqual(resolved_conf[2.0], self.live_confidence[2.0])
        self.assertEqual(resolved_gate[2.0], self.live_gate[2.0])

    def test_activated_series_is_specific_not_global(self):
        promotion_decisions = {
            1.0: {
                "outcome": "use_agentic",
                "live_confidence": self.live_confidence[1.0],
                "agentic_confidence": {"overall": "high"},
                "live_gate": self.live_gate[1.0],
                "agentic_gate": {"belongs_to_series": True},
            }
        }
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "42"
        ):
            # series_id=1 is not in the allowlist ("42" only).
            resolved_conf, _ = resolve_routing_decisions(1, self.live_confidence, self.live_gate, promotion_decisions)

        self.assertEqual(resolved_conf[1.0], self.live_confidence[1.0])

    def test_fail_soft_when_is_agentic_activated_raises(self):
        promotion_decisions = {
            1.0: {
                "outcome": "use_agentic",
                "live_confidence": self.live_confidence[1.0],
                "agentic_confidence": {"overall": "high"},
                "live_gate": self.live_gate[1.0],
                "agentic_gate": {"belongs_to_series": True},
            }
        }
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch(
            "settings.is_agentic_activated", side_effect=RuntimeError("allowlist parsing exploded")
        ):
            resolved_conf, resolved_gate = resolve_routing_decisions(
                1, self.live_confidence, self.live_gate, promotion_decisions
            )

        self.assertEqual(resolved_conf, self.live_confidence)
        self.assertEqual(resolved_gate, self.live_gate)

    def test_fail_soft_with_malformed_promotion_decisions(self):
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            resolved_conf, resolved_gate = resolve_routing_decisions(
                1, self.live_confidence, self.live_gate, "not-a-dict"  # type: ignore[arg-type]
            )

        # promotion_decisions isn't iterable the way this function
        # expects -- must fall back to the live snapshots, not raise.
        self.assertEqual(resolved_conf, self.live_confidence)
        self.assertEqual(resolved_gate, self.live_gate)

    def test_none_inputs_do_not_raise(self):
        resolved_conf, resolved_gate = resolve_routing_decisions(1, None, None, None)
        self.assertEqual(resolved_conf, {})
        self.assertEqual(resolved_gate, {})

    def test_empty_promotion_decisions_with_activation_on_resolves_to_live(self):
        with patch.object(settings, "AGENTIC_ROUTING_ENABLED", True), patch.object(
            settings, "AGENTIC_SERIES_ACTIVATION", "1"
        ):
            resolved_conf, resolved_gate = resolve_routing_decisions(1, self.live_confidence, self.live_gate, {})

        self.assertEqual(resolved_conf, self.live_confidence)
        self.assertEqual(resolved_gate, self.live_gate)


if __name__ == "__main__":
    unittest.main()
