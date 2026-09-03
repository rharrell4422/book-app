"""Unit coverage for provider_io.fetch_canonical_page_text and
fetch_canonical_page_candidates -- the Guided Discovery (locked
2026-09-03, iterations 1-5) canonical-URL page-fetch capability described
in that design chat's iteration 3/5 diffs.
"""
import json
import unittest
from unittest.mock import MagicMock, patch

import httpx

import provider_io
from llm_client import LLMResponse


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
            provider_io, "_structure_canonical_page_with_llm"
        ) as mock_structure:
            result = provider_io.fetch_canonical_page_candidates(
                "https://example.com/series", "Goodreads", "Some Series", "Some Author"
            )
        mock_structure.assert_not_called()
        self.assertEqual(result, [])

    def test_returns_empty_when_llm_structures_nothing(self):
        with patch.object(provider_io, "fetch_canonical_page_text", return_value="page text"), patch.object(
            provider_io, "_structure_canonical_page_with_llm", return_value=[]
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
        # No result_index in these -- _structure_canonical_page_with_llm's
        # dedicated prompt/schema drops it entirely (see that function's
        # own docstring: every item is paired with the same single source
        # dict regardless of index, so it would serve no purpose here).
        structured_items = [
            {
                "title": "Book One",
                "series_name": "Some Series",
                "book_number": 1,
                "author_names": ["Some Author"],
                "published_date": "2020-01-01",
                "is_upcoming": False,
                "isbn13": None,
            },
            {
                "title": "Book Two",
                "series_name": "Some Series",
                "book_number": 2,
                "author_names": ["Some Author"],
                "published_date": "2021-01-01",
                "is_upcoming": False,
                "isbn13": None,
            },
            {
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
            provider_io, "_structure_canonical_page_with_llm", return_value=structured_items
        ):
            result = provider_io.fetch_canonical_page_candidates(
                "https://example.com/series", "Goodreads", "Some Series", "Some Author"
            )
        self.assertEqual(len(result), 3)
        self.assertEqual({item["title"] for item in result}, {"Book One", "Book Two", "Book Three"})
        # Every candidate shares the same single source URL -- confirms
        # each was paired against the one canonical raw_result, not lost.
        self.assertTrue(all(item.get("source_url") == "https://example.com/series" for item in result))
        # Guided Discovery Option A confidence fix (2026-09-03): tagged
        # "canonical_page", NOT "web_search" -- confidence_engine grades
        # "canonical_page" one tier higher ("medium" vs "low"), which is
        # what actually gets these candidates past _overall_confidence's
        # title_confidence=="unverified" + any-other-dim=="low" auto-reject
        # rule for a brand-new series number (see the live Jonathan Hunt/
        # Goodreads validation test that caught this).
        self.assertTrue(all(item.get("source") == "canonical_page" for item in result))

    def test_structuring_failure_is_swallowed(self):
        with patch.object(provider_io, "fetch_canonical_page_text", return_value="page text"), patch.object(
            provider_io, "_structure_canonical_page_with_llm", side_effect=RuntimeError("boom")
        ):
            result = provider_io.fetch_canonical_page_candidates(
                "https://example.com/series", "Goodreads", "Some Series", "Some Author"
            )
        self.assertEqual(result, [])


class StructureCanonicalPageWithLlmTest(unittest.TestCase):
    """Coverage for the Option A fix (2026-09-03 Goodreads/Jonathan Hunt
    validation test): provider_io._structure_canonical_page_with_llm, the
    canonical-page-dedicated structuring function that replaced reusing
    _structure_web_results_with_llm's snippet-oriented prompt (which was
    root-caused to produce zero extracted candidates against a real
    canonical page -- see that function's own docstring).
    """

    def test_returns_empty_without_api_key(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}, clear=False):
            result = provider_io._structure_canonical_page_with_llm(
                "Some Series", "Some Author", "page text", "Canonical Goodreads page"
            )
        self.assertEqual(result, [])

    def test_returns_empty_for_blank_page_text(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}, clear=False):
            result = provider_io._structure_canonical_page_with_llm(
                "Some Series", "Some Author", "   ", "Canonical Goodreads page"
            )
        self.assertEqual(result, [])

    def test_uses_the_dedicated_canonical_page_prompt_not_the_snippet_one(self):
        # The whole point of Option A: this call site must use
        # build_canonical_page_extraction_prompt's prompt text (which
        # tells the model many books are expected), never
        # build_extraction_prompt's snippet prompt (which tells the model
        # to skip "whole series summary" pages -- the exact bug this
        # fixed).
        llm_response = LLMResponse(text="[]", tokens_in=10, tokens_out=2, model_id="claude-haiku-4-5-20251001")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}, clear=False), patch.object(
            provider_io, "call_llm", return_value=llm_response
        ) as mock_call_llm:
            provider_io._structure_canonical_page_with_llm(
                "Some Series", "Some Author", "page listing every book", "Canonical Goodreads page"
            )
        prompt_used = mock_call_llm.call_args.kwargs["prompt"]
        self.assertIn("EXPECTED to describe MANY books", prompt_used)
        self.assertNotIn("fan wiki summaries of a whole series", prompt_used)
        self.assertEqual(mock_call_llm.call_args.kwargs["tier"], "A")

    def test_parses_multiple_structured_books_from_one_call(self):
        structured = [
            {"title": "Book One", "book_number": 1, "author_names": ["Some Author"], "is_upcoming": False},
            {"title": "Book Two", "book_number": 2, "author_names": ["Some Author"], "is_upcoming": False},
        ]
        llm_response = LLMResponse(
            text=json.dumps(structured), tokens_in=10, tokens_out=20, model_id="claude-haiku-4-5-20251001"
        )
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}, clear=False), patch.object(
            provider_io, "call_llm", return_value=llm_response
        ):
            result = provider_io._structure_canonical_page_with_llm(
                "Some Series", "Some Author", "page listing every book", "Canonical Goodreads page"
            )
        self.assertEqual(len(result), 2)
        self.assertEqual({item["title"] for item in result}, {"Book One", "Book Two"})

    def test_strips_markdown_fences_before_parsing(self):
        llm_response = LLMResponse(
            text='```json\n[{"title": "Book One", "book_number": 1}]\n```',
            tokens_in=10,
            tokens_out=10,
            model_id="claude-haiku-4-5-20251001",
        )
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}, clear=False), patch.object(
            provider_io, "call_llm", return_value=llm_response
        ):
            result = provider_io._structure_canonical_page_with_llm(
                "Some Series", "Some Author", "page text", "Canonical Goodreads page"
            )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Book One")

    def test_returns_empty_on_json_parse_failure(self):
        llm_response = LLMResponse(text="not json", tokens_in=10, tokens_out=5, model_id="claude-haiku-4-5-20251001")
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}, clear=False), patch.object(
            provider_io, "call_llm", return_value=llm_response
        ):
            result = provider_io._structure_canonical_page_with_llm(
                "Some Series", "Some Author", "page text", "Canonical Goodreads page"
            )
        self.assertEqual(result, [])

    def test_returns_empty_when_llm_call_raises(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}, clear=False), patch.object(
            provider_io, "call_llm", side_effect=RuntimeError("boom")
        ):
            result = provider_io._structure_canonical_page_with_llm(
                "Some Series", "Some Author", "page text", "Canonical Goodreads page"
            )
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
