import json
import logging
import os
import time
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import crud
import discovery_engine
import provider_io
from agents.series_agent import (
    SeriesIntelligenceAgent,
    _needs_review_to_skeleton_updates,
    discover_more_by_author,
    discover_series_by_name,
)
from database import Base
from models import Book, Series, SeriesCandidateNotification, SeriesSkeleton
from services.discovery_cache import DiscoveryCache
from services.discovery_telemetry import DiscoveryTelemetry


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


class NormalizeSeriesNameForQueryTest(unittest.TestCase):
    """discovery_text.normalize_series_name_for_query -- the LitRPG-
    discovery-plan query normalizer. Strips a trailing LitRPG-style
    genre-marketing subtitle from a series name for outgoing query text
    only; every other identity use of series_name is untouched (see the
    integration tests below and the function's own docstring).
    """

    def test_strips_a_litrpg_adventure_subtitle(self):
        self.assertEqual(
            discovery_engine.normalize_series_name_for_query(
                "He Who Fights with Monsters: A LitRPG Adventure"
            ),
            "He Who Fights with Monsters",
        )

    def test_strips_a_litrpg_progression_fantasy_subtitle(self):
        self.assertEqual(
            discovery_engine.normalize_series_name_for_query(
                "Ripple System: A LitRPG Progression Fantasy Adventure"
            ),
            "Ripple System",
        )

    def test_leaves_a_series_name_with_no_litrpg_subtitle_unchanged(self):
        self.assertEqual(
            discovery_engine.normalize_series_name_for_query("Percy Jackson and the Olympians"),
            "Percy Jackson and the Olympians",
        )

    def test_none_and_empty_input_return_empty_string(self):
        self.assertEqual(discovery_engine.normalize_series_name_for_query(None), "")
        self.assertEqual(discovery_engine.normalize_series_name_for_query(""), "")
        self.assertEqual(discovery_engine.normalize_series_name_for_query("   "), "")

    def test_a_name_that_becomes_empty_after_stripping_falls_back_to_the_original(self):
        # Defensive: the pattern can't actually reduce a real series name to
        # nothing on its own (it always requires a non-LitRPG stem before
        # the colon), but normalize_series_name_for_query still guards
        # against returning an empty query string for any edge case that
        # somehow strips everything.
        self.assertEqual(
            discovery_engine.normalize_series_name_for_query(": A LitRPG Adventure"),
            ": A LitRPG Adventure",
        )


class LitRpgQueryNormalizationIntegrationTest(unittest.TestCase):
    """Confirms normalize_series_name_for_query is actually wired into the
    outgoing query strings at each of its real call sites -- not just
    correct in isolation (see NormalizeSeriesNameForQueryTest above) -- and
    that every other use of series_name at those same call sites (fusion/
    contamination identity, gate/telemetry logging) still sees the
    original, unstripped string.
    """

    def setUp(self):
        self._env_patch = patch.dict(os.environ, {"SERPER_API_KEY": "", "ANTHROPIC_API_KEY": ""})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_fetch_all_providers_parallel_strips_subtitle_from_its_own_google_query_only(self):
        # google_query is the one query string _fetch_all_providers_parallel
        # builds internally from the raw `series_name` parameter -- everything
        # else here (hardcover_query, the openlibrary default) is whatever
        # the caller already passed in as targeted_query_text, deliberately
        # left un-stripped in this call so the two behaviors don't get
        # conflated with each other.
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]) as mock_hardcover, patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ) as mock_google, patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]) as mock_openlibrary:
            discovery_engine._fetch_all_providers_parallel(
                "Some Author",
                "Ripple System: A LitRPG Progression Fantasy Adventure",
                "Ripple System: A LitRPG Progression Fantasy Adventure Some Author",
                None,
                author="Some Author",
                enable_web_search=False,
            )

        mock_google.assert_called_once()
        self.assertEqual(mock_google.call_args[0][0], '"Ripple System" inauthor:"Some Author"')
        # Hardcover/OpenLibrary use targeted_query_text/openlibrary_query
        # verbatim -- normalizing those is each *caller's* responsibility
        # (see discover_candidates_for_series/_fetch_fallback_series_
        # providers/precheck_for_new_volumes tests below), not this
        # function's.
        self.assertEqual(
            mock_hardcover.call_args[0][0], "Ripple System: A LitRPG Progression Fantasy Adventure Some Author"
        )
        self.assertEqual(
            mock_openlibrary.call_args[0][0], "Ripple System: A LitRPG Progression Fantasy Adventure Some Author"
        )

    def test_discover_candidates_for_series_strips_subtitle_from_its_targeted_query_text(self):
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]), patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ) as mock_google, patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            discovery_engine.discover_candidates_for_series(
                "Ripple System: A LitRPG Progression Fantasy Adventure",
                "Some Author",
                allow_author_fallback=False,
            )

        self.assertEqual(mock_google.call_args[0][0], '"Ripple System" inauthor:"Some Author"')

    def test_precheck_for_new_volumes_strips_subtitle_from_its_query(self):
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]), patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ) as mock_google, patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            discovery_engine.precheck_for_new_volumes(
                "Ripple System: A LitRPG Progression Fantasy Adventure", "Some Author", ceiling=3.0
            )

        self.assertEqual(mock_google.call_args[0][0], '"Ripple System" inauthor:"Some Author"')

    def test_fallback_pass_strips_query_but_contamination_filtering_still_uses_the_real_series_name(self):
        # _fetch_fallback_series_providers builds its own google/openlibrary/
        # web-search query text from series_name (same as the targeted pass)
        # -- normalized here too -- but also runs _filter_cross_series_
        # contamination against series_name afterwards, which must keep
        # seeing the original, unstripped string so a hit explicitly tagged
        # for a *different* series is still dropped.
        with patch.dict(os.environ, {"SERPER_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"}), patch.object(
            discovery_engine, "_fetch_hardcover", return_value=[]
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]) as mock_google, patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ), patch.object(
            provider_io,
            "_fetch_serper_web_search",
            return_value=[{"title": "Unrelated Book", "url": "https://example.com/1", "description": ""}],
        ), patch.object(
            provider_io,
            "_structure_web_results_with_llm",
            return_value=[
                {
                    "result_index": 0,
                    "title": "Unrelated Book",
                    "series_name": "A Completely Different Series",
                    "book_number": 9,
                    "author_names": ["Some Author"],
                    "published_date": None,
                    "is_upcoming": False,
                    "isbn13": None,
                }
            ],
        ):
            fallback_results, _, _ = discovery_engine._fetch_fallback_series_providers(
                "Some Author",
                "Ripple System: A LitRPG Progression Fantasy Adventure",
                None,
                "Some Author",
                enable_fallback_web_search=True,
                discovery_drop_diagnostics=[],
                telemetry=None,
                cache=None,
                apify_budget=discovery_engine.ApifyCallBudget(),
            )

        self.assertEqual(mock_google.call_args[0][0], '"Ripple System" inauthor:"Some Author"')
        self.assertEqual(fallback_results["web"], [])


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

    def test_returns_none_instead_of_raising_when_the_llm_call_fails(self):
        # CR-2 regression: this function had no try/except at all around the
        # LLM call -- a raised exception (timeout, API error, etc.) would
        # propagate all the way up to the "Series Overview" button's request
        # handler as an unhandled 500 instead of degrading to "no overview
        # available", which is this function's own documented contract for
        # every other failure mode (missing key, no descriptions).
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("boom")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=mock_client
        ):
            result = discovery_engine.generate_series_overview(
                "Exile", "Glynn Stewart", [{"title": "Exile", "description": "A shackled Earth..."}]
            )
        self.assertIsNone(result)


class PrecheckForNewVolumesTest(unittest.TestCase):
    """Tests discovery_engine.precheck_for_new_volumes -- the catalog-only
    (no web search, no LLM) short-circuit check described in
    discovery_catchup_architecture_spec.md #7.2.
    """

    def setUp(self):
        self._env_patch = patch.dict(os.environ, {"SERPER_API_KEY": "", "ANTHROPIC_API_KEY": ""})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_returns_false_when_no_hardcover_hit_exceeds_ceiling(self):
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {
                    "source": "hardcover",
                    "authors": ["Georgia Wagner"],
                    "title": "The Jericho Siege",
                    "series_number_hint": 1,
                }
            ],
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ):
            found = discovery_engine.precheck_for_new_volumes("Jonathan Hunt", "Georgia Wagner", ceiling=18.0)

        self.assertFalse(found)

    def test_returns_true_when_a_hardcover_hit_exceeds_ceiling(self):
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {
                    "source": "hardcover",
                    "authors": ["Georgia Wagner"],
                    "title": "The Next Hunt",
                    "series_number_hint": 19,
                }
            ],
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ):
            found = discovery_engine.precheck_for_new_volumes("Jonathan Hunt", "Georgia Wagner", ceiling=18.0)

        self.assertTrue(found)

    def test_ignores_hits_from_a_non_matching_author(self):
        # A same-titled hit by an unrelated author must not trigger a full
        # loop just because its series_number_hint happens to be high.
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {
                    "source": "hardcover",
                    "authors": ["Some Other Author"],
                    "title": "Unrelated Book",
                    "series_number_hint": 99,
                }
            ],
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ):
            found = discovery_engine.precheck_for_new_volumes("Jonathan Hunt", "Georgia Wagner", ceiling=18.0)

        self.assertFalse(found)

    def test_never_issues_a_web_search_or_llm_call(self):
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]), patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]), patch.object(
            provider_io, "_fetch_serper_web_search"
        ) as mock_serper, patch.object(provider_io, "_structure_web_results_with_llm") as mock_llm:
            discovery_engine.precheck_for_new_volumes("Jonathan Hunt", "Georgia Wagner", ceiling=18.0)

        mock_serper.assert_not_called()
        mock_llm.assert_not_called()

    def test_returns_true_when_a_google_books_hit_title_infers_past_ceiling(self):
        # Google Books/OpenLibrary carry no structured series-position field
        # at all (unlike Hardcover's series_number_hint) -- this precheck
        # now also infers a number from the hit's own title text for those
        # two, rather than only ever being able to confirm "something new"
        # via Hardcover.
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]), patch.object(
            discovery_engine,
            "_fetch_google_books",
            return_value=[
                {
                    "source": "google_books",
                    "authors": ["Georgia Wagner"],
                    "title": "Jonathan Hunt Book 19",
                }
            ],
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            found = discovery_engine.precheck_for_new_volumes("Jonathan Hunt", "Georgia Wagner", ceiling=18.0)

        self.assertTrue(found)

    def test_returns_true_when_an_openlibrary_hit_title_infers_past_ceiling(self):
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]), patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ), patch.object(
            discovery_engine,
            "_fetch_openlibrary",
            return_value=[
                {
                    "source": "openlibrary",
                    "authors": ["Georgia Wagner"],
                    "title": "Jonathan Hunt Book 19",
                }
            ],
        ):
            found = discovery_engine.precheck_for_new_volumes("Jonathan Hunt", "Georgia Wagner", ceiling=18.0)

        self.assertTrue(found)

    def test_google_books_hit_below_ceiling_does_not_trigger(self):
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]), patch.object(
            discovery_engine,
            "_fetch_google_books",
            return_value=[
                {
                    "source": "google_books",
                    "authors": ["Georgia Wagner"],
                    "title": "Jonathan Hunt Book 3",
                }
            ],
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            found = discovery_engine.precheck_for_new_volumes("Jonathan Hunt", "Georgia Wagner", ceiling=18.0)

        self.assertFalse(found)

    def test_google_books_hit_from_non_matching_author_is_ignored(self):
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]), patch.object(
            discovery_engine,
            "_fetch_google_books",
            return_value=[
                {
                    "source": "google_books",
                    "authors": ["Some Other Author"],
                    "title": "Jonathan Hunt Book 19",
                }
            ],
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            found = discovery_engine.precheck_for_new_volumes("Jonathan Hunt", "Georgia Wagner", ceiling=18.0)

        self.assertFalse(found)


class WebSearchDiagnosticModeTest(unittest.TestCase):
    """Tests discover_candidates_for_series' diagnostic-only short-circuit:
    a Serper key with no Anthropic key means there's no way to structure
    raw hits into real candidates, so the whole normal pipeline (every
    provider, fusion, filtering) is skipped entirely and the caller gets
    back raw search snippets instead -- a standalone coverage probe for
    Serper's still-unverified indie/LitRPG/web-serial coverage, not a
    partial discovery run.
    """

    def test_web_search_enabled_without_llm_returns_raw_snippets_only(self):
        raw_snippets = [{"title": "Some Hit", "description": "d", "url": "https://example.com/1"}]
        # discover_candidates_for_series' diagnostic-probe short-circuit calls
        # _fetch_serper_web_search directly (not through _fetch_web_search),
        # from code that still lives in discovery_engine.py -- so this must be
        # patched at that level, unlike the unit-level _fetch_web_search tests.
        with patch.dict(os.environ, {"SERPER_API_KEY": "test-key", "ANTHROPIC_API_KEY": ""}), patch.object(
            discovery_engine, "_fetch_serper_web_search", return_value=raw_snippets
        ) as mock_serper, patch.object(discovery_engine, "_fetch_all_providers_parallel") as mock_parallel:
            result = discovery_engine.discover_candidates_for_series("Some Series", "Some Author")

        self.assertEqual(result["diagnostic_mode"], "web_search_coverage_probe")
        self.assertEqual(result["diagnostic_raw_web_snippets"], raw_snippets)
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["unified_candidates"], [])
        mock_serper.assert_called_once()
        # No other provider/pass runs at all -- this is a standalone probe,
        # not a partial discovery run.
        mock_parallel.assert_not_called()

    def test_probe_failure_is_caught_and_falls_through_to_normal_pipeline(self):
        # Regression: this lone Serper call used to have zero error
        # handling, so any 4xx/5xx from Serper crashed the entire Check
        # Now in under a second, before Hardcover/Google/OpenLibrary ever
        # got a chance to run -- see the Apify integration design chat's
        # follow-up finding. Now it's caught and falls through to the
        # normal pipeline instead (web search stays off either way, since
        # there's still no Anthropic key here).
        with patch.dict(os.environ, {"SERPER_API_KEY": "test-key", "ANTHROPIC_API_KEY": ""}), patch.object(
            discovery_engine, "_fetch_serper_web_search", side_effect=RuntimeError("403 Forbidden")
        ) as mock_serper, patch.object(
            discovery_engine, "_fetch_hardcover", return_value=[]
        ), patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            result = discovery_engine.discover_candidates_for_series("Some Series", "Some Author")

        mock_serper.assert_called_once()
        self.assertNotIn("diagnostic_mode", result)
        self.assertFalse(result["all_providers_failed"])

    def test_both_keys_present_runs_the_normal_pipeline_not_diagnostic_mode(self):
        with patch.dict(os.environ, {"SERPER_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"}), patch.object(
            provider_io, "_fetch_serper_web_search"
        ) as mock_serper, patch.object(
            discovery_engine, "_fetch_hardcover", return_value=[]
        ), patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=[]
        ):
            result = discovery_engine.discover_candidates_for_series("Some Series", "Some Author")

        self.assertNotIn("diagnostic_mode", result)
        self.assertNotIn("diagnostic_raw_web_snippets", result)

    def test_neither_key_present_runs_the_normal_pipeline_not_diagnostic_mode(self):
        with patch.dict(os.environ, {"SERPER_API_KEY": "", "ANTHROPIC_API_KEY": ""}), patch.object(
            provider_io, "_fetch_serper_web_search"
        ) as mock_serper, patch.object(
            discovery_engine, "_fetch_hardcover", return_value=[]
        ), patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            result = discovery_engine.discover_candidates_for_series("Some Series", "Some Author")

        self.assertNotIn("diagnostic_mode", result)
        mock_serper.assert_not_called()


