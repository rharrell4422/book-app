import json
import logging
import os
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import crud
import discovery_engine
from agents.series_agent import SeriesIntelligenceAgent, discover_more_by_author, discover_series_by_name
from database import Base
from models import Book, Series


class DiscoveryEngineHelperTest(unittest.TestCase):
    """Unit tests for the pure text-normalization/matching helpers that the
    live API-based discovery pipeline depends on to identify which API
    results are new books versus ones already owned.
    """

    def test_core_title_key_matches_across_differently_formatted_titles(self):
        owned_style = "1% Lifesteal (Volume 4): A LitRPG: (1% Lifesteal Book 4)"
        api_style = "1% Lifesteal (Volume 4): A LitRPG Adventure"
        self.assertEqual(discovery_engine.core_title_key(owned_style), discovery_engine.core_title_key(api_style))

    def test_core_title_key_distinguishes_volumes_with_shared_prefix(self):
        # Regression: volume number lives inside the "(...)" segment for this
        # series, so truncating there (without folding the number back in)
        # collapsed every volume to the same key.
        key_4 = discovery_engine.core_title_key("1% Lifesteal (Volume 4): A LitRPG Adventure")
        key_5 = discovery_engine.core_title_key("1% Lifesteal (Volume 5): A LitRPG Adventure")
        self.assertNotEqual(key_4, key_5)

    def test_core_title_key_matches_across_comma_and_bare_subtitle_formats(self):
        # Regression: some providers separate a short-story subtitle with a
        # comma rather than a colon/paren (e.g. Hardcover's "..., A Series
        # Short Story"), while others return just the bare title.
        comma_style = "Havoc in the Deathyards, A Completionist Chronicles Short Story"
        bare_style = "Havoc in the Deathyards"
        self.assertEqual(discovery_engine.core_title_key(comma_style), discovery_engine.core_title_key(bare_style))
        self.assertEqual(discovery_engine.bare_title_key(comma_style), discovery_engine.bare_title_key(bare_style))

    def test_core_title_key_comma_split_still_distinguishes_numbered_siblings(self):
        # Some owned titles use a comma as part of the title itself (not a
        # subtitle separator), e.g. this series' convention. Splitting on
        # the first comma over-truncates the "core", but the book number
        # (parsed from the full raw title) still keeps siblings distinct.
        key_9 = discovery_engine.core_title_key("Webs of Power, The Grand Game, Book 9: A Dark Fantasy LitRPG")
        key_10 = discovery_engine.core_title_key("The Mad God, The Grand Game, Book 10: A Dark Fantasy LitRPG")
        self.assertNotEqual(key_9, key_10)

    def test_core_title_key_matches_across_dash_series_suffix_format(self):
        # Regression (live bug): Hardcover-style "<Title> - <Series> #<N>"
        # listings (e.g. "A Little Too Close - Madigan Mountain #2") never
        # matched the same book's cleaner "A Little Too Close" listing from
        # another source, so both survived as separate rows -- one properly
        # grouped under its series, one dumped in "standalone" as a
        # duplicate.
        dashed = "A Little Too Close - Madigan Mountain #2"
        clean = "A Little Too Close"
        self.assertEqual(discovery_engine.bare_title_key(dashed), discovery_engine.bare_title_key(clean))

    def test_title_core_segment_does_not_split_on_a_hyphenated_word(self):
        # The dash-suffix split requires spaces on both sides specifically
        # so it never fires on a hyphenated word inside the title itself.
        key = discovery_engine.core_title_key("Self-Made Superhero: Origins")
        self.assertIn("self made superhero", key)

    def test_core_title_key_matches_across_leading_article_variants(self):
        # Regression (live bug): "The Reality of Everything" (a cleanly
        # tagged Flight & Glory candidate) and "Reality of Everything -
        # Flight & Glory #5" (the same book from a dash-suffixed listing,
        # missing "The") didn't resolve to the same identity key.
        with_article = "The Reality of Everything"
        without_article = "Reality of Everything"
        self.assertEqual(discovery_engine.bare_title_key(with_article), discovery_engine.bare_title_key(without_article))

    def test_normalize_series_branding_name_keeps_leading_article(self):
        # The leading-article strip is scoped to book *titles* only -- series
        # names must keep "The" as part of their identity (this exact
        # expectation is asserted elsewhere too; re-asserted here as a
        # guardrail against accidentally generalizing the new title-only
        # article-stripping into normalize_text() itself).
        self.assertEqual(discovery_engine.normalize_series_branding_name("The Empyrean Series"), "the empyrean")

    def test_core_title_key_matches_across_ampersand_and_spelled_out_and(self):
        # Regression (live bug): "Muses & Melodies" (cleanly tagged Hush
        # Note candidate) and "Muses and Melodies - Hush Note #3" (same
        # book, different source) didn't resolve to the same identity key.
        ampersand_style = "Muses & Melodies"
        spelled_out = "Muses and Melodies"
        self.assertEqual(discovery_engine.bare_title_key(ampersand_style), discovery_engine.bare_title_key(spelled_out))

    def test_infer_series_hint_from_title_text_reads_dash_series_suffix(self):
        self.assertEqual(
            discovery_engine.infer_series_hint_from_title_text("A Little Too Close - Madigan Mountain #2"),
            "Madigan Mountain",
        )
        self.assertEqual(discovery_engine.infer_series_hint_from_title_text("Ignite - Legacy #0.7"), "Legacy")

    def test_infer_series_hint_from_title_text_ignores_a_plain_hyphenated_title(self):
        self.assertIsNone(discovery_engine.infer_series_hint_from_title_text("Self-Made Superhero"))

    def test_clean_display_title_strips_the_dash_series_suffix(self):
        self.assertEqual(
            discovery_engine.clean_display_title("A Little Too Close - Madigan Mountain #2"),
            "A Little Too Close",
        )

    def test_clean_display_title_leaves_an_ordinary_title_unchanged(self):
        self.assertEqual(discovery_engine.clean_display_title("Fourth Wing"), "Fourth Wing")

    def test_infer_number_from_title_recognizes_common_patterns(self):
        self.assertEqual(discovery_engine.infer_number_from_title("Cherry Blossom Girls Book 7"), 7)
        self.assertEqual(discovery_engine.infer_number_from_title("Cherry Blossom Girls Volume 7"), 7)
        self.assertEqual(discovery_engine.infer_number_from_title("Cherry Blossom Girls #7"), 7)
        self.assertEqual(discovery_engine.infer_number_from_title("Cherry Blossom Girls Book Seven"), 7)

    def test_infer_number_from_title_recognizes_bare_trailing_number(self):
        # Many rapid-release indie/LitRPG series just number titles as
        # "<Series Name> <N>" with no "book"/"vol"/"#" keyword at all.
        self.assertEqual(discovery_engine.infer_number_from_title("All the Skills 5", "All The Skills"), 5)

    def test_infer_number_from_title_recognizes_series_name_and_number_mid_title(self):
        # Regression: a reprint listing titled "By Schism Rent Asunder
        # (Safehold 2) Publisher: Tor Science Fiction; Reprint edition"
        # embeds "<series name> <N>" as a parenthetical partway through the
        # title rather than as a prefix, so the bare-trailing-number check
        # (which only looks at the very start of the title) missed it,
        # leaving this reprint of an already-owned book with no resolvable
        # number at all.
        self.assertEqual(
            discovery_engine.infer_number_from_title(
                "By Schism Rent Asunder (Safehold 2) Publisher: Tor Science Fiction; Reprint edition",
                "Safehold",
            ),
            2,
        )

    def test_infer_number_from_title_preserves_fractional_positions(self):
        # Regression coverage for the Phase 4 number-inference unification
        # (see project design chat): this used to truncate a companion/
        # novella's fractional position to its integer part, colliding its
        # identity with the numbered entry beside it (see
        # services/identity.py's fractional-collision docstring). Now
        # preserved as a genuine float for "#N.N", "book N.N", "volume N.N"
        # and "vol N.N" forms.
        self.assertEqual(discovery_engine.infer_number_from_title("Threshing Day (Empyrean Book 3.5)"), 3.5)
        self.assertEqual(discovery_engine.infer_number_from_title("Ignite - Legacy #0.7"), 0.7)
        self.assertEqual(discovery_engine.infer_number_from_title("Some Title Volume 2.5"), 2.5)
        self.assertEqual(discovery_engine.infer_number_from_title("Some Title Vol. 2.5"), 2.5)

    def test_infer_number_from_title_still_returns_whole_numbers_for_plain_titles(self):
        # A whole-number match must come back as a value that compares
        # equal to (and formats identically to) a plain int -- core_title_key
        # depends on this not silently growing a ".0" suffix.
        result = discovery_engine.infer_number_from_title("Cherry Blossom Girls Book 7")
        self.assertEqual(result, 7)
        self.assertEqual(int(result), 7)

    def test_infer_number_from_title_fractional_form_survives_a_colon_before_the_number(self):
        # "Book: 3.5" -- normalize_text would collapse the colon to a space
        # exactly like "Book 3.5", but the naive fix of matching the
        # fractional pattern against *raw* text (skipping normalize_text
        # entirely) would break on exactly this input. Covers
        # _normalize_number_context's decimal-preserving normalization pass.
        self.assertEqual(discovery_engine.infer_number_from_title("Some Series Book: 3.5"), 3.5)

    def test_core_title_key_stays_byte_identical_for_a_representative_whole_number_corpus(self):
        # Pins core_title_key's exact output for a representative corpus of
        # whole-number titles from this suite's own regression history, so
        # a future change to infer_number_from_title can't silently alter a
        # discovery matching key that this function's docstring explicitly
        # promises to keep stable.
        expectations = {
            "1% Lifesteal (Volume 4): A LitRPG: (1% Lifesteal Book 4)": "1 lifesteal 4",
            # No ":"/","/"("/" - " separator to truncate the core segment on,
            # so the "book 7" phrase survives inside normalized_core *and*
            # the folded-in number gets appended again -- a pre-existing
            # quirk of this function unrelated to number-inference
            # unification, pinned here as-is rather than "fixed", since
            # fixing it is out of scope for this regression guard.
            "Cherry Blossom Girls Book 7": "cherry blossom girls book 7 7",
            "Webs of Power, The Grand Game, Book 9: A Dark Fantasy LitRPG": "webs of power 9",
            "The Mad God, The Grand Game, Book 10: A Dark Fantasy LitRPG": "mad god 10",
            "Fourth Wing": "fourth wing",
        }
        for title, expected_key in expectations.items():
            self.assertEqual(discovery_engine.core_title_key(title), expected_key)

    def test_core_title_key_truncates_a_fractional_title_to_its_integer_part(self):
        # core_title_key intentionally does NOT gain fractional precision
        # even though infer_number_from_title now supports it -- see both
        # functions' docstrings for why (discovery matching key stability;
        # the fractional-collision fix lives at the persistence identity
        # layer instead, in services/identity.py).
        key = discovery_engine.core_title_key("Threshing Day (Empyrean Book 3.5)")
        self.assertTrue(key.endswith(" 3"))
        self.assertNotIn(".", key)

    def test_looks_like_non_new_release_filters_bundles_and_editions(self):
        self.assertTrue(discovery_engine.looks_like_non_new_release("Cherry Blossom Girls Books 1-3 Box Set"))
        self.assertTrue(discovery_engine.looks_like_non_new_release("Cherry Blossom Girls: French Edition"))
        self.assertFalse(discovery_engine.looks_like_non_new_release("Cherry Blossom Girls Book 7"))

    def test_looks_like_non_new_release_does_not_flag_a_new_short_story_collection(self):
        # Regression (live bug): "Threshing Day (Wing and Claw Collection)"
        # is a real, brand-new September 2026 Empyrean release (a themed
        # collection of new short stories), not a repackaging of
        # already-published books -- but the bare "collection" marker used
        # to reject it outright, so "Check for New" silently found zero
        # candidates for it on every run.
        self.assertFalse(
            discovery_engine.looks_like_non_new_release("Threshing Day (Wing and Claw Collection)")
        )

    def test_looks_like_non_new_release_filters_series_volume_compilations(self):
        # Regression (live bug): "Safehold Series, Volume I" and "The
        # Safehold Series, Volume I: Off Armageddon Reef, ..." are both a
        # common indie/legacy-publisher compilation-listing naming
        # convention, distinct from a standalone "Volume 7" entry.
        self.assertTrue(discovery_engine.looks_like_non_new_release("Safehold Series, Volume I"))
        self.assertTrue(
            discovery_engine.looks_like_non_new_release(
                "The Safehold Series, Volume I: Off Armageddon Reef, By Schism Rent Asunder"
            )
        )
        self.assertFalse(discovery_engine.looks_like_non_new_release("Safehold Volume 7"))

    def test_parse_flexible_date_handles_partial_precision(self):
        self.assertEqual(discovery_engine.parse_flexible_date("2024-03-12"), date(2024, 3, 12))
        self.assertEqual(discovery_engine.parse_flexible_date("2024-03"), date(2024, 3, 1))
        self.assertEqual(discovery_engine.parse_flexible_date("2024"), date(2024, 1, 1))
        self.assertIsNone(discovery_engine.parse_flexible_date(""))

    def test_classify_upcoming_uses_date_when_available(self):
        past = date(2020, 1, 1)
        future = date(date.today().year + 5, 1, 1)
        self.assertFalse(discovery_engine.classify_upcoming(past, upcoming_hint=True))
        self.assertTrue(discovery_engine.classify_upcoming(future, upcoming_hint=False))

    def test_classify_upcoming_falls_back_to_hint_without_a_date(self):
        self.assertTrue(discovery_engine.classify_upcoming(None, upcoming_hint=True))
        self.assertFalse(discovery_engine.classify_upcoming(None, upcoming_hint=False))
        self.assertFalse(discovery_engine.classify_upcoming(None, upcoming_hint=None))

    def test_looks_like_placeholder_title_filters_unconfirmed_future_titles(self):
        # Regression (live bug): a fan wiki/forum mention of an unannounced
        # future book got structured by the web-search LLM pass into a
        # candidate literally titled "Untitled".
        self.assertTrue(discovery_engine.looks_like_placeholder_title("Untitled"))
        self.assertTrue(discovery_engine.looks_like_placeholder_title("Untitled: (The Empyrean Book 5)"))
        self.assertTrue(discovery_engine.looks_like_placeholder_title("TBD"))
        self.assertTrue(discovery_engine.looks_like_placeholder_title("Coming Soon"))
        self.assertFalse(discovery_engine.looks_like_placeholder_title("Threshing Day"))

    def test_looks_like_series_index_entry_filters_bare_series_name_listings(self):
        # Regression (live bug): "Check Now" on "The Empyrean" surfaced two
        # extra candidates literally titled "The Empyrean" and "The Empyrean
        # Series" -- catalog listings for the series itself (an aggregation
        # page/boxed-set entity), not any single book -- alongside the real
        # numbered books.
        self.assertTrue(
            discovery_engine.looks_like_series_index_entry("The Empyrean", "The Empyrean", isbn13=None, has_number_hint=False)
        )
        self.assertTrue(
            discovery_engine.looks_like_series_index_entry(
                "The Empyrean Series", "The Empyrean", isbn13=None, has_number_hint=False
            )
        )
        # Other generic, non-book suffixes cataloguers tack onto a bare
        # series name for a series-level (not book-level) listing.
        self.assertTrue(
            discovery_engine.looks_like_series_index_entry(
                "The Empyrean Universe", "The Empyrean", isbn13=None, has_number_hint=False
            )
        )
        self.assertTrue(
            discovery_engine.looks_like_series_index_entry(
                "The Empyrean Collection", "The Empyrean", isbn13=None, has_number_hint=False
            )
        )
        # A real, individually-cataloged book carries an ISBN or a resolved
        # series position, even if (rare) its title happens to equal the
        # bare series name -- e.g. an eponymous book 1.
        self.assertFalse(
            discovery_engine.looks_like_series_index_entry("The Empyrean", "The Empyrean", isbn13="9781234567897", has_number_hint=False)
        )
        self.assertFalse(
            discovery_engine.looks_like_series_index_entry("The Empyrean", "The Empyrean", isbn13=None, has_number_hint=True)
        )
        self.assertFalse(
            discovery_engine.looks_like_series_index_entry(
                "Fourth Wing: (The Empyrean Book 1)", "The Empyrean", isbn13=None, has_number_hint=True
            )
        )

    def test_looks_like_series_index_entry_ignores_a_leading_article_mismatch(self):
        # Regression (live bug): a profile's series was auto-created from an
        # imported spreadsheet's bare series column value ("Empyrean", no
        # "The"), while Google Books' own bare-series-listing records used
        # the full name with its article ("The Empyrean" / "The Empyrean
        # Series"). The exact-text comparison never matched, so both stub
        # listings sailed through as if they were new, unread books --
        # exactly the pattern this whole check exists to catch, just missed
        # over one word.
        self.assertTrue(
            discovery_engine.looks_like_series_index_entry("The Empyrean", "Empyrean", isbn13=None, has_number_hint=False)
        )
        self.assertTrue(
            discovery_engine.looks_like_series_index_entry(
                "The Empyrean Series", "Empyrean", isbn13=None, has_number_hint=False
            )
        )
        # Same mismatch in the other direction (tracked series carries the
        # article, candidate's title doesn't).
        self.assertTrue(
            discovery_engine.looks_like_series_index_entry("Empyrean", "The Empyrean", isbn13=None, has_number_hint=False)
        )
        # A real, individually-cataloged book still isn't caught just
        # because it happens to share a root word with the series name.
        self.assertFalse(
            discovery_engine.looks_like_series_index_entry(
                "The Empyrean Rises", "Empyrean", isbn13=None, has_number_hint=False
            )
        )

    def test_title_is_series_variant_rejects_bare_genre_tagline_regardless_of_series_wording(self):
        # Regression (live bug): "Check Now" on Georgia Wagner's "Jonathan
        # Hunt Thriller Series" admitted "A Jonathan Hunt Thriller" -- no
        # ISBN, no real subtitle -- as a new book. An earlier fix only
        # caught this when the tracked series name itself already
        # contained "Thriller" (so the word cancelled out against the
        # series name); it recurred once the series was tracked under the
        # shorter name "Jonathan Hunt", where "thriller" no longer overlaps
        # the series name and was treated as real, distinguishing content.
        self.assertTrue(
            discovery_engine._title_is_series_variant(
                "A Jonathan Hunt Thriller", "Jonathan Hunt", isbn13=None, structured_number_hint=None
            )
        )
        # Still caught under the longer tracked name too (already worked,
        # must keep working).
        self.assertTrue(
            discovery_engine._title_is_series_variant(
                "A Jonathan Hunt Thriller", "Jonathan Hunt Thriller Series", isbn13=None, structured_number_hint=None
            )
        )
        # Other bare genre taglines follow the same idiom.
        self.assertTrue(
            discovery_engine._title_is_series_variant(
                "A Jonathan Hunt Mystery", "Jonathan Hunt", isbn13=None, structured_number_hint=None
            )
        )
        # An ISBN is still strong enough evidence on its own to short-circuit
        # this entirely, tagline or not.
        self.assertFalse(
            discovery_engine._title_is_series_variant(
                "A Jonathan Hunt Thriller", "Jonathan Hunt", isbn13="9781234567897", structured_number_hint=None
            )
        )
        # A genre word alongside genuine additional content is left alone --
        # only a *lone* bare genre word is treated as filler.
        self.assertFalse(
            discovery_engine._title_is_series_variant(
                "A Jonathan Hunt Thriller: The Reckoning", "Jonathan Hunt", isbn13=None, structured_number_hint=None
            )
        )
        # A real, distinctly-titled book unrelated to the tagline idiom is
        # never caught just because it shares the series name.
        self.assertFalse(
            discovery_engine._title_is_series_variant(
                "The Jericho Siege", "Jonathan Hunt", isbn13=None, structured_number_hint=None
            )
        )

    def test_normalize_series_branding_name_strips_generic_words(self):
        # Regression (live bug): an author-wide discovery pass guessed
        # series name "Duchy of Terra Universe" for a book already owned
        # under the tracked series "Duchy of Terra" -- an exact-text
        # comparison missed that single extra word and reported the book
        # "not yet tracked" even though it was.
        self.assertEqual(discovery_engine.normalize_series_branding_name("Duchy of Terra Universe"), "duchy of terra")
        self.assertEqual(discovery_engine.normalize_series_branding_name("Duchy of Terra"), "duchy of terra")
        self.assertEqual(discovery_engine.normalize_series_branding_name("The Empyrean Series"), "the empyrean")
        self.assertEqual(discovery_engine.normalize_series_branding_name("The Empyrean Collection"), "the empyrean")

    def test_normalize_series_branding_name_does_not_strip_distinctive_qualifiers(self):
        # These are real, distinct sub-series/rebranded editions, not a
        # cataloguer's generic suffix -- collapsing them to their parent
        # series name would reintroduce the exact cross-series
        # contamination bug fixed for "Starship's Mage: Red Falcon".
        self.assertNotEqual(
            discovery_engine.normalize_series_branding_name("Starship's Mage: Red Falcon"),
            discovery_engine.normalize_series_branding_name("Starship's Mage"),
        )
        self.assertNotEqual(
            discovery_engine.normalize_series_branding_name("Starship's Mage: UnArcana Rebellion"),
            discovery_engine.normalize_series_branding_name("Starship's Mage"),
        )

    def test_looks_like_placeholder_date_flags_literal_january_first(self):
        # Regression (live bug): an author-wide discovery pass showed
        # several standalone titles with dates like 1/1/1900 and 1/1/2017
        # (the same 1/1/2017 on three unrelated titles) -- a common
        # "year-only precision" stand-in some catalogs use, displayed with
        # the same confidence as a genuinely-dated release.
        self.assertTrue(discovery_engine.looks_like_placeholder_date("2017-01-01"))
        self.assertTrue(discovery_engine.looks_like_placeholder_date("1900-01-01"))
        self.assertFalse(discovery_engine.looks_like_placeholder_date("2017-01-02"))
        self.assertFalse(discovery_engine.looks_like_placeholder_date("2024-03-12"))
        self.assertFalse(discovery_engine.looks_like_placeholder_date(None))
        self.assertFalse(discovery_engine.looks_like_placeholder_date(""))

    def test_infer_series_hint_from_title_text_reads_universe_novella_pattern(self):
        # Regression (live bug): Google Books/OpenLibrary never carry a
        # structured series field, so "Fae, Flames & Fedoras: A Changeling
        # Blood Universe Novella" showed up as an unlabeled standalone even
        # though its own subtitle names the series it belongs to.
        self.assertEqual(
            discovery_engine.infer_series_hint_from_title_text(
                "Fae, Flames & Fedoras: A Changeling Blood Universe Novella"
            ),
            "Changeling Blood",
        )
        self.assertEqual(
            discovery_engine.infer_series_hint_from_title_text("Some Story: A Duchy of Terra Series Short Story"),
            "Duchy of Terra",
        )

    def test_infer_series_hint_from_title_text_returns_none_when_no_marker(self):
        self.assertIsNone(discovery_engine.infer_series_hint_from_title_text("Refuge"))
        self.assertIsNone(discovery_engine.infer_series_hint_from_title_text("Exile: A Space Opera"))
        self.assertIsNone(discovery_engine.infer_series_hint_from_title_text(None))

    # --- Phase 4 pure helpers (shadow-mode diagnostics) ---

    def test_integral_or_none_rejects_fractional_without_truncating(self):
        # The whole reason this exists next to _to_int_or_none: a 3.5
        # novella must not be counted as volume 3.
        self.assertEqual(discovery_engine._to_int_or_none(3.5), 3)
        self.assertIsNone(discovery_engine._integral_or_none(3.5))

    def test_integral_or_none_accepts_integral_values_of_every_type(self):
        for value, expected in [(3, 3), (3.0, 3), ("3", 3), ("3.0", 3), (0, 0), (-2, -2)]:
            with self.subTest(value=value):
                self.assertEqual(discovery_engine._integral_or_none(value), expected)

    def test_integral_or_none_returns_none_for_unusable_values(self):
        for value in [None, "", "Book Seven", [], {}, float("nan"), float("inf")]:
            with self.subTest(value=value):
                self.assertIsNone(discovery_engine._integral_or_none(value))

    def test_external_missing_vs_owned_subtracts_only_owned_books(self):
        owned = [{"book_number": float(n)} for n in [1, 2, 3, 4, 5, 6, 8, 9]]
        self.assertEqual(
            discovery_engine.compute_external_missing_vs_owned(10, owned),
            [7, 10],
        )

    def test_external_missing_vs_owned_ignores_null_and_fractional_numbers(self):
        owned = [{"book_number": 1.0}, {"book_number": None}, {"book_number": 2.5}]
        self.assertEqual(discovery_engine.compute_external_missing_vs_owned(3, owned), [2, 3])

    def test_external_missing_vs_owned_returns_empty_without_a_usable_total(self):
        owned = [{"book_number": 1.0}]
        self.assertEqual(discovery_engine.compute_external_missing_vs_owned(None, owned), [])
        self.assertEqual(discovery_engine.compute_external_missing_vs_owned(0, owned), [])
        self.assertEqual(discovery_engine.compute_external_missing_vs_owned(-3, owned), [])

    def test_external_gap_ratio_is_none_without_a_usable_total(self):
        # None, not 0.0 -- "no external data" must stay distinguishable
        # from "owns every volume".
        self.assertIsNone(discovery_engine.compute_external_gap_ratio(None, []))
        self.assertIsNone(discovery_engine.compute_external_gap_ratio(0, []))

    def test_external_gap_ratio_rounds_to_four_places(self):
        self.assertEqual(discovery_engine.compute_external_gap_ratio(3, [2]), 0.3333)
        self.assertEqual(discovery_engine.compute_external_gap_ratio(10, [7, 10]), 0.2)
        self.assertEqual(discovery_engine.compute_external_gap_ratio(10, []), 0.0)

    def test_owned_number_coverage_counts_only_integral_numbers(self):
        owned = [{"book_number": 1.0}, {"book_number": None}, {"book_number": 2.5}, {"book_number": 3}]
        self.assertEqual(
            discovery_engine.compute_owned_number_coverage(owned),
            {"owned_books_total": 4, "owned_books_with_numbers": 2},
        )

    def test_inferred_number_prefers_the_hint_then_falls_back_to_the_title(self):
        self.assertEqual(
            discovery_engine.compute_inferred_number(
                {"title": "Cherry Blossom Girls 4", "series_number_hint": 7}, "Cherry Blossom Girls"
            ),
            7,
        )
        # The bare "<Series Name> <N>" pattern only resolves when the series
        # name is passed through.
        self.assertEqual(
            discovery_engine.compute_inferred_number({"title": "Cherry Blossom Girls 4"}, "Cherry Blossom Girls"),
            4,
        )
        self.assertIsNone(discovery_engine.compute_inferred_number({"title": "Unmapped"}, "Cherry Blossom Girls"))

    def test_new_volume_flags_marks_only_unowned_expected_numbers(self):
        candidates = [
            {"title": "Book 7", "series_number_hint": 7, "isbn13": "9780000000007"},
            {"title": "Book 2", "series_number_hint": 2},
            {"title": "Novella", "series_number_hint": 3.5},
            {"title": "Unmapped"},
        ]
        flags = discovery_engine.compute_new_volume_flags(
            candidates, "Cherry Blossom Girls", [7, 10], belongs_indices={0, 1, 2}, known_indices={1}
        )

        self.assertEqual([flag["is_new_volume"] for flag in flags], [True, False, False, False])
        self.assertEqual([flag["belongs_to_series"] for flag in flags], [True, True, True, False])
        self.assertEqual([flag["suppressed_as_known"] for flag in flags], [False, True, False, False])
        self.assertEqual(flags[0]["isbn13"], "9780000000007")
        self.assertIsNone(flags[1]["isbn13"])
        # The fractional number survives verbatim even though it can never
        # be flagged as a new volume.
        self.assertEqual(flags[2]["series_number"], 3.5)
        self.assertIsNone(flags[3]["series_number"])

    def test_drop_explanations_flatten_nested_and_missing_identities(self):
        diagnostics = [
            {
                "stage": "cross_series_filter",
                "reason": "series_name_mismatch",
                "candidate_identity": {"title": "Other Series 1", "isbn13": "9780000000001", "series_number": 1},
            },
            {"stage": "web_structuring", "reason": "json_parse_failure", "candidate_identity": None},
            {"stage": "mystery_stage", "reason": "mystery_reason", "candidate_identity": {}},
        ]
        explained = discovery_engine.compute_drop_explanations(diagnostics)
        first, second, third = explained["drop_explanations"]

        self.assertEqual(first["title"], "Other Series 1")
        self.assertEqual(first["isbn13"], "9780000000001")
        self.assertEqual(first["series_number"], 1)
        self.assertEqual(
            first["explanation"], "Candidate dropped because its series name did not match the target series."
        )
        self.assertIsNone(second["title"])
        self.assertEqual(
            second["explanation"],
            "Provider returned unstructured or invalid JSON; the entire structuring pass was discarded.",
        )
        self.assertEqual(third["explanation"], "Candidate dropped for an unclassified reason.")

    def test_drop_explanations_cap_the_list_but_not_the_counts(self):
        diagnostics = [
            {"stage": "already_known", "reason": "suppressed_as_known", "candidate_identity": {"title": f"Book {n}"}}
            for n in range(discovery_engine.MAX_DROP_EXPLANATIONS + 5)
        ]
        diagnostics.append({"stage": "llm_reconciliation", "reason": "excluded_by_llm", "candidate_identity": None})
        explained = discovery_engine.compute_drop_explanations(diagnostics)

        self.assertEqual(len(explained["drop_explanations"]), discovery_engine.MAX_DROP_EXPLANATIONS)
        self.assertEqual(explained["drop_explanations_total"], discovery_engine.MAX_DROP_EXPLANATIONS + 6)
        self.assertEqual(
            explained["drop_explanation_counts"],
            {
                "already_known:suppressed_as_known": discovery_engine.MAX_DROP_EXPLANATIONS + 5,
                "llm_reconciliation:excluded_by_llm": 1,
            },
        )
        self.assertEqual(explained["drop_explanations"][0]["title"], "Book 0")

    def test_drop_explanations_handle_an_empty_diagnostic_list(self):
        self.assertEqual(
            discovery_engine.compute_drop_explanations([]),
            {"drop_explanations": [], "drop_explanations_total": 0, "drop_explanation_counts": {}},
        )


