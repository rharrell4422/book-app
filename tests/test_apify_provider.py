"""Regression coverage for apify_provider.py -- the Apify integration's
own module (Check Now only, Phase 1 -- see the Apify integration design
chat's consensus). Patches apify_client.ApifyClient directly (the same
pattern test_series_discovery.py uses for anthropic.Anthropic) so this
suite never depends on live Apify actors or a real APIFY_API_TOKEN.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

import apify_provider
from apify_provider import ApifyCallBudget, apify_enabled, fetch_apify_candidates


def _mock_apify_client(dataset_items_by_actor: dict[str, list[dict]]):
    """Builds a fake ApifyClient whose .actor(id).call(...) always
    "succeeds" and whose .dataset(...).list_items().items returns
    dataset_items_by_actor[actor_id] -- mirroring the real
    ApifyClient.actor(...).call()/.dataset(...).list_items() call shape
    apify_provider._run_actor_sync uses.

    Exposes the per-actor-id mock via client.actor_clients_by_id so tests
    can inspect exactly what run_input each actor was called with (a plain
    MagicMock().actor.call_args_list only records the actor id argument,
    not each returned actor client's own .call() arguments).
    """
    client = MagicMock()
    client.actor_clients_by_id = {}

    def fake_actor(actor_id):
        actor_client = client.actor_clients_by_id.setdefault(actor_id, MagicMock())
        actor_client.call.return_value = {"defaultDatasetId": actor_id}
        return actor_client

    def fake_dataset(dataset_id):
        dataset_client = MagicMock()
        dataset_client.list_items.return_value = MagicMock(items=dataset_items_by_actor.get(dataset_id, []))
        return dataset_client

    client.actor.side_effect = fake_actor
    client.dataset.side_effect = fake_dataset
    return client


class ApifyCallBudgetTest(unittest.TestCase):
    def test_allows_calls_up_to_max_then_denies(self):
        budget = ApifyCallBudget(max_calls=2)
        self.assertTrue(budget.try_consume())
        self.assertTrue(budget.try_consume())
        self.assertFalse(budget.try_consume())

    def test_default_max_matches_module_constant(self):
        budget = ApifyCallBudget()
        for _ in range(apify_provider.APIFY_MAX_CALLS_PER_SERIES_RUN):
            self.assertTrue(budget.try_consume())
        self.assertFalse(budget.try_consume())

    def test_zero_budget_denies_immediately(self):
        budget = ApifyCallBudget(max_calls=0)
        self.assertFalse(budget.try_consume())


class ApifyEnabledTest(unittest.TestCase):
    def test_false_when_token_unset(self):
        with patch.dict(os.environ, {"APIFY_API_TOKEN": ""}):
            self.assertFalse(apify_enabled())

    def test_true_when_token_set(self):
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}):
            self.assertTrue(apify_enabled())


class FetchApifyCandidatesTest(unittest.TestCase):
    def test_returns_empty_without_api_token(self):
        with patch.dict(os.environ, {"APIFY_API_TOKEN": ""}):
            result = fetch_apify_candidates("Some Series Book 3", None, ApifyCallBudget())
        self.assertEqual(result, [])

    def test_returns_empty_when_budget_already_exhausted(self):
        budget = ApifyCallBudget(max_calls=0)
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}):
            result = fetch_apify_candidates("Some Series Book 3", None, budget)
        self.assertEqual(result, [])

    def test_direct_amazon_url_skips_search_actor_and_calls_product_actor_only(self):
        mock_client = _mock_apify_client(
            {
                apify_provider.APIFY_AMAZON_PRODUCT_ACTOR_ID: [
                    {
                        "title": "Fourth Wing",
                        "author": ["Rebecca Yarros"],
                        "asin": "B0BXYZ1234",
                        "publicationDate": "2023-05-02",
                        "isbn13": "9781649374042",
                        "thumbnailImage": "https://example.com/cover.jpg",
                        "url": "https://amazon.com/dp/B0BXYZ1234",
                    }
                ]
            }
        )
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_apify_candidates("Fourth Wing", ["https://amazon.com/dp/B0BXYZ1234"], ApifyCallBudget())

        mock_client.actor.assert_called_once_with(apify_provider.APIFY_AMAZON_PRODUCT_ACTOR_ID)
        self.assertEqual(len(result), 1)
        candidate = result[0]
        self.assertEqual(candidate["source"], "apify")
        self.assertEqual(candidate["title"], "Fourth Wing")
        self.assertEqual(candidate["authors"], ["Rebecca Yarros"])
        self.assertEqual(candidate["asin"], "B0BXYZ1234")
        self.assertEqual(candidate["isbn13"], "9781649374042")
        self.assertEqual(candidate["cover_image"], "https://example.com/cover.jpg")
        self.assertEqual(candidate["published_date"], "2023-05-02")

    def test_no_url_falls_back_to_search_actor_then_product_actor(self):
        mock_client = _mock_apify_client(
            {
                apify_provider.APIFY_AMAZON_SEARCH_ACTOR_ID: [
                    {"asin": "B0AAA1111", "url": "https://amazon.com/dp/B0AAA1111"},
                    {"asin": "B0BBB2222", "url": "https://amazon.com/dp/B0BBB2222"},
                ],
                apify_provider.APIFY_AMAZON_PRODUCT_ACTOR_ID: [
                    {"title": "Iron Flame", "author": ["Rebecca Yarros"], "asin": "B0AAA1111"}
                ],
            }
        )
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_apify_candidates("Iron Flame Rebecca Yarros", None, ApifyCallBudget())

        self.assertEqual(mock_client.actor.call_count, 2)
        called_actor_ids = [call.args[0] for call in mock_client.actor.call_args_list]
        self.assertEqual(
            called_actor_ids,
            [apify_provider.APIFY_AMAZON_SEARCH_ACTOR_ID, apify_provider.APIFY_AMAZON_PRODUCT_ACTOR_ID],
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Iron Flame")

    def test_only_top_1_asin_from_search_results_is_used_for_product_extraction(self):
        mock_client = _mock_apify_client(
            {
                apify_provider.APIFY_AMAZON_SEARCH_ACTOR_ID: [
                    {"asin": "B0AAA1111", "url": "https://amazon.com/dp/B0AAA1111"},
                    {"asin": "B0BBB2222", "url": "https://amazon.com/dp/B0BBB2222"},
                ],
                apify_provider.APIFY_AMAZON_PRODUCT_ACTOR_ID: [{"title": "Only First Result"}],
            }
        )
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            fetch_apify_candidates("query", None, ApifyCallBudget())

        product_actor_client = mock_client.actor_clients_by_id[apify_provider.APIFY_AMAZON_PRODUCT_ACTOR_ID]
        product_run_input = product_actor_client.call.call_args.kwargs["run_input"]
        self.assertEqual(product_run_input, {"urls": ["https://amazon.com/dp/B0AAA1111"]})

    def test_second_apify_call_denied_when_budget_exhausted_between_search_and_product(self):
        budget = ApifyCallBudget(max_calls=1)
        mock_client = _mock_apify_client(
            {
                apify_provider.APIFY_AMAZON_SEARCH_ACTOR_ID: [
                    {"asin": "B0AAA1111", "url": "https://amazon.com/dp/B0AAA1111"}
                ],
            }
        )
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_apify_candidates("query", None, budget)

        # Search actor consumed the only budget slot; product actor must
        # never even be attempted.
        mock_client.actor.assert_called_once_with(apify_provider.APIFY_AMAZON_SEARCH_ACTOR_ID)
        self.assertEqual(result, [])

    def test_actor_exception_is_caught_and_returns_empty_list(self):
        mock_client = MagicMock()
        mock_client.actor.side_effect = RuntimeError("boom")
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_apify_candidates("query", ["https://amazon.com/dp/B0BXYZ1234"], ApifyCallBudget())
        self.assertEqual(result, [])


class NormalizeProductItemTest(unittest.TestCase):
    def test_missing_fields_are_none_not_absent_or_empty_string(self):
        normalized = apify_provider._normalize_product_item({"title": "Untitled Draft"})
        self.assertIsNotNone(normalized)
        self.assertIsNone(normalized["published_date"])
        self.assertIsNone(normalized["isbn13"])
        self.assertIsNone(normalized["asin"])
        self.assertIsNone(normalized["cover_image"])
        self.assertEqual(normalized["authors"], [])

    def test_returns_none_without_a_title(self):
        self.assertIsNone(apify_provider._normalize_product_item({"asin": "B0AAA1111"}))

    def test_cover_image_list_field_takes_first_item(self):
        normalized = apify_provider._normalize_product_item({"title": "T", "image": ["https://a.example/1.jpg", "https://a.example/2.jpg"]})
        self.assertEqual(normalized["cover_image"], "https://a.example/1.jpg")

    def test_cover_image_dict_field_extracts_url(self):
        normalized = apify_provider._normalize_product_item({"title": "T", "mainImage": {"url": "https://a.example/1.jpg"}})
        self.assertEqual(normalized["cover_image"], "https://a.example/1.jpg")


if __name__ == "__main__":
    unittest.main()
