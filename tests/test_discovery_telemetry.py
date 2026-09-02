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


class RecordLlmCallCostAttributionTest(unittest.TestCase):
    """HTA Orchestrator Step 2/3: model_id/tier/cost_usd/correlation_id
    attribution on record_llm_call(), and their surfacing through
    summary()'s per_model/per_tier/by_correlation_id breakdowns.
    """

    def test_computes_exact_cost_from_known_model_pricing(self):
        telemetry = DiscoveryTelemetry()
        telemetry.record_llm_call(
            duration_s=1.0, tokens_in=1_000_000, tokens_out=1_000_000, model_id="claude-haiku-4-5-20251001"
        )
        summary = telemetry.summary()
        # $1/M input + $5/M output at 1M tokens each.
        self.assertAlmostEqual(summary["total_cost_usd"], 6.0)
        self.assertAlmostEqual(summary["per_model"]["claude-haiku-4-5-20251001"]["cost_usd"], 6.0)

    def test_unknown_model_id_fails_soft_to_zero_cost(self):
        telemetry = DiscoveryTelemetry()
        telemetry.record_llm_call(duration_s=1.0, tokens_in=1000, tokens_out=1000, model_id="some-future-model")
        summary = telemetry.summary()
        self.assertEqual(summary["total_cost_usd"], 0.0)
        self.assertEqual(summary["per_model"]["some-future-model"]["cost_usd"], 0.0)
        self.assertEqual(summary["per_model"]["some-future-model"]["calls"], 1)

    def test_pre_existing_call_shape_without_model_id_still_works(self):
        # Backward compatibility: callers that don't pass model_id (the
        # shape every call site used before Step 2) must keep working, with
        # sane defaults and no crash in summary().
        telemetry = DiscoveryTelemetry()
        telemetry.record_llm_call(duration_s=0.5, tokens_in=100, tokens_out=200)
        summary = telemetry.summary()
        self.assertEqual(summary["total_cost_usd"], 0.0)
        self.assertEqual(summary["per_model"]["unknown"]["calls"], 1)
        self.assertEqual(summary["per_tier"]["none"]["calls"], 1)

    def test_distinct_models_and_tiers_tracked_independently(self):
        telemetry = DiscoveryTelemetry()
        with telemetry.pass_scope("targeted", tier="A"):
            telemetry.record_llm_call(
                duration_s=0.1, tokens_in=1000, tokens_out=1000, model_id="claude-haiku-4-5-20251001"
            )
        with telemetry.pass_scope("reconciliation", tier="B"):
            telemetry.record_llm_call(
                duration_s=0.1, tokens_in=2000, tokens_out=2000, model_id="claude-haiku-4-5-20251001"
            )

        summary = telemetry.summary()
        self.assertEqual(summary["per_tier"]["A"]["tokens_in"], 1000)
        self.assertEqual(summary["per_tier"]["B"]["tokens_in"], 2000)
        self.assertEqual(summary["per_model"]["claude-haiku-4-5-20251001"]["calls"], 2)

    def test_correlation_id_is_fresh_per_pass_scope_invocation(self):
        telemetry = DiscoveryTelemetry()
        with telemetry.pass_scope("targeted", tier="A"):
            telemetry.record_llm_call(duration_s=0.1, tokens_in=10, tokens_out=10, model_id="claude-haiku-4-5-20251001")
        with telemetry.pass_scope("targeted", tier="A"):
            telemetry.record_llm_call(duration_s=0.1, tokens_in=10, tokens_out=10, model_id="claude-haiku-4-5-20251001")

        summary = telemetry.summary()
        # Two separate pass_scope() invocations -> two distinct
        # correlation_ids, each with exactly one call.
        self.assertEqual(len(summary["by_correlation_id"]), 2)
        for entry in summary["by_correlation_id"].values():
            self.assertEqual(entry["calls"], 1)
            self.assertEqual(entry["tier"], "A")

    def test_calls_outside_any_pass_scope_group_under_none(self):
        telemetry = DiscoveryTelemetry()
        telemetry.record_llm_call(duration_s=0.1, tokens_in=10, tokens_out=10, model_id="claude-haiku-4-5-20251001")
        summary = telemetry.summary()
        self.assertIn("none", summary["by_correlation_id"])
        self.assertEqual(summary["by_correlation_id"]["none"]["calls"], 1)

    def test_by_pass_bucket_sums_cost_usd_alongside_existing_token_fields(self):
        telemetry = DiscoveryTelemetry()
        with telemetry.pass_scope("targeted", tier="A"):
            telemetry.record_llm_call(
                duration_s=0.1, tokens_in=1_000_000, tokens_out=0, model_id="claude-haiku-4-5-20251001"
            )
        summary = telemetry.summary()
        self.assertAlmostEqual(summary["by_pass"]["targeted"]["cost_usd"], 1.0)


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


