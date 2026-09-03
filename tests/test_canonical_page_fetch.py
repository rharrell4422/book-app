"""Unit coverage for provider_io.fetch_canonical_page_text and
fetch_canonical_page_candidates -- the Guided Discovery (locked
2026-09-03, iterations 1-5) canonical-URL page-fetch capability described
in that design chat's iteration 3/5 diffs.
"""
import unittest
from unittest.mock import MagicMock, patch

import httpx

import provider_io


class FetchCanonicalPageTextTest(unittest.TestCase):
    def test_returns_none_for_empty_url(self):
        self.assertIsNone(provider_io.fetch_canonical_page_text(""))
        self.assertIsNone(provider_io.fetch_canonical_page_text(None))

    def test_returns_none_when_fetch_raises(self):
        with patch.object(provider_io.httpx, "get", side_effect=httpx.ConnectTimeout("boom")):
            self.assertIsNone(provider_io.fetch_canonical_page_text("https://example.com/series"))

    def test_returns_none_when_fetch_returns_error_status(self):
        response = MagicMock()
        response.raise_for_status.side_effect = httpx.HTTPStatusError("blocked", request=MagicMock(), response=MagicMock())
        with patch.object(provider_io.httpx, "get", return_value=response):
            self.assertIsNone(provider_io.fetch_canonical_page_text("https://example.com/series"))

    def test_returns_none_when_trafilatura_extracts_nothing(self):
        # Models a JS-shell page (empty/near-empty HTML body) -- exactly
        # the failure mode a plain httpx GET hits against a client-
        # rendered SPA (Kobo/Google Play Books) with no extractable main
        # content for trafilatura to find.
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.text = "<html><body><div id='app'></div></body></html>"
        with patch.object(provider_io.httpx, "get", return_value=response), patch.object(
            provider_io.trafilatura, "extract", return_value=None
        ):
            self.assertIsNone(provider_io.fetch_canonical_page_text("https://example.com/series"))

    def test_returns_cleaned_and_capped_text_on_success(self):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.text = "<html>...</html>"
        long_text = "Book one. " * 1000  # comfortably longer than the cap
        with patch.object(provider_io.httpx, "get", return_value=response), patch.object(
            provider_io.trafilatura, "extract", return_value=f"  {long_text}  "
        ):
            result = provider_io.fetch_canonical_page_text("https://example.com/series")
        self.assertIsNotNone(result)
        self.assertLessEqual(len(result), provider_io.CANONICAL_PAGE_TEXT_MAX_CHARS)
        self.assertFalse(result.startswith(" "))


class FetchCanonicalPageCandidatesTest(unittest.TestCase):
    def test_returns_empty_when_page_fetch_fails(self):
        with patch.object(provider_io, "fetch_canonical_page_text", return_value=None), patch.object(
            provider_io, "_structure_web_results_with_llm"
        ) as mock_structure:
            result = provider_io.fetch_canonical_page_candidates(
                "https://example.com/series", "Goodreads", "Some Series", "Some Author"
            )
        mock_structure.assert_not_called()
        self.assertEqual(result, [])

    def test_returns_empty_when_llm_structures_nothing(self):
        with patch.object(provider_io, "fetch_canonical_page_text", return_value="page text"), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=[]
        ):
            result = provider_io.fetch_canonical_page_candidates(
                "https://example.com/series", "Goodreads", "Some Series", "Some Author"
            )
        self.assertEqual(result, [])

    def test_extracts_multiple_books_from_one_page_without_dropping_any(self):
        # The whole point of Guided Discovery's canonical page fetch: ONE
        # URL/one raw result can legitimately describe MANY books. This
        # must NOT be routed through _structure_with_verdict_cache's own
        # URL-keyed pairing (see fetch_canonical_page_candidates's own
        # docstring) -- that pairing keeps only the LAST structured item
        # per URL, which would silently drop every book but one here.
        structured_items = [
            {
                "result_index": 0,
                "title": "Book One",
                "series_name": "Some Series",
                "book_number": 1,
                "author_names": ["Some Author"],
                "published_date": "2020-01-01",
                "is_upcoming": False,
                "isbn13": None,
            },
            {
                "result_index": 0,
                "title": "Book Two",
                "series_name": "Some Series",
                "book_number": 2,
                "author_names": ["Some Author"],
                "published_date": "2021-01-01",
                "is_upcoming": False,
                "isbn13": None,
            },
            {
                "result_index": 0,
                "title": "Book Three",
                "series_name": "Some Series",
                "book_number": 3,
                "author_names": ["Some Author"],
                "published_date": None,
                "is_upcoming": True,
                "isbn13": None,
            },
        ]
        with patch.object(provider_io, "fetch_canonical_page_text", return_value="page listing all three books"), patch.object(
            provider_io, "_structure_web_results_with_llm", return_value=structured_items
        ):
            result = provider_io.fetch_canonical_page_candidates(
                "https://example.com/series", "Goodreads", "Some Series", "Some Author"
            )
        self.assertEqual(len(result), 3)
        self.assertEqual({item["title"] for item in result}, {"Book One", "Book Two", "Book Three"})
        # Every candidate shares the same single source URL -- confirms
        # each was paired against the one canonical raw_result, not lost.
        self.assertTrue(all(item.get("source_url") == "https://example.com/series" for item in result))

    def test_structuring_failure_is_swallowed(self):
        with patch.object(provider_io, "fetch_canonical_page_text", return_value="page text"), patch.object(
            provider_io, "_structure_web_results_with_llm", side_effect=RuntimeError("boom")
        ):
            result = provider_io.fetch_canonical_page_candidates(
                "https://example.com/series", "Goodreads", "Some Series", "Some Author"
            )
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