class DiscoverCandidatesForSeriesTest(unittest.TestCase):
    """Tests discovery_engine.discover_candidates_for_series's merge/priority
    behavior across the catalog providers, with all network calls mocked
    out so this runs offline and deterministically. The web-search provider
    is disabled here (via cleared env vars) since it's exercised on its own
    in WebSearchProviderTest below -- these tests only care about the three
    original catalog APIs.
    """

    def setUp(self):
        self._env_patch = patch.dict(os.environ, {"SERPER_API_KEY": "", "ANTHROPIC_API_KEY": ""})
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

    def test_providers_succeed_but_every_candidate_gets_filtered_is_not_all_providers_failed(self):
        # TG-2: every provider returning real (non-empty) data that then
        # gets entirely filtered out (already-owned here) is a normal,
        # successful "nothing new" outcome -- not the same signal as every
        # provider call itself failing. all_providers_failed must mean "we
        # got no usable data at all", never "filtering left zero
        # candidates" (see _fetch_all_providers_parallel's own docstring).
        owned_title = "Cherry Blossom Girls Book 7"
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {
                    "source": "hardcover",
                    "source_id": "hc-1",
                    "title": owned_title,
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
                    "title": owned_title,
                    "authors": ["Harmon Cooper"],
                    "published_date": "2024-02-20",
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
                    "source_id": "ol-1",
                    "title": owned_title,
                    "authors": ["Harmon Cooper"],
                    "published_date": "2024-02-20",
                    "isbn13": None,
                    "source_url": None,
                    "language": "",
                }
            ],
        ):
            owned_key = discovery_engine.core_title_key(owned_title)
            result = discovery_engine.discover_candidates_for_series(
                "Cherry Blossom Girls", "Harmon Cooper", exclude_title_keys={owned_key}, allow_author_fallback=False
            )

        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["all_providers_failed"])
        self.assertEqual(result["provider_failures"], [])

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


class FilterAndMergeConfidenceTagTest(unittest.TestCase):
    """TG-8: direct unit test on _filter_and_merge -- a candidate's already-
    assigned confidence tag must survive being re-merged into a later,
    lower-trust pass, rather than getting unconditionally overwritten by
    that later call's blanket `confidence` argument. Regression this
    guards: Georgia Wagner's "Jonathan Hunt Thriller Series" -- author-
    fallback always triggers because providers under-index it, and an
    earlier version of this merge unconditionally re-stamped every
    candidate (including already-"targeted" ones) with "author_fallback",
    silently defeating series_agent.py's targeted_with_number acceptance
    check for the whole series.
    """

    def _raw(self, title: str = "Some Book", **overrides) -> dict:
        raw = {
            "source": "hardcover",
            "source_id": "hc-1",
            "title": title,
            "authors": ["Some Author"],
            "published_date": "2024-01-01",
            "isbn13": None,
            "source_url": None,
            "language": "",
            "series_number_hint": 5,
        }
        raw.update(overrides)
        return raw

    def test_targeted_confidence_survives_a_later_author_fallback_merge(self):
        first_pass = discovery_engine._filter_and_merge(
            [self._raw()], "Some Author", set(), confidence="targeted", series_name="Some Series"
        )
        self.assertEqual(len(first_pass), 1)
        self.assertEqual(first_pass[0]["confidence"], "targeted")

        second_pass = discovery_engine._filter_and_merge(
            first_pass, "Some Author", set(), confidence="author_fallback", series_name="Some Series"
        )
        self.assertEqual(len(second_pass), 1)
        self.assertEqual(second_pass[0]["confidence"], "targeted")

    def test_a_fresh_candidate_with_no_prior_confidence_gets_the_calls_blanket_tag(self):
        merged = discovery_engine._filter_and_merge(
            [self._raw()], "Some Author", set(), confidence="author_fallback", series_name="Some Series"
        )
        self.assertEqual(merged[0]["confidence"], "author_fallback")

    def test_growing_candidate_set_preserves_each_candidates_own_tag(self):
        already_targeted = self._raw(title="Book A", source_id="hc-a")
        already_targeted["confidence"] = "targeted"
        fresh_fallback_candidate = self._raw(title="Book B", source_id="hc-b")

        merged = discovery_engine._filter_and_merge(
            [already_targeted, fresh_fallback_candidate],
            "Some Author",
            set(),
            confidence="author_fallback",
            series_name="Some Series",
        )

        by_title = {c["title"]: c["confidence"] for c in merged}
        self.assertEqual(by_title["Book A"], "targeted")
        self.assertEqual(by_title["Book B"], "author_fallback")