def _mock_anthropic_client(response_text):
    mock_text_block = MagicMock()
    mock_text_block.type = "text"
    mock_text_block.text = response_text
    mock_message = MagicMock()
    mock_message.content = [mock_text_block]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message
    return mock_client


class GenerateSeriesOverviewTest(unittest.TestCase):
    """generate_series_overview is only ever called on-demand (a "Series
    Overview" button click in the frontend), never during discovery itself
    -- these tests cover the function in isolation, not that call-site
    contract.
    """

    def test_returns_none_without_anthropic_api_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            result = discovery_engine.generate_series_overview(
                "Exile", "Glynn Stewart", [{"title": "Exile", "description": "A shackled Earth..."}]
            )
        self.assertIsNone(result)

    def test_returns_none_when_no_book_has_a_description(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            result = discovery_engine.generate_series_overview(
                "Exile", "Glynn Stewart", [{"title": "Exile", "description": None}, {"title": "Refuge"}]
            )
        self.assertIsNone(result)

    def test_returns_llm_generated_overview_text(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=_mock_anthropic_client("A space opera trilogy about exiled rebels.")
        ):
            result = discovery_engine.generate_series_overview(
                "Exile",
                "Glynn Stewart",
                [{"title": "Exile", "description": "A shackled Earth, ruled by an unstoppable tyrant..."}],
            )
        self.assertEqual(result, "A space opera trilogy about exiled rebels.")

    def test_only_passes_books_with_descriptions_to_the_prompt(self):
        captured = {}

        def fake_anthropic(api_key):
            client = _mock_anthropic_client("An overview.")

            def capture_create(**kwargs):
                captured["prompt"] = kwargs["messages"][0]["content"]
                return client.messages.create.return_value

            client.messages.create.side_effect = capture_create
            return client

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch("anthropic.Anthropic", side_effect=fake_anthropic):
            discovery_engine.generate_series_overview(
                "Exile",
                "Glynn Stewart",
                [
                    {"title": "Exile", "description": "Book one premise text."},
                    {"title": "Untitled Draft", "description": None},
                ],
            )

        self.assertIn("Book one premise text.", captured["prompt"])
        self.assertNotIn("Untitled Draft", captured["prompt"])


class DiscoverCandidatesForSeriesTest(unittest.TestCase):
    """Tests discovery_engine.discover_candidates_for_series's merge/priority
    behavior across the catalog providers, with all network calls mocked
    out so this runs offline and deterministically. The web-search provider
    is disabled here (via cleared env vars) since it's exercised on its own
    in WebSearchProviderTest below -- these tests only care about the three
    original catalog APIs.
    """

    def setUp(self):
        self._env_patch = patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "", "ANTHROPIC_API_KEY": ""})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_hardcover_result_wins_over_google_on_same_book(self):
        # Hardcover tags each hit with its actual series position and
        # release status, which is more trustworthy than Google's free-text
        # match for indie/self-published titles -- so when both providers
        # return the same book, Hardcover's copy (with its number hint)
        # should be the one that survives the merge.
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {
                    "source": "hardcover",
                    "source_id": "hc-1",
                    "title": "Cherry Blossom Girls Book 7",
                    "authors": ["Harmon Cooper"],
                    "published_date": "2024-02-20",
                    "isbn13": None,
                    "source_url": None,
                    "language": "",
                    "series_number_hint": 7,
                    "upcoming_hint": False,
                }
            ],
        ), patch.object(
            discovery_engine,
            "_fetch_google_books",
            return_value=[
                {
                    "source": "google_books",
                    "source_id": "gb-1",
                    "title": "Cherry Blossom Girls Book 7",
                    "authors": ["Harmon Cooper"],
                    "published_date": "2024-02-20",
                    "isbn13": None,
                    "source_url": None,
                    "language": "",
                }
            ],
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            result = discovery_engine.discover_candidates_for_series("Cherry Blossom Girls", "Harmon Cooper")

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["source"], "hardcover")
        self.assertEqual(result["candidates"][0]["series_number_hint"], 7)

    def test_excludes_titles_already_owned(self):
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {
                    "source": "hardcover",
                    "source_id": "hc-1",
                    "title": "Cherry Blossom Girls Book 7",
                    "authors": ["Harmon Cooper"],
                    "published_date": "2024-02-20",
                    "isbn13": None,
                    "source_url": None,
                    "language": "",
                    "series_number_hint": 7,
                    "upcoming_hint": False,
                }
            ],
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ):
            owned_key = discovery_engine.core_title_key("Cherry Blossom Girls Book 7")
            result = discovery_engine.discover_candidates_for_series(
                "Cherry Blossom Girls", "Harmon Cooper", exclude_title_keys={owned_key}
            )

        self.assertEqual(result["candidates"], [])

    def test_surfaces_a_companion_collection_with_fractional_series_position(self):
        # Regression (live bug), end-to-end through discover_candidates_for_series:
        # Rebecca Yarros's "Threshing Day (Wing and Claw Collection)" is a
        # real September 2026 Empyrean release at Hardcover series position
        # 3.5 -- it used to be silently dropped by looks_like_non_new_release
        # (bare "collection" marker), so "Check for New" found nothing at
        # all for it on every run.
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {
                    "source": "hardcover",
                    "source_id": "hc-threshing-day",
                    "title": "Threshing Day (Wing and Claw Collection)",
                    "authors": ["Rebecca Yarros"],
                    "published_date": "2026-09-29",
                    "isbn13": "9781682818084",
                    "source_url": None,
                    "language": "",
                    "series_number_hint": 3.5,
                    "upcoming_hint": True,
                    "series_name_hint": "The Empyrean",
                }
            ],
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ):
            result = discovery_engine.discover_candidates_for_series("The Empyrean", "Rebecca Yarros")

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["series_number_hint"], 3.5)

    def test_excludes_bare_series_name_listing_with_no_number_or_isbn(self):
        # Regression (live bug): Google Books/OpenLibrary both returned a
        # record for "The Empyrean" series itself (no book number, no ISBN)
        # alongside the real numbered books, which slipped through as a new
        # "available" entry.
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]), patch.object(
            discovery_engine,
            "_fetch_google_books",
            return_value=[
                {
                    "source": "google_books",
                    "source_id": "gb-series",
                    "title": "The Empyrean",
                    "authors": ["Rebecca Yarros"],
                    "published_date": "2023",
                    "isbn13": None,
                    "source_url": None,
                    "language": "",
                }
            ],
        ), patch.object(
            discovery_engine,
            "_fetch_openlibrary",
            return_value=[
                {
                    "source": "openlibrary",
                    "source_id": "ol-series",
                    "title": "The Empyrean Series",
                    "authors": ["Rebecca Yarros"],
                    "published_date": "2023",
                    "isbn13": None,
                    "source_url": None,
                    "language": "",
                }
            ],
        ):
            result = discovery_engine.discover_candidates_for_series(
                "The Empyrean", "Rebecca Yarros", allow_author_fallback=False
            )

        self.assertEqual(result["candidates"], [])

    def test_all_providers_failing_is_reported_distinctly_from_no_results(self):
        with patch.object(discovery_engine, "_fetch_hardcover", side_effect=RuntimeError("boom")), patch.object(
            discovery_engine, "_fetch_google_books", side_effect=RuntimeError("boom")
        ), patch.object(discovery_engine, "_fetch_openlibrary", side_effect=RuntimeError("boom")):
            result = discovery_engine.discover_candidates_for_series(
                "Cherry Blossom Girls", "Harmon Cooper", allow_author_fallback=False
            )

        self.assertTrue(result["all_providers_failed"])
        self.assertEqual(len(result["provider_failures"]), 3)

    def test_partial_provider_failure_is_not_all_providers_failed(self):
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]), patch.object(
            discovery_engine, "_fetch_google_books", side_effect=RuntimeError("503")
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            result = discovery_engine.discover_candidates_for_series(
                "Cherry Blossom Girls", "Harmon Cooper", allow_author_fallback=False
            )

        self.assertFalse(result["all_providers_failed"])
        self.assertEqual(len(result["provider_failures"]), 1)


class DiscoverCandidatesForAuthorTest(unittest.TestCase):
    """Tests discovery_engine.discover_candidates_for_author -- the lighter,
    non-series-scoped sibling used by "More by this author". Network calls
    mocked out for determinism.
    """

    def setUp(self):
        self._env_patch = patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "", "ANTHROPIC_API_KEY": ""})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_queries_bare_author_bibliography_on_all_catalog_apis(self):
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]) as mock_hardcover, patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ) as mock_google, patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]) as mock_openlibrary:
            discovery_engine.discover_candidates_for_author("Harmon Cooper")

        mock_google.assert_called_once_with('inauthor:"Harmon Cooper"')
        mock_openlibrary.assert_called_once_with('author:"Harmon Cooper"')
        mock_hardcover.assert_called_once_with("Harmon Cooper")

    def test_no_lookahead_queries_for_author_wide_search(self):
        # Unlike discover_candidates_for_series, there's no single "next
        # book number" to look ahead from when results can span several
        # different series -- only one plain web-search query should fire.
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"}), patch.object(
            discovery_engine, "_fetch_hardcover", return_value=[]
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ), patch.object(discovery_engine, "_fetch_web_search", return_value=[]) as mock_web_search:
            discovery_engine.discover_candidates_for_author("Harmon Cooper")

        mock_web_search.assert_called_once()
        queries_used = mock_web_search.call_args[0][0]
        self.assertEqual(len(queries_used), 1)
        # series_name argument (second positional) is None -- no single
        # target series for an author-wide search.
        self.assertIsNone(mock_web_search.call_args[0][1])

    def test_recovers_series_hint_from_a_dash_suffixed_title_with_no_structured_field(self):
        # Regression (live bug): a raw candidate whose *only* series
        # information is baked into the title text as "<Title> - <Series>
        # #<N>" (a real Hardcover listing format, not just a hypothetical)
        # had no structured series_name_hint field at all, so it fell all
        # the way through to "standalone" in the response even when it was
        # the only listing found for an entire series (e.g. Rebecca
        # Yarros's "Legacy" novellas never formed a series group at all).
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {
                    "source": "hardcover",
                    "source_id": "hc-1",
                    "title": "Ignite - Legacy #0.7",
                    "authors": ["Rebecca Yarros"],
                    "published_date": "2014-01-01",
                    "isbn13": None,
                    "source_url": None,
                    "language": "",
                    "series_number_hint": None,
                    "upcoming_hint": False,
                    "series_name_hint": None,
                }
            ],
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ):
            result = discovery_engine.discover_candidates_for_author("Rebecca Yarros")

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["series_name_hint"], "Legacy")

    def test_excludes_already_owned_titles_across_the_authors_whole_library(self):
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {
                    "source": "hardcover",
                    "source_id": "hc-1",
                    "title": "Cherry Blossom Girls Book 7",
                    "authors": ["Harmon Cooper"],
                    "published_date": "2024-02-20",
                    "isbn13": None,
                    "source_url": None,
                    "language": "",
                    "series_number_hint": 7,
                    "upcoming_hint": False,
                    "series_name_hint": "Cherry Blossom Girls",
                }
            ],
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ):
            owned_key = discovery_engine.core_title_key("Cherry Blossom Girls Book 7")
            result = discovery_engine.discover_candidates_for_author(
                "Harmon Cooper", exclude_title_keys={owned_key}
            )

        self.assertEqual(result["candidates"], [])

    def test_excludes_bare_series_name_stub_using_per_candidate_series_hint(self):
        # Regression: author-wide search has no single fixed series name to
        # compare a stub listing against, so it must fall back to each
        # candidate's own guessed series_name_hint instead.
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {
                    "source": "hardcover",
                    "source_id": "hc-stub",
                    "title": "The Empyrean",
                    "authors": ["Rebecca Yarros"],
                    "published_date": "2023",
                    "isbn13": None,
                    "source_url": None,
                    "language": "",
                    "series_number_hint": None,
                    "upcoming_hint": False,
                    "series_name_hint": "The Empyrean",
                }
            ],
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ):
            result = discovery_engine.discover_candidates_for_author("Rebecca Yarros")

        self.assertEqual(result["candidates"], [])


