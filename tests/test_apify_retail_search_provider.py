"""Regression coverage for apify_retail_search_provider.py -- the second,
independent Apify actor (Second Apify Actor architecture review,
2026-09-02 chat). Mirrors tests/test_apify_provider.py's own patching
pattern (patches apify_client.ApifyClient directly) so this suite never
depends on live Apify actors or a real APIFY_API_TOKEN.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

import apify_retail_search_provider
from apify_provider import ApifyCallBudget
from apify_retail_search_provider import fetch_retail_search_candidates


def _mock_apify_client(dataset_items: list[dict]):
    """Same fake-ApifyClient shape as test_apify_provider.py's own helper
    -- see that module's docstring for why .call() returns an attribute-
    style object rather than a dict.
    """
    client = MagicMock()
    client.actor_client = MagicMock()
    client.actor_client.call.return_value = MagicMock(default_dataset_id="the-dataset")
    client.actor.return_value = client.actor_client

    dataset_client = MagicMock()
    dataset_client.list_items.return_value = MagicMock(items=dataset_items)
    client.dataset.return_value = dataset_client
    return client


class FetchRetailSearchCandidatesTest(unittest.TestCase):
    def test_returns_empty_without_api_token(self):
        with patch.dict(os.environ, {"APIFY_API_TOKEN": ""}):
            result = fetch_retail_search_candidates("Some Series Author", ApifyCallBudget())
        self.assertEqual(result, [])

    def test_returns_empty_when_budget_already_exhausted(self):
        budget = ApifyCallBudget(max_calls=0)
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}):
            result = fetch_retail_search_candidates("Some Series Author", budget)
        self.assertEqual(result, [])

    def test_calls_actor_with_query_maxpages_and_country(self):
        mock_client = _mock_apify_client(
            [{"asin": "B0AAA1111", "product_title": "Book Five", "product_url": "https://www.amazon.com/dp/B0AAA1111"}]
        )
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_retail_search_candidates("Some Series Author", ApifyCallBudget())

        mock_client.actor.assert_called_once_with(apify_retail_search_provider.APIFY_RETAIL_SEARCH_ACTOR_ID)
        run_input = mock_client.actor_client.call.call_args.kwargs["run_input"]
        self.assertEqual(run_input["query"], "Some Series Author")
        self.assertEqual(run_input["maxPages"], apify_retail_search_provider.APIFY_RETAIL_SEARCH_MAX_PAGES)
        self.assertEqual(run_input["country"], apify_retail_search_provider.APIFY_RETAIL_SEARCH_COUNTRY)
        self.assertNotIn("categoryUrls", run_input)  # different actor, different input shape
        self.assertEqual(len(result), 1)
        candidate = result[0]
        self.assertEqual(candidate["source"], "apify_retail_search")
        self.assertEqual(candidate["title"], "Book Five")
        self.assertEqual(candidate["asin"], "B0AAA1111")
        self.assertEqual(candidate["source_url"], "https://www.amazon.com/dp/B0AAA1111")

    def test_multiple_items_from_one_call_all_become_candidates(self):
        mock_client = _mock_apify_client(
            [
                {"asin": "B0AAA1111", "product_title": "Book Two"},
                {"asin": "B0BBB2222", "product_title": "Book Five"},
            ]
        )
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_retail_search_candidates("Some Series Author", ApifyCallBudget())

        self.assertEqual(mock_client.actor.call_count, 1)
        self.assertEqual([c["title"] for c in result], ["Book Two", "Book Five"])

    def test_error_typed_items_are_skipped(self):
        mock_client = _mock_apify_client(
            [
                {"error": "no_results_found"},
                {"asin": "B0AAA1111", "product_title": "Real Book"},
            ]
        )
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_retail_search_candidates("Some Series Author", ApifyCallBudget())

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Real Book")

    def test_budget_exhausted_before_the_single_call_returns_empty(self):
        budget = ApifyCallBudget(max_calls=0)
        mock_client = _mock_apify_client([{"product_title": "Should Not Be Reached"}])
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_retail_search_candidates("Some Series Author", budget)

        mock_client.actor.assert_not_called()
        self.assertEqual(result, [])

    def test_actor_exception_is_caught_and_returns_empty_list(self):
        mock_client = MagicMock()
        mock_client.actor.side_effect = RuntimeError("boom")
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_retail_search_candidates("Some Series Author", ApifyCallBudget())
        self.assertEqual(result, [])

    def test_uses_its_own_budget_not_the_primary_actors(self):
        # Distinct ApifyCallBudget instances -- exhausting one must never
        # affect the other (explicit architecture decision: separate
        # counters, never shared/raised).
        primary_budget = ApifyCallBudget(max_calls=0)  # already exhausted
        retail_budget = ApifyCallBudget(max_calls=1)
        mock_client = _mock_apify_client([{"asin": "B0AAA1111", "product_title": "Book Five"}])
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_retail_search_candidates("Some Series Author", retail_budget)

        self.assertEqual(len(result), 1)
        self.assertFalse(primary_budget.try_consume())  # untouched, still exhausted
        self.assertFalse(retail_budget.try_consume())  # its own single call was spent


class NormalizeSearchResultItemTest(unittest.TestCase):
    def test_missing_fields_are_none_or_empty_not_absent(self):
        normalized = apify_retail_search_provider._normalize_search_result_item({"product_title": "Untitled Draft"})
        self.assertIsNotNone(normalized)
        self.assertIsNone(normalized["asin"])
        self.assertIsNone(normalized["source_url"])
        self.assertIsNone(normalized["cover_image"])
        self.assertIsNone(normalized["isbn13"])
        self.assertIsNone(normalized["published_date"])
        self.assertIsNone(normalized["series_number_hint"])
        self.assertEqual(normalized["authors"], [])

    def test_returns_none_without_a_title(self):
        self.assertIsNone(apify_retail_search_provider._normalize_search_result_item({"asin": "B0AAA1111"}))

    def test_confidence_is_not_hardcoded(self):
        normalized = apify_retail_search_provider._normalize_search_result_item({"product_title": "Desert Protocol"})
        self.assertIsNone(normalized["confidence"])

    def test_source_is_tagged_distinctly_from_primary_actor(self):
        normalized = apify_retail_search_provider._normalize_search_result_item({"product_title": "T"})
        self.assertEqual(normalized["source"], "apify_retail_search")

    def test_source_id_prefers_asin_falls_back_to_url(self):
        with_asin = apify_retail_search_provider._normalize_search_result_item(
            {"product_title": "T", "asin": "B0AAA1111", "product_url": "https://example.com/dp/B0AAA1111"}
        )
        self.assertEqual(with_asin["source_id"], "B0AAA1111")

        without_asin = apify_retail_search_provider._normalize_search_result_item(
            {"product_title": "T", "product_url": "https://example.com/dp/B0AAA1111"}
        )
        self.assertEqual(without_asin["source_id"], "https://example.com/dp/B0AAA1111")


if __name__ == "__main__":
    unittest.main()
