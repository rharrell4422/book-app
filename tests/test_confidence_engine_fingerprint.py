"""Tests for the Series Fingerprint influence on confidence_engine.py --
see discovery_agentic_fingerprint_recommendation.md for the full
ten-round design chain. Covers the provider-bias grade nudge and the
cadence-based "implausibly early" number_confidence downgrade, both gated
on an explicit `fingerprint` argument so `fingerprint=None` (every call
site before this feature existed) is a total no-op.
"""

import unittest

from confidence_engine import (
    _apply_provider_bias_to_grade,
    _cadence_flags_implausibly_early,
    _nudge_grade,
    _number_confidence,
    _provider_confidence,
    compute_confidence,
)


class NudgeGradeTest(unittest.TestCase):
    def test_nudge_up_one_step(self):
        self.assertEqual(_nudge_grade("medium", 1), "high")

    def test_nudge_down_one_step(self):
        self.assertEqual(_nudge_grade("medium", -1), "low")

    def test_nudge_up_clamps_at_high(self):
        self.assertEqual(_nudge_grade("high", 1), "high")

    def test_nudge_down_clamps_at_zero(self):
        self.assertEqual(_nudge_grade("zero", -1), "zero")


class ApplyProviderBiasToGradeTest(unittest.TestCase):
    def test_none_bias_is_a_no_op(self):
        self.assertEqual(_apply_provider_bias_to_grade("medium", None), "medium")

    def test_bias_at_or_above_up_threshold_nudges_up(self):
        self.assertEqual(_apply_provider_bias_to_grade("medium", 1.2), "high")

    def test_bias_at_or_below_down_threshold_nudges_down(self):
        self.assertEqual(_apply_provider_bias_to_grade("medium", 0.8), "low")

    def test_bias_in_neutral_band_is_a_no_op(self):
        self.assertEqual(_apply_provider_bias_to_grade("medium", 1.0), "medium")


class ProviderConfidenceFingerprintTest(unittest.TestCase):
    def _candidate(self, source="hardcover"):
        return {"source_provenance": [{"source": source}]}

    def test_fingerprint_none_reproduces_pre_feature_grade(self):
        self.assertEqual(_provider_confidence(self._candidate("hardcover"), None), "high")

    def test_high_provider_bias_upgrades_web_search_from_low_to_medium(self):
        fingerprint = {"provider_bias": {"web_search": 1.3}}
        self.assertEqual(
            _provider_confidence(self._candidate("web_search"), fingerprint), "medium"
        )

    def test_low_provider_bias_downgrades_hardcover_from_high_to_medium(self):
        fingerprint = {"provider_bias": {"hardcover": 0.6}}
        self.assertEqual(
            _provider_confidence(self._candidate("hardcover"), fingerprint), "medium"
        )

    def test_provider_absent_from_bias_map_is_unaffected(self):
        fingerprint = {"provider_bias": {"hardcover": 0.6}}
        self.assertEqual(
            _provider_confidence(self._candidate("google_books"), fingerprint), "medium"
        )


class CadenceFlagsImplausiblyEarlyTest(unittest.TestCase):
    def _skeleton_with_cadence_history(self):
        # Books 1-4, six-month cadence, all library-sourced/dated --
        # mean=~182 days, stddev=0 (perfectly regular).
        return [
            {"book_number": 1.0, "release_date": "2020-01-01", "source_class": "library"},
            {"book_number": 2.0, "release_date": "2020-07-01", "source_class": "library"},
            {"book_number": 3.0, "release_date": "2021-01-01", "source_class": "library"},
            {"book_number": 4.0, "release_date": "2021-07-01", "source_class": "library"},
        ]

    def _fingerprint(self, mean=182.0, stddev=0.0, count=3):
        return {
            "release_cadence": {
                "mean_interval_days": mean,
                "stddev_interval_days": stddev,
                "interval_count": count,
            }
        }

    def test_no_fingerprint_never_flags(self):
        candidate = {"published_date": "2021-08-01"}
        self.assertFalse(
            _cadence_flags_implausibly_early(candidate, 5.0, self._skeleton_with_cadence_history(), None)
        )

    def test_insufficient_history_never_flags(self):
        candidate = {"published_date": "2021-08-01"}
        fingerprint = self._fingerprint(count=1)
        self.assertFalse(
            _cadence_flags_implausibly_early(
                candidate, 5.0, self._skeleton_with_cadence_history(), fingerprint
            )
        )

    def test_plausible_date_at_expected_cadence_does_not_flag(self):
        # Book 4 released 2021-07-01; book 5 one interval later (~182 days)
        # lands around 2021-12-30 -- comfortably plausible.
        candidate = {"published_date": "2022-01-01"}
        fingerprint = self._fingerprint()
        self.assertFalse(
            _cadence_flags_implausibly_early(
                candidate, 5.0, self._skeleton_with_cadence_history(), fingerprint
            )
        )

    def test_implausibly_early_date_flags(self):
        # Book 5 supposedly releasing just one month after book 4, with a
        # perfectly regular ~182-day cadence (stddev=0) and no margin
        # (FULL precision) -- well below the earliest plausible date.
        candidate = {"published_date": "2021-08-01"}
        fingerprint = self._fingerprint()
        self.assertTrue(
            _cadence_flags_implausibly_early(
                candidate, 5.0, self._skeleton_with_cadence_history(), fingerprint
            )
        )

    def test_year_only_precision_gets_a_wide_margin(self):
        # A YEAR_ONLY candidate date resolving to slightly before the
        # naive earliest-plausible date (book 4 released 2021-07-01,
        # ~182-day cadence -> book 5 "earliest plausible" without a
        # margin would land ~2022-01-01) is absorbed by the 365-day
        # YEAR_ONLY margin, unlike the FULL-precision case above.
        candidate = {"published_date": "2022"}
        fingerprint = self._fingerprint()
        self.assertFalse(
            _cadence_flags_implausibly_early(
                candidate, 5.0, self._skeleton_with_cadence_history(), fingerprint
            )
        )

    def test_non_library_reference_entries_are_ignored(self):
        skeleton = [
            {"book_number": 4.0, "release_date": "2021-07-01", "source_class": "discovered"},
        ]
        candidate = {"published_date": "2021-08-01"}
        fingerprint = self._fingerprint()
        self.assertFalse(_cadence_flags_implausibly_early(candidate, 5.0, skeleton, fingerprint))

    def test_unparseable_candidate_date_never_flags(self):
        candidate = {"published_date": "not-a-date"}
        fingerprint = self._fingerprint()
        self.assertFalse(
            _cadence_flags_implausibly_early(
                candidate, 5.0, self._skeleton_with_cadence_history(), fingerprint
            )
        )