def _hardcover_result(title, series_name_hint=None, series_number_hint=None):
    return {
        "source": "hardcover",
        "source_id": f"hc-{title}",
        "title": title,
        "authors": ["Glynn Stewart"],
        "published_date": "2019-01-30",
        "isbn13": None,
        "source_url": None,
        "language": "",
        "series_number_hint": series_number_hint,
        "upcoming_hint": False,
        "series_name_hint": series_name_hint,
    }


class EnrichMissingSeriesHintsTest(unittest.TestCase):
    """Regression coverage for a live bug: an author-wide bibliography
    search on Hardcover for "Glynn Stewart" returned "Refuge", "Crusade" and
    "Ashen Stars" with no series info at all, even though Hardcover's own
    per-title search ("Refuge Glynn Stewart") correctly identifies it as
    Exile #2 -- the bibliography-wide query and a title-specific query hit
    different index paths on Hardcover's side. discover_candidates_for_author
    should recover that missing series data with a supplemental per-title
    lookup, only when Hardcover is actually configured.
    """

    def setUp(self):
        self._env_patch = patch.dict(
            os.environ,
            {"HARDCOVER_API_KEY": "test-key", "BRAVE_SEARCH_API_KEY": "", "ANTHROPIC_API_KEY": ""},
        )
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_recovers_series_name_via_per_title_lookup(self):
        bibliography_results = [_hardcover_result("Refuge")]

        def fake_fetch_hardcover(query, max_results=25):
            if query == "Glynn Stewart":
                return bibliography_results
            self.assertEqual(query, "Refuge Glynn Stewart")
            return [_hardcover_result("Refuge", series_name_hint="Exile", series_number_hint=2)]

        with patch.object(discovery_engine, "_fetch_hardcover", side_effect=fake_fetch_hardcover), patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            result = discovery_engine.discover_candidates_for_author("Glynn Stewart")

        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["series_name_hint"], "Exile")
        self.assertEqual(candidate["series_number_hint"], 2)

    def test_drops_bare_series_name_title_revealed_by_sibling_lookup(self):
        # "ONSET" is never itself a book -- only "To Serve and Protect",
        # "My Enemy's Enemy", etc. are. A per-title lookup for "ONSET Glynn
        # Stewart" surfaces those siblings, all tagged with series "ONSET",
        # which is strong evidence the candidate itself is a bare series
        # name/stub listing that should be dropped, not shown as a new book.
        bibliography_results = [_hardcover_result("ONSET")]

        def fake_fetch_hardcover(query, max_results=25):
            if query == "Glynn Stewart":
                return bibliography_results
            self.assertEqual(query, "ONSET Glynn Stewart")
            return [
                _hardcover_result("To Serve and Protect", series_name_hint="ONSET", series_number_hint=1),
                _hardcover_result("My Enemy's Enemy", series_name_hint="ONSET", series_number_hint=2),
                _hardcover_result("Blood of the Innocent", series_name_hint="ONSET", series_number_hint=3),
            ]

        with patch.object(discovery_engine, "_fetch_hardcover", side_effect=fake_fetch_hardcover), patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            result = discovery_engine.discover_candidates_for_author("Glynn Stewart")

        self.assertEqual(result["candidates"], [])

    def test_does_not_run_without_hardcover_api_key_configured(self):
        with patch.dict(os.environ, {"HARDCOVER_API_KEY": ""}):
            bibliography_results = [_hardcover_result("Refuge")]
            with patch.object(
                discovery_engine, "_fetch_hardcover", return_value=bibliography_results
            ) as mock_hardcover, patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
                discovery_engine, "_fetch_openlibrary", return_value=[]
            ):
                result = discovery_engine.discover_candidates_for_author("Glynn Stewart")

        # Only the one bibliography call -- no supplemental per-title lookup.
        mock_hardcover.assert_called_once_with("Glynn Stewart")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertIsNone(result["candidates"][0]["series_name_hint"])

    def test_leaves_already_tagged_candidates_alone(self):
        bibliography_results = [_hardcover_result("Exile", series_name_hint="Exile", series_number_hint=1)]
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=bibliography_results) as mock_hardcover, patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            result = discovery_engine.discover_candidates_for_author("Glynn Stewart")

        # No supplemental lookup needed -- already has a series name.
        mock_hardcover.assert_called_once_with("Glynn Stewart")
        self.assertEqual(result["candidates"][0]["series_name_hint"], "Exile")


