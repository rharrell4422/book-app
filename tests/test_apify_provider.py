"""Regression coverage for apify_provider.py -- the Apify integration's
own module (Check Now only, Phase 1 -- see the Apify integration design
chat's consensus). Patches apify_client.ApifyClient directly (the same
pattern test_series_discovery.py uses for anthropic.Anthropic) so this
suite never depends on live Apify actors or a real APIFY_API_TOKEN.

Single-actor design (2026-08-24): one actor
(apify_provider.APIFY_AMAZON_ACTOR_ID) handles both the "search by query"
and "look up a known Amazon URL" jobs in exactly one call -- see that
module's docstring for why (and what two-actor design this replaced).
"""
import os
import unittest
from unittest.mock import MagicMock, patch

import apify_provider
from apify_provider import ApifyCallBudget, apify_enabled, fetch_apify_candidates


def _mock_apify_client(dataset_items: list[dict]):
    """Builds a fake ApifyClient whose .actor(id).call(...) always
    "succeeds" and whose .dataset(...).list_items().items returns
    dataset_items -- mirroring the real ApifyClient.actor(...).call()/
    .dataset(...).list_items() call shape apify_provider._run_actor_sync
    uses. Since the single-actor design only ever calls one actor id per
    fetch_apify_candidates() call, this doesn't need the old per-actor-id
    dataset_items_by_actor mapping -- one dataset is enough.

    .call()'s return value is a MagicMock with a `default_dataset_id`
    attribute rather than a plain dict -- apify-client 3.x's real
    ActorClient.call() returns a typed Run object (attribute access), not
    a dict. A live regression (2026-08-24) traced "Check Now finds
    nothing from Apify" to _run_actor_sync using dict-style
    run.get("defaultDatasetId") against this real attribute-style Run
    object, which raised AttributeError on every single real call -- a bug
    this suite's previous dict-shaped mock could never have caught. See
    that fix's own comment in _run_actor_sync for the full story.

    Exposes the actor client via client.actor_client so tests can inspect
    exactly what run_input it was called with.
    """
    client = MagicMock()
    client.actor_client = MagicMock()
    client.actor_client.call.return_value = MagicMock(default_dataset_id="the-dataset")
    client.actor.return_value = client.actor_client

    dataset_client = MagicMock()
    dataset_client.list_items.return_value = MagicMock(items=dataset_items)
    client.dataset.return_value = dataset_client
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

    def test_direct_amazon_url_calls_actor_once_with_that_url(self):
        mock_client = _mock_apify_client(
            [
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
        )
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_apify_candidates("Fourth Wing", ["https://amazon.com/dp/B0BXYZ1234"], ApifyCallBudget())

        mock_client.actor.assert_called_once_with(apify_provider.APIFY_AMAZON_ACTOR_ID)
        run_input = mock_client.actor_client.call.call_args.kwargs["run_input"]
        self.assertEqual(run_input["categoryUrls"], [{"url": "https://amazon.com/dp/B0BXYZ1234"}])
        self.assertEqual(len(result), 1)
        candidate = result[0]
        self.assertEqual(candidate["source"], "apify")
        self.assertEqual(candidate["title"], "Fourth Wing")
        self.assertEqual(candidate["authors"], ["Rebecca Yarros"])
        self.assertEqual(candidate["asin"], "B0BXYZ1234")
        self.assertEqual(candidate["isbn13"], "9781649374042")
        self.assertEqual(candidate["cover_image"], "https://example.com/cover.jpg")
        self.assertEqual(candidate["published_date"], "2023-05-02")

    def test_no_url_builds_amazon_search_url_from_query(self):
        mock_client = _mock_apify_client(
            [{"title": "Iron Flame", "author": ["Rebecca Yarros"], "asin": "B0AAA1111"}]
        )
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_apify_candidates("Iron Flame Rebecca Yarros", None, ApifyCallBudget())

        mock_client.actor.assert_called_once_with(apify_provider.APIFY_AMAZON_ACTOR_ID)
        run_input = mock_client.actor_client.call.call_args.kwargs["run_input"]
        self.assertEqual(
            run_input["categoryUrls"],
            [{"url": "https://www.amazon.com/s?k=Iron+Flame+Rebecca+Yarros"}],
        )
        self.assertEqual(run_input["maxItemsPerStartUrl"], apify_provider.APIFY_MAX_ITEMS_PER_START_URL)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Iron Flame")

    def test_multiple_items_from_one_search_call_all_become_candidates(self):
        mock_client = _mock_apify_client(
            [
                {"title": "Book Two", "asin": "B0AAA1111"},
                {"title": "Book Five", "asin": "B0BBB2222"},
                {"title": "Book Ten", "asin": "B0CCC3333"},
            ]
        )
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_apify_candidates("Some Series", None, ApifyCallBudget())

        self.assertEqual(mock_client.actor.call_count, 1)
        self.assertEqual([c["title"] for c in result], ["Book Two", "Book Five", "Book Ten"])

    def test_error_typed_items_are_skipped(self):
        mock_client = _mock_apify_client(
            [
                {"error": "no_results_found", "errorDescription": "No results were found."},
                {"title": "Real Book", "asin": "B0AAA1111"},
            ]
        )
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_apify_candidates("Some Series", None, ApifyCallBudget())

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Real Book")

    def test_budget_exhausted_before_the_single_call_returns_empty(self):
        budget = ApifyCallBudget(max_calls=0)
        mock_client = _mock_apify_client([{"title": "Should Not Be Reached"}])
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_apify_candidates("query", None, budget)

        mock_client.actor.assert_not_called()
        self.assertEqual(result, [])

    def test_actor_exception_is_caught_and_returns_empty_list(self):
        mock_client = MagicMock()
        mock_client.actor.side_effect = RuntimeError("boom")
        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_apify_candidates("query", ["https://amazon.com/dp/B0BXYZ1234"], ApifyCallBudget())
        self.assertEqual(result, [])

    def test_run_object_uses_attribute_access_not_dict_get(self):
        """Regression test for the 2026-08-24 live bug: apify-client 3.x's
        real ActorClient.call() returns a typed Run object, not a dict, so
        a mock (or real client) that would raise AttributeError on
        dict-style .get("defaultDatasetId") access must still work here --
        i.e. _run_actor_sync must be reading run.default_dataset_id, not
        run["defaultDatasetId"]/run.get(...). A plain dict would silently
        pass this test even with the old broken code (dicts have .get()),
        so this uses a real object with __slots__ and no .get() method at
        all, matching apify_client's actual Run model shape closely enough
        to catch the same AttributeError the live bug hit.
        """

        class FakeRun:
            __slots__ = ("default_dataset_id",)

            def __init__(self, default_dataset_id):
                self.default_dataset_id = default_dataset_id

        mock_client = MagicMock()
        mock_client.actor_client = MagicMock()
        mock_client.actor_client.call.return_value = FakeRun("the-dataset")
        mock_client.actor.return_value = mock_client.actor_client

        dataset_client = MagicMock()
        dataset_client.list_items.return_value = MagicMock(items=[{"title": "Real Book", "asin": "B0AAA1111"}])
        mock_client.dataset.return_value = dataset_client

        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_apify_candidates("query", None, ApifyCallBudget())

        mock_client.dataset.assert_called_once_with("the-dataset")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "Real Book")

    def test_run_with_no_dataset_id_returns_empty(self):
        class FakeRun:
            __slots__ = ("default_dataset_id",)

            def __init__(self, default_dataset_id):
                self.default_dataset_id = default_dataset_id

        mock_client = MagicMock()
        mock_client.actor_client = MagicMock()
        mock_client.actor_client.call.return_value = FakeRun(None)
        mock_client.actor.return_value = mock_client.actor_client

        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_apify_candidates("query", None, ApifyCallBudget())

        mock_client.dataset.assert_not_called()
        self.assertEqual(result, [])

    def test_none_run_returns_empty(self):
        mock_client = MagicMock()
        mock_client.actor_client = MagicMock()
        mock_client.actor_client.call.return_value = None
        mock_client.actor.return_value = mock_client.actor_client

        with patch.dict(os.environ, {"APIFY_API_TOKEN": "test-token"}), patch(
            "apify_client.ApifyClient", return_value=mock_client
        ):
            result = fetch_apify_candidates("query", None, ApifyCallBudget())

        mock_client.dataset.assert_not_called()
        self.assertEqual(result, [])