class NumberConfidenceCadenceIntegrationTest(unittest.TestCase):
    def test_implausibly_early_valid_new_number_downgrades_medium_to_low(self):
        skeleton_entries = [
            {"book_number": 1.0, "release_date": "2020-01-01", "source_class": "library"},
            {"book_number": 2.0, "release_date": "2020-07-01", "source_class": "library"},
            {"book_number": 3.0, "release_date": "2021-01-01", "source_class": "library"},
            {"book_number": 4.0, "release_date": "2021-07-01", "source_class": "library"},
        ]
        fingerprint = {
            "release_cadence": {"mean_interval_days": 182.0, "stddev_interval_days": 0.0, "interval_count": 3}
        }
        candidate = {"series_number": 5.0, "published_date": "2021-08-01"}
        skeleton_numbers = {1.0, 2.0, 3.0, 4.0}
        result = _number_confidence(candidate, skeleton_numbers, set(), skeleton_entries, fingerprint)
        self.assertEqual(result, "low")

    def test_without_fingerprint_same_candidate_stays_medium(self):
        skeleton_entries = [
            {"book_number": 4.0, "release_date": "2021-07-01", "source_class": "library"},
        ]
        candidate = {"series_number": 5.0, "published_date": "2021-08-01"}
        skeleton_numbers = {4.0}
        result = _number_confidence(candidate, skeleton_numbers, set(), skeleton_entries, None)
        self.assertEqual(result, "medium")


class ComputeConfidenceFingerprintEndToEndTest(unittest.TestCase):
    def test_fingerprint_none_matches_pre_feature_behavior(self):
        candidate = {
            "title": "Cherry Blossom Girls Book 4",
            "authors": ["Harmon Cooper"],
            "series_number": 4.0,
            "metadata_completeness_score": 0.9,
            "source_provenance": [{"source": "hardcover"}],
        }
        result = compute_confidence(
            series_id=1,
            skeleton_entries=[],
            provider_candidates=[candidate],
            delta={"malformed_books": []},
            series_name="Cherry Blossom Girls",
            series_author="Harmon Cooper",
            fingerprint=None,
        )
        scored = result["confidence"][0]
        self.assertEqual(scored["provider_confidence"], "high")

    def test_negative_provider_bias_flows_through_to_overall(self):
        candidate = {
            "title": "Cherry Blossom Girls Book 4",
            "authors": ["Harmon Cooper"],
            "series_number": 4.0,
            "metadata_completeness_score": 0.9,
            "source_provenance": [{"source": "hardcover"}],
        }
        fingerprint = {"provider_bias": {"hardcover": 0.6}}
        result = compute_confidence(
            series_id=1,
            skeleton_entries=[],
            provider_candidates=[candidate],
            delta={"malformed_books": []},
            series_name="Cherry Blossom Girls",
            series_author="Harmon Cooper",
            fingerprint=fingerprint,
        )
        scored = result["confidence"][0]
        self.assertEqual(scored["provider_confidence"], "medium")


if __name__ == "__main__":
    unittest.main()