class HardcoverProviderTest(unittest.TestCase):
    """Regression coverage for _fetch_hardcover's raw-document parsing --
    verified against a real API response shape, not a guessed one.
    """

    def _mock_response(self, hits):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"data": {"search": {"results": {"hits": hits}}}}
        return mock_response

    def test_series_name_hint_reads_the_nested_series_name_field(self):
        # Regression (live bug): the series name lives at
        # featured_series.series.name, one level deeper than
        # position/unreleased -- a "More by this author" search for
        # Nicoli Gonnella's "Unbound" series mistakenly read a flat
        # featured_series.name (which doesn't exist) and always got None,
        # so every Unbound book came back tagged "Standalone" instead of
        # being recognized as part of an already-tracked series.
        hits = [
            {
                "document": {
                    "title": "Ruin",
                    "author_names": ["Nicoli Gonnella"],
                    "isbns": ["9781637663271"],
                    "release_date": "2026-06-24",
                    "featured_series": {
                        "position": 12.0,
                        "unreleased": False,
                        "series": {"id": 23995, "name": "Unbound", "slug": "unbound"},
                    },
                }
            }
        ]
        with patch.object(discovery_engine, "os") as mock_os, patch.object(
            discovery_engine.httpx, "post", return_value=self._mock_response(hits)
        ):
            mock_os.environ.get.return_value = "test-key"
            results = discovery_engine._fetch_hardcover("Nicoli Gonnella")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["series_name_hint"], "Unbound")
        self.assertEqual(results[0]["series_number_hint"], 12)

    def test_fractional_series_position_is_not_rounded_to_next_integer(self):
        # Regression (live bug): Hardcover tags companion/side-story content
        # with a fractional position (e.g. "Threshing Day", a short story
        # collection in Rebecca Yarros's The Empyrean universe, at position
        # 3.5). Rounding that to the nearest int used to turn it into
        # "Book 4" -- indistinguishable from (and mistaken for) the real
        # next numbered entry in the main series, even though Hardcover
        # itself classifies it as a side book, not book 4. Note Python's
        # round-half-to-even means 3.5 specifically rounds *up* to 4, which
        # is exactly what happened here.
        hits = [
            {
                "document": {
                    "title": "Threshing Day",
                    "author_names": ["Rebecca Yarros"],
                    "isbns": [],
                    "release_date": "2026-09-29",
                    "featured_series": {
                        "position": 3.5,
                        "unreleased": True,
                        "series": {"id": 1, "name": "The Empyrean"},
                    },
                }
            }
        ]
        with patch.object(discovery_engine, "os") as mock_os, patch.object(
            discovery_engine.httpx, "post", return_value=self._mock_response(hits)
        ):
            mock_os.environ.get.return_value = "test-key"
            results = discovery_engine._fetch_hardcover("Rebecca Yarros")

        self.assertEqual(results[0]["series_number_hint"], 3.5)

    def test_series_total_hint_prefers_primary_books_count(self):
        # Verified against a live API response for Glynn Stewart's "Exile"
        # series: books_count (4) includes a 0.5 novella, primary_books_count
        # (3) counts only the main numbered entries -- the latter is the
        # more intuitive "book N of M" figure for a maturity indicator.
        hits = [
            {
                "document": {
                    "title": "Exile",
                    "author_names": ["Glynn Stewart"],
                    "isbns": ["9781988035307"],
                    "release_date": "2018-07-17",
                    "featured_series": {
                        "position": 1.0,
                        "unreleased": False,
                        "series": {"id": 8618, "name": "Exile", "books_count": 4, "primary_books_count": 3},
                    },
                }
            }
        ]
        with patch.object(discovery_engine, "os") as mock_os, patch.object(
            discovery_engine.httpx, "post", return_value=self._mock_response(hits)
        ):
            mock_os.environ.get.return_value = "test-key"
            results = discovery_engine._fetch_hardcover("Glynn Stewart")

        self.assertEqual(results[0]["series_total_hint"], 3)

    def test_series_total_hint_falls_back_to_books_count(self):
        hits = [
            {
                "document": {
                    "title": "Some Book",
                    "author_names": ["Some Author"],
                    "isbns": [],
                    "release_date": "",
                    "featured_series": {
                        "position": 1.0,
                        "unreleased": False,
                        "series": {"id": 1, "name": "Some Series", "books_count": 2},
                    },
                }
            }
        ]
        with patch.object(discovery_engine, "os") as mock_os, patch.object(
            discovery_engine.httpx, "post", return_value=self._mock_response(hits)
        ):
            mock_os.environ.get.return_value = "test-key"
            results = discovery_engine._fetch_hardcover("Some Author")

        self.assertEqual(results[0]["series_total_hint"], 2)

    def test_series_name_hint_is_none_when_no_featured_series(self):
        hits = [
            {
                "document": {
                    "title": "Dissonance: A LitRPG Adventure",
                    "author_names": ["Nicoli Gonnella"],
                    "isbns": [],
                    "release_date": "",
                    "featured_series": {},
                }
            }
        ]
        with patch.object(discovery_engine, "os") as mock_os, patch.object(
            discovery_engine.httpx, "post", return_value=self._mock_response(hits)
        ):
            mock_os.environ.get.return_value = "test-key"
            results = discovery_engine._fetch_hardcover("Nicoli Gonnella")

        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["series_name_hint"])


class BackfillMissingPublicationDatesTest(unittest.TestCase):
    """Regression coverage for backfill_missing_publication_dates -- live
    bug: web-search-only candidates with no published_date at all default
    to "Upcoming" via classify_upcoming's conservative fallback even when
    they're real, already-released books (Georgia Wagner's "Jonathan Hunt
    Thriller Series" -- every sequel this surfaced came back with no
    published_date, and all but one showed up as Upcoming despite being
    already out).
    """

    def setUp(self):
        patcher = patch.object(discovery_engine.os.environ, "get", side_effect=lambda key, default="": (
            "test-key" if key == "HARDCOVER_API_KEY" else default
        ))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_backfills_a_real_date_via_isbn_lookup(self):
        candidates = [
            {"title": "The Levee Ghosts", "authors": ["Georgia Wagner"], "isbn13": "9798242217126", "published_date": ""}
        ]
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {"title": "The Levee Ghosts", "authors": ["Georgia Wagner", "Scott Cook"], "isbn13": "9798242217126", "published_date": "2026-01-01"}
            ],
        ):
            discovery_engine.backfill_missing_publication_dates(candidates, "Georgia Wagner; Scott Cook")

        self.assertEqual(candidates[0]["published_date"], "2026-01-01")

    def test_backfills_via_title_lookup_when_isbn_is_unknown(self):
        candidates = [{"title": "Desert Protocol", "authors": ["Georgia Wagner"], "isbn13": None, "published_date": ""}]
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {"title": "Desert Protocol", "authors": ["Georgia Wagner", "Scott Cook"], "isbn13": "9798242216228", "published_date": "2026-01-01"}
            ],
        ):
            discovery_engine.backfill_missing_publication_dates(candidates, "Georgia Wagner; Scott Cook")

        self.assertEqual(candidates[0]["published_date"], "2026-01-01")
        # The now-confirmed ISBN is also worth keeping, since the candidate
        # never had one before this lookup found it.
        self.assertEqual(candidates[0]["isbn13"], "9798242216228")

    def test_rejects_a_same_titled_hit_by_an_unrelated_author(self):
        # Regression (live bug): a bare title lookup for "The Winter Siege"
        # returned a real, unrelated historical-fiction book by a completely
        # different author with the same generic title.
        candidates = [{"title": "The Winter Siege", "authors": ["Georgia Wagner"], "isbn13": None, "published_date": ""}]
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {"title": "The Winter Siege", "authors": ["Ariana Franklin", "Samantha Norman"], "isbn13": "9780593070611", "published_date": "2014-10-09"}
            ],
        ):
            discovery_engine.backfill_missing_publication_dates(candidates, "Georgia Wagner; Scott Cook")

        self.assertEqual(candidates[0]["published_date"], "")

    def test_never_overwrites_an_already_known_date(self):
        candidates = [
            {"title": "Desert Protocol", "authors": ["Georgia Wagner"], "isbn13": None, "published_date": "2026-01-01"}
        ]
        with patch.object(discovery_engine, "_fetch_hardcover") as mock_fetch:
            discovery_engine.backfill_missing_publication_dates(candidates, "Georgia Wagner; Scott Cook")

        mock_fetch.assert_not_called()
        self.assertEqual(candidates[0]["published_date"], "2026-01-01")

    def test_stops_after_the_lookup_cap_is_reached(self):
        candidates = [
            {"title": f"Book {n}", "authors": ["Georgia Wagner"], "isbn13": None, "published_date": ""}
            for n in range(discovery_engine.MAX_PUBLICATION_DATE_BACKFILL_LOOKUPS + 3)
        ]
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]) as mock_fetch:
            discovery_engine.backfill_missing_publication_dates(candidates, "Georgia Wagner; Scott Cook")

        self.assertEqual(mock_fetch.call_count, discovery_engine.MAX_PUBLICATION_DATE_BACKFILL_LOOKUPS)

    def test_no_op_without_a_hardcover_api_key(self):
        candidates = [{"title": "Desert Protocol", "authors": ["Georgia Wagner"], "isbn13": None, "published_date": ""}]
        with patch.object(discovery_engine.os.environ, "get", return_value=""), patch.object(
            discovery_engine, "_fetch_hardcover"
        ) as mock_fetch:
            discovery_engine.backfill_missing_publication_dates(candidates, "Georgia Wagner; Scott Cook")

        mock_fetch.assert_not_called()
        self.assertEqual(candidates[0]["published_date"], "")