class NormalizeProductItemTest(unittest.TestCase):
    def test_missing_fields_are_none_not_absent_or_empty_string(self):
        normalized = apify_provider._normalize_product_item({"title": "Untitled Draft"})
        self.assertIsNotNone(normalized)
        self.assertIsNone(normalized["published_date"])
        self.assertIsNone(normalized["isbn13"])
        self.assertIsNone(normalized["asin"])
        self.assertIsNone(normalized["cover_image"])
        self.assertIsNone(normalized["series_number_hint"])
        self.assertIsNone(normalized["series_name_hint"])
        self.assertIsNone(normalized["series_total_hint"])
        self.assertEqual(normalized["authors"], [])

    def test_returns_none_without_a_title(self):
        self.assertIsNone(apify_provider._normalize_product_item({"asin": "B0AAA1111"}))

    def test_cover_image_list_field_takes_first_item(self):
        normalized = apify_provider._normalize_product_item({"title": "T", "image": ["https://a.example/1.jpg", "https://a.example/2.jpg"]})
        self.assertEqual(normalized["cover_image"], "https://a.example/1.jpg")

    def test_cover_image_dict_field_extracts_url(self):
        normalized = apify_provider._normalize_product_item({"title": "T", "mainImage": {"url": "https://a.example/1.jpg"}})
        self.assertEqual(normalized["cover_image"], "https://a.example/1.jpg")

    def test_series_position_parsed_from_dynamic_book_n_of_m_attribute_key(self):
        normalized = apify_provider._normalize_product_item(
            {
                "title": "Desert Protocol",
                "attributes": [
                    {"key": "Publication date", "value": "January 1, 2026"},
                    {"key": "Book 2 of 18", "value": "Jonathan Hunt Thriller Series"},
                ],
            }
        )
        self.assertEqual(normalized["published_date"], "January 1, 2026")
        self.assertEqual(normalized["series_number_hint"], "2")
        self.assertEqual(normalized["series_name_hint"], "Jonathan Hunt Thriller Series")
        self.assertEqual(normalized["series_total_hint"], 18)

    def test_series_position_absent_when_no_matching_attribute(self):
        normalized = apify_provider._normalize_product_item(
            {"title": "Standalone Book", "attributes": [{"key": "Publication date", "value": "2024"}]}
        )
        self.assertIsNone(normalized["series_number_hint"])
        self.assertIsNone(normalized["series_name_hint"])
        self.assertIsNone(normalized["series_total_hint"])
        self.assertEqual(normalized["published_date"], "2024")

    def test_attributes_field_of_wrong_type_does_not_raise(self):
        normalized = apify_provider._normalize_product_item({"title": "T", "attributes": "not-a-list"})
        self.assertIsNotNone(normalized)
        self.assertIsNone(normalized["series_number_hint"])


if __name__ == "__main__":
    unittest.main()