class DiscoverCandidatesForAuthorTest(unittest.TestCase):
    """Tests discovery_engine.discover_candidates_for_author -- the lighter,
    non-series-scoped sibling used by "More by this author". Network calls
    mocked out for determinism.
    """

    def setUp(self):
        self._env_patch = patch.dict(os.environ, {"SERPER_API_KEY": "", "ANTHROPIC_API_KEY": ""})
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
        with patch.dict(os.environ, {"SERPER_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"}), patch.object(
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
            {"HARDCOVER_API_KEY": "test-key", "SERPER_API_KEY": "", "ANTHROPIC_API_KEY": ""},
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
        with patch.object(provider_io, "os") as mock_os, patch.object(
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
        with patch.object(provider_io, "os") as mock_os, patch.object(
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
        with patch.object(provider_io, "os") as mock_os, patch.object(
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
        with patch.object(provider_io, "os") as mock_os, patch.object(
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
        with patch.object(provider_io, "os") as mock_os, patch.object(
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
            provider_io,
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
            provider_io,
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
            provider_io,
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
        with patch.object(provider_io, "_fetch_hardcover") as mock_fetch:
            discovery_engine.backfill_missing_publication_dates(candidates, "Georgia Wagner; Scott Cook")

        mock_fetch.assert_not_called()
        self.assertEqual(candidates[0]["published_date"], "2026-01-01")

    def test_stops_after_the_lookup_cap_is_reached(self):
        candidates = [
            {"title": f"Book {n}", "authors": ["Georgia Wagner"], "isbn13": None, "published_date": ""}
            for n in range(discovery_engine.MAX_PUBLICATION_DATE_BACKFILL_LOOKUPS + 3)
        ]
        with patch.object(provider_io, "_fetch_hardcover", return_value=[]) as mock_fetch:
            discovery_engine.backfill_missing_publication_dates(candidates, "Georgia Wagner; Scott Cook")

        self.assertEqual(mock_fetch.call_count, discovery_engine.MAX_PUBLICATION_DATE_BACKFILL_LOOKUPS)

    def test_no_op_without_a_hardcover_api_key(self):
        candidates = [{"title": "Desert Protocol", "authors": ["Georgia Wagner"], "isbn13": None, "published_date": ""}]
        with patch.object(discovery_engine.os.environ, "get", return_value=""), patch.object(
            provider_io, "_fetch_hardcover"
        ) as mock_fetch:
            discovery_engine.backfill_missing_publication_dates(candidates, "Georgia Wagner; Scott Cook")

        mock_fetch.assert_not_called()
        self.assertEqual(candidates[0]["published_date"], "")


class VerifyMissingVolumeRecoveryDatesTest(unittest.TestCase):
    """Regression coverage for verify_missing_volume_recovery_dates -- live
    bug (2026-08-24): "Jonathan Hunt Thriller Series" Book 9 ("The Terror
    Plot") was recovered via the missing-volume lookahead pass with
    published_date misread by the LLM as a full year after its real release,
    wrongly classifying an already-available book as upcoming. Unlike
    backfill_missing_publication_dates (which only ever fills a *blank*
    date), this must override an already-present-but-wrong one -- but only
    for confidence=="missing_volume_recovery" candidates.
    """

    def setUp(self):
        patcher = patch.object(discovery_engine.os.environ, "get", side_effect=lambda key, default="": (
            "test-key" if key == "HARDCOVER_API_KEY" else default
        ))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_overrides_a_wrong_existing_date_via_hardcover(self):
        candidates = [
            {
                "title": "The Terror Plot",
                "authors": ["Georgia Wagner"],
                "isbn13": None,
                "published_date": "2027-03-06",
                "confidence": "missing_volume_recovery",
            }
        ]
        with patch.object(
            provider_io,
            "_fetch_hardcover",
            return_value=[
                {
                    "title": "The Terror Plot",
                    "authors": ["Georgia Wagner", "Scott Cook"],
                    "isbn13": "9798242219999",
                    "published_date": "2026-06-11",
                }
            ],
        ):
            discovery_engine.verify_missing_volume_recovery_dates(candidates, "Georgia Wagner; Scott Cook")

        self.assertEqual(candidates[0]["published_date"], "2026-06-11")
        self.assertEqual(candidates[0]["isbn13"], "9798242219999")

    def test_leaves_non_recovery_candidates_untouched_even_with_a_bad_date(self):
        candidates = [
            {
                "title": "The Terror Plot",
                "authors": ["Georgia Wagner"],
                "isbn13": None,
                "published_date": "2027-03-06",
                "confidence": "targeted",
            }
        ]
        with patch.object(provider_io, "_fetch_hardcover") as mock_fetch:
            discovery_engine.verify_missing_volume_recovery_dates(candidates, "Georgia Wagner; Scott Cook")

        mock_fetch.assert_not_called()
        self.assertEqual(candidates[0]["published_date"], "2027-03-06")

    def test_falls_back_to_apify_when_hardcover_has_no_match(self):
        candidates = [
            {
                "title": "The Terror Plot",
                "authors": ["Georgia Wagner"],
                "isbn13": None,
                "published_date": "2027-03-06",
                "confidence": "missing_volume_recovery",
            }
        ]
        with patch.object(provider_io, "_fetch_hardcover", return_value=[]), patch.object(
            provider_io, "apify_enabled", return_value=True
        ), patch.object(
            provider_io,
            "fetch_apify_candidates",
            return_value=[
                {
                    "title": "The Terror Plot",
                    "authors": ["Georgia Wagner", "Scott Cook"],
                    "isbn13": "9798242219999",
                    "published_date": "2026-06-11",
                }
            ],
        ) as mock_apify:
            discovery_engine.verify_missing_volume_recovery_dates(candidates, "Georgia Wagner; Scott Cook")

        mock_apify.assert_called_once()
        self.assertEqual(candidates[0]["published_date"], "2026-06-11")

    def test_no_change_when_neither_source_has_a_usable_date(self):
        candidates = [
            {
                "title": "The Terror Plot",
                "authors": ["Georgia Wagner"],
                "isbn13": None,
                "published_date": "2027-03-06",
                "confidence": "missing_volume_recovery",
            }
        ]
        with patch.object(provider_io, "_fetch_hardcover", return_value=[]), patch.object(
            provider_io, "apify_enabled", return_value=False
        ):
            discovery_engine.verify_missing_volume_recovery_dates(candidates, "Georgia Wagner; Scott Cook")

        self.assertEqual(candidates[0]["published_date"], "2027-03-06")

    def test_no_op_without_hardcover_key_or_apify(self):
        candidates = [
            {
                "title": "The Terror Plot",
                "authors": ["Georgia Wagner"],
                "isbn13": None,
                "published_date": "2027-03-06",
                "confidence": "missing_volume_recovery",
            }
        ]
        with patch.object(discovery_engine.os.environ, "get", return_value=""), patch.object(
            provider_io, "apify_enabled", return_value=False
        ), patch.object(provider_io, "_fetch_hardcover") as mock_fetch:
            discovery_engine.verify_missing_volume_recovery_dates(candidates, "Georgia Wagner; Scott Cook")

        mock_fetch.assert_not_called()
        self.assertEqual(candidates[0]["published_date"], "2027-03-06")

    def test_stops_after_the_lookup_cap_is_reached(self):
        candidates = [
            {
                "title": f"Book {n}",
                "authors": ["Georgia Wagner"],
                "isbn13": None,
                "published_date": "2027-01-01",
                "confidence": "missing_volume_recovery",
            }
            for n in range(discovery_engine.MAX_MISSING_VOLUME_DATE_VERIFICATION_LOOKUPS + 3)
        ]
        with patch.object(provider_io, "_fetch_hardcover", return_value=[]) as mock_fetch, patch.object(
            provider_io, "apify_enabled", return_value=False
        ):
            discovery_engine.verify_missing_volume_recovery_dates(candidates, "Georgia Wagner; Scott Cook")

        self.assertEqual(mock_fetch.call_count, discovery_engine.MAX_MISSING_VOLUME_DATE_VERIFICATION_LOOKUPS)

    def test_rejects_a_same_titled_hit_by_an_unrelated_author(self):
        candidates = [
            {
                "title": "The Winter Siege",
                "authors": ["Georgia Wagner"],
                "isbn13": None,
                "published_date": "2027-03-06",
                "confidence": "missing_volume_recovery",
            }
        ]
        with patch.object(
            provider_io,
            "_fetch_hardcover",
            return_value=[
                {
                    "title": "The Winter Siege",
                    "authors": ["Ariana Franklin", "Samantha Norman"],
                    "isbn13": "9780593070611",
                    "published_date": "2014-10-09",
                }
            ],
        ), patch.object(provider_io, "apify_enabled", return_value=False):
            discovery_engine.verify_missing_volume_recovery_dates(candidates, "Georgia Wagner; Scott Cook")

        self.assertEqual(candidates[0]["published_date"], "2027-03-06")

    def test_no_targets_short_circuits_before_any_lookup(self):
        candidates = [
            {"title": "Some Other Book", "authors": ["Georgia Wagner"], "published_date": "", "confidence": "targeted"}
        ]
        with patch.object(provider_io, "_fetch_hardcover") as mock_fetch:
            discovery_engine.verify_missing_volume_recovery_dates(candidates, "Georgia Wagner")

        mock_fetch.assert_not_called()


class UrlKeyedResolutionTest(unittest.TestCase):
    """TG-9: _structure_with_verdict_cache resolves the LLM's result_index
    against uncached_raw (the exact subset actually sent to the LLM), then
    immediately converts to a URL-keyed map -- never a raw, positional
    index into the original raw_results list. This is a real misattachment
    risk, not just a naming nuance: whenever some URLs are already cached,
    uncached_raw is a *different* list (shorter, differently ordered) than
    raw_results, so a result_index that's valid against uncached_raw would
    silently resolve to the wrong book under naive global-index resolution.
    """

    def test_result_index_resolves_against_uncached_subset_not_the_original_list(self):
        cache = DiscoveryCache()
        # url_a is already cached (accepted) -- excluded from uncached_raw.
        # Key is "some" (not "some series") -- FIX-LB-KEY: this cache is
        # keyed by _normalize_series_name_for_identity, which strips the
        # trailing "series" word.
        cache.set_llm_verdict("series", "some", "https://example.com/a", {"title": "Book A (cached)"})

        raw_results = [
            {"title": "Book A", "description": "", "url": "https://example.com/a"},
            {"title": "Book B", "description": "", "url": "https://example.com/b"},
            {"title": "Book C", "description": "", "url": "https://example.com/c"},
        ]

        # The LLM only ever sees uncached_raw == [Book B, Book C] (url_a
        # excluded) -- result_index=0 here means Book B. Under a naive
        # resolution against the ORIGINAL raw_results list, index 0 would
        # instead wrongly resolve to Book A.
        with patch.object(
            provider_io,
            "_structure_web_results_with_llm",
            return_value=[{"result_index": 0, "title": "Book B", "series_name": "Some Series", "book_number": 2}],
        ):
            verdicts = discovery_engine._structure_with_verdict_cache(
                raw_results, "Some Series", "Some Author", cache=cache, scope_type="series"
            )

        self.assertIn("https://example.com/b", verdicts)
        self.assertEqual(verdicts["https://example.com/b"]["title"], "Book B")
        # Book C got no verdict from the LLM (only index 0 was returned)
        # and was never cached, so it's simply absent -- not misattached.
        self.assertNotIn("https://example.com/c", verdicts)
        # Book A's pre-existing cached verdict survives untouched.
        self.assertIn("https://example.com/a", verdicts)
        self.assertEqual(verdicts["https://example.com/a"]["title"], "Book A (cached)")

    def test_a_result_index_out_of_range_for_the_uncached_subset_is_dropped_not_misattached(self):
        raw_results = [{"title": "Book A", "description": "", "url": "https://example.com/a"}]
        with patch.object(
            provider_io,
            "_structure_web_results_with_llm",
            return_value=[{"result_index": 5, "title": "Nonexistent", "series_name": "Some Series"}],
        ):
            verdicts = discovery_engine._structure_with_verdict_cache(
                raw_results, "Some Series", "Some Author", cache=None
            )
        self.assertEqual(verdicts, {})


class WebSearchProviderTest(unittest.TestCase):
    """Tests the Serper + Claude web-search discovery provider, with the
    HTTP call to Serper and the Anthropic client both mocked out so this
    runs offline, deterministically, and without spending real API credits.
    """

    def test_fetch_serper_web_search_returns_empty_without_api_key(self):
        with patch.dict(os.environ, {"SERPER_API_KEY": ""}):
            self.assertEqual(discovery_engine._fetch_serper_web_search("Some Series Author"), [])

    def test_fetch_serper_web_search_parses_response(self):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "organic": [
                {"title": "Book Announced", "snippet": "A new entry.", "link": "https://example.com/a"},
                {"title": "", "snippet": "Missing title, should be skipped", "link": "https://example.com/b"},
            ]
        }
        with patch.dict(os.environ, {"SERPER_API_KEY": "test-key"}), patch.object(
            discovery_engine.httpx, "post", return_value=mock_response
        ) as mock_post:
            results = discovery_engine._fetch_serper_web_search("Some Series Author")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Book Announced")
        self.assertEqual(results[0]["url"], "https://example.com/a")
        self.assertTrue(mock_post.called)

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

    def test_structure_web_results_returns_empty_instead_of_raising_when_the_llm_call_fails(self):
        # CR-2 regression: the try/finally around this call had no except
        # clause, so a raised exception (timeout, API error, etc.) left
        # `response` at its initial None and the very next line
        # (`response.content`) raised AttributeError instead of degrading
        # gracefully the way a JSON-parse failure already does a few lines
        # down -- see _reconcile_candidates_with_llm for the pattern this
        # now matches.
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("boom")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=mock_client
        ):
            result = discovery_engine._structure_web_results_with_llm(
                "Some Series", "Some Author", [{"title": "t", "description": "d", "url": "u"}]
            )
        self.assertEqual(result, [])

    def test_fetch_web_search_combines_serper_and_llm_structuring(self):
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
        with patch.object(provider_io, "_fetch_serper_web_search", return_value=raw_results), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=structured
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

    def test_fetch_web_search_preserves_fractional_book_numbers(self):
        # CR-3 regression: `int(book_number)` silently truncated a
        # companion/novella entry's fractional position (e.g. 3.5) from
        # LLM-structured web results to a whole number, while Hardcover's
        # own hint keeps the float (see _fetch_hardcover's series_position
        # for the identical rationale) -- an asymmetric loss of legitimate
        # fractional entries depending on which provider surfaced them.
        raw_results = [{"title": "Interlude", "description": "snippet", "url": "https://example.com/3.5"}]
        structured = [
            {
                "result_index": 0,
                "title": "Interlude",
                "book_number": 3.5,
                "author_names": ["Some Author"],
                "published_date": "2026-08-09",
                "is_upcoming": False,
                "isbn13": None,
            }
        ]
        with patch.object(provider_io, "_fetch_serper_web_search", return_value=raw_results), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=structured
        ):
            results = discovery_engine._fetch_web_search(["query"], "Some Series", "Some Author")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["series_number_hint"], 3.5)

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
        with patch.object(provider_io, "_fetch_serper_web_search", return_value=raw_results), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=structured
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

        def fake_serper(query, **kwargs):
            if "release date" in query:
                return refinement_raw
            return raw_results

        def fake_structure(series_name, author, results, **kwargs):
            if results == refinement_raw:
                return refinement_structured
            return first_pass_structured

        with patch.object(provider_io, "_fetch_serper_web_search", side_effect=fake_serper), patch.object(
            provider_io, "_structure_web_results_with_llm", side_effect=fake_structure
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

        def fake_serper(query, **kwargs):
            captured_queries.append(query)
            if "release date" in query:
                return []
            return raw_results

        with patch.object(provider_io, "_fetch_serper_web_search", side_effect=fake_serper), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=first_pass_structured
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

        with patch.object(provider_io, "_fetch_serper_web_search", return_value=raw_results), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=structured
        ):
            results = discovery_engine._fetch_web_search(["query"], "The First Peacemaker", "Some Author")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["published_date"], "")
        self.assertEqual(results[0]["upcoming_hint"], True)

    def test_refinement_pass_reuses_layer_a_and_b_cache_across_calls(self):
        # Regression: _refine_undated_web_search_result used to call
        # _fetch_serper_web_search/_structure_web_results_with_llm directly,
        # bypassing the per-job cache entirely -- the same undated candidate
        # recurring across rounds (a genuine undated preorder gets re-checked
        # every round) paid a fresh web-search+LLM call for the identical
        # "<title> release date" query every single time.
        cache = DiscoveryCache()
        raw_results = [{"title": "Listing", "description": "snippet, no date", "url": "https://example.com/1"}]
        first_pass_structured = [
            {
                "result_index": 0,
                "title": "Book One",
                "book_number": 1,
                "author_names": ["Some Author"],
                "published_date": None,
                "is_upcoming": False,
                "isbn13": None,
            }
        ]
        refinement_raw = [{"title": "Book One release date", "description": "Released 2024-01-01", "url": "https://example.com/1-date"}]
        refinement_structured = [
            {
                "result_index": 0,
                "title": "Book One",
                "book_number": 1,
                "author_names": ["Some Author"],
                "published_date": "2024-01-01",
                "is_upcoming": False,
                "isbn13": None,
            }
        ]

        def fake_serper(query, **kwargs):
            if "release date" in query:
                return refinement_raw
            return raw_results

        def fake_structure(series_name, author, results, **kwargs):
            if results == refinement_raw:
                return refinement_structured
            return first_pass_structured

        with patch.object(provider_io, "_fetch_serper_web_search", side_effect=fake_serper) as mock_serper, patch.object(
            provider_io, "_structure_web_results_with_llm", side_effect=fake_structure
        ) as mock_llm:
            first = discovery_engine._fetch_web_search(["query"], "Some Series", "Some Author", cache=cache)
            second = discovery_engine._fetch_web_search(["query"], "Some Series", "Some Author", cache=cache)

        # Round two's identical raw query and identical undated candidate
        # must be served entirely from cache -- one web-search call and one
        # LLM call per layer, not two.
        self.assertEqual(mock_serper.call_count, 2)  # 1 targeted query + 1 refinement query, round one only
        self.assertEqual(mock_llm.call_count, 2)  # 1 targeted structuring + 1 refinement structuring, round one only
        self.assertEqual(first[0]["published_date"], "2024-01-01")
        self.assertEqual(second[0]["published_date"], "2024-01-01")

    def test_refinement_batches_multiple_candidates_into_one_llm_call(self):
        # Cost optimization: N undated candidates in one round must cost one
        # dedicated web-search query each (query text has to stay per-title)
        # but only ONE combined LLM structuring call, not N separate ones.
        raw_results = [
            {"title": f"Generic listing {n}", "description": "snippet, no date", "url": f"https://example.com/{n}"}
            for n in range(3)
        ]
        first_pass_structured = [
            {
                "result_index": n,
                "title": f"Book {n}",
                "book_number": n,
                "author_names": ["Some Author"],
                "published_date": None,
                "is_upcoming": False,
                "isbn13": None,
            }
            for n in range(3)
        ]
        refinement_raw_by_title = {
            f"Book {n}": [{"title": f"Book {n} release date", "description": f"Released 2024-0{n+1}-01", "url": f"https://example.com/{n}-date"}]
            for n in range(3)
        }

        def fake_serper(query, **kwargs):
            for title, raw in refinement_raw_by_title.items():
                if title in query:
                    return raw
            return raw_results

        def fake_structure(series_name, author, results, **kwargs):
            if results == raw_results:
                return first_pass_structured
            # Refinement's own combined call -- structure every distinct
            # undated-candidate release-date result it was actually given,
            # regardless of how many separate web-search queries fed into it.
            structured = []
            for index, item in enumerate(results):
                for n in range(3):
                    if f"Book {n} release date" == item["title"]:
                        structured.append(
                            {
                                "result_index": index,
                                "title": f"Book {n}",
                                "book_number": n,
                                "author_names": ["Some Author"],
                                "published_date": f"2024-0{n + 1}-01",
                                "is_upcoming": False,
                                "isbn13": None,
                            }
                        )
            return structured

        with patch.object(provider_io, "_fetch_serper_web_search", side_effect=fake_serper) as mock_serper, patch.object(
            provider_io, "_structure_web_results_with_llm", side_effect=fake_structure
        ) as mock_llm:
            results = discovery_engine._fetch_web_search(["query"], "Some Series", "Some Author")

        # 1 targeted-pass web-search call + 3 per-candidate refinement calls.
        self.assertEqual(mock_serper.call_count, 4)
        # 1 targeted-pass LLM call + exactly 1 combined refinement LLM call
        # (not 3) -- this is the whole point of batching.
        self.assertEqual(mock_llm.call_count, 2)
        dates = {r["title"]: r["published_date"] for r in results}
        self.assertEqual(dates, {"Book 0": "2024-01-01", "Book 1": "2024-02-01", "Book 2": "2024-03-01"})

    def test_refinement_never_misattributes_a_date_across_candidates_with_similar_urls(self):
        # Guardrail regression: if two different undated candidates'
        # refinement queries happen to surface an overlapping URL (e.g. a
        # generic series-catalog page both searches turn up), a date
        # resolved for that URL must never be applied to the WRONG
        # candidate just because it appeared in that candidate's own raw
        # fetch too -- only a title match to the correct candidate counts.
        shared_url = "https://example.com/shared-catalog-page"
        raw_results = [
            {"title": "Book Alpha listing", "description": "no date", "url": "https://example.com/alpha"},
            {"title": "Book Beta listing", "description": "no date", "url": "https://example.com/beta"},
        ]
        first_pass_structured = [
            {"result_index": 0, "title": "Book Alpha", "book_number": 1, "author_names": ["Some Author"], "published_date": None, "is_upcoming": False, "isbn13": None},
            {"result_index": 1, "title": "Book Beta", "book_number": 2, "author_names": ["Some Author"], "published_date": None, "is_upcoming": False, "isbn13": None},
        ]

        def fake_serper(query, **kwargs):
            if "Book Alpha" in query:
                return [{"title": "series catalog", "description": "...", "url": shared_url}]
            if "Book Beta" in query:
                return [{"title": "series catalog", "description": "...", "url": shared_url}]
            return raw_results

        def fake_structure(series_name, author, results, **kwargs):
            if results == raw_results:
                return first_pass_structured
            # The shared catalog page only ever resolves to "Book Alpha" --
            # Book Beta must NOT receive this date just because its own
            # query also happened to surface the same URL.
            return [
                {
                    "result_index": 0,
                    "title": "Book Alpha",
                    "book_number": 1,
                    "author_names": ["Some Author"],
                    "published_date": "2024-05-01",
                    "is_upcoming": False,
                    "isbn13": None,
                }
            ]

        with patch.object(provider_io, "_fetch_serper_web_search", side_effect=fake_serper), patch.object(
            provider_io, "_structure_web_results_with_llm", side_effect=fake_structure
        ):
            results = discovery_engine._fetch_web_search(["query"], "Some Series", "Some Author")

        dates = {r["title"]: r["published_date"] for r in results}
        self.assertEqual(dates["Book Alpha"], "2024-05-01")
        self.assertEqual(dates["Book Beta"], "")  # left unresolved, not misattributed

    def test_fetch_web_search_refinement_is_capped_and_tolerates_failures(self):
        # Bound the extra cost: only the first WEB_SEARCH_DATE_REFINEMENT_MAX
        # undated candidates get a second look, and a refinement query
        # blowing up must not take down the whole discovery run.
        # Five distinct raw results/URLs (not the same URL five times) --
        # the Layer B LLM-verdict cache is URL-keyed, so a fixture reusing
        # one URL for all five would collapse down to a single verdict.
        raw_results = [
            {"title": f"Generic listing {n}", "description": "snippet, no date", "url": f"https://example.com/{n}"}
            for n in range(5)
        ]
        structured = [
            {
                "result_index": n,
                "title": f"Book {n}",
                "book_number": n,
                "author_names": ["Some Author"],
                "published_date": None,
                "is_upcoming": False,
                "isbn13": None,
            }
            for n in range(5)
        ]

        call_count = {"n": 0}

        def fake_serper(query, **kwargs):
            if "release date" in query:
                call_count["n"] += 1
                raise RuntimeError("boom")
            return raw_results

        with patch.object(provider_io, "_fetch_serper_web_search", side_effect=fake_serper), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=structured
        ):
            results = discovery_engine._fetch_web_search(["query"], "Series", "Some Author")

        self.assertEqual(len(results), 5)
        self.assertTrue(all(r["upcoming_hint"] for r in results))
        self.assertEqual(call_count["n"], discovery_engine.WEB_SEARCH_DATE_REFINEMENT_MAX)

    def test_fetch_web_search_skips_llm_items_with_out_of_range_index(self):
        raw_results = [{"title": "Some Result", "description": "snippet", "url": "https://example.com/1"}]
        structured = [{"result_index": 5, "title": "Bad Index", "book_number": None, "author_names": [], "is_upcoming": False}]
        with patch.object(provider_io, "_fetch_serper_web_search", return_value=raw_results), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=structured
        ):
            results = discovery_engine._fetch_web_search(["query"], "Series", "Author")
        self.assertEqual(results, [])

    def test_fetch_web_search_returns_empty_when_web_search_has_no_results(self):
        with patch.object(provider_io, "_fetch_serper_web_search", return_value=[]):
            results = discovery_engine._fetch_web_search(["query"], "Series", "Author")
        self.assertEqual(results, [])

    def test_fetch_web_search_merges_and_dedups_results_across_multiple_queries(self):
        # The lookahead queries ("<series> book <N>") run alongside the
        # generic query and can legitimately return overlapping pages --
        # those should be merged into one deduped raw-result list (by URL)
        # before the single LLM structuring call, not passed through twice.
        def fake_serper(query, **kwargs):
            if query == "generic":
                return [
                    {"title": "Series Book 1", "description": "d", "url": "https://example.com/1"},
                    {"title": "Series Book 9", "description": "d", "url": "https://example.com/9"},
                ]
            if query == "book 9":
                return [{"title": "Series Book 9", "description": "d", "url": "https://example.com/9"}]
            return []

        with patch.object(provider_io, "_fetch_serper_web_search", side_effect=fake_serper), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=[]
        ) as mock_structure:
            discovery_engine._fetch_web_search(["generic", "book 9"], "Series", "Author")

        passed_raw_results = mock_structure.call_args[0][2]
        self.assertEqual(len(passed_raw_results), 2)
        self.assertEqual({r["url"] for r in passed_raw_results}, {"https://example.com/1", "https://example.com/9"})

    def test_fetch_web_search_parallelizes_web_search_calls_with_a_bounded_worker_count(self):
        # Latency optimization: multiple cache-miss queries fire
        # concurrently rather than sequentially, but the concurrency itself
        # must be bounded (WEB_SEARCH_MAX_PARALLEL_WORKERS), not one
        # thread per query -- a wide lookahead window must not burst every
        # query at the web-search provider simultaneously.
        import threading

        max_concurrent = {"value": 0}
        current_concurrent = {"value": 0}
        lock = threading.Lock()

        def fake_serper(query, **kwargs):
            with lock:
                current_concurrent["value"] += 1
                max_concurrent["value"] = max(max_concurrent["value"], current_concurrent["value"])
            time.sleep(0.05)
            with lock:
                current_concurrent["value"] -= 1
            return [{"title": f"Result for {query}", "description": "d", "url": f"https://example.com/{query}"}]

        queries = [f"query {n}" for n in range(12)]
        with patch.object(provider_io, "_fetch_serper_web_search", side_effect=fake_serper), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=[]
        ):
            discovery_engine._fetch_web_search(queries, "Series", "Author")

        self.assertGreater(max_concurrent["value"], 1)  # actually ran concurrently, not sequentially
        self.assertLessEqual(max_concurrent["value"], discovery_engine.WEB_SEARCH_MAX_PARALLEL_WORKERS)

    def test_fetch_web_search_tolerates_one_query_failing_if_another_succeeds(self):
        def fake_serper(query, **kwargs):
            if query == "bad":
                raise RuntimeError("rate limited")
            return [{"title": "Found It", "description": "d", "url": "https://example.com/ok"}]

        with patch.object(provider_io, "_fetch_serper_web_search", side_effect=fake_serper), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=[]
        ) as mock_structure:
            discovery_engine._fetch_web_search(["bad", "good"], "Series", "Author")

        passed_raw_results = mock_structure.call_args[0][2]
        self.assertEqual(len(passed_raw_results), 1)

    def test_fetch_web_search_no_longer_raises_when_every_query_fails(self):
        # Superseded: a total Serper failure used to raise straight out of
        # _fetch_web_search, which skipped the Apify fallback sub-flow
        # entirely (see ApifyDiscoverySubFlowTest's
        # test_fetch_web_search_falls_back_to_apify_when_every_serper_query_fails).
        # With no apify_budget passed here, the fallback attempt itself is
        # a no-op, so this now returns [] rather than raising.
        with patch.object(provider_io, "_fetch_serper_web_search", side_effect=RuntimeError("boom")):
            results = discovery_engine._fetch_web_search(["bad1", "bad2"], "Series", "Author")
        self.assertEqual(results, [])

    def test_fetch_web_search_records_diagnostic_when_every_query_fails(self):
        diagnostics: list[dict] = []
        with patch.object(provider_io, "_fetch_serper_web_search", side_effect=RuntimeError("boom")):
            discovery_engine._fetch_web_search(
                ["bad1", "bad2"], "Series", "Author", diagnostics=diagnostics, pass_label="targeted"
            )
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["type"], "web_search_provider_unhealthy")
        self.assertEqual(diagnostics[0]["pass_label"], "targeted")
        self.assertIn("boom", diagnostics[0]["error"])

    def test_fetch_web_search_records_diagnostic_when_no_results_found(self):
        diagnostics: list[dict] = []
        with patch.object(provider_io, "_fetch_serper_web_search", return_value=[]):
            discovery_engine._fetch_web_search(["query"], "Series", "Author", diagnostics=diagnostics)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(diagnostics[0]["type"], "web_search_provider_unhealthy")
        self.assertEqual(diagnostics[0]["error"], "no results")

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
        with patch.dict(os.environ, {"SERPER_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"}), patch.object(
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
        with patch.dict(os.environ, {"SERPER_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"}), patch.object(
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
        with patch.dict(os.environ, {"SERPER_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"}), patch.object(
            discovery_engine, "_fetch_hardcover", return_value=[]
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ), patch.object(discovery_engine, "_fetch_web_search", return_value=[]) as mock_web_search:
            discovery_engine.discover_candidates_for_series(
                "The First Peacemaker", "Some Author", highest_owned_book_number=8
            )

        queries_used = mock_web_search.call_args[0][0]
        for number in range(9, 9 + discovery_engine.WEB_SEARCH_LOOKAHEAD_BOOKS):
            self.assertIn(f'"The First Peacemaker" Some Author book {number}', queries_used)
        self.assertNotIn(
            f'"The First Peacemaker" Some Author book {9 + discovery_engine.WEB_SEARCH_LOOKAHEAD_BOOKS}', queries_used
        )

    def test_discover_candidates_for_series_lookahead_query_disambiguates_generic_series_names(self):
        # Regression (live bug): "The World Book" by Jason Cheek is a real
        # series, but that name is also the brand of an actual, heavily
        # SEO'd encyclopedia sold in 20+ numbered volumes -- a bare
        # "<series> book <N>" lookahead query returned nothing but
        # encyclopedia listings and missed a real new release (book 21,
        # "Here We Go Again", released 2026-07-15). Including the author
        # name in the query disambiguates it.
        with patch.dict(os.environ, {"SERPER_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"}), patch.object(
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
        with patch.dict(os.environ, {"SERPER_API_KEY": "", "ANTHROPIC_API_KEY": ""}), patch.object(
            discovery_engine, "_fetch_hardcover", return_value=[]
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ), patch.object(discovery_engine, "_fetch_web_search") as mock_web_search:
            discovery_engine.discover_candidates_for_series("The First Peacemaker", "Some Author")

        mock_web_search.assert_not_called()


class ApifyDiscoverySubFlowTest(unittest.TestCase):
    """Tests _fetch_apify_discovery (the sequential Apify sub-flow attached
    to the Serper+Anthropic web-search pass, Check Now only for Phase 1)
    and its wiring into _fetch_web_search. discovery_engine.fetch_apify_candidates
    is patched directly here (imported from apify_provider) -- apify_provider.py's
    own test_apify_provider.py suite covers that function's internals
    (actor calls, budget, field normalization) in isolation.
    """

    def _structured_result(self, source_url, title="A Book"):
        return {
            "source": "web_search",
            "title": title,
            "authors": ["Some Author"],
            "published_date": "2026-01-01",
            "isbn13": None,
            "source_url": source_url,
            "series_number_hint": None,
            "upcoming_hint": False,
            "series_name_hint": None,
        }

    def test_returns_empty_without_a_budget(self):
        result = discovery_engine._fetch_apify_discovery(
            "query", [self._structured_result("https://amazon.com/dp/B0AAA1111")], None
        )
        self.assertEqual(result, [])

    def test_returns_empty_when_apify_not_enabled(self):
        with patch.dict(os.environ, {"APIFY_API_TOKEN": ""}):
            result = discovery_engine._fetch_apify_discovery(
                "query", [self._structured_result("https://amazon.com/dp/B0AAA1111")], discovery_engine.ApifyCallBudget()
            )
        self.assertEqual(result, [])

    def test_passes_top_1_amazon_url_from_structured_results_when_present(self):
        results = [
            self._structured_result("https://example.com/not-amazon"),
            self._structured_result("https://amazon.com/dp/B0AAA1111"),
            self._structured_result("https://amazon.com/dp/B0BBB2222"),
        ]
        budget = discovery_engine.ApifyCallBudget()
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch.object(
            provider_io, "fetch_apify_candidates", return_value=[]
        ) as mock_fetch:
            discovery_engine._fetch_apify_discovery("query", results, budget)

        mock_fetch.assert_called_once_with("query", ["https://amazon.com/dp/B0AAA1111"], budget)

    def test_passes_none_for_urls_when_no_amazon_url_present(self):
        results = [self._structured_result("https://example.com/not-amazon")]
        budget = discovery_engine.ApifyCallBudget()
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch.object(
            provider_io, "fetch_apify_candidates", return_value=[]
        ) as mock_fetch:
            discovery_engine._fetch_apify_discovery("query", results, budget)

        mock_fetch.assert_called_once_with("query", None, budget)

    def test_exception_from_fetch_apify_candidates_is_caught(self):
        budget = discovery_engine.ApifyCallBudget()
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch.object(
            provider_io, "fetch_apify_candidates", side_effect=RuntimeError("boom")
        ):
            result = discovery_engine._fetch_apify_discovery(
                "query", [self._structured_result("https://amazon.com/dp/B0AAA1111")], budget
            )
        self.assertEqual(result, [])

    def test_fetch_web_search_prepends_apify_candidates_ahead_of_web_search(self):
        raw_results = [{"title": "Peacemaker Book 8", "description": "snippet", "url": "https://amazon.com/dp/B0AAA1111"}]
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
        apify_candidate = {
            "source": "apify",
            "title": "The First Peacemaker",
            "authors": ["Some Author"],
            "published_date": "2026-08-09",
            "isbn13": "9781234567897",
            "source_url": "https://amazon.com/dp/B0AAA1111",
            "series_number_hint": None,
            "upcoming_hint": None,
            "series_name_hint": None,
            "asin": "B0AAA1111",
            "cover_image": "https://example.com/cover.jpg",
        }
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch.object(
            provider_io, "_fetch_serper_web_search", return_value=raw_results
        ), patch.object(provider_io, "_structure_web_results_with_llm", return_value=structured), patch.object(
            provider_io, "fetch_apify_candidates", return_value=[apify_candidate]
        ):
            results = discovery_engine._fetch_web_search(
                ["query"], "The First Peacemaker", "Some Author", apify_budget=discovery_engine.ApifyCallBudget()
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["source"], "apify")
        self.assertEqual(results[1]["source"], "web_search")

    def test_fetch_web_search_without_apify_budget_never_calls_apify(self):
        raw_results = [{"title": "Peacemaker Book 8", "description": "snippet", "url": "https://amazon.com/dp/B0AAA1111"}]
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
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch.object(
            provider_io, "_fetch_serper_web_search", return_value=raw_results
        ), patch.object(provider_io, "_structure_web_results_with_llm", return_value=structured), patch.object(
            provider_io, "fetch_apify_candidates"
        ) as mock_apify:
            results = discovery_engine._fetch_web_search(["query"], "The First Peacemaker", "Some Author")

        mock_apify.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], "web_search")

    def test_fetch_web_search_falls_back_to_apify_when_every_serper_query_fails(self):
        # The core fix (Apify integration design chat's final consensus):
        # a Serper 403/etc. on every query used to raise straight out of
        # _fetch_web_search, skipping the Apify sub-flow entirely. Now it
        # should fall through and attempt Apify's search-actor fallback.
        apify_candidate = {
            "source": "apify",
            "title": "The First Peacemaker",
            "authors": ["Some Author"],
            "published_date": "2026-08-09",
            "isbn13": None,
            "source_url": "https://amazon.com/dp/B0AAA1111",
            "series_number_hint": None,
            "upcoming_hint": None,
            "series_name_hint": None,
            "asin": "B0AAA1111",
            "cover_image": None,
        }
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch.object(
            provider_io, "_fetch_serper_web_search", side_effect=RuntimeError("403 Unauthorized")
        ), patch.object(provider_io, "fetch_apify_candidates", return_value=[apify_candidate]) as mock_fetch:
            results = discovery_engine._fetch_web_search(
                ["bad1", "bad2"], "The First Peacemaker", "Some Author", apify_budget=discovery_engine.ApifyCallBudget()
            )

        self.assertEqual(mock_fetch.call_args[0][0], "bad1")
        self.assertIsNone(mock_fetch.call_args[0][1])
        self.assertEqual(results, [apify_candidate])

    def test_fetch_web_search_falls_back_to_apify_when_serper_finds_nothing(self):
        apify_candidate = {
            "source": "apify",
            "title": "The First Peacemaker",
            "authors": ["Some Author"],
            "published_date": "2026-08-09",
            "isbn13": None,
            "source_url": "https://amazon.com/dp/B0AAA1111",
            "series_number_hint": None,
            "upcoming_hint": None,
            "series_name_hint": None,
            "asin": "B0AAA1111",
            "cover_image": None,
        }
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch.object(
            provider_io, "_fetch_serper_web_search", return_value=[]
        ), patch.object(provider_io, "fetch_apify_candidates", return_value=[apify_candidate]):
            results = discovery_engine._fetch_web_search(
                ["query"], "The First Peacemaker", "Some Author", apify_budget=discovery_engine.ApifyCallBudget()
            )

        self.assertEqual(results, [apify_candidate])

    def test_fetch_web_search_llm_rejecting_everything_does_not_trigger_apify_fallback(self):
        # Deliberately different from the empty/failed-raw-fetch case: raw
        # hits DID come back, the LLM just decided none of them are a real
        # book. That's a softer semantic failure where Apify's independent
        # Amazon search is more likely to add noise than signal -- see
        # _fetch_web_search's own docstring/comments.
        raw_results = [{"title": "Unrelated", "description": "snippet", "url": "https://example.com/1"}]
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch.object(
            provider_io, "_fetch_serper_web_search", return_value=raw_results
        ), patch.object(provider_io, "_structure_web_results_with_llm", return_value=[]), patch.object(
            provider_io, "fetch_apify_candidates"
        ) as mock_fetch:
            results = discovery_engine._fetch_web_search(
                ["query"], "Series", "Author", apify_budget=discovery_engine.ApifyCallBudget()
            )

        mock_fetch.assert_not_called()
        self.assertEqual(results, [])

    def test_discover_candidates_for_series_promotes_serper_failure_into_provider_failures(self):
        # Even when Apify substitutes successfully, Serper's own health
        # must still be visible in provider_failures (not silently
        # swallowed just because the pass produced candidates) -- see
        # _promote_web_search_health_diagnostics.
        apify_candidate = {
            "source": "apify",
            "title": "The First Peacemaker Book 8",
            "authors": ["Some Author"],
            "published_date": "2026-08-09",
            "isbn13": None,
            "source_url": "https://amazon.com/dp/B0AAA1111",
            "series_number_hint": 8,
            "upcoming_hint": False,
            "series_name_hint": None,
            "asin": "B0AAA1111",
            "cover_image": None,
        }
        with patch.dict(
            os.environ, {"SERPER_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key", "APIFY_API_TOKEN": "test-token"}
        ), patch.object(discovery_engine, "_fetch_hardcover", return_value=[]), patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]), patch.object(
            provider_io, "_fetch_serper_web_search", side_effect=RuntimeError("403 Unauthorized")
        ), patch.object(provider_io, "fetch_apify_candidates", return_value=[apify_candidate]):
            result = discovery_engine.discover_candidates_for_series("The First Peacemaker", "Some Author")

        web_failures = [f for f in result["provider_failures"] if f["provider"] == "web_search"]
        self.assertEqual(len(web_failures), 1)
        self.assertIn("403", web_failures[0]["error"])
        self.assertTrue(web_failures[0]["apify_fallback_used"])
        # Apify's substitution still means the run overall did not fail.
        self.assertFalse(result["all_providers_failed"])
        # The health marker must not leak into drop_diagnostics as a fake
        # per-candidate drop entry.
        self.assertEqual(result["drop_diagnostics"], [])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["source"], "apify")

    def test_discover_candidates_for_series_shares_one_budget_across_targeted_and_fallback_passes(self):
        # Regression guard for the Apify integration design chat's
        # consensus: APIFY_MAX_CALLS_PER_SERIES_RUN must apply across the
        # WHOLE series-check run, not reset per pass -- so the exact same
        # ApifyCallBudget instance must reach both
        # _fetch_all_providers_parallel calls.
        captured_budgets = []

        def fake_fetch_all_providers_parallel(*args, **kwargs):
            captured_budgets.append(kwargs.get("apify_budget"))
            return {"google": [], "openlibrary": [], "hardcover": [], "web": [], "_failures": {}}

        with patch.dict(os.environ, {"SERPER_API_KEY": "", "ANTHROPIC_API_KEY": ""}), patch.object(
            discovery_engine, "_fetch_all_providers_parallel", side_effect=fake_fetch_all_providers_parallel
        ), patch.object(discovery_engine, "_should_trigger_author_fallback", return_value=True):
            discovery_engine.discover_candidates_for_series("Some Series", "Some Author")

        self.assertEqual(len(captured_budgets), 2)
        self.assertIsNotNone(captured_budgets[0])
        self.assertIs(captured_budgets[0], captured_budgets[1])