class WebSearchProviderTest(unittest.TestCase):
    """Tests the Brave Search + Claude web-search discovery provider, with
    the HTTP call to Brave and the Anthropic client both mocked out so this
    runs offline, deterministically, and without spending real API credits.
    """

    def test_fetch_brave_web_search_returns_empty_without_api_key(self):
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": ""}):
            self.assertEqual(discovery_engine._fetch_brave_web_search("Some Series Author"), [])

    def test_fetch_brave_web_search_parses_response(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "web": {
                "results": [
                    {"title": "Book Announced", "description": "A new entry.", "url": "https://example.com/a"},
                    {"title": "", "description": "Missing title, should be skipped", "url": "https://example.com/b"},
                ]
            }
        }
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "test-key"}), patch.object(
            discovery_engine.httpx, "get", return_value=mock_response
        ) as mock_get:
            results = discovery_engine._fetch_brave_web_search("Some Series Author")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Book Announced")
        self.assertEqual(results[0]["url"], "https://example.com/a")
        self.assertTrue(mock_get.called)

    def test_structure_web_results_returns_empty_without_api_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            result = discovery_engine._structure_web_results_with_llm(
                "Some Series", "Some Author", [{"title": "t", "description": "d", "url": "u"}]
            )
        self.assertEqual(result, [])

    def test_structure_web_results_returns_empty_without_raw_results(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            result = discovery_engine._structure_web_results_with_llm("Some Series", "Some Author", [])
        self.assertEqual(result, [])

    def _mock_anthropic_client(self, response_text):
        mock_text_block = MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = response_text
        mock_message = MagicMock()
        mock_message.content = [mock_text_block]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_message
        return mock_client

    def test_structure_web_results_parses_valid_json_array(self):
        payload = json.dumps(
            [
                {
                    "result_index": 0,
                    "title": "Peacemaker",
                    "book_number": 8,
                    "author_names": ["Some Author"],
                    "published_date": "2026-08-09",
                    "is_upcoming": False,
                    "isbn13": None,
                }
            ]
        )
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=self._mock_anthropic_client(payload)
        ):
            result = discovery_engine._structure_web_results_with_llm(
                "The First Peacemaker", "Some Author", [{"title": "t", "description": "d", "url": "u"}]
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["book_number"], 8)

    def test_structure_web_results_strips_markdown_fences(self):
        payload = "```json\n[]\n```"
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=self._mock_anthropic_client(payload)
        ):
            result = discovery_engine._structure_web_results_with_llm(
                "Some Series", "Some Author", [{"title": "t", "description": "d", "url": "u"}]
            )
        self.assertEqual(result, [])

    def test_structure_web_results_returns_empty_on_invalid_json(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=self._mock_anthropic_client("not json at all")
        ):
            result = discovery_engine._structure_web_results_with_llm(
                "Some Series", "Some Author", [{"title": "t", "description": "d", "url": "u"}]
            )
        self.assertEqual(result, [])

    def test_fetch_web_search_combines_brave_and_llm_structuring(self):
        raw_results = [{"title": "Peacemaker Book 8 Announced", "description": "snippet", "url": "https://example.com/8"}]
        structured = [
            {
                "result_index": 0,
                "title": "The First Peacemaker",
                "book_number": 8,
                "author_names": ["Some Author"],
                "published_date": "2026-08-09",
                "is_upcoming": False,
                "isbn13": None,
            }
        ]
        with patch.object(discovery_engine, "_fetch_brave_web_search", return_value=raw_results), patch.object(
            discovery_engine, "_structure_web_results_with_llm", return_value=structured
        ):
            results = discovery_engine._fetch_web_search(["query"], "The First Peacemaker", "Some Author")

        self.assertEqual(len(results), 1)
        candidate = results[0]
        self.assertEqual(candidate["source"], "web_search")
        self.assertEqual(candidate["title"], "The First Peacemaker")
        self.assertEqual(candidate["series_number_hint"], 8)
        self.assertEqual(candidate["upcoming_hint"], False)
        self.assertEqual(candidate["source_url"], "https://example.com/8")
        self.assertEqual(candidate["authors"], ["Some Author"])

    def test_fetch_web_search_treats_undated_result_as_upcoming_even_if_llm_says_not(self):
        # Regression (live bug): a retailer listing existing (no date in the
        # snippet) isn't proof a book is actually out -- pre-order listings
        # look identical -- but the LLM sometimes still guesses
        # is_upcoming=False when it has no date at all. This should be
        # overridden by the code-level safety net rather than trusted.
        raw_results = [{"title": "New Book Listing", "description": "snippet", "url": "https://example.com/9"}]
        structured = [
            {
                "result_index": 0,
                "title": "Embers of the Ancients",
                "book_number": 9,
                "author_names": ["Some Author"],
                "published_date": None,
                "is_upcoming": False,
                "isbn13": None,
            }
        ]
        with patch.object(discovery_engine, "_fetch_brave_web_search", return_value=raw_results), patch.object(
            discovery_engine, "_structure_web_results_with_llm", return_value=structured
        ):
            results = discovery_engine._fetch_web_search(["query"], "Series", "Some Author")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["upcoming_hint"], True)
        self.assertEqual(results[0]["published_date"], "")

    def test_fetch_web_search_refines_undated_result_with_a_second_targeted_query(self):
        # Regression (live bug): "Edge of Shadow" (Peacemaker Book 8) was
        # already out, but the broad "<series> book 8" query's top snippet
        # had no date, so it got the conservative "upcoming" default. A
        # second, title-specific "<title> release date" query found a page
        # that did state the date -- that should override the guess.
        raw_results = [{"title": "Edge of Shadow listing", "description": "snippet, no date", "url": "https://example.com/8"}]
        first_pass_structured = [
            {
                "result_index": 0,
                "title": "Edge of Shadow",
                "book_number": 8,
                "author_names": ["Some Author"],
                "published_date": None,
                "is_upcoming": False,
                "isbn13": None,
            }
        ]
        refinement_raw = [{"title": "Edge of Shadow release date", "description": "Released Aug 9, 2026", "url": "https://example.com/8-date"}]
        refinement_structured = [
            {
                "result_index": 0,
                "title": "Edge of Shadow",
                "book_number": 8,
                "author_names": ["Some Author"],
                "published_date": "2026-08-09",
                "is_upcoming": False,
                "isbn13": None,
            }
        ]

        def fake_brave(query):
            if "release date" in query:
                return refinement_raw
            return raw_results

        def fake_structure(series_name, author, results, **kwargs):
            if results == refinement_raw:
                return refinement_structured
            return first_pass_structured

        with patch.object(discovery_engine, "_fetch_brave_web_search", side_effect=fake_brave), patch.object(
            discovery_engine, "_structure_web_results_with_llm", side_effect=fake_structure
        ):
            results = discovery_engine._fetch_web_search(["query"], "The First Peacemaker", "Some Author")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["published_date"], "2026-08-09")
        self.assertEqual(results[0]["upcoming_hint"], False)

    def test_fetch_web_search_refinement_query_is_not_an_exact_title_only_phrase(self):
        # Regression (live bug): "Here We Go Again" (The World Book 21) is
        # also a Demi Lovato song/album, a movie, and a TV series -- quoting
        # just the bare title as an exact phrase (the old query) got
        # swamped by those unrelated hits and found nothing useful. Adding
        # the series name and author as unquoted extra terms (soft ranking
        # signals) is what actually surfaced the real source page live.
        raw_results = [{"title": "Here We Go Again listing", "description": "snippet, no date", "url": "https://example.com/21"}]
        first_pass_structured = [
            {
                "result_index": 0,
                "title": "Here We Go Again",
                "book_number": 21,
                "author_names": ["Jason Cheek"],
                "published_date": None,
                "is_upcoming": False,
                "isbn13": None,
            }
        ]
        captured_queries = []

        def fake_brave(query):
            captured_queries.append(query)
            if "release date" in query:
                return []
            return raw_results

        with patch.object(discovery_engine, "_fetch_brave_web_search", side_effect=fake_brave), patch.object(
            discovery_engine, "_structure_web_results_with_llm", return_value=first_pass_structured
        ):
            discovery_engine._fetch_web_search(["query"], "The World Book", "Jason Cheek")

        refinement_queries = [q for q in captured_queries if "release date" in q]
        self.assertEqual(len(refinement_queries), 1)
        refinement_query = refinement_queries[0]
        self.assertNotIn('"Here We Go Again"', refinement_query)
        self.assertIn("Here We Go Again", refinement_query)
        self.assertIn("The World Book", refinement_query)
        self.assertIn("Jason Cheek", refinement_query)

    def test_fetch_web_search_keeps_upcoming_default_when_refinement_finds_nothing(self):
        # The refinement pass is best-effort -- if the second query also
        # can't find a date (e.g. a genuine undated preorder like Peacemaker
        # Book 9), the original conservative "upcoming" classification must
        # still stand rather than being cleared out.
        raw_results = [{"title": "Embers listing", "description": "snippet, no date", "url": "https://example.com/9"}]
        structured = [
            {
                "result_index": 0,
                "title": "Embers of the Ancients",
                "book_number": 9,
                "author_names": ["Some Author"],
                "published_date": None,
                "is_upcoming": False,
                "isbn13": None,
            }
        ]

        with patch.object(discovery_engine, "_fetch_brave_web_search", return_value=raw_results), patch.object(
            discovery_engine, "_structure_web_results_with_llm", return_value=structured
        ):
            results = discovery_engine._fetch_web_search(["query"], "The First Peacemaker", "Some Author")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["published_date"], "")
        self.assertEqual(results[0]["upcoming_hint"], True)

    def test_fetch_web_search_refinement_is_capped_and_tolerates_failures(self):
        # Bound the extra cost: only the first WEB_SEARCH_DATE_REFINEMENT_MAX
        # undated candidates get a second look, and a refinement query
        # blowing up must not take down the whole discovery run.
        raw_results = [{"title": "Generic listing", "description": "snippet, no date", "url": "https://example.com/x"}]
        structured = [
            {
                "result_index": 0,
                "title": f"Book {n}",
                "book_number": n,
                "author_names": ["Some Author"],
                "published_date": None,
                "is_upcoming": False,
                "isbn13": None,
            }
            for n in range(5)
        ]
        # Each item maps to the same lone raw result via result_index -- that's fine,
        # this test is only exercising the refinement-cap/error-tolerance behavior.

        call_count = {"n": 0}

        def fake_brave(query):
            if "release date" in query:
                call_count["n"] += 1
                raise RuntimeError("boom")
            return raw_results

        with patch.object(discovery_engine, "_fetch_brave_web_search", side_effect=fake_brave), patch.object(
            discovery_engine, "_structure_web_results_with_llm", return_value=structured
        ):
            results = discovery_engine._fetch_web_search(["query"], "Series", "Some Author")

        self.assertEqual(len(results), 5)
        self.assertTrue(all(r["upcoming_hint"] for r in results))
        self.assertEqual(call_count["n"], discovery_engine.WEB_SEARCH_DATE_REFINEMENT_MAX)

    def test_fetch_web_search_skips_llm_items_with_out_of_range_index(self):
        raw_results = [{"title": "Some Result", "description": "snippet", "url": "https://example.com/1"}]
        structured = [{"result_index": 5, "title": "Bad Index", "book_number": None, "author_names": [], "is_upcoming": False}]
        with patch.object(discovery_engine, "_fetch_brave_web_search", return_value=raw_results), patch.object(
            discovery_engine, "_structure_web_results_with_llm", return_value=structured
        ):
            results = discovery_engine._fetch_web_search(["query"], "Series", "Author")
        self.assertEqual(results, [])

    def test_fetch_web_search_returns_empty_when_brave_has_no_results(self):
        with patch.object(discovery_engine, "_fetch_brave_web_search", return_value=[]):
            results = discovery_engine._fetch_web_search(["query"], "Series", "Author")
        self.assertEqual(results, [])

    def test_fetch_web_search_merges_and_dedups_results_across_multiple_queries(self):
        # The lookahead queries ("<series> book <N>") run alongside the
        # generic query and can legitimately return overlapping pages --
        # those should be merged into one deduped raw-result list (by URL)
        # before the single LLM structuring call, not passed through twice.
        def fake_brave(query):
            if query == "generic":
                return [
                    {"title": "Series Book 1", "description": "d", "url": "https://example.com/1"},
                    {"title": "Series Book 9", "description": "d", "url": "https://example.com/9"},
                ]
            if query == "book 9":
                return [{"title": "Series Book 9", "description": "d", "url": "https://example.com/9"}]
            return []

        with patch.object(discovery_engine, "_fetch_brave_web_search", side_effect=fake_brave), patch.object(
            discovery_engine, "_structure_web_results_with_llm", return_value=[]
        ) as mock_structure:
            discovery_engine._fetch_web_search(["generic", "book 9"], "Series", "Author")

        passed_raw_results = mock_structure.call_args[0][2]
        self.assertEqual(len(passed_raw_results), 2)
        self.assertEqual({r["url"] for r in passed_raw_results}, {"https://example.com/1", "https://example.com/9"})

    def test_fetch_web_search_tolerates_one_query_failing_if_another_succeeds(self):
        def fake_brave(query):
            if query == "bad":
                raise RuntimeError("rate limited")
            return [{"title": "Found It", "description": "d", "url": "https://example.com/ok"}]

        with patch.object(discovery_engine, "_fetch_brave_web_search", side_effect=fake_brave), patch.object(
            discovery_engine, "_structure_web_results_with_llm", return_value=[]
        ) as mock_structure:
            discovery_engine._fetch_web_search(["bad", "good"], "Series", "Author")

        passed_raw_results = mock_structure.call_args[0][2]
        self.assertEqual(len(passed_raw_results), 1)

    def test_fetch_web_search_raises_when_every_query_fails(self):
        with patch.object(discovery_engine, "_fetch_brave_web_search", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                discovery_engine._fetch_web_search(["bad1", "bad2"], "Series", "Author")

    def test_discover_candidates_for_series_wires_in_web_search_results(self):
        web_candidate = {
            "source": "web_search",
            "source_id": "https://example.com/8",
            "title": "The First Peacemaker Book 8",
            "authors": ["Some Author"],
            "published_date": "2026-08-09",
            "description": "snippet",
            "isbn13": None,
            "source_url": "https://example.com/8",
            "language": "",
            "series_number_hint": 8,
            "upcoming_hint": False,
        }
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"}), patch.object(
            discovery_engine, "_fetch_hardcover", return_value=[]
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ), patch.object(
            discovery_engine, "_fetch_web_search", return_value=[web_candidate]
        ):
            result = discovery_engine.discover_candidates_for_series("The First Peacemaker", "Some Author")

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["source"], "web_search")
        self.assertEqual(result["candidates"][0]["confidence"], "targeted")

    def test_discover_candidates_for_series_excludes_untitled_placeholder_from_web_search(self):
        # Regression (live bug): a lookahead "book 5" web search surfaced fan
        # speculation about an unannounced future book, which the LLM
        # structuring pass turned into a candidate literally titled
        # "Untitled" with a guessed book_number -- that guessed number alone
        # was enough to pass series-membership checks downstream, so it
        # needs to be filtered out here before it ever gets that far.
        placeholder_candidate = {
            "source": "web_search",
            "source_id": "https://example.com/speculation",
            "title": "Untitled",
            "authors": ["Rebecca Yarros"],
            "published_date": "",
            "description": "Fans speculate about the untitled fifth book",
            "isbn13": None,
            "source_url": "https://example.com/speculation",
            "language": "",
            "series_number_hint": 5,
            "upcoming_hint": True,
        }
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"}), patch.object(
            discovery_engine, "_fetch_hardcover", return_value=[]
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ), patch.object(
            discovery_engine, "_fetch_web_search", return_value=[placeholder_candidate]
        ):
            result = discovery_engine.discover_candidates_for_series("The Empyrean", "Rebecca Yarros")

        self.assertEqual(result["candidates"], [])

    def test_discover_candidates_for_series_adds_lookahead_queries_for_next_books(self):
        # Regression (live bug): a generic "<series> <author>" search's
        # relevance ranking favors whichever book has the most existing
        # links (almost always book 1), so a brand-new release/announcement
        # can fail to surface at all even with a large result count. When
        # the caller knows the highest book number currently owned, explicit
        # "<series> book <N>" queries for the next few numbers should be
        # added alongside the generic one.
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"}), patch.object(
            discovery_engine, "_fetch_hardcover", return_value=[]
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ), patch.object(discovery_engine, "_fetch_web_search", return_value=[]) as mock_web_search:
            discovery_engine.discover_candidates_for_series(
                "The First Peacemaker", "Some Author", highest_owned_book_number=8
            )

        queries_used = mock_web_search.call_args[0][0]
        self.assertIn('"The First Peacemaker" Some Author book 9', queries_used)
        self.assertIn('"The First Peacemaker" Some Author book 10', queries_used)
        self.assertIn('"The First Peacemaker" Some Author book 11', queries_used)
        self.assertNotIn('"The First Peacemaker" Some Author book 12', queries_used)

    def test_discover_candidates_for_series_lookahead_query_disambiguates_generic_series_names(self):
        # Regression (live bug): "The World Book" by Jason Cheek is a real
        # series, but that name is also the brand of an actual, heavily
        # SEO'd encyclopedia sold in 20+ numbered volumes -- a bare
        # "<series> book <N>" lookahead query returned nothing but
        # encyclopedia listings and missed a real new release (book 21,
        # "Here We Go Again", released 2026-07-15). Including the author
        # name in the query disambiguates it.
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"}), patch.object(
            discovery_engine, "_fetch_hardcover", return_value=[]
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ), patch.object(discovery_engine, "_fetch_web_search", return_value=[]) as mock_web_search:
            discovery_engine.discover_candidates_for_series(
                "The World Book", "Jason Cheek", highest_owned_book_number=20
            )

        queries_used = mock_web_search.call_args[0][0]
        self.assertIn('"The World Book" Jason Cheek book 21', queries_used)

    def test_discover_candidates_for_series_skips_web_search_without_keys(self):
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "", "ANTHROPIC_API_KEY": ""}), patch.object(
            discovery_engine, "_fetch_hardcover", return_value=[]
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ), patch.object(discovery_engine, "_fetch_web_search") as mock_web_search:
            discovery_engine.discover_candidates_for_series("The First Peacemaker", "Some Author")

        mock_web_search.assert_not_called()


