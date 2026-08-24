"""Executable decision table for confidence_engine._overall_confidence's
"unverified" combination rule -- see that function's docstring and
discovery_agentic_replacement_evaluation.md §4/§6/§8, which flagged the
original single-worked-example version of this rule as underspecified.
Each test below is one named cell of the finalized decision table plus
its acceptance/escalation/rejection classification.
"""

import unittest

import delta_engine
from confidence_engine import _overall_confidence, compute_confidence


class OverallConfidenceUnverifiedTableTest(unittest.TestCase):
    # "unverified" is title-only in practice (see confidence_engine's
    # module docstring: "Only title_confidence ever produces 'unverified'"),
    # so every case below pairs it against the other three dimensions
    # (provider/number/series-alignment confidence).

    def test_unverified_plus_high_caps_at_medium_not_high(self):
        # provider=high, title=unverified, number=high, alignment=high --
        # the exact "genuinely new book" shape from the module docstring.
        # unverified must never let this reach "high": nothing not already
        # owned can have a skeleton entry to compare against, so "high"
        # would be an unearned ceiling.
        self.assertEqual(
            _overall_confidence(["high", "unverified", "high", "high"]),
            "medium",
        )

    def test_unverified_plus_medium_is_medium(self):
        self.assertEqual(
            _overall_confidence(["high", "unverified", "medium", "high"]),
            "medium",
        )

    def test_unverified_plus_medium_and_high_mixed_is_medium(self):
        self.assertEqual(
            _overall_confidence(["medium", "unverified", "high", "medium"]),
            "medium",
        )

    def test_unverified_plus_low_is_low(self):
        # A genuine negative signal elsewhere (e.g. an unresolved/malformed
        # number) still pulls the whole thing down to "low" even though
        # title is merely unverified, not contradicted -- "unverified"
        # only relaxes the ceiling, it never raises the floor.
        self.assertEqual(
            _overall_confidence(["high", "unverified", "low", "high"]),
            "low",
        )

    def test_unverified_plus_low_in_any_position_is_low(self):
        for levels in (
            ["low", "unverified", "high", "high"],
            ["high", "unverified", "high", "low"],
        ):
            with self.subTest(levels=levels):
                self.assertEqual(_overall_confidence(levels), "low")

    def test_unverified_plus_zero_is_zero(self):
        # Documented per the review loop's explicit ask: this cell is
        # resolved by the pre-existing "any zero wins outright" rule, not
        # a dedicated unverified-specific branch -- listed here so the
        # table is complete, not because the logic needed to change.
        self.assertEqual(
            _overall_confidence(["high", "unverified", "zero", "high"]),
            "zero",
        )

    def test_unverified_plus_zero_in_any_position_is_zero(self):
        for levels in (
            ["zero", "unverified", "high", "high"],
            ["high", "unverified", "high", "zero"],
            ["low", "unverified", "zero", "high"],
        ):
            with self.subTest(levels=levels):
                self.assertEqual(_overall_confidence(levels), "zero")

    def test_unverified_alone_with_all_others_medium_is_medium(self):
        self.assertEqual(
            _overall_confidence(["medium", "unverified", "medium", "medium"]),
            "medium",
        )


class OverallConfidenceNonUnverifiedTableTest(unittest.TestCase):
    """The same total order, exercised without "unverified" in play at
    all -- confirms the rule generalizes rather than special-casing title.
    """

    def test_all_high_is_high(self):
        self.assertEqual(_overall_confidence(["high", "high", "high", "high"]), "high")

    def test_any_zero_wins_outright_regardless_of_position(self):
        for levels in (
            ["zero", "high", "high", "high"],
            ["high", "high", "zero", "high"],
            ["low", "medium", "high", "zero"],
        ):
            with self.subTest(levels=levels):
                self.assertEqual(_overall_confidence(levels), "zero")

    def test_all_medium_is_medium(self):
        self.assertEqual(_overall_confidence(["medium", "medium", "medium", "medium"]), "medium")

    def test_high_medium_mix_with_no_low_or_zero_is_medium(self):
        self.assertEqual(_overall_confidence(["high", "medium", "high", "medium"]), "medium")

    def test_any_low_with_no_zero_pulls_down_to_low(self):
        self.assertEqual(_overall_confidence(["high", "high", "low", "high"]), "low")

    def test_mixed_high_and_low_with_no_zero_is_low(self):
        self.assertEqual(_overall_confidence(["high", "low", "medium", "high"]), "low")


class InsufficientMetadataConfidenceTest(unittest.TestCase):
    """CR-8 regression: a candidate delta_engine.compute_series_delta
    already flagged malformed via "insufficient_metadata" must not still
    score confidently through compute_confidence. Not a full test suite
    for compute_confidence itself (that's TG-8/Wave 2's remit) -- scoped
    to the one bug CR-8 fixes.
    """

    def _candidate(self, **overrides):
        candidate = {
            "title": "Desert Protocol",
            "authors": ["Harmon Cooper"],
            "isbn13": None,
            "series_number": 7.0,
            "metadata_completeness_score": 0.2,  # below the 0.5 reconciliation threshold
            "source_provenance": [{"source": "hardcover"}],
        }
        candidate.update(overrides)
        return candidate

    def test_delta_flagged_insufficient_metadata_candidate_scores_zero_not_confidently(self):
        candidate = self._candidate()
        delta = delta_engine.compute_series_delta(
            series_id=1,
            skeleton_entries=[],
            provider_candidates=[candidate],
            series_name="Cherry Blossom Girls",
        )
        # Sanity: confirms this fixture actually trips insufficient_metadata
        # (and not some earlier _malformed_reason check) before asserting
        # anything about confidence_engine's handling of it.
        reasons = {entry["reason"] for entry in delta["malformed_books"]}
        self.assertIn("insufficient_metadata", reasons)

        result = compute_confidence(
            series_id=1,
            skeleton_entries=[],
            provider_candidates=[candidate],
            delta=delta,
            series_name="Cherry Blossom Girls",
            series_author="Harmon Cooper",
        )
        scored = result["confidence"][0]
        # CR-8 regression: before the fix, neither _title_confidence nor
        # _number_confidence tested delta_reasons for "insufficient_metadata"
        # at all, so this candidate scored "unverified" (title) / "medium"
        # (number) -> overall "medium" here despite delta already having
        # flagged it as malformed just one function call earlier.
        self.assertEqual(scored["title_confidence"], "zero")
        self.assertEqual(scored["overall"], "zero")

    def test_well_formed_candidate_with_sufficient_metadata_is_unaffected(self):
        candidate = self._candidate(metadata_completeness_score=0.9)
        delta = delta_engine.compute_series_delta(
            series_id=1,
            skeleton_entries=[],
            provider_candidates=[candidate],
            series_name="Cherry Blossom Girls",
        )
        reasons = {entry["reason"] for entry in delta["malformed_books"]}
        self.assertNotIn("insufficient_metadata", reasons)

        result = compute_confidence(
            series_id=1,
            skeleton_entries=[],
            provider_candidates=[candidate],
            delta=delta,
            series_name="Cherry Blossom Girls",
            series_author="Harmon Cooper",
        )
        scored = result["confidence"][0]
        self.assertNotEqual(scored["title_confidence"], "zero")
        self.assertNotEqual(scored["overall"], "zero")


if __name__ == "__main__":
    unittest.main()