class DiscoveryCacheTest(unittest.TestCase):
    """Tests the two-layer per-job cache (services/discovery_cache.py,
    architecture spec #2.4/#7.1) as it's exercised through
    _fetch_all_providers_parallel and _fetch_web_search.
    """

    def setUp(self):
        self._env_patch = patch.dict(os.environ, {"SERPER_API_KEY": "", "ANTHROPIC_API_KEY": ""})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def test_repeated_catalog_query_across_rounds_hits_cache_not_network(self):
        # Exercises _fetch_all_providers_parallel directly (rather than the
        # full discover_candidates_for_series) so this only tests Layer A
        # provider-fetch caching in isolation, not fallback-trigger logic.
        cache = DiscoveryCache()
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]) as mock_hardcover, patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ) as mock_google, patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]) as mock_openlibrary:
            for _round in range(2):
                discovery_engine._fetch_all_providers_parallel(
                    "Some Author",
                    "The First Peacemaker",
                    "The First Peacemaker Some Author",
                    None,
                    author="Some Author",
                    enable_web_search=False,
                    cache=cache,
                )

        # Round 2's identical query text is a cache hit -- each provider is
        # called exactly once across both "rounds".
        mock_hardcover.assert_called_once()
        mock_google.assert_called_once()
        mock_openlibrary.assert_called_once()
        self.assertEqual(cache.summary()["provider_fetch_hits"], 3)

    def test_cache_is_scoped_per_provider_not_shared_across_providers(self):
        cache = DiscoveryCache()
        cache.set_provider_fetch("hardcover", "same text", [{"title": "from hardcover"}])
        self.assertIs(cache.get_provider_fetch("google", "same text"), discovery_engine.CACHE_MISS)

    def test_llm_verdict_cache_avoids_resending_same_url_across_rounds(self):
        cache = DiscoveryCache()
        raw_results = [{"title": "Listing", "description": "snippet", "url": "https://example.com/1"}]
        structured = [
            {
                "result_index": 0,
                "title": "Book One",
                "book_number": 1,
                "author_names": ["Some Author"],
                "published_date": "2024-01-01",
                "is_upcoming": False,
                "isbn13": None,
            }
        ]

        with patch.object(provider_io, "_fetch_serper_web_search", return_value=raw_results), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=structured
        ) as mock_llm:
            first = discovery_engine._fetch_web_search(["query"], "Some Series", "Some Author", cache=cache)
            second = discovery_engine._fetch_web_search(["query"], "Some Series", "Some Author", cache=cache)

        # The LLM is only ever sent this URL once -- round two's identical
        # URL is served entirely from the Layer B verdict cache.
        mock_llm.assert_called_once()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["title"], "Book One")
        self.assertEqual(cache.summary()["llm_verdict_hits"], 1)

    def test_llm_verdict_negative_sentinel_prevents_resending_rejected_url(self):
        # A URL the LLM structured nothing useful for (no result_index match,
        # i.e. implicitly excluded) must be remembered as "checked, nothing
        # here" -- not re-sent to the LLM on the next round.
        cache = DiscoveryCache()
        raw_results = [{"title": "Junk listing", "description": "snippet", "url": "https://example.com/junk"}]

        with patch.object(provider_io, "_fetch_serper_web_search", return_value=raw_results), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=[]
        ) as mock_llm:
            discovery_engine._fetch_web_search(["query"], "Some Series", "Some Author", cache=cache)
            discovery_engine._fetch_web_search(["query"], "Some Series", "Some Author", cache=cache)

        mock_llm.assert_called_once()
        self.assertEqual(cache.summary()["llm_verdict_rejected"], 1)

    def test_missing_volume_pass_bypasses_cached_rejection_but_not_acceptance(self):
        # Regression (recall-gap root cause, discovery_catchup_architecture_
        # spec.md #8): the broad targeted pass's large batch wrongly
        # rejected book-number-bearing URLs it would correctly accept in
        # isolation, and that wrong rejection getting cached silently
        # poisoned the missing-volume interior-gap pass's own dedicated
        # retry of the exact same URL -- the whole point of that pass is to
        # give it a second, cleaner look. A cached rejection must be
        # bypassed (re-sent to the LLM) when pass_label="missing_volume",
        # but a cached acceptance must still be trusted as-is (no need to
        # redo confirmed-correct work).
        cache = DiscoveryCache()
        # Key is "some" (not "some series") -- FIX-LB-KEY.
        cache.set_llm_verdict("series", "some", "https://example.com/rejected", None)
        cache.set_llm_verdict(
            "series",
            "some",
            "https://example.com/accepted",
            {"title": "Already Confirmed", "book_number": 2, "author_names": ["Some Author"]},
        )
        raw_results = [
            {"title": "Rejected listing", "description": "snippet", "url": "https://example.com/rejected"},
            {"title": "Accepted listing", "description": "snippet", "url": "https://example.com/accepted"},
        ]
        fresh_structured = [
            {
                "result_index": 0,
                "title": "Actually A Real Book",
                "book_number": 10,
                "author_names": ["Some Author"],
                "published_date": "2024-01-01",
                "is_upcoming": False,
                "isbn13": None,
            }
        ]

        with patch.object(provider_io, "_fetch_serper_web_search", return_value=raw_results), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=fresh_structured
        ) as mock_llm:
            results = discovery_engine._fetch_web_search(
                ["query"], "Some Series", "Some Author", cache=cache, pass_label="missing_volume"
            )

        # Only the previously-rejected URL gets re-sent to the LLM -- the
        # already-accepted one is still served from cache untouched.
        mock_llm.assert_called_once()
        sent_raw = mock_llm.call_args.args[2]
        self.assertEqual([item["url"] for item in sent_raw], ["https://example.com/rejected"])

        titles = {item["title"] for item in results}
        self.assertIn("Actually A Real Book", titles)
        self.assertIn("Already Confirmed", titles)

    def test_non_missing_volume_pass_still_trusts_cached_rejection(self):
        # The bypass is scoped specifically to the missing_volume pass --
        # every other pass_label (targeted, author_fallback, the default,
        # etc.) must keep trusting a cached rejection exactly as before.
        cache = DiscoveryCache()
        # Key is "some" (not "some series") -- FIX-LB-KEY.
        cache.set_llm_verdict("series", "some", "https://example.com/rejected", None)
        raw_results = [{"title": "Rejected listing", "description": "snippet", "url": "https://example.com/rejected"}]

        with patch.object(provider_io, "_fetch_serper_web_search", return_value=raw_results), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=[]
        ) as mock_llm:
            results = discovery_engine._fetch_web_search(["query"], "Some Series", "Some Author", cache=cache)

        mock_llm.assert_not_called()
        self.assertEqual(results, [])

    def test_llm_verdict_cache_is_scoped_by_series_name_not_bare_url(self):
        # Same URL surfacing under two different series-scoped searches
        # (e.g. a cross-series-contamination edge case) must not leak one
        # series' cached verdict into the other's results.
        cache = DiscoveryCache()
        raw_results = [{"title": "Ambiguous listing", "description": "snippet", "url": "https://example.com/shared"}]
        structured = [
            {
                "result_index": 0,
                "title": "Book For Series A",
                "book_number": 1,
                "author_names": ["Some Author"],
                "published_date": "2024-01-01",
                "is_upcoming": False,
                "isbn13": None,
            }
        ]

        with patch.object(provider_io, "_fetch_serper_web_search", return_value=raw_results), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=structured
        ) as mock_llm:
            discovery_engine._fetch_web_search(["query"], "Series A", "Some Author", cache=cache)
            discovery_engine._fetch_web_search(["query"], "Series B", "Some Author", cache=cache)

        self.assertEqual(mock_llm.call_count, 2)

    def test_mixed_cached_and_fresh_urls_preserve_original_raw_order(self):
        # Reassembly must follow raw_results' own order, not "fresh then
        # cached" or vice versa.
        cache = DiscoveryCache()
        # Key is "some" (not "some series") -- FIX-LB-KEY.
        cache.set_llm_verdict(
            "series",
            "some",
            "https://example.com/cached",
            {
                "title": "Cached Book",
                "book_number": 1,
                "author_names": ["Some Author"],
                "published_date": "2024-01-01",
                "is_upcoming": False,
                "isbn13": None,
            },
        )
        raw_results = [
            {"title": "Cached listing", "description": "snippet", "url": "https://example.com/cached"},
            {"title": "Fresh listing", "description": "snippet", "url": "https://example.com/fresh"},
        ]
        fresh_structured = [
            {
                "result_index": 0,  # index into uncached_raw, i.e. the "fresh" entry only
                "title": "Fresh Book",
                "book_number": 2,
                "author_names": ["Some Author"],
                "published_date": "2024-02-01",
                "is_upcoming": False,
                "isbn13": None,
            }
        ]

        with patch.object(provider_io, "_fetch_serper_web_search", return_value=raw_results), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=fresh_structured
        ) as mock_llm:
            results = discovery_engine._fetch_web_search(["query"], "Some Series", "Some Author", cache=cache)

        # Only the uncached ("Fresh listing") entry is sent to the LLM.
        self.assertEqual(len(mock_llm.call_args[0][2]), 1)
        self.assertEqual(mock_llm.call_args[0][2][0]["url"], "https://example.com/fresh")
        # Output order follows raw_results (cached entry first).
        self.assertEqual([r["title"] for r in results], ["Cached Book", "Fresh Book"])

    def test_reconciliation_is_never_cached(self):
        # _reconcile_candidates_with_llm stays permanently excluded from
        # caching (architecture spec #7.1) -- it must be called fresh every
        # time it's triggered, even across otherwise-cached rounds.
        cache = DiscoveryCache()
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]), patch.object(
            discovery_engine, "_fetch_google_books", return_value=[]
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]), patch.object(
            discovery_engine, "_needs_llm_reconciliation", return_value=True
        ), patch.object(
            discovery_engine, "_reconcile_candidates_with_llm", side_effect=lambda candidates, *a, **k: candidates
        ) as mock_reconcile:
            discovery_engine.discover_candidates_for_series(
                "The First Peacemaker", "Some Author", cache=cache, allow_author_fallback=False
            )
            discovery_engine.discover_candidates_for_series(
                "The First Peacemaker", "Some Author", cache=cache, allow_author_fallback=False
            )

        self.assertEqual(mock_reconcile.call_count, 2)
        for call in mock_reconcile.call_args_list:
            self.assertNotIn("cache", call.kwargs)