class SeriesCheckIntegrationTest(unittest.TestCase):
    """Integration tests for SeriesIntelligenceAgent.run_series_check against
    an in-memory database, with discovery_engine mocked so behavior is
    deterministic and doesn't depend on live third-party APIs.
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
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

        for number in [1, 2, 3, 4, 5, 6, 8, 9]:
            self.db.add(
                Book(
                    title=f"Cherry Blossom Girls Book {number}",
                    author="Harmon Cooper",
                    series_id=series.id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=False,
                )
            )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _mock_discovery(self, candidates, **overrides):
        result = {
            "candidates": candidates,
            "provider_failures": [],
            "all_providers_failed": False,
            "used_author_fallback": False,
        }
        result.update(overrides)
        return patch("discovery_engine.discover_candidates_for_series", return_value=result)

    def test_available_book_is_added_and_classified_available(self):
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-7",
                "title": "Cherry Blossom Girls Book 7",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": 7,
                "upcoming_hint": False,
            }
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)
        self.assertEqual(result["available_missing"][0]["series_number"], 7)
        self.assertEqual(result["upcoming_books"], [])

    def test_future_dated_book_is_classified_upcoming(self):
        far_future_year = date.today().year + 5
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-10",
                "title": "Cherry Blossom Girls Book 10",
                "authors": ["Harmon Cooper"],
                "published_date": f"{far_future_year}-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": 10,
                "upcoming_hint": False,
            }
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertTrue(result["found"])
        self.assertEqual(result["available_missing"], [])
        self.assertEqual(len(result["upcoming_books"]), 1)
        self.assertEqual(result["upcoming_books"][0]["series_number"], 10)

    def test_unreleased_hint_marks_upcoming_even_without_a_parseable_date(self):
        # Hardcover can flag a book as not-yet-released without providing a
        # release date at all -- that hint alone should be enough.
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-10",
                "title": "Cherry Blossom Girls Book 10",
                "authors": ["Harmon Cooper"],
                "published_date": "",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": 10,
                "upcoming_hint": True,
            }
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(result["available_missing"], [])
        self.assertEqual(len(result["upcoming_books"]), 1)

    def test_already_owned_book_number_is_not_reported_as_new(self):
        candidates = [
            {
                "source": "google_books",
                "source_id": "gb-2",
                "title": "Cherry Blossom Girls Book 2 -- Special Reissue",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            }
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertFalse(result["found"])
        self.assertEqual(result["available_missing"], [])
        self.assertEqual(result["upcoming_books"], [])

    def test_bare_title_with_no_number_matches_owned_book_by_unique_stem(self):
        # Regression (live bug): "Unbound" book 9 is titled "Crown: A LitRPG:
        # (Unbound Book 9)" in the library, but Google Books surfaced it as
        # just the bare word "Crown" -- no "Book 9" suffix, no digits at
        # all, and no series_number_hint. core_title_key folds the "9" into
        # the *owned* book's key ("crown 9") but has nothing to fold into
        # the bare candidate's key ("crown"), so the two never matched and
        # it got re-added as a live "available" duplicate of an already-read
        # book. It should instead be recognized as already owned via the
        # unique bare-title fallback.
        self.db.add(
            Book(
                title="Crown: A LitRPG: (Cherry Blossom Girls Book 9)",
                author="Harmon Cooper",
                series_id=self.series.id,
                series_order=9,
                book_number=9.0,
                record_status="active",
                is_read=True,
            )
        )
        self.db.commit()

        candidates = [
            {
                "source": "google_books",
                "source_id": "gb-crown",
                "title": "Crown",
                "authors": ["Harmon Cooper"],
                "published_date": "2020-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            }
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertFalse(result["found"])
        self.assertEqual(result["available_missing"], [])
        self.assertEqual(result["upcoming_books"], [])

    def test_same_new_book_from_two_providers_with_different_title_formats_is_added_once(self):
        # Regression (live bug): checking a series surfaced a companion short
        # story that Hardcover titled "Havoc in the Deathyards, A Cherry
        # Blossom Girls Short Story" and OpenLibrary titled bare "Havoc in
        # the Deathyards" -- neither had an ISBN or a parseable book number,
        # so both slipped through as "new" and got added as two separate
        # duplicate library entries for the same real short story. (The
        # first candidate's title explicitly references the series by name,
        # which is what makes this different from an unrelated same-author
        # book that merely came back as a "targeted"-confidence hit --
        # confidence alone isn't trusted without either a number or some
        # textual tie to the series; see test_author_fallback_... below.)
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-havoc",
                "title": "Havoc in the Deathyards, A Cherry Blossom Girls Short Story",
                "authors": ["Harmon Cooper"],
                "published_date": "2022-06-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
            {
                "source": "openlibrary",
                "source_id": "ol-havoc",
                "title": "Havoc in the Deathyards",
                "authors": ["Harmon Cooper"],
                "published_date": "2022-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(result["added_count"], 1)
        self.assertEqual(len(result["available_missing"]), 1)

    def test_targeted_confidence_alone_does_not_pull_in_unrelated_same_author_books(self):
        # Regression (live bug): checking "Safehold" by David Weber (a
        # prolific author with many unrelated series) surfaced "Bolo!",
        # "Worlds Of Honor", and "At All Costs" -- real David Weber books,
        # but from entirely different series -- as "available" candidates.
        # They came back tagged confidence="targeted" (the API's own
        # relevance ranking against "Safehold David Weber"), had no
        # parseable book number, and had zero textual reference to
        # "Safehold" anywhere in the title. Trusting confidence=="targeted"
        # alone, with no number and no textual tie to the series, is too
        # weak a signal -- it should be rejected, while a same-batch hit
        # that actually continues the numbering (book 8) should still be
        # accepted.
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-bolo",
                "title": "Bolo!",
                "authors": ["Harmon Cooper"],
                "published_date": "2020-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
            {
                "source": "hardcover",
                "source_id": "hc-worlds-of-honor",
                "title": "Worlds Of Honor",
                "authors": ["Harmon Cooper"],
                "published_date": "2020-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
            {
                "source": "hardcover",
                "source_id": "hc-book10",
                "title": "Cherry Blossom Girls Book 10",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(result["added_count"], 1)
        self.assertEqual(
            [book["title"] for book in result["available_missing"]],
            ["Cherry Blossom Girls Book 10"],
        )

    def test_universe_tie_in_spinoff_series_is_not_pulled_into_flagship_series(self):
        # Regression (live bug): checking "Starship's Mage" (Glynn Stewart)
        # pulled in "Interstellar Mage", "Mage-Provocateur", and "Agents of
        # Mars" -- all real Glynn Stewart books, but from "Starship's Mage:
        # Red Falcon", an entirely separate 3-book spin-off series that just
        # shares the same "Starship's Mage Universe" branding. Each one's
        # subtitle literally says "A Starship's Mage Universe Novel", so the
        # flagship series name was textually present as a substring/token
        # match -- but that's a spin-off marker, not proof of membership in
        # the flagship series being checked, and none of them had a
        # series-position number tying them to it either.
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-interstellar-mage",
                "title": "Interstellar Mage: A Cherry Blossom Girls Universe Novel",
                "authors": ["Harmon Cooper"],
                "published_date": "2017-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
            {
                "source": "hardcover",
                "source_id": "hc-mage-provocateur",
                "title": "Mage-Provocateur: A Cherry Blossom Girls Universe Novel",
                "authors": ["Harmon Cooper"],
                "published_date": "2018-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
            {
                "source": "hardcover",
                "source_id": "hc-book10",
                "title": "Cherry Blossom Girls Book 10",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(result["added_count"], 1)
        self.assertEqual(
            [book["title"] for book in result["available_missing"]],
            ["Cherry Blossom Girls Book 10"],
        )

    def test_universe_tie_in_is_still_accepted_with_a_real_series_position_number(self):
        # The universe-tie-in downgrade should only kick in when there's no
        # other evidence tying the book to *this* series -- a same-batch
        # spin-off-labeled title that Hardcover tags with an actual
        # series-position number for the series being checked should still
        # be accepted.
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-tie-in-numbered",
                "title": "Some Companion Tale: A Cherry Blossom Girls Universe Novel",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": 10,
                "upcoming_hint": False,
            },
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(result["added_count"], 1)

    def test_owned_omnibus_range_prevents_individual_volume_reappearing_as_new(self):
        # Regression (live bug): "Safehold" is owned as a boxed set covering
        # books 1-3 in one row ("Safehold Boxed Set 1: (Safehold Books
        # 1-3)"), plus individually-owned books 4-7. Because that omnibus
        # row's own book_number is just 1 (its position in the shelf, not a
        # range), the discovery/dedupe identity sets didn't know books 2 and
        # 3 were already covered -- so a reprint single-volume edition like
        # "By Heresies Distressed (Safehold Book 3)" slipped through and got
        # added as a second, duplicate "available" copy of an already-owned
        # book.
        series = Series(name="Safehold", author="David Weber")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)

        self.db.add(
            Book(
                title="Safehold Boxed Set 1: (Safehold Books 1-3)",
                author="David Weber",
                series_id=series.id,
                series_order=1,
                book_number=1.0,
                record_status="active",
                is_read=True,
            )
        )
        for number in [4, 5, 6, 7]:
            self.db.add(
                Book(
                    title=f"Safehold Book {number}",
                    author="David Weber",
                    series_id=series.id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=True,
                )
            )
        self.db.commit()

        candidates = [
            {
                "source": "google_books",
                "source_id": "gb-heresies-reprint",
                "title": "By Heresies Distressed (Safehold Book 3)",
                "authors": ["David Weber"],
                "published_date": "2010-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
            {
                "source": "google_books",
                "source_id": "gb-book8",
                "title": "Safehold Book 8",
                "authors": ["David Weber"],
                "published_date": "2024-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, series.id, emit_summary=False)

        self.assertEqual(
            [book["title"] for book in result["available_missing"]],
            ["Safehold Book 8"],
        )

    def test_compilation_listing_naming_multiple_owned_titles_is_rejected(self):
        # Regression (live bug): checking "Safehold" surfaced compilation
        # listings that spell out several already-owned book titles by name
        # instead of using a "Books 1-3"/"Boxed Set"/"Omnibus"/"Series,
        # Volume" label (which discovery_engine.looks_like_non_new_release
        # already catches -- see the DiscoveryEngineHelperTest above). This
        # is the series_agent-level backstop for a differently-worded
        # compilation that has no bundle keyword at all, just several owned
        # titles strung together, so it had no parseable number and no
        # bundle keyword and slipped through as a new "available" book.
        series = Series(name="Safehold", author="David Weber")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)

        owned_titles = [
            "Off Armageddon Reef",
            "By Schism Rent Asunder",
            "By Heresies Distressed",
            "A Mighty Fortress",
            "How Firm a Foundation",
        ]
        for index, title in enumerate(owned_titles, start=1):
            self.db.add(
                Book(
                    title=f"{title}: (Safehold Book {index})",
                    author="David Weber",
                    series_id=series.id,
                    series_order=index,
                    book_number=float(index),
                    record_status="active",
                    is_read=True,
                )
            )
        self.db.commit()

        candidates = [
            {
                "source": "google_books",
                "source_id": "gb-compilation-no-keyword",
                "title": (
                    "David Weber Reader's Companion: Off Armageddon Reef and By Schism Rent Asunder"
                ),
                "authors": ["David Weber"],
                "published_date": "2015-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
            {
                "source": "google_books",
                "source_id": "gb-book6",
                "title": "Safehold Book 6",
                "authors": ["David Weber"],
                "published_date": "2024-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, series.id, emit_summary=False)

        self.assertEqual(
            [book["title"] for book in result["available_missing"]],
            ["Safehold Book 6"],
        )

    def test_bare_title_with_no_series_reference_gets_a_synthesized_suffix(self):
        # Regression: Hardcover tracks series position as structured data
        # rather than embedding it in the title text, so it can return a
        # clean bare title like "Unmapped" with no series name or book
        # number anywhere in it -- making it hard to tell at a glance that
        # the newly added book belongs to this series at all.
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-13",
                "title": "Unmapped",
                "authors": ["Harmon Cooper"],
                "published_date": "2026-01-14",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": 13,
                "upcoming_hint": False,
            }
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(len(result["available_missing"]), 1)
        self.assertEqual(
            result["available_missing"][0]["title"],
            "Unmapped: (Cherry Blossom Girls Book 13)",
        )

    def test_missing_volume_recovery_does_not_downgrade_an_already_targeted_candidates_confidence(self):
        # Regression (live bug): when the missing-volume lookahead recovers
        # ANY gap, series_agent re-merges discovery["unified_candidates"]
        # (every candidate found so far) alongside the newly-recovered ones
        # through _filter_and_merge with a single blanket confidence value
        # based on used_author_fallback -- which used to silently downgrade
        # an already-"targeted" candidate (found by the earlier, real
        # targeted-search pass) to "author_fallback" too, purely because
        # *some* recovery happened this run. For a series whose real books
        # carry standalone titles with zero textual tie back to the series
        # name (e.g. "Desert Protocol" for "Cherry Blossom Girls" -- modeled
        # on the real live-bug case, Georgia Wagner's "Jonathan Hunt
        # Thriller Series"), that confidence is the ONLY signal
        # belongs_to_series' targeted_with_number check has to go on, so the
        # downgrade meant "Check Now" always reported zero new books despite
        # discovery correctly finding them.
        existing_candidate = discovery_engine.UnifiedCandidate(
            title="Desert Protocol",
            authors=["Harmon Cooper"],
            series_name="Cherry Blossom Girls",
            series_number=7.0,
            isbn13="9780000000007",
            source_provenance=[
                {
                    "source": "hardcover",
                    "source_id": "hc-7",
                    "source_url": None,
                    "language": "",
                    "series_number_hint": 7,
                    "confidence": "targeted",
                    "upcoming_hint": False,
                }
            ],
        )
        recovered_candidate = discovery_engine.UnifiedCandidate(
            title="The Levee Ghosts",
            authors=["Harmon Cooper"],
            series_name="Cherry Blossom Girls",
            series_number=10.0,
            isbn13="9780000000010",
            source_provenance=[
                {
                    "source": "web_search",
                    "source_id": "ws-10",
                    "source_url": None,
                    "language": "",
                    "series_number_hint": 10,
                    "upcoming_hint": False,
                }
            ],
        )
        # A stray same-author hit with no prior confidence, a number that is
        # NOT one the lookahead specifically recovered, and no textual tie to
        # the series -- must stay rejected. Confirms the fix is scoped to
        # candidates the lookahead actually searched for, not a blanket
        # "any unconfirmed candidate near a gap is fine" loosening.
        stray_candidate = discovery_engine.UnifiedCandidate(
            title="Unrelated Standalone Thriller",
            authors=["Harmon Cooper"],
            series_name="Cherry Blossom Girls",
            series_number=11.0,
            isbn13="9780000000011",
            source_provenance=[
                {
                    "source": "hardcover",
                    "source_id": "hc-11",
                    "source_url": None,
                    "language": "",
                    "series_number_hint": 11,
                    "upcoming_hint": False,
                }
            ],
        )
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-7",
                "title": "Desert Protocol",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-01-01",
                "isbn13": "9780000000007",
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": 7,
                "upcoming_hint": False,
            }
        ]
        skeleton_result = {
            "candidates": [existing_candidate, recovered_candidate, stray_candidate],
            "expected_total": 11,
            "missing_numbers": [7, 10, 11],
            "recovered_numbers": [10],
        }
        with self._mock_discovery(
            candidates, used_author_fallback=True, unified_candidates=[existing_candidate]
        ), patch.object(discovery_engine, "_reconstruct_series_skeleton", return_value=skeleton_result):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        available_titles = {book["title"] for book in result["available_missing"]}
        upcoming_titles = {book["title"] for book in result["upcoming_books"]}
        all_titles = available_titles | upcoming_titles
        # The pre-existing "targeted" candidate must survive -- its title has
        # no textual tie to "Cherry Blossom Girls" at all, so only a
        # preserved "targeted" confidence (via targeted_with_number) can
        # clear belongs_to_series for it. (Gets the series suffix appended
        # since the raw title itself never references the series -- see
        # _title_references_series/display_title in run_series_check.)
        self.assertIn("Desert Protocol: (Cherry Blossom Girls Book 7)", all_titles)
        # The brand-new recovered candidate never had a "targeted"
        # confidence to preserve, but its number (10) IS one the
        # missing-volume lookahead specifically searched for -- that
        # narrow, number-specific query is at least as trustworthy as the
        # plain targeted pass, so it's tagged "missing_volume_recovery" and
        # accepted the same way, rather than falling back to the weaker
        # "author_fallback" default and being rejected for lacking any
        # textual tie to the series name.
        self.assertIn("The Levee Ghosts: (Cherry Blossom Girls Book 10)", all_titles)
        # The stray hit's number was never actually recovered by the
        # lookahead, so it gets no special trust -- still correctly
        # rejected for lacking both a strong confidence and any textual
        # tie to the series.
        self.assertNotIn("Unrelated Standalone Thriller: (Cherry Blossom Girls Book 11)", all_titles)

    def test_no_author_on_file_returns_empty_result_without_calling_apis(self):
        series = Series(name="No Author Series")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)

        with patch("discovery_engine.discover_candidates_for_series") as mock_discover:
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, series.id, emit_summary=False)

        mock_discover.assert_not_called()
        self.assertEqual(result["reason"], "series-missing-author")
        self.assertFalse(result["found"])


class Phase4DiagnosticsTest(unittest.TestCase):
    """Phase 4 shadow-mode diagnostics, end to end through
    run_series_check: new_volume_flags, external_gap_ratio and
    drop_explanations reach the series_external_reality log entry, and
    nothing about them reaches the returned result.

    A plain sibling of SeriesCheckIntegrationTest rather than a subclass,
    so the parent's suite doesn't get re-run against this fixture -- the
    owned-books fixture (1-6, 8, 9) is duplicated deliberately.
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
        # Any test module that imports `main` runs Alembic on import, and
        # Alembic's fileConfig() disables every logger that already exists
        # -- including this one, which assertLogs cannot see through. So
        # whether these tests pass would otherwise depend on which other
        # files pytest happened to collect alongside them.
        agent_logger = logging.getLogger("agents.series_agent")
        was_disabled = agent_logger.disabled
        agent_logger.disabled = False
        self.addCleanup(setattr, agent_logger, "disabled", was_disabled)

        self.db = self.SessionLocal()
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

        for number in [1, 2, 3, 4, 5, 6, 8, 9]:
            self.db.add(
                Book(
                    title=f"Cherry Blossom Girls Book {number}",
                    author="Harmon Cooper",
                    series_id=series.id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=False,
                )
            )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _mock_discovery(self, candidates, **overrides):
        result = {
            "candidates": candidates,
            "provider_failures": [],
            "all_providers_failed": False,
            "used_author_fallback": False,
        }
        result.update(overrides)
        return patch("discovery_engine.discover_candidates_for_series", return_value=result)

    @staticmethod
    def _raw_candidate(number, title=None, **overrides):
        candidate = {
            "source": "hardcover",
            "source_id": f"hc-{number}",
            "title": title or f"Cherry Blossom Girls Book {number}",
            "authors": ["Harmon Cooper"],
            "published_date": "2024-02-20",
            "isbn13": None,
            "source_url": None,
            "language": "",
            "confidence": "targeted",
            "series_number_hint": number,
            "upcoming_hint": False,
        }
        candidate.update(overrides)
        return candidate

    @staticmethod
    def _unified_candidate(number, total_hint):
        # series_number lives at the top level (Phase 3.5 reads it off the
        # model) while the total hint lives in source_provenance[0], which
        # is where external_expected_total is derived from.
        return discovery_engine.UnifiedCandidate(
            title=f"Cherry Blossom Girls Book {number}",
            authors=["Harmon Cooper"],
            series_name="Cherry Blossom Girls",
            series_number=float(number),
            source_provenance=[{"source": "hardcover", "series_total_hint": total_hint}],
        )

    def _run_and_capture(self, candidates, **overrides):
        """Runs a check with the web-search providers disabled (so the
        result never depends on ambient API keys) and returns the parsed
        series_external_reality payload alongside the result.
        """
        with patch.dict(os.environ, {"BRAVE_SEARCH_API_KEY": "", "ANTHROPIC_API_KEY": ""}), self._mock_discovery(
            candidates, **overrides
        ), self.assertLogs("agents.series_agent", level="INFO") as captured:
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        payloads = [
            json.loads(record.getMessage().split("series_external_reality: ", 1)[1])
            for record in captured.records
            if "series_external_reality: " in record.getMessage()
        ]
        self.assertEqual(len(payloads), 1)
        return result, payloads[0]

    def test_new_volume_flag_is_true_for_an_externally_expected_unowned_number(self):
        # External total of 10 against owned 1-6, 8, 9: volumes 7 and 10
        # are the externally-expected gaps, and the candidate fills 7.
        result, payload = self._run_and_capture(
            [self._raw_candidate(7)],
            unified_candidates=[self._unified_candidate(7, 10)],
        )

        self.assertEqual(payload["external_expected_total"], 10)
        self.assertEqual(payload["external_total_hint_count"], 1)
        self.assertEqual(payload["external_missing_vs_owned"], [7, 10])
        # Phase 3.5's own list subtracts the discovered candidate too,
        # which is exactly why is_new_volume can't be derived from it.
        self.assertEqual(payload["external_missing_numbers"], [10])
        self.assertEqual(payload["external_gap_ratio"], 0.2)
        self.assertEqual(payload["owned_books_total"], 8)
        self.assertEqual(payload["owned_books_with_numbers"], 8)

        self.assertEqual(len(payload["new_volume_flags"]), 1)
        flag = payload["new_volume_flags"][0]
        self.assertTrue(flag["is_new_volume"])
        self.assertTrue(flag["belongs_to_series"])
        self.assertFalse(flag["suppressed_as_known"])
        self.assertEqual(flag["series_number"], 7)

        # Shadow mode: the live result is exactly what it was before
        # Phase 4, and carries none of its fields.
        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)
        for field in ["new_volume_flags", "external_gap_ratio", "drop_explanations", "external_missing_vs_owned"]:
            self.assertNotIn(field, result)

    def test_new_volume_flag_is_false_for_an_already_owned_number(self):
        result, payload = self._run_and_capture(
            [self._raw_candidate(6)],
            unified_candidates=[self._unified_candidate(6, 10)],
        )

        flag = payload["new_volume_flags"][0]
        self.assertFalse(flag["is_new_volume"])
        # Owned book 6 exists, so the candidate is suppressed as known --
        # and Phase 4 still records that it was scanned.
        self.assertTrue(flag["belongs_to_series"])
        self.assertTrue(flag["suppressed_as_known"])
        self.assertEqual(result["available_missing"], [])

    def test_diagnostics_degrade_to_none_without_any_external_total(self):
        _, payload = self._run_and_capture(
            [self._raw_candidate(7)],
            unified_candidates=[self._unified_candidate(7, None)],
        )

        self.assertIsNone(payload["external_expected_total"])
        self.assertEqual(payload["external_total_hint_count"], 0)
        self.assertEqual(payload["external_missing_vs_owned"], [])
        self.assertIsNone(payload["external_gap_ratio"])
        self.assertFalse(payload["new_volume_flags"][0]["is_new_volume"])

    def test_suppressed_known_candidate_produces_a_readable_drop_explanation(self):
        _, payload = self._run_and_capture(
            [self._raw_candidate(6)],
            unified_candidates=[self._unified_candidate(6, 10)],
        )

        explanations = payload["drop_explanations"]
        self.assertEqual(payload["drop_explanations_total"], len(explanations))
        self.assertEqual(payload["drop_explanation_counts"]["already_known:suppressed_as_known"], 1)
        suppressed = [entry for entry in explanations if entry["reason"] == "suppressed_as_known"]
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(suppressed[0]["title"], "Cherry Blossom Girls Book 6")
        self.assertEqual(suppressed[0]["series_number"], 6)
        self.assertEqual(
            suppressed[0]["explanation"], "Candidate suppressed because it matches an already-owned book."
        )
        # Superseded by drop_explanations, which carries every one of its
        # fields plus the explanation text.
        self.assertNotIn("drop_diagnostics", payload)

    def test_a_failing_drop_explanation_helper_does_not_disturb_the_other_fields(self):
        with patch.object(discovery_engine, "compute_drop_explanations", side_effect=RuntimeError("boom")):
            result, payload = self._run_and_capture(
                [self._raw_candidate(7)],
                unified_candidates=[self._unified_candidate(7, 10)],
            )

        self.assertEqual(payload["drop_explanations"], [])
        self.assertEqual(payload["drop_explanations_total"], 0)
        self.assertEqual(payload["drop_explanation_counts"], {})
        # Isolated: the new-volume group is unaffected.
        self.assertEqual(payload["external_gap_ratio"], 0.2)
        self.assertTrue(payload["new_volume_flags"][0]["is_new_volume"])
        self.assertTrue(result["found"])

    def test_a_failing_gap_helper_blanks_its_whole_dependent_group(self):
        # external_missing_vs_owned feeds both the ratio and the flags, so
        # they have to fail with it -- logging 0.0/false off its empty
        # fallback would read as "series complete, nothing new".
        with patch.object(discovery_engine, "compute_external_missing_vs_owned", side_effect=RuntimeError("boom")):
            result, payload = self._run_and_capture(
                [self._raw_candidate(7)],
                unified_candidates=[self._unified_candidate(7, 10)],
            )

        self.assertEqual(payload["external_missing_vs_owned"], [])
        self.assertIsNone(payload["external_gap_ratio"])
        self.assertEqual(payload["new_volume_flags"], [])
        # Drop explanations are computed separately and still run.
        self.assertEqual(payload["drop_explanations_total"], len(payload["drop_explanations"]))
        # Still pure shadow mode: the live result is untouched by any of it.
        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)
        self.assertEqual(result["available_missing"][0]["series_number"], 7)

    def test_phase_4_does_not_change_the_live_result(self):
        candidates = [self._raw_candidate(7), self._raw_candidate(6)]
        unified = [self._unified_candidate(7, 10), self._unified_candidate(6, 10)]

        result, payload = self._run_and_capture(candidates, unified_candidates=unified)

        # Baseline: the same run with every Phase 4 helper raising must
        # produce a byte-identical result.
        with patch.object(discovery_engine, "compute_external_missing_vs_owned", side_effect=RuntimeError("boom")), (
            patch.object(discovery_engine, "compute_owned_number_coverage", side_effect=RuntimeError("boom"))
        ), patch.object(discovery_engine, "compute_drop_explanations", side_effect=RuntimeError("boom")):
            baseline, _ = self._run_and_capture(candidates, unified_candidates=unified)

        self.assertEqual(result, baseline)
        self.assertEqual(len(payload["new_volume_flags"]), 2)


