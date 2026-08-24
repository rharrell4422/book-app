"""PB-9: coverage for DiscoveryTelemetry's generalized per-provider call
counters (record_provider_call) and named gate-outcome counters
(record_gate_outcome), plus their by_provider/by_gate summary()
breakdowns.
"""
import unittest

from services.discovery_telemetry import DiscoveryTelemetry


class RecordProviderCallTest(unittest.TestCase):
    def test_counts_ok_and_failed_calls_separately_per_provider(self):
        telemetry = DiscoveryTelemetry()
        telemetry.record_provider_call("google", ok=True, duration_s=0.1)
        telemetry.record_provider_call("google", ok=True, duration_s=0.2)
        telemetry.record_provider_call("google", ok=False, duration_s=0.05)
        telemetry.record_provider_call("hardcover", ok=True, duration_s=0.3)

        summary = telemetry.summary()
        self.assertEqual(summary["by_provider"]["google"]["calls"], 3)
        self.assertEqual(summary["by_provider"]["google"]["ok"], 2)
        self.assertEqual(summary["by_provider"]["google"]["failed"], 1)
        self.assertEqual(summary["by_provider"]["google"]["duration_s"], 0.35)
        self.assertEqual(summary["by_provider"]["hardcover"]["calls"], 1)

    def test_no_provider_calls_is_an_empty_dict_not_a_missing_key(self):
        summary = DiscoveryTelemetry().summary()
        self.assertEqual(summary["by_provider"], {})

    def test_provider_calls_are_bucketed_by_current_pass_scope(self):
        telemetry = DiscoveryTelemetry()
        with telemetry.pass_scope("targeted"):
            telemetry.record_provider_call("google", ok=True, duration_s=0.1)
        with telemetry.pass_scope("author_fallback"):
            telemetry.record_provider_call("google", ok=True, duration_s=0.1)
        # by_provider aggregates across passes -- by_pass is where per-pass
        # timing already lives; this just confirms recording doesn't crash
        # or lose entries across a pass_scope boundary.
        summary = telemetry.summary()
        self.assertEqual(summary["by_provider"]["google"]["calls"], 2)


class RecordGateOutcomeTest(unittest.TestCase):
    def test_counts_distinct_outcomes_for_the_same_gate(self):
        telemetry = DiscoveryTelemetry()
        telemetry.record_gate_outcome("confidence_grade", "high")
        telemetry.record_gate_outcome("confidence_grade", "high")
        telemetry.record_gate_outcome("confidence_grade", "low")

        summary = telemetry.summary()
        self.assertEqual(summary["by_gate"]["confidence_grade"], {"high": 2, "low": 1})

    def test_different_gates_are_tracked_independently(self):
        telemetry = DiscoveryTelemetry()
        telemetry.record_gate_outcome("all_providers_failed", "true")
        telemetry.record_gate_outcome("author_fallback", "triggered")

        summary = telemetry.summary()
        self.assertEqual(summary["by_gate"]["all_providers_failed"], {"true": 1})
        self.assertEqual(summary["by_gate"]["author_fallback"], {"triggered": 1})

    def test_no_gate_outcomes_is_an_empty_dict(self):
        summary = DiscoveryTelemetry().summary()
        self.assertEqual(summary["by_gate"], {})


if __name__ == "__main__":
    unittest.main()