class NeedsReviewToSkeletonUpdatesTest(unittest.TestCase):
    """Unit tests for the pure PB-1 mapping function in isolation, separate
    from the full run_series_check integration coverage below."""

    def test_maps_series_number_to_book_number_and_carries_confidence(self):
        needs_review = [
            {
                "title": "Desert Protocol",
                "series_number": "7",
                "overall_confidence": "medium",
                "date_iso": "2024-02-20",
                "provider": "hardcover",
                "url": "https://example.com/desert-protocol",
            }
        ]
        updates = _needs_review_to_skeleton_updates(needs_review)
        self.assertEqual(len(updates), 1)
        update = updates[0]
        self.assertEqual(update["book_number"], 7.0)
        self.assertEqual(update["title"], "Desert Protocol")
        self.assertEqual(update["status"], "unconfirmed")
        self.assertEqual(update["confidence"], "medium")
        self.assertEqual(update["release_date"], "2024-02-20")

    def test_fractional_series_number_is_preserved(self):
        needs_review = [{"title": "Novella", "series_number": "3.5", "overall_confidence": "unverified"}]
        updates = _needs_review_to_skeleton_updates(needs_review)
        self.assertEqual(updates[0]["book_number"], 3.5)

    def test_unparseable_series_number_is_skipped(self):
        needs_review = [{"title": "Untitled", "series_number": "", "overall_confidence": "medium"}]
        self.assertEqual(_needs_review_to_skeleton_updates(needs_review), [])

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(_needs_review_to_skeleton_updates([]), [])

    def test_isbn13_is_carried_through_to_the_skeleton_update(self):
        # LitRPG-discovery-plan addition -- canonical's isbn13 (added
        # alongside the pre-existing "identifier" fallback field in
        # agents/series_agent.py) flows through here into skeleton_json.
        needs_review = [
            {
                "title": "Desert Protocol",
                "series_number": "7",
                "overall_confidence": "medium",
                "isbn13": "9781111111111",
            }
        ]
        updates = _needs_review_to_skeleton_updates(needs_review)
        self.assertEqual(updates[0]["isbn13"], "9781111111111")

    def test_missing_isbn13_maps_to_none_not_a_missing_key(self):
        needs_review = [{"title": "Desert Protocol", "series_number": "7", "overall_confidence": "medium"}]
        updates = _needs_review_to_skeleton_updates(needs_review)
        self.assertIsNone(updates[0]["isbn13"])


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
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
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

    def test_belongs_to_series_gate_is_wrapped_in_a_tier_c_pass_scope(self):
        # HTA Orchestrator Step 4: the belongs_to_series gate (run once
        # per candidate, inside run_series_check's classification loop)
        # must be wrapped in a maybe_pass_scope(telemetry, "belongs_to_
        # series", tier="C") boundary -- telemetry-only, no LLM call yet.
        # Observable via summary()["by_pass"] picking up a "belongs_to_
        # series" bucket with zero llm_calls (nothing inside the scope
        # calls call_llm), one entry per candidate classified.
        from services.discovery_telemetry import DiscoveryTelemetry

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
        telemetry = DiscoveryTelemetry()
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False, telemetry=telemetry)

        summary = telemetry.summary()
        self.assertIn("belongs_to_series", summary["by_pass"])
        self.assertEqual(summary["by_pass"]["belongs_to_series"]["llm_calls"], 0)

    def test_string_series_number_hint_past_highest_owned_does_not_crash(self):
        # Regression test for a live crash (2026-08-24): Apify's (and
        # potentially other providers') series_number_hint comes back as
        # a *string* (e.g. "10"), not an int. run_series_check's
        # continues_numbering check used to compare that raw string
        # directly against highest_owned_book_number (an int) with `>`,
        # raising "TypeError: '>' not supported between instances of
        # 'str' and 'int'" and aborting the entire Check Now job with a
        # terminal_error -- even after already finding real candidates
        # earlier in the same run. Book 9 is the series' highest owned
        # number here, so a string "10" hint must clear continues_
        # numbering (and, combined with the explicit title match, get
        # added) without raising.
        candidates = [
            {
                "source": "apify",
                "source_id": "B0AAA1111",
                "title": "Cherry Blossom Girls Book 10",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": "10",
                "upcoming_hint": False,
            }
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)
        self.assertEqual(result["available_missing"][0]["series_number"], "10")

    def test_medium_confidence_candidate_creates_candidate_notification_with_series_name_hint(self):
        # Title has no textual tie to "Cherry Blossom Girls" at all, isn't
        # tagged "targeted"/"missing_volume_recovery" (so no
        # targeted_with_number credit), and its number (7) isn't past the
        # highest owned number (9) either (no continues_numbering credit) --
        # belongs_to_series fails outright, making this genuinely ambiguous
        # (low_confidence_ambiguous=True), which is what actually routes a
        # medium/unverified grade into this branch. See the routing block's
        # own comment in run_series_check for why a candidate that instead
        # passed belongs_to_series cleanly would auto-accept on this same
        # confidence grade -- "unverified" title_confidence, from the
        # missing SeriesSkeleton row, is the permanent state for any
        # candidate number nothing has seen before, not a signal of
        # ambiguity on its own.
        #
        # LitRPG Enhanced Discovery ("Review Candidate Book") replacement:
        # this branch no longer appends to needs_review at all -- it
        # creates a durable series_candidate_notifications row instead
        # (see services/candidate_notifications.py), carrying
        # series_name_hint through so a human reviewer can still dismiss a
        # same-author/different-series false positive on sight, without
        # confidence_engine needing a series-identity dimension.
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-7",
                "title": "Desert Protocol",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "author_fallback",
                "series_number_hint": 7,
                "series_name_hint": "Cherry Blossom Girls",
                "upcoming_hint": False,
            }
        ]
        unified_candidate = discovery_engine.UnifiedCandidate(
            title="Desert Protocol",
            authors=["Harmon Cooper"],
            series_name="Cherry Blossom Girls",
            series_number=7.0,
            metadata_completeness_score=1.0,
            source_provenance=[{"source": "hardcover"}],
        )
        with self._mock_discovery(candidates, unified_candidates=[unified_candidate]):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertFalse(result["found"])
        self.assertEqual(result["available_missing"], [])
        # needs_review is unconditionally empty for this branch now -- see
        # module docstring on services/candidate_notifications.py.
        self.assertEqual(result["needs_review"], [])

        rows = self.db.query(SeriesCandidateNotification).filter(
            SeriesCandidateNotification.series_id == self.series.id
        ).all()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.candidate_title, "Desert Protocol")
        self.assertEqual(row.overall_confidence, "medium")
        self.assertEqual(row.series_name_hint, "Cherry Blossom Girls")
        self.assertIsNone(row.resolution)

    def test_needs_review_candidate_no_longer_populates_skeleton_updates(self):
        # PB-1 originally wired needs_review candidates through to
        # result["skeleton_updates"] so apply_skeleton_updates (called by
        # services/series_check_engine.py after persistence) had something
        # to write. LitRPG Enhanced Discovery ("Review Candidate Book")
        # deliberately supersedes that for this branch: SeriesSkeleton must
        # stay unaware of an ambiguous candidate until "Add to Series" is
        # explicitly chosen (services/candidate_notifications.
        # resolve_add_to_series backfills the skeleton itself at that
        # point) -- so skeleton_updates/probes must both stay empty here,
        # with a series_candidate_notifications row created in their place.
        # Same scenario as
        # test_medium_confidence_candidate_creates_candidate_notification_with_series_name_hint.
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-7",
                "title": "Desert Protocol",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "source_url": "https://example.com/desert-protocol",
                "language": "",
                "confidence": "author_fallback",
                "series_number_hint": 7,
                "series_name_hint": "Cherry Blossom Girls",
                "upcoming_hint": False,
            }
        ]
        unified_candidate = discovery_engine.UnifiedCandidate(
            title="Desert Protocol",
            authors=["Harmon Cooper"],
            series_name="Cherry Blossom Girls",
            series_number=7.0,
            metadata_completeness_score=1.0,
            source_provenance=[{"source": "hardcover"}],
        )
        with self._mock_discovery(candidates, unified_candidates=[unified_candidate]):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(result["skeleton_updates"], [])
        self.assertEqual(result["probes"], [])

        rows = self.db.query(SeriesCandidateNotification).filter(
            SeriesCandidateNotification.series_id == self.series.id
        ).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source_url, "https://example.com/desert-protocol")
        # series_number_hint (7) is present, so this is NOT ambiguous
        # number extraction.
        self.assertNotIn("number_inferred_from_title", rows[0].reason_flags)

    def test_candidate_notification_flags_number_inferred_from_title(self):
        # No series_number_hint at all -- the only source for a number is
        # infer_number_from_title's "book N" keyword pattern parsing the
        # raw title text, which is exactly the "ambiguous number
        # extraction" this reason_flag exists to surface (see the routing
        # block's comment in run_series_check: book_number_source can't be
        # used here, it's Book-row-only and hardcoded to "provider" for
        # every discovery-persisted book).
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-7",
                "title": "Desert Protocol Book 7",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "author_fallback",
                "series_number_hint": None,
                "series_name_hint": "Cherry Blossom Girls",
                "upcoming_hint": False,
            }
        ]
        unified_candidate = discovery_engine.UnifiedCandidate(
            # series_number=7.0, not None: this mirrors what a real fused
            # candidate looks like (fusion resolves the number from the
            # same title text confidence_engine's number_confidence reads
            # off, independently of series_agent's own raw.get(
            # "series_number_hint")-first inference below). Leaving this
            # None would make confidence_engine's number_confidence grade
            # "low" (no structured number at all), which drops the
            # candidate at the `overall_grade in ("low", "zero")` gate
            # above -- before it ever reaches this branch -- rather than
            # exercising the "ambiguous number extraction" flag this test
            # is actually about.
            title="Desert Protocol Book 7",
            authors=["Harmon Cooper"],
            series_name="Cherry Blossom Girls",
            series_number=7.0,
            metadata_completeness_score=1.0,
            source_provenance=[{"source": "hardcover"}],
        )
        with self._mock_discovery(candidates, unified_candidates=[unified_candidate]):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False)

        rows = self.db.query(SeriesCandidateNotification).filter(
            SeriesCandidateNotification.series_id == self.series.id
        ).all()
        self.assertEqual(len(rows), 1)
        self.assertIn("number_inferred_from_title", rows[0].reason_flags)
        self.assertEqual(rows[0].candidate_number, 7.0)

    def test_candidate_notification_flags_missing_series_number(self):
        # No series_number_hint AND no number-shaped text in the title at
        # all -- infer_number_from_title has nothing to parse either, so
        # this candidate has no resolvable number whatsoever. Must still
        # get a durable notification (with candidate_number: null) rather
        # than silently vanishing the way _needs_review_to_skeleton_updates
        # used to drop a numberless needs_review entry via its own `continue`.
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-x",
                "title": "Desert Protocol",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "author_fallback",
                "series_number_hint": None,
                "series_name_hint": "Cherry Blossom Girls",
                "upcoming_hint": False,
            }
        ]
        # Deliberately no matching `unified_candidates` entry: a genuinely
        # numberless candidate always grades number_confidence="low" in
        # confidence_engine (see _number_confidence -- "no structured
        # number to place at all"), and "no zero, any remaining low ->
        # low" means overall_grade can never be "medium"/None from real
        # computation for this candidate. The only way it reaches this
        # branch at all is the `confidence_entry is None` fallback (empty
        # confidence_lookup -- see routing block's own comment on that
        # None case), which is exactly what leaving `unified_candidates`
        # empty here reproduces.
        with self._mock_discovery(candidates, unified_candidates=[]):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False)

        rows = self.db.query(SeriesCandidateNotification).filter(
            SeriesCandidateNotification.series_id == self.series.id
        ).all()
        self.assertEqual(len(rows), 1)
        self.assertIn("missing_series_number", rows[0].reason_flags)
        self.assertIsNone(rows[0].candidate_number)

    def test_candidate_notification_carries_asin_through_for_review_action(self):
        # LitRPG Enhanced Discovery ASIN-threading: raw["asin"] (captured by
        # apify_provider.py upstream, or here directly on the mocked
        # post-fusion candidate dict) must flow through canonical["asin"]
        # into the notification row, so the Review action's "Optional ASIN
        # lookup if available" has something to link to.
        candidates = [
            {
                "source": "apify",
                "source_id": "B0EXAMPLE1",
                "title": "Desert Protocol",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "asin": "B0EXAMPLE1",
                "source_url": None,
                "language": "",
                "confidence": "author_fallback",
                "series_number_hint": 7,
                "series_name_hint": "Cherry Blossom Girls",
                "upcoming_hint": False,
            }
        ]
        unified_candidate = discovery_engine.UnifiedCandidate(
            title="Desert Protocol",
            authors=["Harmon Cooper"],
            series_name="Cherry Blossom Girls",
            series_number=7.0,
            metadata_completeness_score=1.0,
            source_provenance=[{"source": "apify"}],
        )
        with self._mock_discovery(candidates, unified_candidates=[unified_candidate]):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False)

        rows = self.db.query(SeriesCandidateNotification).filter(
            SeriesCandidateNotification.series_id == self.series.id
        ).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].asin, "B0EXAMPLE1")

    def test_low_confidence_candidate_does_not_poison_known_numbers_for_a_later_good_candidate(self):
        # PB-11 regression (Percy Jackson books 4/5 investigation,
        # 2026-08-25, live incident): two raw hits resolve to the *same*
        # missing book number (7) under different titles -- exactly what
        # happens when one provider's text-only title ("...Book 7:...")
        # carries no structured series_number_hint at all (so
        # confidence_engine grades it number_confidence="low" -> dropped)
        # while a different provider's plain title for the same real book
        # *does* carry a real series_number_hint + isbn13 and would
        # otherwise score well. The first (bad) candidate must not mark
        # number 7 as "known" before its own confidence is graded --
        # doing so used to make the loop treat the second (good) candidate
        # as already_known and silently drop it before it ever reached
        # confidence grading, exactly reproducing "catalog clearly shows
        # the book, Check Now still reports nothing found."
        candidates = [
            {
                "source": "google_books",
                "source_id": "gb-bad-7",
                "title": "Cherry Blossom Girls, Book 7: Working Title",
                "authors": ["Harmon Cooper"],
                "published_date": "",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
            {
                "source": "hardcover",
                "source_id": "hc-good-7",
                "title": "Cherry Blossom Girls Book Seven",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": "9781111111111",
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": 7,
                "upcoming_hint": False,
            },
        ]
        bad_unified_candidate = discovery_engine.UnifiedCandidate(
            title="Cherry Blossom Girls, Book 7: Working Title",
            authors=["Harmon Cooper"],
            series_name="Cherry Blossom Girls",
            series_number=None,
            isbn13=None,
            metadata_completeness_score=0.5,
            source_provenance=[{"source": "google_books"}],
        )
        good_unified_candidate = discovery_engine.UnifiedCandidate(
            title="Cherry Blossom Girls Book Seven",
            authors=["Harmon Cooper"],
            series_name="Cherry Blossom Girls",
            series_number=7.0,
            isbn13="9781111111111",
            metadata_completeness_score=1.0,
            source_provenance=[{"source": "hardcover"}],
        )
        with self._mock_discovery(candidates, unified_candidates=[bad_unified_candidate, good_unified_candidate]):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(result["available_missing"], [
            {
                "title": "Cherry Blossom Girls Book Seven",
                "author": "Harmon Cooper",
                "series_name": "Cherry Blossom Girls",
                "series_number": 7,
                "date_iso": "2024-02-20",
                "url": None,
                "provider": "hardcover",
                "identifier": "9781111111111",
                "isbn13": "9781111111111",
                # LitRPG Enhanced Discovery ASIN-threading addition --
                # canonical now always carries a real "asin" field
                # (None here since this candidate has none).
                "asin": None,
            }
        ])
        self.assertTrue(result["found"])

    def test_available_missing_and_upcoming_books_do_not_leak_into_skeleton_updates(self):
        # available_missing/upcoming_books get persisted as real Book rows
        # this same round and become `library`-class skeleton entries on
        # the next backfill -- routing them through skeleton_updates too
        # would be a redundant, immediately-stale duplicate (see
        # _needs_review_to_skeleton_updates's docstring).
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

        self.assertEqual(len(result["available_missing"]), 1)
        self.assertEqual(result["skeleton_updates"], [])

    def test_high_confidence_candidate_still_auto_accepts(self):
        # A skeleton entry that already agrees with the discovered title
        # for this number is what lets title_confidence reach "high" (the
        # only grade the routing auto-accepts on) -- see
        # confidence_engine._title_confidence. run_series_check backfills
        # the skeleton from owned Book rows on every run (see its own
        # comment), which would immediately overwrite this hand-seeded row
        # with one that has no entry for book 7 at all (book 7 isn't
        # owned -- it's the very candidate under test), silently
        # regressing this back to "unverified"/medium. Patched out here so
        # this test can isolate confidence routing from that backfill
        # mechanics -- see the class-level note on this being the only
        # currently reachable path to "high" for a not-yet-owned
        # candidate; a real Check Now run cannot manufacture this state
        # today (backfill_skeleton_for_series only ever knows about
        # already-owned numbers, and any candidate reaching this point has
        # already been excluded from being a title/ISBN match against
        # those by discovery's own owned-title filter).
        self.db.add(
            SeriesSkeleton(
                series_id=self.series.id,
                skeleton_json=[{"book_number": 7.0, "title": "Cherry Blossom Girls Book 7"}],
            )
        )
        self.db.commit()

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
        unified_candidate = discovery_engine.UnifiedCandidate(
            title="Cherry Blossom Girls Book 7",
            authors=["Harmon Cooper"],
            series_name="Cherry Blossom Girls",
            series_number=7.0,
            metadata_completeness_score=1.0,
            source_provenance=[{"source": "hardcover"}],
        )
        with self._mock_discovery(candidates, unified_candidates=[unified_candidate]), patch(
            "agents.series_agent.backfill_skeleton_for_series"
        ):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)
        self.assertEqual(result["needs_review"], [])

    def test_zero_confidence_candidate_is_dropped_not_reviewed(self):
        # Author mismatch drives series_alignment_confidence to "zero",
        # which wins outright regardless of the other three dimensions --
        # this must be dropped, not surfaced for review (needs_review is
        # for genuine uncertainty, not a confirmed mismatch).
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-7",
                "title": "Cherry Blossom Girls Book 7",
                "authors": ["A Totally Different Author"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": 7,
                "upcoming_hint": False,
            }
        ]
        unified_candidate = discovery_engine.UnifiedCandidate(
            title="Cherry Blossom Girls Book 7",
            authors=["A Totally Different Author"],
            series_name="Cherry Blossom Girls",
            series_number=7.0,
            metadata_completeness_score=1.0,
            source_provenance=[{"source": "hardcover"}],
        )
        with self._mock_discovery(candidates, unified_candidates=[unified_candidate]):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertFalse(result["found"])
        self.assertEqual(result["available_missing"], [])
        self.assertEqual(result["needs_review"], [])

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
                profile_id=self.series.profile_id,
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
        series = Series(name="Safehold", author="David Weber", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)

        self.db.add(
            Book(
                title="Safehold Boxed Set 1: (Safehold Books 1-3)",
                author="David Weber",
                series_id=series.id,
                profile_id=series.profile_id,
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
                    profile_id=series.profile_id,
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
        series = Series(name="Safehold", author="David Weber", profile_id="robbie")
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
                    profile_id=series.profile_id,
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
            # Fusion always computes a real completeness score for a live
            # candidate; the field's own pydantic default (0.0) is only
            # ever seen here because this test builds a UnifiedCandidate
            # directly rather than through fusion, and 0.0 is below
            # discovery_engine.RECONCILIATION_METADATA_COMPLETENESS_THRESHOLD
            # -- delta_engine would misread that as "insufficient_metadata"
            # and confidence_engine would downgrade provider_confidence off
            # of it, for a reason with nothing to do with what this test
            # actually exercises.
            metadata_completeness_score=1.0,
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
        # This branch no longer appends to needs_review at all -- see
        # test_medium_confidence_candidate_creates_candidate_notification_with_series_name_hint.
        review_titles: set[str] = set()
        self.assertEqual(result["needs_review"], [])
        all_titles = available_titles | upcoming_titles | review_titles
        # The pre-existing "targeted" candidate must survive -- its title has
        # no textual tie to "Cherry Blossom Girls" at all, so only a
        # preserved "targeted" confidence (via targeted_with_number) can
        # clear belongs_to_series for it. (Gets the series suffix appended
        # since the raw title itself never references the series -- see
        # _title_references_series/display_title in run_series_check.) It
        # lands in available_missing, not needs_review: belongs_to_series
        # passed cleanly (targeted_with_number), so confidence's "unverified"
        # title grade -- the permanent, expected state for title_confidence
        # on any book not already in SeriesSkeleton -- doesn't gate
        # acceptance the way it does for a candidate belongs_to_series
        # couldn't confirm on its own (see the routing block's own comment,
        # and the live-bug verification that motivated it: unconditionally
        # gating on "unverified" routed every legitimate Jonathan Hunt
        # sequel to needs_review and made "Check Now" report nothing found).
        self.assertIn("Desert Protocol: (Cherry Blossom Girls Book 7)", available_titles)
        self.assertNotIn("Desert Protocol: (Cherry Blossom Girls Book 7)", review_titles | upcoming_titles)
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
        # lookahead, so it gets no special trust and correctly fails
        # belongs_to_series for lacking both a strong confidence and any
        # textual tie to the series -- but failing that gate no longer
        # means silently dropped. series_agent never asked confidence_engine
        # to score it either (it's not in this test's `unified_candidates`
        # override), so there's no grade to route on; the safe fallback for
        # "ambiguous and nothing to grade it" is a durable candidate
        # notification, not silent disappearance -- a human still needs to
        # see and dismiss it, just via services/candidate_notifications.py
        # now rather than needs_review.
        self.assertNotIn(
            "Unrelated Standalone Thriller: (Cherry Blossom Girls Book 11)", available_titles | upcoming_titles
        )
        # Notification candidate_title is the raw provider title ("Unrelated
        # Standalone Thriller"), not canonical's display_title -- see
        # run_series_check's own comment at the create_or_refresh_candidate_
        # notification call site for why the "(Series Name Book N)" suffix
        # (built for the auto-accept path's persisted-Book-title use case)
        # is deliberately not reused for the notification's candidate_title.
        stray_notifications = self.db.query(SeriesCandidateNotification).filter(
            SeriesCandidateNotification.series_id == self.series.id,
            SeriesCandidateNotification.candidate_title == "Unrelated Standalone Thriller",
        ).all()
        self.assertEqual(len(stray_notifications), 1)

    def test_no_author_on_file_returns_empty_result_without_calling_apis(self):
        series = Series(name="No Author Series", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)

        with patch("discovery_engine.discover_candidates_for_series") as mock_discover:
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, series.id, emit_summary=False)

        mock_discover.assert_not_called()
        self.assertEqual(result["reason"], "series-missing-author")
        self.assertFalse(result["found"])

    def test_run_series_check_never_writes_book_rows_itself(self):
        # TG-3: persistence of newly-discovered books is
        # services/series_check_engine.py's job, invoked only AFTER
        # run_series_check returns its in-memory `added_books` list --
        # run_series_check itself (discovery + classification) must be a
        # pure read against the Book table.
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
        books_before = [
            (b.id, b.title, b.book_number, b.record_status) for b in self.db.query(Book).order_by(Book.id).all()
        ]

        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        # Confirms discovery genuinely found something -- otherwise an
        # unchanged Book table would be a vacuous pass.
        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)

        books_after = [
            (b.id, b.title, b.book_number, b.record_status) for b in self.db.query(Book).order_by(Book.id).all()
        ]
        self.assertEqual(books_before, books_after)

    def test_highest_owned_book_number_reflects_a_fresh_db_read_across_calls(self):
        # TG-4: highest_owned_book_number must come from a fresh DB read on
        # every run_series_check call, never a value cached/carried over
        # from an earlier one -- services/series_check_engine.py's round
        # loop persists each round's new Book rows before invoking
        # run_series_check again, and round N+1 must see round N's inserts
        # (see that module's own comment on SERIES_CHECK_MAX_ROUNDS).
        with self._mock_discovery([]):
            agent = SeriesIntelligenceAgent()
            first_result = agent.run_series_check(self.db, self.series.id, emit_summary=False)
        self.assertEqual(first_result["highest_owned_book_number"], 9)  # setUp's highest owned book

        # Simulate series_check_engine persisting a newly-discovered book
        # between rounds (the round loop commits before looping again).
        self.db.add(
            Book(
                title="Cherry Blossom Girls Book 12",
                author="Harmon Cooper",
                series_id=self.series.id,
                profile_id=self.series.profile_id,
                series_order=12,
                book_number=12.0,
                record_status="active",
                is_read=False,
            )
        )
        self.db.commit()

        with self._mock_discovery([]):
            agent2 = SeriesIntelligenceAgent()
            second_result = agent2.run_series_check(self.db, self.series.id, emit_summary=False)
        self.assertEqual(second_result["highest_owned_book_number"], 12)