class DiscoverMoreByAuthorTest(unittest.TestCase):
    """Integration tests for agents.series_agent.discover_more_by_author
    ("More by this author") against an in-memory database, with
    discovery_engine mocked so behavior is deterministic.
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
        self.series = Series(name="Cherry Blossom Girls", author="Harmon Cooper")
        self.db.add(self.series)
        self.db.commit()
        self.db.refresh(self.series)

        self.db.add(
            Book(
                title="Cherry Blossom Girls Book 7",
                author="Harmon Cooper",
                series_id=self.series.id,
                series_order=7,
                book_number=7.0,
                record_status="active",
                is_read=True,
                isbn13="9781111111111",
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _mock_discovery(self, candidates, **overrides):
        result = {"candidates": candidates, "provider_failures": [], "all_providers_failed": False}
        result.update(overrides)
        return patch("discovery_engine.discover_candidates_for_author", return_value=result)

    def test_no_author_returns_empty_result_without_calling_discovery(self):
        with patch("discovery_engine.discover_candidates_for_author") as mock_discover:
            result = discover_more_by_author(self.db, "")

        mock_discover.assert_not_called()
        self.assertEqual(result["candidates"], [])

    def test_already_owned_isbn_is_excluded(self):
        candidates = [
            {
                "source": "hardcover",
                "title": "Cherry Blossom Girls Book 7",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": "9781111111111",
                "source_url": None,
                "series_number_hint": 7,
                "upcoming_hint": False,
                "series_name_hint": "Cherry Blossom Girls",
            }
        ]
        with self._mock_discovery(candidates):
            result = discover_more_by_author(self.db, "Harmon Cooper")

        self.assertEqual(result["candidates"], [])

    def test_new_book_in_a_new_series_reports_no_matched_series(self):
        candidates = [
            {
                "source": "web_search",
                "title": "Space Colony One",
                "authors": ["Harmon Cooper"],
                "published_date": "2025-05-01",
                "isbn13": None,
                "source_url": "https://example.com/space-colony-one",
                "series_number_hint": 1,
                "upcoming_hint": False,
                "series_name_hint": "Space Colony",
            }
        ]
        with self._mock_discovery(candidates):
            result = discover_more_by_author(self.db, "Harmon Cooper")

        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["series_name"], "Space Colony")
        self.assertIsNone(candidate["matched_series_id"])
        self.assertEqual(candidate["status"], "available")
        self.assertEqual(candidate["release_date"], "2025-05-01")
        self.assertEqual(candidate["source_url"], "https://example.com/space-colony-one")

    def test_new_book_in_an_already_tracked_series_reports_the_matched_series_id(self):
        candidates = [
            {
                "source": "hardcover",
                "title": "Cherry Blossom Girls Book 10",
                "authors": ["Harmon Cooper"],
                "published_date": "",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": 10,
                "upcoming_hint": True,
                "series_name_hint": "Cherry Blossom Girls",
            }
        ]
        with self._mock_discovery(candidates):
            result = discover_more_by_author(self.db, "Harmon Cooper")

        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["matched_series_id"], self.series.id)
        self.assertEqual(candidate["status"], "upcoming")


class DiscoverSeriesByNameTest(unittest.TestCase):
    """Regression coverage for a live bug: "More by this author" for Glynn
    Stewart showed "Scattered Stars" as "Found 1 of ~6" (Hardcover's own
    series_total_hint said 6) but the broad author-wide sweep only ever
    turned up book 1, "Conviction" -- with no way to find the other 5
    without leaving the app. discover_series_by_name is the deeper,
    targeted on-demand follow-up for exactly that gap.
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

    def tearDown(self):
        self.db.close()

    def _mock_discovery(self, candidates, **overrides):
        result = {"candidates": candidates, "provider_failures": [], "all_providers_failed": False}
        result.update(overrides)
        return patch("discovery_engine.discover_candidates_for_series", return_value=result)

    def test_no_series_name_or_author_returns_empty_result_without_calling_discovery(self):
        with patch("discovery_engine.discover_candidates_for_series") as mock_discover:
            result = discover_series_by_name(self.db, "", "Glynn Stewart")
        mock_discover.assert_not_called()
        self.assertEqual(result["candidates"], [])

        with patch("discovery_engine.discover_candidates_for_series") as mock_discover:
            result = discover_series_by_name(self.db, "Scattered Stars", "")
        mock_discover.assert_not_called()
        self.assertEqual(result["candidates"], [])

    def test_queries_the_targeted_series_search_without_author_fallback(self):
        # allow_author_fallback=False is deliberate: discover_candidates_for_series's
        # fallback pass drops the series-name scoping entirely and sweeps the
        # author's whole bibliography -- which is exactly what the broad
        # discover_more_by_author pass already did. Allowing it here would
        # flood a "find the rest of this one series" result with unrelated
        # books from every other series this (often prolific) author writes.
        with patch("discovery_engine.discover_candidates_for_series", return_value={
            "candidates": [], "provider_failures": [], "all_providers_failed": False
        }) as mock_discover:
            discover_series_by_name(self.db, "Scattered Stars", "Glynn Stewart")

        mock_discover.assert_called_once()
        _, kwargs = mock_discover.call_args
        self.assertEqual(mock_discover.call_args[0], ("Scattered Stars", "Glynn Stewart"))
        self.assertFalse(kwargs["allow_author_fallback"])

    def test_finds_the_rest_of_a_series_the_broad_sweep_only_partially_covered(self):
        candidates = [
            {
                "source": "hardcover",
                "title": "Confederacy",
                "authors": ["Glynn Stewart"],
                "published_date": "2020-06-15",
                "description": "Book two of Scattered Stars.",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": 2,
                "upcoming_hint": False,
                "series_name_hint": "Scattered Stars",
                "series_total_hint": 6,
            },
            {
                "source": "hardcover",
                "title": "Conspiracy",
                "authors": ["Glynn Stewart"],
                "published_date": "2020-11-10",
                "description": "Book three of Scattered Stars.",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": 3,
                "upcoming_hint": False,
                "series_name_hint": "Scattered Stars",
                "series_total_hint": 6,
            },
        ]
        with self._mock_discovery(candidates):
            result = discover_series_by_name(self.db, "Scattered Stars", "Glynn Stewart")

        self.assertEqual(len(result["candidates"]), 2)
        titles = {c["title"] for c in result["candidates"]}
        self.assertEqual(titles, {"Confederacy", "Conspiracy"})

    def test_excludes_a_book_already_owned_in_this_series(self):
        series = Series(name="Scattered Stars", author="Glynn Stewart")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.db.add(Book(
            title="Conviction", author="Glynn Stewart", series_id=series.id, series_order=1,
            book_number=1.0, record_status="active", is_read=True, isbn13="9780000000001",
        ))
        self.db.commit()

        candidates = [
            {
                "source": "hardcover",
                "title": "Conviction",
                "authors": ["Glynn Stewart"],
                "published_date": "2020-01-27",
                "isbn13": "9780000000001",
                "source_url": None,
                "series_number_hint": 1,
                "upcoming_hint": False,
                "series_name_hint": "Scattered Stars",
            },
            {
                "source": "hardcover",
                "title": "Confederacy",
                "authors": ["Glynn Stewart"],
                "published_date": "2020-06-15",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": 2,
                "upcoming_hint": False,
                "series_name_hint": "Scattered Stars",
            },
        ]
        with self._mock_discovery(candidates):
            result = discover_series_by_name(self.db, "Scattered Stars", "Glynn Stewart")

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["title"], "Confederacy")


