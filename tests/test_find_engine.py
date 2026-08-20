"""Regression coverage for services/find_engine.py -- the multi-provider
FIND rebuild for the Add Book workflow's Resolve/Select states (see project
design chat's consolidated Add Book specification).

Patches discovery_engine._fetch_google_books/_fetch_openlibrary/
_fetch_hardcover directly (the same pattern test_series_discovery.py uses)
so this suite is deterministic and never depends on live third-party APIs.
"""
import unittest
from unittest.mock import patch

import discovery_engine
from services.find_engine import find_book_candidates


def _hit(source: str, title: str, authors: list[str], isbn13: str | None = None, description: str | None = None):
    return {
        "source": source,
        "title": title,
        "authors": authors,
        "isbn13": isbn13,
        "description": description,
        "source_url": f"https://example.com/{source}",
        "published_date": "2024",
    }


def _patched_providers(google=None, openlibrary=None, hardcover=None):
    return patch.multiple(
        discovery_engine,
        _fetch_google_books=lambda *a, **k: google or [],
        _fetch_openlibrary=lambda *a, **k: openlibrary or [],
        _fetch_hardcover=lambda *a, **k: hardcover or [],
    )


class FindBookCandidatesTest(unittest.TestCase):
    def test_empty_title_returns_no_candidates_without_calling_any_provider(self):
        with _patched_providers() as _:
            result = find_book_candidates("   ")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["provider_failures"], [])

    def test_high_confidence_requires_all_three_signals(self):
        hit = _hit("hardcover", "Fourth Wing", ["Rebecca Yarros"], isbn13="9781649374042", description="A dragon rider story.")
        with _patched_providers(hardcover=[hit]):
            result = find_book_candidates("Fourth Wing", author="Rebecca Yarros")

        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["confidence"], "high")
        self.assertTrue(candidate["signals"]["author_match"])
        self.assertTrue(candidate["signals"]["isbn_present"])
        self.assertTrue(candidate["signals"]["strong_title_match"])

    def test_medium_confidence_when_only_two_signals_present(self):
        # Author matches and title matches, but no ISBN from any provider.
        hit = _hit("google_books", "Fourth Wing", ["Rebecca Yarros"], isbn13=None)
        with _patched_providers(google=[hit]):
            result = find_book_candidates("Fourth Wing", author="Rebecca Yarros")

        candidate = result["candidates"][0]
        self.assertEqual(candidate["confidence"], "medium")

    def test_low_confidence_when_one_or_zero_signals_present(self):
        # Title matches but no author supplied and no ISBN -- only one signal.
        hit = _hit("openlibrary", "Fourth Wing", ["Someone Else"], isbn13=None)
        with _patched_providers(openlibrary=[hit]):
            result = find_book_candidates("Fourth Wing")

        candidate = result["candidates"][0]
        self.assertEqual(candidate["confidence"], "low")

    def test_candidates_are_ranked_highest_confidence_first(self):
        low_hit = _hit("openlibrary", "Some Unrelated Title", ["Nobody"], isbn13=None)
        high_hit = _hit(
            "hardcover", "Fourth Wing", ["Rebecca Yarros"], isbn13="9781649374042", description="A dragon rider story."
        )
        with _patched_providers(openlibrary=[low_hit], hardcover=[high_hit]):
            result = find_book_candidates("Fourth Wing", author="Rebecca Yarros")

        confidences = [c["confidence"] for c in result["candidates"]]
        self.assertEqual(confidences[0], "high")

    def test_hits_across_providers_sharing_an_isbn_are_grouped_into_one_candidate(self):
        google_hit = _hit("google_books", "Fourth Wing", ["Rebecca Yarros"], isbn13="9781649374042")
        hardcover_hit = _hit(
            "hardcover", "Fourth Wing", ["Rebecca Yarros"], isbn13="9781649374042", description="A dragon rider story."
        )
        with _patched_providers(google=[google_hit], hardcover=[hardcover_hit]):
            result = find_book_candidates("Fourth Wing", author="Rebecca Yarros")

        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertIn("google_books", candidate["providers"])
        self.assertIn("hardcover", candidate["providers"])
        # Hardcover is preferred for description per _FIELD_PROVIDER_PRIORITY.
        self.assertEqual(candidate["field_provenance"]["description"], "hardcover")

    def test_hits_with_no_isbn_group_by_normalized_title_identity(self):
        # Different providers, same book, no ISBN from either -- must still
        # collapse into a single candidate via the title-identity fallback
        # key rather than becoming two separate low-signal candidates.
        google_hit = _hit("google_books", "Fourth Wing SIGNED", ["Rebecca Yarros"])
        openlibrary_hit = _hit("openlibrary", "Fourth Wing", ["Rebecca Yarros"])
        with _patched_providers(google=[google_hit], openlibrary=[openlibrary_hit]):
            result = find_book_candidates("Fourth Wing", author="Rebecca Yarros")

        self.assertEqual(len(result["candidates"]), 1)

    def test_different_titles_are_not_grouped_together(self):
        hit_a = _hit("google_books", "Fourth Wing", ["Rebecca Yarros"])
        hit_b = _hit("hardcover", "Iron Flame", ["Rebecca Yarros"])
        with _patched_providers(google=[hit_a], hardcover=[hit_b]):
            result = find_book_candidates("Fourth Wing", author="Rebecca Yarros")

        self.assertEqual(len(result["candidates"]), 2)

    def test_a_failing_provider_does_not_prevent_the_others_from_returning_results(self):
        hit = _hit("hardcover", "Fourth Wing", ["Rebecca Yarros"], isbn13="9781649374042", description="desc")
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[hit]), patch.object(
            discovery_engine, "_fetch_google_books", side_effect=RuntimeError("boom")
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            result = find_book_candidates("Fourth Wing", author="Rebecca Yarros")

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(len(result["provider_failures"]), 1)
        self.assertEqual(result["provider_failures"][0]["provider"], "google_books")

    def test_query_is_echoed_back_including_book_number_and_series_name(self):
        with _patched_providers():
            result = find_book_candidates("Some Title", author="Some Author", book_number=3, series_name="Some Series")

        self.assertEqual(
            result["query"],
            {"title": "Some Title", "author": "Some Author", "book_number": 3, "series_name": "Some Series"},
        )

    def test_author_match_tolerates_multiple_authors_per_provider_hit(self):
        hit = _hit("google_books", "Fourth Wing", ["Someone Else", "Rebecca Yarros"], isbn13="9781649374042")
        with _patched_providers(google=[hit]):
            result = find_book_candidates("Fourth Wing", author="Rebecca Yarros")

        candidate = result["candidates"][0]
        self.assertTrue(candidate["signals"]["author_match"])

    def test_raw_ku_style_title_with_series_suffix_still_finds_the_core_title(self):
        # Regression: a user pasting the full Amazon/KU listing title
        # verbatim -- "Core Title: subtitle Book N (Series Name)" -- used to
        # return zero candidates from every provider, because the only
        # query ever tried was the raw string itself (Google's intitle: is
        # an exact-phrase match, so a catalog listing titled just "The
        # Jericho Siege" can never match the full raw string). The fallback
        # "core title" variant must still surface it.
        raw_title = "The Jericho Siege: A Jonathan Hunt Thriller Book 1 (Jonathan Hunt Thriller Series)"
        hit = _hit(
            "google_books",
            "The Jericho Siege",
            ["Georgia Wagner", "Scott Cook"],
            isbn13="9798242213814",
            description="A Jonathan Hunt thriller.",
        )

        def fake_google_books(query, *a, **k):
            return [hit] if query == 'intitle:"The Jericho Siege"' else []

        with patch.object(discovery_engine, "_fetch_google_books", side_effect=fake_google_books), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ), patch.object(discovery_engine, "_fetch_hardcover", return_value=[]):
            result = find_book_candidates(raw_title)

        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["title"], "The Jericho Siege")
        # Title match should be recognized against the stripped core variant
        # even though it doesn't equal the raw query string.
        self.assertTrue(candidate["signals"]["strong_title_match"])

    def test_failures_from_multiple_title_variants_collapse_to_one_entry_per_provider(self):
        # A title with a core variant (so two google_books tasks run) whose
        # provider fails on both should surface as one failure entry, not
        # two, in the response.
        raw_title = "Some Title: A Thriller Book 1 (Some Series)"
        with patch.object(
            discovery_engine, "_fetch_google_books", side_effect=RuntimeError("boom")
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]), patch.object(
            discovery_engine, "_fetch_hardcover", return_value=[]
        ):
            result = find_book_candidates(raw_title)

        self.assertEqual(len(result["provider_failures"]), 1)
        self.assertEqual(result["provider_failures"][0]["provider"], "google_books")


if __name__ == "__main__":
    unittest.main()
