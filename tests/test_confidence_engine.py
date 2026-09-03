"""Executable decision table for confidence_engine._overall_confidence's
"unverified" combination rule -- see that function's docstring and
discovery_agentic_replacement_evaluation.md §4/§6/§8, which flagged the
original single-worked-example version of this rule as underspecified.
Each test below is one named cell of the finalized decision table plus
its acceptance/escalation/rejection classification.
"""

import unittest

import delta_engine
from confidence_engine import _overall_confidence, _provider_confidence, _series_alignment_confidence, compute_confidence


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


class CanonicalPageProviderConfidenceTierTest(unittest.TestCase):
    """Guided Discovery Option A confidence fix (2026-09-03, Jonathan
    Hunt/Goodreads live validation): candidates extracted from a user-
    designated canonical page must be tagged "canonical_page", not
    "web_search", or they're indistinguishable from generic search noise
    once graded here. This is the one lever the fix relies on -- see
    provider_io.fetch_canonical_page_candidates's docstring and
    confidence_engine._PROVIDER_CONFIDENCE's "canonical_page" entry for
    the full trace of why "low" vs "medium" here is what previously
    caused every real book on a canonical page to auto-reject via
    _overall_confidence's title="unverified" + any-other-dim="low" rule.
    """

    def _candidate(self, source):
        return {"source_provenance": [{"source": source}]}

    def test_canonical_page_grades_medium(self):
        self.assertEqual(_provider_confidence(self._candidate("canonical_page")), "medium")

    def test_plain_web_search_still_grades_low(self):
        # Regression guard: confirms the fix is additive (a new tag/tier),
        # not a change to web_search's own existing grade.
        self.assertEqual(_provider_confidence(self._candidate("web_search")), "low")

    def test_canonical_page_plus_unverified_title_clears_the_auto_reject_bar(self):
        # The exact real-world shape from the live validation test: a
        # brand-new series number (title_confidence="unverified", no
        # skeleton entry yet to corroborate against) alongside
        # number_confidence="medium"/series_alignment_confidence="high" --
        # before this fix, provider_confidence="low" (web_search) forced
        # overall down to "low" (auto-reject) per _overall_confidence's own
        # documented decision table, despite the other two dimensions
        # already being strong. With provider_confidence="medium"
        # (canonical_page), the same four dimensions now resolve to
        # "medium" -- accept/escalate, not auto-reject.
        self.assertEqual(
            _overall_confidence(["medium", "unverified", "medium", "high"]),
            "medium",
        )
        self.assertEqual(
            _overall_confidence(["low", "unverified", "medium", "high"]),
            "low",
        )


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


class PercyJacksonMissingVolumeIncidentTest(unittest.TestCase):
    """Percy Jackson incident (2026-08-25): after the catalog-sufficiency
    gate + fusion fixes correctly stopped calling Serper/Apify, "The Sea of
    Monsters" (book 2), "The Titan's Curse" (book 3), "The Battle of the
    Labyrinth" (book 4), and "The Last Olympian" (book 5) were still never
    surfaced as missing books to add -- every one of them scored an
    auto-dropped "low" overall confidence, purely because each has its own
    real graphic-novel adaptation (a distinct, legitimately-co-existing
    product with its own ISBN) sharing the same series number.
    delta_engine.compute_series_delta's old duplicate_number check flagged
    *both* the novel and its graphic-novel sibling as malformed just for
    sharing a number, which confidence_engine._number_confidence then
    graded "low" for both -- dragging the well-corroborated novel down
    with its unrelated sibling. This is the end-to-end (delta ->
    confidence) regression test for that fix: each of the four novels
    must now score "medium" or better (accept-worthy), not "low".
    """

    def _novel_and_graphic_novel(self, title, isbn13, graphic_isbn13, number):
        return [
            {
                "title": title,
                "authors": ["Rick Riordan"],
                "isbn13": isbn13,
                "series_number": number,
                "metadata_completeness_score": 0.9,
                "source_provenance": [{"source": "hardcover"}, {"source": "openlibrary"}],
            },
            {
                "title": f"{title}: The Graphic Novel",
                "authors": ["Rick Riordan", "Robert Venditti"],
                "isbn13": graphic_isbn13,
                "series_number": number,
                "metadata_completeness_score": 0.9,
                "source_provenance": [{"source": "hardcover"}],
            },
        ]

    def test_novel_sharing_a_number_with_its_own_graphic_novel_scores_medium_not_low(self):
        # No skeleton entries at all for numbers 2-5 -- these are
        # genuinely new-to-the-library volumes, exactly like the real
        # incident (only 1, 2.5, 6, 7 were ever owned).
        skeleton_entries = [{"book_number": 1.0, "title": "The Lightning Thief"}]

        for title, isbn13, graphic_isbn13, number in [
            ("The Sea of Monsters", "9780786290741", "9781423145509", 2.0),
            ("The Titan's Curse", "9782019109974", "9780141357751", 3.0),
            ("The Battle of the Labyrinth", "9789632454900", "9781484786390", 4.0),
            ("The Last Olympian", "9788804616672", "9781368046084", 5.0),
        ]:
            with self.subTest(title=title):
                candidates = self._novel_and_graphic_novel(title, isbn13, graphic_isbn13, number)
                delta = delta_engine.compute_series_delta(
                    series_id=344,
                    skeleton_entries=skeleton_entries,
                    provider_candidates=candidates,
                    series_name="Percy Jackson & The Olympians",
                )
                # Neither the novel nor its graphic-novel sibling should be
                # flagged malformed just for sharing a series number -- both
                # have their own distinct, real ISBN.
                self.assertEqual(delta["malformed_books"], [])

                result = compute_confidence(
                    series_id=344,
                    skeleton_entries=skeleton_entries,
                    provider_candidates=candidates,
                    delta=delta,
                    series_name="Percy Jackson & The Olympians",
                    series_author="Rick Riordan",
                )
                novel_scored = next(
                    entry for entry in result["confidence"] if entry["candidate"]["title"] == title
                )
                self.assertNotIn(novel_scored["overall"], ("low", "zero"))
                self.assertEqual(novel_scored["number_confidence"], "medium")