class DiscoverMoreByAuthorEditionNoiseRegressionTest(unittest.TestCase):
    """Regression coverage for a live bug: "More by this author" on a
    12-book series ("Unbound" by Nicoli Gonnella) returned ~20 rows that
    were almost entirely re-listings/alternate editions of already-owned
    books, tagged "Standalone" instead of the tracked series, because (a)
    the Hardcover series-name field was read from the wrong nesting level
    (see HardcoverProviderTest) and (b) already-owned matching only
    compared exact core_title_key text, with no fallback for a bare,
    number-less title and no de-duplication of same-book candidates
    returned by multiple providers/editions within one batch.
    """

    def setUp(self):
        # A fresh engine per test (rather than the class-shared-engine
        # pattern used elsewhere in this file) because this test class
        # relies on bare-title-uniqueness for owned-book matching --
        # leftover rows from a previous test in the class would silently
        # make every bare title "non-unique" and break that check.
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()
        self.series = Series(name="Unbound", author="Nicoli Gonnella")
        self.db.add(self.series)
        self.db.commit()
        self.db.refresh(self.series)

        # Mirrors the real owned catalog: 12 numbered books, each with its
        # own ISBN, titled with a "(Unbound Book N)"-style suffix.
        for number, bare_title in enumerate(
            ["Dissonance", "Silence", "Hunger", "Fury", "Threshold", "Expanse", "Abyss", "Vault", "Crown", "Empire", "Chains", "Ruin"],
            start=1,
        ):
            self.db.add(
                Book(
                    title=f"{bare_title}: (Unbound Book {number})",
                    author="Nicoli Gonnella",
                    series_id=self.series.id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=True,
                    isbn13=f"97800000000{number:02d}",
                )
            )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_editions_and_relistings_of_owned_books_are_fully_excluded(self):
        candidates = [
            # Hardcover, correctly series-tagged (post-fix): bare title,
            # structured position, no textual number.
            {
                "source": "hardcover",
                "title": "Ruin",
                "authors": ["Nicoli Gonnella"],
                "published_date": "2026-06-24",
                "isbn13": "9780000000012",
                "source_url": None,
                "series_number_hint": 12,
                "upcoming_hint": False,
                "series_name_hint": "Unbound",
            },
            # Hardcover, an edition with no featured_series data at all but
            # the number spelled out in the title text itself.
            {
                "source": "hardcover",
                "title": "Chains: A LitRPG Adventure: Unbound, Book 11",
                "authors": ["Nicoli Gonnella"],
                "published_date": "",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": None,
                "upcoming_hint": False,
                "series_name_hint": None,
            },
            # Google Books: bare title, different ISBN (different edition),
            # no series metadata at all.
            {
                "source": "google_books",
                "title": "Crown",
                "authors": ["Nicoli Gonnella"],
                "published_date": "2024",
                "isbn13": "9781637662717",
                "source_url": None,
                "series_number_hint": None,
                "upcoming_hint": False,
                "series_name_hint": None,
            },
            # OpenLibrary: bare title, no ISBN, no series metadata.
            {
                "source": "openlibrary",
                "title": "Dissonance",
                "authors": ["Nicoli Gonnella"],
                "published_date": "2022",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": None,
                "upcoming_hint": False,
                "series_name_hint": None,
            },
        ]
        with patch("discovery_engine.discover_candidates_for_author", return_value={
            "candidates": candidates, "provider_failures": [], "all_providers_failed": False
        }):
            result = discover_more_by_author(self.db, "Nicoli Gonnella")

        self.assertEqual(result["candidates"], [])

    def test_genuinely_new_book_still_surfaces_amid_owned_book_noise(self):
        candidates = [
            {
                "source": "openlibrary",
                "title": "Dissonance",
                "authors": ["Nicoli Gonnella"],
                "published_date": "2022",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": None,
                "upcoming_hint": False,
                "series_name_hint": None,
            },
            {
                "source": "hardcover",
                "title": "Reckoning",
                "authors": ["Nicoli Gonnella"],
                "published_date": "2027-01-15",
                "isbn13": "9780000000013",
                "source_url": "https://hardcover.app/books/reckoning",
                "series_number_hint": 13,
                "upcoming_hint": False,
                "series_name_hint": "Unbound",
            },
            # Same new book, also returned by Google Books with a different
            # (ebook) ISBN and no series metadata -- should collapse into
            # the one Hardcover-sourced row above, not appear separately.
            {
                "source": "google_books",
                "title": "Reckoning",
                "authors": ["Nicoli Gonnella"],
                "published_date": "2027-01-15",
                "isbn13": "9780000000099",
                "source_url": None,
                "series_number_hint": None,
                "upcoming_hint": False,
                "series_name_hint": None,
            },
        ]
        with patch("discovery_engine.discover_candidates_for_author", return_value={
            "candidates": candidates, "provider_failures": [], "all_providers_failed": False
        }):
            result = discover_more_by_author(self.db, "Nicoli Gonnella")

        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["title"], "Reckoning")
        self.assertEqual(candidate["matched_series_id"], self.series.id)
        self.assertEqual(candidate["series_number"], 13)
        self.assertEqual(candidate["source_url"], "https://hardcover.app/books/reckoning")


class DiscoverMoreByAuthorAlternateBrandingRegressionTest(unittest.TestCase):
    """Regression coverage for a live bug: "More by this author" for Glynn
    Stewart (48 owned books across 5 tracked series) still showed several
    already-owned books as "new"/"not yet tracked" after the edition-noise
    fix above, because:

    1) A candidate's guessed series name can carry an extra generic word
       ("Duchy of Terra Universe") that an exact-text comparison against
       the tracked series name ("Duchy of Terra") fails to match, so
       matched_series ends up None even though the series is tracked.
    2) Glynn Stewart re-releases/rebrands some already-owned main-series
       books under a differently-named product listing (e.g. "Starship's
       Mage: UnArcana Rebellion" repackaging books 6/8/9 of the main
       numbered "Starship's Mage" series) -- these candidates carry a
       number, so the old bare-title fallback (only used when no number was
       resolved) never ran, and no other check caught them either.
    """

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()

        self.duchy = Series(name="Duchy of Terra", author="Glynn Stewart")
        self.mage = Series(name="Starship's Mage", author="Glynn Stewart")
        self.db.add_all([self.duchy, self.mage])
        self.db.commit()
        self.db.refresh(self.duchy)
        self.db.refresh(self.mage)

        for number, title in [
            (1, "The Terran Privateer: (Duchy of Terra Book 1)"),
            (2, "Duchess of Terra: (Duchy of Terra Book 2)"),
            (3, "Terra and Imperium: (Duchy of Terra Book 3)"),
            (4, "Darkness Beyond: (Duchy of Terra Book 4)"),
        ]:
            self.db.add(Book(
                title=title, author="Glynn Stewart", series_id=self.duchy.id, series_order=number,
                book_number=float(number), record_status="active", is_read=True,
            ))

        for number, title in [
            (8, "Mountain of Mars (Starship's Mage Book 8)"),
            (9, "The Service of Mars (Starship's Mage Book 9)"),
        ]:
            self.db.add(Book(
                title=title, author="Glynn Stewart", series_id=self.mage.id, series_order=number,
                book_number=float(number), record_status="active", is_read=True,
            ))
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def _mock_discovery(self, candidates):
        return patch("discovery_engine.discover_candidates_for_author", return_value={
            "candidates": candidates, "provider_failures": [], "all_providers_failed": False
        })

    def test_owned_book_matches_tracked_series_despite_extra_branding_word(self):
        candidates = [
            {
                "source": "hardcover",
                "title": "Duchess of Terra",
                "authors": ["Glynn Stewart"],
                "published_date": "2017-02-09",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": 2,
                "upcoming_hint": False,
                "series_name_hint": "Duchy of Terra Universe",
            }
        ]
        with self._mock_discovery(candidates):
            result = discover_more_by_author(self.db, "Glynn Stewart")

        self.assertEqual(result["candidates"], [])

    def test_owned_book_republished_under_different_series_branding_is_excluded(self):
        candidates = [
            {
                "source": "hardcover",
                "title": "Mountain of Mars",
                "authors": ["Glynn Stewart"],
                "published_date": "2020-03-17",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": 3,
                "upcoming_hint": False,
                "series_name_hint": "Starship's Mage: UnArcana Rebellion",
            },
            {
                "source": "hardcover",
                "title": "The Service of Mars",
                "authors": ["Glynn Stewart"],
                "published_date": "2020-08-25",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": 4,
                "upcoming_hint": False,
                "series_name_hint": "Starship's Mage: UnArcana Rebellion",
            },
        ]
        with self._mock_discovery(candidates):
            result = discover_more_by_author(self.db, "Glynn Stewart")

        self.assertEqual(result["candidates"], [])

    def test_genuinely_new_series_is_not_matched_to_an_unrelated_tracked_series(self):
        # "Starship's Mage: Red Falcon" must stay distinct from the tracked
        # "Starship's Mage" -- same guard that prevents the earlier
        # cross-series contamination bug from reappearing here.
        candidates = [
            {
                "source": "hardcover",
                "title": "Interstellar Mage",
                "authors": ["Glynn Stewart"],
                "published_date": "2021-05-01",
                "isbn13": "9780000000501",
                "source_url": None,
                "series_number_hint": 1,
                "upcoming_hint": False,
                "series_name_hint": "Starship's Mage: Red Falcon",
            }
        ]
        with self._mock_discovery(candidates):
            result = discover_more_by_author(self.db, "Glynn Stewart")

        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertIsNone(candidate["matched_series_id"])
        self.assertEqual(candidate["series_name"], "Starship's Mage: Red Falcon")

    def test_placeholder_date_is_cleared_but_candidate_still_surfaces(self):
        candidates = [
            {
                "source": "openlibrary",
                "title": "Heart of Vengeance",
                "authors": ["Glynn Stewart"],
                "published_date": "2017-01-01",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": None,
                "upcoming_hint": False,
                "series_name_hint": None,
            }
        ]
        with self._mock_discovery(candidates):
            result = discover_more_by_author(self.db, "Glynn Stewart")

        self.assertEqual(len(result["candidates"]), 1)
        self.assertIsNone(result["candidates"][0]["release_date"])

    def test_carries_description_and_series_total_for_a_new_series_candidate(self):
        candidates = [
            {
                "source": "hardcover",
                "title": "Broken Prince",
                "authors": ["Glynn Stewart"],
                "published_date": "2026-03-26",
                "description": "The war is over. Their duty remains.",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": 5,
                "upcoming_hint": False,
                "series_name_hint": "House Adamant",
                "series_total_hint": 6,
            }
        ]
        with self._mock_discovery(candidates):
            result = discover_more_by_author(self.db, "Glynn Stewart")

        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["description"], "The war is over. Their duty remains.")
        self.assertEqual(candidate["series_total_books"], 6)
        self.assertIsNone(candidate["matched_series_is_finished"])
        self.assertIsNone(candidate["matched_series_total_books"])

    def test_carries_the_tracked_series_own_maturity_fields_for_a_matched_candidate(self):
        self.duchy.is_finished = True
        self.duchy.total_books = 4
        self.db.commit()

        candidates = [
            {
                "source": "hardcover",
                "title": "A New Duchy Book",
                "authors": ["Glynn Stewart"],
                "published_date": "2026-05-01",
                "description": "New installment.",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": 5,
                "upcoming_hint": False,
                "series_name_hint": "Duchy of Terra",
                "series_total_hint": None,
            }
        ]
        with self._mock_discovery(candidates):
            result = discover_more_by_author(self.db, "Glynn Stewart")

        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["matched_series_id"], self.duchy.id)
        # Tracked series' own fields win over anything the discovery batch
        # itself could guess -- more authoritative and kept up to date
        # independently of this one-off search.
        self.assertTrue(candidate["matched_series_is_finished"])
        self.assertEqual(candidate["matched_series_total_books"], 4)


class DiscoverMoreByAuthorDashSeriesSuffixRegressionTest(unittest.TestCase):
    """Regression coverage for a live bug: "More by this author" for
    Rebecca Yarros found several real new series (good), but every one of
    those same books *also* showed up a second time under "New standalone
    books" with a "<Title> - <Series> #<N>" suffix baked into the title
    (e.g. "A Little Too Close - Madigan Mountain #2" duplicating the
    already-grouped "A Little Too Close"), and an entire series ("Legacy")
    never formed a group at all because none of its listings had anything
    but this dash-suffixed format.
    """

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def _mock_discovery(self, candidates):
        return patch("discovery_engine.discover_candidates_for_author", return_value={
            "candidates": candidates, "provider_failures": [], "all_providers_failed": False
        })

    def test_dash_suffixed_duplicate_is_merged_into_the_cleanly_tagged_candidate(self):
        # discover_candidates_for_author (mocked here) already runs its own
        # _filter_and_merge internally, which is what actually resolves
        # series_name_hint via infer_series_hint_from_title_text for a
        # candidate whose only listing uses the dash-suffix format --
        # covered directly in DiscoverCandidatesForAuthorTest. This second
        # candidate's hint is populated here to mirror that real output,
        # since this test's job is the *dedup/grouping* layer downstream of
        # it, not re-proving the hint inference itself.
        candidates = [
            {
                "source": "google_books",
                "title": "A Little Too Close",
                "authors": ["Rebecca Yarros"],
                "published_date": "2022-10-11",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": 2,
                "upcoming_hint": False,
                "series_name_hint": "Madigan Mountain",
            },
            {
                "source": "hardcover",
                "title": "A Little Too Close - Madigan Mountain #2",
                "authors": ["Rebecca Yarros"],
                "published_date": None,
                "isbn13": None,
                "source_url": None,
                "series_number_hint": None,
                "upcoming_hint": False,
                "series_name_hint": "Madigan Mountain",
            },
        ]
        with self._mock_discovery(candidates):
            result = discover_more_by_author(self.db, "Rebecca Yarros")

        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["title"], "A Little Too Close")
        self.assertEqual(candidate["series_name"], "Madigan Mountain")

    def test_a_series_with_only_dash_suffixed_listings_still_forms_its_own_group(self):
        candidates = [
            {
                "source": "hardcover",
                "title": "Reason to Believe - Legacy #1",
                "authors": ["Rebecca Yarros"],
                "published_date": "2013-01-01",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": None,
                "upcoming_hint": False,
                "series_name_hint": "Legacy",
            },
            {
                "source": "hardcover",
                "title": "Ignite - Legacy #0.7",
                "authors": ["Rebecca Yarros"],
                "published_date": "2014-01-01",
                "isbn13": None,
                "source_url": None,
                "series_number_hint": None,
                "upcoming_hint": False,
                "series_name_hint": "Legacy",
            },
        ]
        with self._mock_discovery(candidates):
            result = discover_more_by_author(self.db, "Rebecca Yarros")

        self.assertEqual(len(result["candidates"]), 2)
        self.assertTrue(all(c["series_name"] == "Legacy" for c in result["candidates"]))
        titles = {c["title"] for c in result["candidates"]}
        self.assertEqual(titles, {"Reason to Believe", "Ignite"})


class ManualDeleteRecalculationTest(unittest.TestCase):
    """total_books tracks the highest known book number in the series (not
    a plain count), so deleting a book that isn't the highest-numbered one
    should leave total_books unchanged while read/unread counts still
    reflect the smaller active set.
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
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

        for number in [1, 2, 3, 4, 5, 6, 8, 9]:
            self.db.add(
                Book(
                    title=f"Book {number}",
                    author="Harmon Cooper",
                    series_id=series.id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=False,
                )
            )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_manual_delete_recounts_series_aggregates(self):
        keep_read = self.db.query(Book).filter(Book.series_id == self.series.id, Book.book_number == 2.0).first()
        delete_target = self.db.query(Book).filter(Book.series_id == self.series.id, Book.book_number == 1.0).first()
        self.assertIsNotNone(keep_read)
        self.assertIsNotNone(delete_target)
        keep_read.is_read = True
        self.db.commit()

        deleted = crud.delete_book(self.db, delete_target.id, profile_id="robbie")
        self.assertTrue(deleted)

        refreshed = self.db.query(Series).filter(Series.id == self.series.id).first()
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.total_books, 9)
        self.assertEqual(refreshed.read_count, 1)
        self.assertEqual(refreshed.unread_count, 6)


if __name__ == "__main__":
    unittest.main()