def _mock_anthropic_client(response_text, *, input_tokens=10, output_tokens=20):
    mock_text_block = MagicMock()
    mock_text_block.type = "text"
    mock_text_block.text = response_text
    mock_usage = MagicMock()
    mock_usage.input_tokens = input_tokens
    mock_usage.output_tokens = output_tokens
    mock_message = MagicMock()
    mock_message.content = [mock_text_block]
    mock_message.usage = mock_usage
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message
    return mock_client


class TierCShadowLlmTest(unittest.TestCase):
    """HTA Orchestrator Step 5: coverage for the Tier C shadow LLM call in
    run_series_check's classification loop -- fires only when
    low_confidence_ambiguous is True, overall_grade is "medium"/missing,
    and neither downgrade flag (is_universe_tie_in/is_compilation_of_
    owned_titles) is set; never affects belongs_to_series, overall_grade,
    or routing; recorded exclusively via DiscoveryTelemetry's shadow
    section (never llm_calls/total_cost_usd).

    A plain sibling of SeriesCheckIntegrationTest rather than a subclass,
    per that class's own note above Phase4DiagnosticsTest -- the
    owned-books fixture (1-6, 8, 9) is duplicated deliberately so this
    class's tests don't re-run the parent's whole suite.

    tests/conftest.py's `_no_real_anthropic_key_during_tests` autouse
    fixture already blanks a developer's real local ANTHROPIC_API_KEY for
    every test -- the tests below that actually want the shadow call to
    fire re-supply their own fake key plus a mocked anthropic.Anthropic,
    the same pattern tests/test_llm_client.py uses.
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

        for number in [1, 2, 3, 4, 5, 6, 8, 9]:
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

    def _mock_discovery(self, candidates, **overrides):
        result = {
            "candidates": candidates,
            "provider_failures": [],
            "all_providers_failed": False,
            "used_author_fallback": False,
        }
        result.update(overrides)
        return patch("discovery_engine.discover_candidates_for_series", return_value=result)

    def test_fires_for_an_ambiguous_medium_confidence_candidate(self):
        # Same fixture as SeriesCheckIntegrationTest's
        # test_medium_confidence_candidate_creates_candidate_notification_
        # with_series_name_hint: no textual tie to the series, not
        # "targeted"/"missing_volume_recovery", number (7) doesn't clear
        # the highest owned (9) -- belongs_to_series is False, and with no
        # confidence_lookup entry for it (no `unified_candidates` override
        # below) overall_grade is None. Both satisfy the Tier C predicate.
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-7",
                "title": "Desert Protocol",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "author_fallback",
                "series_number_hint": 7,
                "series_name_hint": "Cherry Blossom Girls",
                "upcoming_hint": False,
            }
        ]
        telemetry = DiscoveryTelemetry()
        with self._mock_discovery(candidates), patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "test-key"}
        ), patch("anthropic.Anthropic", return_value=_mock_anthropic_client('{"belongs_to_series": false}')):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False, telemetry=telemetry)

        summary = telemetry.summary()
        self.assertEqual(summary["shadow"]["total_llm_calls"], 1)
        self.assertEqual(summary["shadow"]["per_tier"]["C"]["calls"], 1)
        self.assertIn("belongs_to_series_shadow_check", summary["by_pass"])
        # Shadow calls must never leak into the production counters.
        self.assertEqual(summary["total_llm_calls"], 0)
        self.assertEqual(summary["total_cost_usd"], 0.0)

    def test_does_not_fire_when_belongs_to_series_is_true(self):
        # Explicit title match + a real series-position number --
        # belongs_to_series is True, so low_confidence_ambiguous is False
        # and the predicate can never be satisfied, regardless of grade.
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
        telemetry = DiscoveryTelemetry()
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False, telemetry=telemetry)

        self.assertEqual(telemetry.summary()["shadow"]["total_llm_calls"], 0)

    def test_does_not_fire_for_a_zero_confidence_author_mismatch(self):
        # No textual tie to the series (ambiguous, belongs_to_series
        # False) AND a confirmed author mismatch drives overall_grade to
        # "zero" -- predicate excludes anything but "medium"/None.
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-7",
                "title": "The Silver Falcon",
                "authors": ["A Totally Different Author"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "author_fallback",
                "series_number_hint": 7,
                "upcoming_hint": False,
            }
        ]
        unified_candidate = discovery_engine.UnifiedCandidate(
            title="The Silver Falcon",
            authors=["A Totally Different Author"],
            series_name="Cherry Blossom Girls",
            series_number=7.0,
            metadata_completeness_score=1.0,
            source_provenance=[{"source": "hardcover"}],
        )
        telemetry = DiscoveryTelemetry()
        with self._mock_discovery(candidates, unified_candidates=[unified_candidate]):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False, telemetry=telemetry)

        summary = telemetry.summary()
        self.assertEqual(summary["by_gate"]["confidence_grade"], {"zero": 1})
        self.assertEqual(summary["shadow"]["total_llm_calls"], 0)

    def test_does_not_fire_for_a_universe_tie_in_candidate(self):
        # Same fixture as test_universe_tie_in_spinoff_series_is_not_
        # pulled_into_flagship_series: both tie-in candidates get
        # is_universe_tie_in=True (excluded by the predicate's clause (c))
        # and the third is a clean, non-ambiguous accept -- shadow calls
        # must stay at 0 across the whole batch.
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
        telemetry = DiscoveryTelemetry()
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False, telemetry=telemetry)

        self.assertEqual(telemetry.summary()["shadow"]["total_llm_calls"], 0)

    def test_does_not_fire_for_a_compilation_of_owned_titles(self):
        # Same fixture shape as test_compilation_listing_naming_multiple_
        # owned_titles_is_rejected: strings together several owned titles
        # by name with no bundle keyword -- is_compilation_of_owned_titles
        # excludes it from the shadow predicate too.
        series = Series(name="Safehold", author="David Weber", profile_id="robbie")
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
                    profile_id=series.profile_id,
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
            }
        ]
        telemetry = DiscoveryTelemetry()
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, series.id, emit_summary=False, telemetry=telemetry)

        self.assertEqual(telemetry.summary()["shadow"]["total_llm_calls"], 0)

    def test_shadow_call_failure_does_not_sink_the_check_now_run(self):
        # Fail-soft: a missing/invalid ANTHROPIC_API_KEY (the default in
        # this suite, per conftest.py) must raise LLMCallError inside the
        # shadow block and be swallowed there -- run_series_check must
        # still complete and still create the candidate notification.
        # The attempt itself is still recorded (same "record a zero-token
        # entry on failure" convention record_llm_call already uses at
        # every other LLM call site, via the shared `finally` block), just
        # with zero tokens/cost -- see the assertion below.
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-7",
                "title": "Desert Protocol",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "author_fallback",
                "series_number_hint": 7,
                "series_name_hint": "Cherry Blossom Girls",
                "upcoming_hint": False,
            }
        ]
        telemetry = DiscoveryTelemetry()
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False, telemetry=telemetry)

        self.assertFalse(result["found"])
        shadow_summary = telemetry.summary()["shadow"]
        self.assertEqual(shadow_summary["total_llm_calls"], 1)
        self.assertEqual(shadow_summary["total_tokens_in"], 0)
        self.assertEqual(shadow_summary["total_cost_usd"], 0.0)
        rows = self.db.query(SeriesCandidateNotification).filter(
            SeriesCandidateNotification.series_id == self.series.id
        ).all()
        self.assertEqual(len(rows), 1)


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
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
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
            # See test_missing_volume_recovery_does_not_downgrade_an_
            # already_targeted_candidates_confidence's existing_candidate
            # fixture for why this needs to be set explicitly rather than
            # left at the pydantic default.
            metadata_completeness_score=1.0,
            source_provenance=[{"source": "hardcover", "series_total_hint": total_hint}],
        )

    def _run_and_capture(self, candidates, **overrides):
        """Runs a check with the web-search providers disabled (so the
        result never depends on ambient API keys) and returns the parsed
        series_external_reality payload alongside the result.
        """
        with patch.dict(os.environ, {"SERPER_API_KEY": "", "ANTHROPIC_API_KEY": ""}), self._mock_discovery(
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

        # The live result carries none of Phase 4's own fields. The
        # candidate's title explicitly references the series name, so
        # belongs_to_series passes cleanly on its own (explicit_series_
        # match) -- confidence's "unverified" title grade (the permanent,
        # expected state for any number no SeriesSkeleton entry has ever
        # seen, see confidence_engine._overall_confidence) doesn't gate
        # acceptance for a candidate belongs_to_series already confirmed;
        # it only gates candidates belongs_to_series itself couldn't
        # confirm. See the routing block's own comment in run_series_check.
        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)
        self.assertEqual(result["available_missing"][0]["series_number"], 7)
        self.assertEqual(result["needs_review"], [])
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
        # Explicit title match clears belongs_to_series on its own, so this
        # auto-accepts regardless of confidence's "unverified" title grade
        # -- see the comment in test_new_volume_flag_is_true_for_an_
        # externally_expected_unowned_number for the full explanation; this
        # test only cares that the failing helper doesn't change *that*
        # outcome.
        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)

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
        # The live result is untouched by this helper failing -- explicit
        # title match clears belongs_to_series on its own, so this
        # auto-accepts (see the comment in test_new_volume_flag_is_true_
        # for_an_externally_expected_unowned_number for the full
        # explanation).
        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)
        self.assertEqual(result["available_missing"][0]["series_number"], 7)
        self.assertEqual(result["needs_review"], [])

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
        self.series = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
        self.db.add(self.series)
        self.db.commit()
        self.db.refresh(self.series)

        self.db.add(
            Book(
                title="Cherry Blossom Girls Book 7",
                author="Harmon Cooper",
                series_id=self.series.id,
                profile_id=self.series.profile_id,
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
        series = Series(name="Scattered Stars", author="Glynn Stewart", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.db.add(Book(
            title="Conviction", author="Glynn Stewart", series_id=series.id, profile_id=series.profile_id, series_order=1,
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
        self.series = Series(name="Unbound", author="Nicoli Gonnella", profile_id="robbie")
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
                    profile_id=self.series.profile_id,
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

        self.duchy = Series(name="Duchy of Terra", author="Glynn Stewart", profile_id="robbie")
        self.mage = Series(name="Starship's Mage", author="Glynn Stewart", profile_id="robbie")
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
                title=title, author="Glynn Stewart", series_id=self.duchy.id, profile_id=self.duchy.profile_id, series_order=number,
                book_number=float(number), record_status="active", is_read=True,
            ))

        for number, title in [
            (8, "Mountain of Mars (Starship's Mage Book 8)"),
            (9, "The Service of Mars (Starship's Mage Book 9)"),
        ]:
            self.db.add(Book(
                title=title, author="Glynn Stewart", series_id=self.mage.id, profile_id=self.mage.profile_id, series_order=number,
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
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
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


class FusionGroupingTest(unittest.TestCase):
    """Coverage for _fuse_and_score_candidates' TITLE+AUTHOR identity
    fallback (PB-10 grouping fix): two hits for the same real book must
    still be recognized as duplicates -- and get the multi-source
    confidence bonus -- even when only one of them carries an ISBN. Live
    incident that motivated this: Hardcover's ISBN-bearing "The Lightning
    Thief" and OpenLibrary's ISBN-less "The Lightning Thief" landed in two
    separate fusion groups under the old isbn13-or-title-key logic, so
    neither ever got credit for the other corroborating it, capping
    catalog-sufficiency confidence well below any realistic threshold for
    a real series.
    """

    def test_isbn_haver_and_isbn_less_hit_merge_on_title_and_author(self):
        fused = discovery_engine._fuse_and_score_candidates(
            {
                "hardcover": [
                    {
                        "source": "hardcover",
                        "title": "The Lightning Thief",
                        "authors": ["Rick Riordan"],
                        "isbn13": "9780141335919",
                        "series_number_hint": 1,
                    }
                ],
                "google": [],
                "openlibrary": [
                    {
                        "source": "openlibrary",
                        "title": "The Lightning Thief",
                        "authors": ["Rick Riordan"],
                        "isbn13": None,
                        "series_number_hint": None,
                    }
                ],
                "web": [],
            },
            "Rick Riordan",
            "Percy Jackson and the Olympians",
        )
        self.assertEqual(len(fused), 1)
        merged = fused[0]
        self.assertEqual(merged.isbn13, "9780141335919")
        self.assertEqual({m.get("source") for m in merged.source_provenance}, {"hardcover", "openlibrary"})
        # Multi-source corroboration bonus must actually apply now that
        # both hits are recognized as the same book.
        self.assertGreater(merged.confidence_score, 0.75)

    def test_asin_survives_fusion_when_apify_hit_is_not_the_primary_member(self):
        # LitRPG Enhanced Discovery ASIN-threading fix: an Apify hit's real
        # ASIN used to silently vanish whenever it got grouped with any
        # other provider's hit for the same book, since provenance[0] (the
        # base every downstream raw dict is built from -- see
        # _unified_candidate_to_raw_dict) becomes whichever member landed
        # first in `ordered_raw` (hardcover/google/openlibrary before
        # web/apify), and only apify_provider.py ever sets "asin" at all --
        # confirmed by grep, no other provider does. Hardcover is primary
        # here specifically to exercise that multi-source case, not the
        # apify-is-sole-source case a naive `raw.get("asin")` already
        # handled.
        fused = discovery_engine._fuse_and_score_candidates(
            {
                "hardcover": [
                    {
                        "source": "hardcover",
                        "title": "Desert Protocol",
                        "authors": ["Harmon Cooper"],
                        "isbn13": "9781111111111",
                        "series_number_hint": 7,
                    }
                ],
                "google": [],
                "openlibrary": [],
                "web": [
                    {
                        "source": "apify",
                        "title": "Desert Protocol",
                        "authors": ["Harmon Cooper"],
                        "isbn13": None,
                        "series_number_hint": 7,
                        "asin": "B0EXAMPLE1",
                    }
                ],
            },
            "Harmon Cooper",
            "Cherry Blossom Girls",
        )
        self.assertEqual(len(fused), 1)
        merged = fused[0]
        self.assertEqual(merged.source_provenance[0].get("source"), "hardcover")
        self.assertEqual(merged.source_provenance[0].get("asin"), "B0EXAMPLE1")

    def test_asin_backfill_does_not_clobber_a_real_value_already_on_the_primary_member(self):
        # An apify-sourced primary member already carries its own real
        # "asin" -- the backfill must not stomp it with None just because
        # _first_present_field's scan (which includes the primary member
        # itself) happens to run.
        fused = discovery_engine._fuse_and_score_candidates(
            {
                "hardcover": [],
                "google": [],
                "openlibrary": [],
                "web": [
                    {
                        "source": "apify",
                        "title": "Desert Protocol",
                        "authors": ["Harmon Cooper"],
                        "isbn13": None,
                        "series_number_hint": 7,
                        "asin": "B0EXAMPLE1",
                    }
                ],
            },
            "Harmon Cooper",
            "Cherry Blossom Girls",
        )
        self.assertEqual(len(fused), 1)
        self.assertEqual(fused[0].source_provenance[0].get("asin"), "B0EXAMPLE1")

    def test_one_overlapping_author_is_enough_to_merge_graphic_novel_credits(self):
        # A graphic-novel adaptation's author/illustrator/colorist list
        # only needs to share ONE name with the novel's own (single)
        # author to be recognized as the same underlying book.
        fused = discovery_engine._fuse_and_score_candidates(
            {
                "hardcover": [
                    {
                        "source": "hardcover",
                        "title": "The Sea of Monsters: The Graphic Novel",
                        "authors": ["Rick Riordan"],
                        "isbn13": "9781423145509",
                        "series_number_hint": 2,
                    }
                ],
                "google": [],
                "openlibrary": [
                    {
                        "source": "openlibrary",
                        "title": "The Sea of Monsters: The Graphic Novel",
                        "authors": ["Attila Futaki", "Rick Riordan", "Robert Venditti"],
                        "isbn13": None,
                        "series_number_hint": None,
                    }
                ],
                "web": [],
            },
            "Rick Riordan",
            "Percy Jackson and the Olympians",
        )
        self.assertEqual(len(fused), 1)

    def test_same_title_no_author_overlap_stays_separate(self):
        # Same generic title, completely different authors -- these are
        # different real books and must not be merged just because the
        # title string matches.
        fused = discovery_engine._fuse_and_score_candidates(
            {
                "hardcover": [
                    {
                        "source": "hardcover",
                        "title": "Trivia",
                        "authors": ["Rick Riordan"],
                        "isbn13": None,
                        "series_number_hint": None,
                    }
                ],
                "google": [],
                "openlibrary": [
                    {
                        "source": "openlibrary",
                        "title": "Trivia",
                        "authors": ["Trivion Books"],
                        "isbn13": None,
                        "series_number_hint": None,
                    }
                ],
                "web": [],
            },
            "Rick Riordan",
            "Percy Jackson and the Olympians",
        )
        self.assertEqual(len(fused), 2)

    def test_both_sides_having_different_isbns_stays_separate(self):
        # Both hits carry an ISBN and they disagree -- a genuine edition
        # question (e.g. US vs. UK printing) left to _finalize_candidates'
        # own dominance-based edition-collapse downstream, not decided
        # here by title+author alone.
        fused = discovery_engine._fuse_and_score_candidates(
            {
                "hardcover": [
                    {
                        "source": "hardcover",
                        "title": "The Lightning Thief",
                        "authors": ["Rick Riordan"],
                        "isbn13": "9780141335919",
                        "series_number_hint": 1,
                    }
                ],
                "google": [
                    {
                        "source": "google_books",
                        "title": "The Lightning Thief",
                        "authors": ["Rick Riordan"],
                        "isbn13": "9780786838653",
                        "series_number_hint": None,
                    }
                ],
                "openlibrary": [],
                "web": [],
            },
            "Rick Riordan",
            "Percy Jackson and the Olympians",
        )
        self.assertEqual(len(fused), 2)


class CatalogSufficiencyGateTest(unittest.TestCase):
    """Coverage for the catalog-sufficiency gate (deterministic_fusion.
    catalog_providers_are_sufficient + its wiring into provider_io.
    _fetch_all_providers_parallel): skip web-search/Apify when Google
    Books/OpenLibrary/Hardcover already agree on a complete picture of the
    series, to stop the paid providers firing unconditionally on every
    Check Now regardless of whether the free catalogs already answered the
    question (live incident: a well-catalogued 7-book Percy Jackson series
    still fired ~12-15 Serper calls and an Apify Amazon scrape on every
    single run).
    """

    def setUp(self):
        self._env_patch = patch.dict(os.environ, {"SERPER_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"})
        self._env_patch.start()

    def tearDown(self):
        self._env_patch.stop()

    def _catalog_hit(self, title: str, number: float, *, source: str, authors=("Rick Riordan",)):
        return {
            "title": title,
            "authors": list(authors),
            "series_name_hint": "Percy Jackson and the Olympians",
            "series_number_hint": number,
            "isbn13": f"978000000{int(number * 10):03d}",
            "language": "en",
            "source": source,
        }

    def _all_catalogs_agree(self, numbers) -> dict:
        return {
            "hardcover": [self._catalog_hit(f"Book {n}", n, source="hardcover") for n in numbers],
            "google": [self._catalog_hit(f"Book {n}", n, source="google_books") for n in numbers],
            "openlibrary": [self._catalog_hit(f"Book {n}", n, source="openlibrary") for n in numbers],
            "web": [],
        }

    def test_sufficient_when_catalogs_fully_agree_with_no_gaps(self):
        fused = discovery_engine._fuse_and_score_candidates(
            self._all_catalogs_agree(range(1, 6)),
            "Rick Riordan",
            "Percy Jackson and the Olympians",
        )
        self.assertTrue(
            discovery_engine.catalog_providers_are_sufficient(
                fused,
                "Percy Jackson and the Olympians",
                None,
                contributing_provider_count=3,
            )
        )

    def test_not_sufficient_when_only_one_provider_contributed(self):
        candidates = [self._catalog_hit(f"Book {n}", n, source="hardcover") for n in range(1, 6)]
        fused = discovery_engine._fuse_and_score_candidates(
            {"hardcover": candidates, "google": [], "openlibrary": [], "web": []},
            "Rick Riordan",
            "Percy Jackson and the Olympians",
        )
        self.assertFalse(
            discovery_engine.catalog_providers_are_sufficient(
                fused,
                "Percy Jackson and the Olympians",
                None,
                contributing_provider_count=1,
            )
        )

    def test_not_sufficient_when_there_is_a_numbering_gap(self):
        # Only books 1, 2, and 5 are known -- a real gap (3 and 4 missing),
        # not just a genuinely-complete-but-short series.
        fused = discovery_engine._fuse_and_score_candidates(
            self._all_catalogs_agree((1, 2, 5)),
            "Rick Riordan",
            "Percy Jackson and the Olympians",
        )
        self.assertFalse(
            discovery_engine.catalog_providers_are_sufficient(
                fused,
                "Percy Jackson and the Olympians",
                None,
                contributing_provider_count=3,
            )
        )

    def test_percy_jackson_live_incident_data_now_passes_after_fusion_and_slot_fix(self):
        # Exact raw provider data captured from the live Railway incident
        # (2026-08-25 15:14:02 UTC "Percy Jackson & The Olympians" Check
        # Now run) that motivated the PB-10 fusion-grouping and
        # per-number-slot confidence fixes: Google Books returned 0 hits,
        # Hardcover returned 25 (mostly single-source, including several
        # bundle/box-set/graphic-novel variants of book 1), OpenLibrary
        # returned 8 (no ISBNs at all). Before the fix this scored
        # completeness=100%/confidence=51% and FAILED the 75% bar, so
        # Serper+Apify fired to reconfirm a series the catalogs already
        # fully knew about. This test locks in the fixed outcome.
        hardcover_raw = [
            {"source": "hardcover", "title": "Rick Riordan PERCY JACKSON & the OLYMPIANS Series Set Book 1-5", "authors": ["Rick Riordan"], "isbn13": "9798897226153", "series_number_hint": None},
            {"source": "hardcover", "title": "The Sea of Monsters", "authors": ["Rick Riordan"], "isbn13": "9780786290741", "series_number_hint": 2.0},
            {"source": "hardcover", "title": "The Titan's Curse", "authors": ["Rick Riordan"], "isbn13": "9782019109974", "series_number_hint": 3.0},
            {"source": "hardcover", "title": "Demigods and Monsters: Your Favorite Authors on Rick Riordan's Percy Jackson and the Olympians Series", "authors": ["Rick Riordan"], "isbn13": "9781937856373", "series_number_hint": None},
            {"source": "hardcover", "title": "The Lightning Thief", "authors": ["Rick Riordan"], "isbn13": "9780141335919", "series_number_hint": 1.0},
            {"source": "hardcover", "title": "Percy Jackson and the Olympians / The Senior Adventures 1", "authors": ["Rick Riordan"], "isbn13": "9789124282882", "series_number_hint": 1.0},
            {"source": "hardcover", "title": "Percy Jackson and the Olympians / The Senior Adventures 1-2", "authors": ["Rick Riordan"], "isbn13": "9781637995860", "series_number_hint": 1.0},
            {"source": "hardcover", "title": "The Battle of the Labyrinth", "authors": ["Rick Riordan"], "isbn13": "9789632454900", "series_number_hint": 4.0},
            {"source": "hardcover", "title": "The Last Olympian", "authors": ["Rick Riordan"], "isbn13": "9788804616672", "series_number_hint": 5.0},
            {"source": "hardcover", "title": "Percy Jackson and the Olympians: The Complete Series", "authors": ["Rick Riordan"], "isbn13": "9781484707234", "series_number_hint": 1.0},
            {"source": "hardcover", "title": "The Ultimate Guide", "authors": ["Rick Riordan", "Mary-Jane Knight"], "isbn13": "9788580572476", "series_number_hint": None},
            {"source": "hardcover", "title": "The Percy Jackson Coloring Book", "authors": ["Rick Riordan", "Keith Robinson"], "isbn13": "9781484787793", "series_number_hint": None},
            {"source": "hardcover", "title": "Percy Jackson and the Olympians 1-3", "authors": ["John Rocco", "Rick Riordan"], "isbn13": "9781484721476", "series_number_hint": 1.0},
            {"source": "hardcover", "title": "Untitled Percy Jackson and the Olympians #8", "authors": ["Rick Riordan"], "isbn13": None, "series_number_hint": 3.0},
            {"source": "hardcover", "title": "The Chalice of the Gods", "authors": ["Rick Riordan"], "isbn13": "9781368102193", "series_number_hint": 6.0},
            {"source": "hardcover", "title": "The Demigod Files", "authors": ["Rick Riordan"], "isbn13": "9781423121664", "series_number_hint": None},
            {"source": "hardcover", "title": "Wrath of the Triple Goddess", "authors": ["Rick Riordan"], "isbn13": "9781368107785", "series_number_hint": 7.0},
            {"source": "hardcover", "title": "The Sword of Hades", "authors": ["Rick Riordan"], "isbn13": "9781368099325", "series_number_hint": None},
            {"source": "hardcover", "title": "The Lightning Thief: The Graphic Novel", "authors": ["Robert Venditti", "Rick Riordan", "José Villarrubia", "Attila Futaki"], "isbn13": "9781423116967", "series_number_hint": 1.0},
            {"source": "hardcover", "title": "The Sea of Monsters: The Graphic Novel", "authors": ["Attila Futaki", "Rick Riordan", "Tamas Gaspar", "Robert Venditti"], "isbn13": "9781423145509", "series_number_hint": 2.0},
            {"source": "hardcover", "title": "Percy Jackson and the Stolen Chariot", "authors": ["Rick Riordan", "Manuela Salvi"], "isbn13": "9788852030505", "series_number_hint": 2.5},
            {"source": "hardcover", "title": "The Battle of the Labyrinth: The Graphic Novel", "authors": ["Rick Riordan", "Robert Venditti", "Attila Futaki", "Tamas Gaspar"], "isbn13": "9781484786390", "series_number_hint": 4.0},
            {"source": "hardcover", "title": "The Titan's Curse: The Graphic Novel", "authors": ["Robert Venditti", "Rick Riordan", "Attila Futaki", "Greg Guilhaumond", "Chris Dickey"], "isbn13": "9780141357751", "series_number_hint": 3.0},
            {"source": "hardcover", "title": "The Last Olympian: The Graphic Novel", "authors": ["Rick Riordan", "Robert Venditti"], "isbn13": "9781368046084", "series_number_hint": 5.0},
            {"source": "hardcover", "title": "Percy Jackson 1-5 / The Demigod Files / the Red Pyramid", "authors": ["Rick Riordan"], "isbn13": "9781780810065", "series_number_hint": 1.0},
        ]
        openlibrary_raw = [
            {"source": "openlibrary", "title": "The Lightning Thief", "authors": ["Rick Riordan"], "isbn13": None, "series_number_hint": None},
            {"source": "openlibrary", "title": "The Sea of Monsters", "authors": ["Rick Riordan"], "isbn13": None, "series_number_hint": None},
            {"source": "openlibrary", "title": "The Battle of the Labyrinth", "authors": ["Rick Riordan"], "isbn13": None, "series_number_hint": None},
            {"source": "openlibrary", "title": "Demigods and Monsters", "authors": ["Rick Riordan", "Leah Wilson"], "isbn13": None, "series_number_hint": None},
            {"source": "openlibrary", "title": "Percy Jackson and the Olympians Collection Rick Riordan 5 Books Set by Rick Riordan", "authors": [], "isbn13": None, "series_number_hint": None},
            {"source": "openlibrary", "title": "Trivia", "authors": ["Trivion Books"], "isbn13": None, "series_number_hint": None},
            {"source": "openlibrary", "title": "Percy Jackson Color by Number", "authors": ["Zach Walsh"], "isbn13": None, "series_number_hint": None},
            {"source": "openlibrary", "title": "The Titan's Curse", "authors": ["Rick Riordan"], "isbn13": None, "series_number_hint": None},
        ]
        fused = discovery_engine._fuse_and_score_candidates(
            {"hardcover": hardcover_raw, "google": [], "openlibrary": openlibrary_raw, "web": []},
            "Rick Riordan",
            "Percy Jackson & The Olympians",
        )
        sufficient = discovery_engine.catalog_providers_are_sufficient(
            fused,
            "Percy Jackson & The Olympians",
            1,
            contributing_provider_count=2,
        )
        self.assertTrue(
            sufficient,
            "Percy Jackson's live-incident catalog data should now pass the gate "
            "after the fusion-grouping and per-number-slot confidence fixes",
        )

    def test_fetch_all_providers_parallel_skips_web_search_when_catalogs_sufficient(self):
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[self._catalog_hit(f"Book {n}", n, source="hardcover") for n in range(1, 6)],
        ), patch.object(
            discovery_engine,
            "_fetch_google_books",
            return_value=[self._catalog_hit(f"Book {n}", n, source="google_books") for n in range(1, 6)],
        ), patch.object(
            discovery_engine,
            "_fetch_openlibrary",
            return_value=[self._catalog_hit(f"Book {n}", n, source="openlibrary") for n in range(1, 6)],
        ), patch.object(provider_io, "_fetch_serper_web_search") as mock_web_search:
            result = discovery_engine._fetch_all_providers_parallel(
                "Rick Riordan",
                "Percy Jackson and the Olympians",
                "Percy Jackson and the Olympians Rick Riordan",
                None,
                author="Rick Riordan",
                enable_web_search=True,
            )

        mock_web_search.assert_not_called()
        self.assertEqual(result["web"], [])

    def test_fetch_all_providers_parallel_still_runs_web_search_when_catalogs_incomplete(self):
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[self._catalog_hit(f"Book {n}", n, source="hardcover") for n in (1, 2, 5)],
        ), patch.object(
            discovery_engine,
            "_fetch_google_books",
            return_value=[self._catalog_hit(f"Book {n}", n, source="google_books") for n in (1, 2, 5)],
        ), patch.object(
            discovery_engine,
            "_fetch_openlibrary",
            return_value=[self._catalog_hit(f"Book {n}", n, source="openlibrary") for n in (1, 2, 5)],
        ), patch.object(provider_io, "_fetch_serper_web_search", return_value=[]) as mock_web_search:
            discovery_engine._fetch_all_providers_parallel(
                "Rick Riordan",
                "Percy Jackson and the Olympians",
                "Percy Jackson and the Olympians Rick Riordan",
                None,
                author="Rick Riordan",
                enable_web_search=True,
            )

        mock_web_search.assert_called()

    def test_missing_volume_lookahead_records_overridden_gate_outcome(self):
        # _reconstruct_series_skeleton's interior-gap lookahead bypasses
        # _fetch_all_providers_parallel (and therefore the catalog-
        # sufficiency gate) entirely by design -- see its own inline
        # comment. That bypass must still show up in the same
        # catalog_sufficiency telemetry bucket, tagged OVERRIDDEN, so it's
        # visible in the debug summary rather than looking like the gate
        # silently failed to fire.
        from services.discovery_telemetry import DiscoveryTelemetry

        telemetry = DiscoveryTelemetry()
        unified_candidates = [
            discovery_engine.UnifiedCandidate(
                title="Book 1", authors=["Rick Riordan"], series_number=1.0, confidence_score=1.0
            ),
            discovery_engine.UnifiedCandidate(
                title="Book 3", authors=["Rick Riordan"], series_number=3.0, confidence_score=1.0
            ),
        ]
        with patch.dict(
            os.environ, {"SERPER_API_KEY": "test-key", "ANTHROPIC_API_KEY": "test-key"}
        ), patch.object(provider_io, "_fetch_serper_web_search", return_value=[]):
            discovery_engine._reconstruct_series_skeleton(
                unified_candidates,
                [],
                series_name="Percy Jackson and the Olympians",
                author="Rick Riordan",
                telemetry=telemetry,
            )

        self.assertEqual(telemetry.summary()["by_gate"]["catalog_sufficiency"].get("OVERRIDDEN"), 1)

    def test_debug_summary_prints_gate_outcome_line(self):
        from services.discovery_logging import log_discovery_summary

        result = {
            "series_id": 344,
            "series_name": "Percy Jackson and the Olympians",
            "telemetry": {
                "by_pass": {},
                "by_gate": {"catalog_sufficiency": {"FAILED": 2, "OVERRIDDEN": 1}},
                "total_web_search_calls": 14,
                "total_llm_calls": 3,
                "total_tokens_in": 0,
                "total_tokens_out": 0,
            },
        }
        with patch("services.discovery_logging._console_log") as mock_console_log:
            log_discovery_summary(result=result)

        printed_lines = [call.args[0] for call in mock_console_log.call_args_list]
        gate_lines = [line for line in printed_lines if "GATE catalog_sufficiency:" in line]
        self.assertEqual(len(gate_lines), 1)
        self.assertIn("FAILED=2", gate_lines[0])
        self.assertIn("OVERRIDDEN=1", gate_lines[0])

    def test_gate_can_be_disabled_via_env_var(self):
        with patch.dict(os.environ, {"CATALOG_SUFFICIENCY_GATE_ENABLED": "false"}), patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[self._catalog_hit(f"Book {n}", n, source="hardcover") for n in range(1, 6)],
        ), patch.object(
            discovery_engine,
            "_fetch_google_books",
            return_value=[self._catalog_hit(f"Book {n}", n, source="google_books") for n in range(1, 6)],
        ), patch.object(
            discovery_engine,
            "_fetch_openlibrary",
            return_value=[self._catalog_hit(f"Book {n}", n, source="openlibrary") for n in range(1, 6)],
        ), patch.object(provider_io, "_fetch_serper_web_search", return_value=[]) as mock_web_search:
            discovery_engine._fetch_all_providers_parallel(
                "Rick Riordan",
                "Percy Jackson and the Olympians",
                "Percy Jackson and the Olympians Rick Riordan",
                None,
                author="Rick Riordan",
                enable_web_search=True,
            )

        mock_web_search.assert_called()


if __name__ == "__main__":
    unittest.main()