class RecordShadowLlmCallTest(unittest.TestCase):
    """HTA Orchestrator Step 4: record_shadow_llm_call() and summary()'s
    "shadow" section -- mirrors record_llm_call()'s cost/token attribution,
    but must never be mixed into the production totals/per_model/per_tier
    keys record_llm_call() already populates.
    """

    def test_shadow_call_is_costed_like_a_production_call(self):
        telemetry = DiscoveryTelemetry()
        telemetry.record_shadow_llm_call(
            duration_s=1.0, tokens_in=1_000_000, tokens_out=1_000_000, model_id="claude-haiku-4-5-20251001"
        )
        summary = telemetry.summary()
        self.assertAlmostEqual(summary["shadow"]["total_cost_usd"], 6.0)
        self.assertEqual(summary["shadow"]["total_llm_calls"], 1)
        self.assertEqual(summary["shadow"]["total_tokens_in"], 1_000_000)
        self.assertEqual(summary["shadow"]["total_tokens_out"], 1_000_000)
        self.assertAlmostEqual(summary["shadow"]["per_model"]["claude-haiku-4-5-20251001"]["cost_usd"], 6.0)

    def test_shadow_calls_never_leak_into_production_totals(self):
        telemetry = DiscoveryTelemetry()
        telemetry.record_llm_call(
            duration_s=0.1, tokens_in=1000, tokens_out=1000, model_id="claude-haiku-4-5-20251001"
        )
        telemetry.record_shadow_llm_call(
            duration_s=0.1, tokens_in=5000, tokens_out=5000, model_id="claude-haiku-4-5-20251001"
        )
        summary = telemetry.summary()
        self.assertEqual(summary["total_llm_calls"], 1)
        self.assertEqual(summary["total_tokens_in"], 1000)
        self.assertEqual(summary["per_model"]["claude-haiku-4-5-20251001"]["calls"], 1)
        self.assertEqual(summary["shadow"]["total_llm_calls"], 1)
        self.assertEqual(summary["shadow"]["total_tokens_in"], 5000)

    def test_shadow_call_is_tagged_with_current_pass_scope_tier(self):
        telemetry = DiscoveryTelemetry()
        with telemetry.pass_scope("belongs_to_series", tier="C"):
            telemetry.record_shadow_llm_call(
                duration_s=0.1, tokens_in=10, tokens_out=10, model_id="claude-haiku-4-5-20251001"
            )
        summary = telemetry.summary()
        self.assertEqual(summary["shadow"]["per_tier"]["C"]["calls"], 1)

    def test_no_shadow_calls_reports_zeroed_out_section(self):
        summary = DiscoveryTelemetry().summary()
        self.assertEqual(summary["shadow"]["total_llm_calls"], 0)
        self.assertEqual(summary["shadow"]["total_cost_usd"], 0.0)
        self.assertEqual(summary["shadow"]["per_model"], {})
        self.assertEqual(summary["shadow"]["per_tier"], {})


class RecordTierCShadowScoreTest(unittest.TestCase):
    """HTA Orchestrator Step 7: record_tier_c_shadow_score() and
    summary()'s "tier_c_shadow" section -- per-run-only agreement/
    disagreement counts against the deterministic gate, kept separate
    from the cost/token-shaped "shadow" section above.
    """

    def test_agreements_and_disagreements_are_counted_separately(self):
        telemetry = DiscoveryTelemetry()
        telemetry.record_tier_c_shadow_score(
            parsed_ok=True, belongs_to_series_agreement=True, inferred_number_agreement=True
        )
        telemetry.record_tier_c_shadow_score(
            parsed_ok=True,
            belongs_to_series_agreement=False,
            inferred_number_agreement=False,
            tier_c_confidence="high",
            confidence_aligned=True,
        )

        summary = telemetry.summary()["tier_c_shadow"]
        self.assertEqual(summary["total_scored"], 2)
        self.assertEqual(summary["parse_failures"], 0)
        self.assertEqual(summary["belongs_to_series_agreements"], 1)
        self.assertEqual(summary["belongs_to_series_disagreements"], 1)
        self.assertEqual(summary["inferred_number_agreements"], 1)
        self.assertEqual(summary["inferred_number_disagreements"], 1)
        self.assertEqual(summary["confidence_aligned_on_disagreement"], 1)
        self.assertEqual(summary["confidence_misaligned_on_disagreement"], 0)

    def test_parse_failure_is_counted_but_not_scored_as_a_disagreement(self):
        telemetry = DiscoveryTelemetry()
        telemetry.record_tier_c_shadow_score(parsed_ok=False)

        summary = telemetry.summary()["tier_c_shadow"]
        self.assertEqual(summary["total_scored"], 1)
        self.assertEqual(summary["parse_failures"], 1)
        self.assertEqual(summary["belongs_to_series_agreements"], 0)
        self.assertEqual(summary["belongs_to_series_disagreements"], 0)

    def test_alternate_title_flag_is_counted_separately_from_agreement(self):
        telemetry = DiscoveryTelemetry()
        telemetry.record_tier_c_shadow_score(
            parsed_ok=True,
            belongs_to_series_agreement=True,
            inferred_number_agreement=True,
            tier_c_alternate_title_flag=True,
        )

        summary = telemetry.summary()["tier_c_shadow"]
        self.assertEqual(summary["alternate_title_flagged"], 1)
        # The alternate-title signal has no gate counterpart -- it must
        # never be folded into the belongs_to_series agreement count.
        self.assertEqual(summary["belongs_to_series_agreements"], 1)

    def test_no_scores_reports_zeroed_out_section(self):
        summary = DiscoveryTelemetry().summary()
        self.assertEqual(summary["tier_c_shadow"]["total_scored"], 0)
        self.assertEqual(summary["tier_c_shadow"]["parse_failures"], 0)
        self.assertEqual(summary["tier_c_shadow"]["belongs_to_series_agreements"], 0)
        self.assertEqual(summary["tier_c_shadow"]["alternate_title_flagged"], 0)


if __name__ == "__main__":
    unittest.main()