class DungeonDuelMiddleInitialIncidentTest(unittest.TestCase):
    """Dungeon Duel incident (2026-09-02): deleting the owned "Dungeon
    Duel" (The Rogue Dungeon, book 5) and running Check Now re-discovered
    it via catalog providers with a perfect confidence_score=1.00 fusion
    (hardcover + google_books both hit, exact title/number) -- but it was
    still auto-dropped before persistence because
    series_alignment_confidence scored "zero": the series' stored author
    is "James Hunter", while the catalog candidate's author came back as
    "James A. Hunter" (a real published middle initial). The two names
    were never actually in conflict -- see _given_names_are_initials_
    variant's own docstring for the fix -- so this is the end-to-end
    regression test that a plain middle-initial difference no longer
    reads as a confirmed author mismatch.
    """

    def test_middle_initial_addition_is_not_a_mismatch(self):
        self.assertEqual(
            _series_alignment_confidence(
                {"authors": ["James A. Hunter", "eden Hudson"]}, "James Hunter"
            ),
            "medium",
        )

    def test_middle_initial_addition_the_other_direction(self):
        self.assertEqual(
            _series_alignment_confidence(
                {"authors": ["James Hunter"]}, "James A. Hunter"
            ),
            "medium",
        )

    def test_genuinely_different_given_name_with_same_surname_still_zero(self):
        # Guards against the fix above being so loose it stops catching a
        # real mismatch -- "David Hunter" is not "James Hunter" just
        # because they share a surname (and, unlike "John" vs "James",
        # doesn't even share a first letter, so the pre-existing
        # abbreviation check can't paper over this assertion either).
        self.assertEqual(
            _series_alignment_confidence({"authors": ["David Hunter"]}, "James Hunter"),
            "zero",
        )

    def test_dungeon_duel_candidate_scores_medium_overall_not_zero(self):
        candidate = {
            "title": "Dungeon Duel",
            "authors": ["James A. Hunter", "eden Hudson"],
            "isbn13": "9798721060007",
            "series_number": 5.0,
            "metadata_completeness_score": 0.9,
            "source_provenance": [{"source": "hardcover"}, {"source": "google_books"}],
        }
        delta = delta_engine.compute_series_delta(
            series_id=138,
            skeleton_entries=[],
            provider_candidates=[candidate],
            series_name="The Rogue Dungeon",
        )
        self.assertEqual(delta["malformed_books"], [])

        result = compute_confidence(
            series_id=138,
            skeleton_entries=[],
            provider_candidates=[candidate],
            delta=delta,
            series_name="The Rogue Dungeon",
            series_author="James Hunter",
        )
        scored = result["confidence"][0]
        self.assertEqual(scored["series_alignment_confidence"], "medium")
        self.assertNotIn(scored["overall"], ("low", "zero"))


if __name__ == "__main__":
    unittest.main()
