"""PP-6: conformance test suite for every `provider_protocol.py` adapter --
asserts the one contract PP-1/PP-2/PP-3 exist to guarantee: every provider
adapter always returns a `ProviderFetchResult`, never raises, and a genuine
failure (`ok=False`) is always distinguishable from a genuine empty success
(`ok=True, items=[]`).

Catalog providers (Google Books/OpenLibrary/Hardcover) share one
`fetch(query, *, max_results=None, telemetry=None)` shape and are tested
together via `CATALOG_PROVIDERS`. The web and Apify providers have
deliberately different signatures (see `provider_protocol.py`'s
`WebDiscoveryProvider`/`ApifyProvider` docstrings) and get their own tests.

PB-3b's `FixtureBackedProvider` (once it exists) is added to
`CATALOG_PROVIDERS`-style coverage in `tests/test_fixture_provider.py`
instead of here, since it takes a `recordings_dir` constructor argument the
other adapters don't have -- see that file's own conformance test.
"""
import unittest
from unittest.mock import patch

import discovery_engine
import apify_provider
from provider_protocol import (
    ApifyProvider,
    GoogleBooksProvider,
    HardcoverProvider,
    OpenLibraryProvider,
    ProviderFetchResult,
    RawResult,
    WebDiscoveryProvider,
)
from services.discovery_telemetry import DiscoveryTelemetry

CATALOG_PROVIDERS = [
    ("google", GoogleBooksProvider(), "_fetch_google_books"),
    ("openlibrary", OpenLibraryProvider(), "_fetch_openlibrary"),
    ("hardcover", HardcoverProvider(), "_fetch_hardcover"),
]


def _legacy_dict(source: str, title: str) -> dict:
    return {
        "source": source,
        "source_id": "id-1",
        "title": title,
        "authors": ["Some Author"],
        "published_date": "2024",
        "description": None,
        "isbn13": None,
        "source_url": "https://example.com/book",
        "language": "en",
    }


class CatalogProviderConformanceTest(unittest.TestCase):
    def test_successful_fetch_returns_ok_true_with_raw_results(self):
        for name, provider, patch_target in CATALOG_PROVIDERS:
            with self.subTest(provider=name):
                with patch.object(
                    discovery_engine, patch_target, return_value=[_legacy_dict(name, "Some Book")]
                ):
                    result = provider.fetch("some query")
                self.assertIsInstance(result, ProviderFetchResult)
                self.assertTrue(result.ok)
                self.assertIsNone(result.error)
                self.assertEqual(len(result.items), 1)
                self.assertIsInstance(result.items[0], RawResult)
                self.assertEqual(result.items[0].title, "Some Book")

    def test_genuine_empty_result_is_ok_true_not_a_failure(self):
        for name, provider, patch_target in CATALOG_PROVIDERS:
            with self.subTest(provider=name):
                with patch.object(discovery_engine, patch_target, return_value=[]):
                    result = provider.fetch("some query")
                self.assertTrue(result.ok)
                self.assertIsNone(result.error)
                self.assertEqual(result.items, [])

    def test_raising_provider_never_propagates_and_is_ok_false(self):
        for name, provider, patch_target in CATALOG_PROVIDERS:
            with self.subTest(provider=name):
                with patch.object(
                    discovery_engine, patch_target, side_effect=RuntimeError("simulated network error")
                ):
                    result = provider.fetch("some query")  # must not raise
                self.assertFalse(result.ok)
                self.assertIn("simulated network error", result.error)
                self.assertEqual(result.items, [])

    def test_does_not_forward_a_default_max_results_when_caller_omits_it(self):
        # Regression guard: an earlier version of these adapters always
        # forwarded max_results positionally, which changed the exact call
        # signature every existing mock-based test in test_series_discovery.py
        # asserted against (e.g. `_fetch_hardcover("query")` became
        # `_fetch_hardcover("query", 25)`). Omitting the override entirely
        # when the caller doesn't ask for one keeps both behaviors intact.
        for name, provider, patch_target in CATALOG_PROVIDERS:
            with self.subTest(provider=name):
                with patch.object(discovery_engine, patch_target, return_value=[]) as mock_fn:
                    provider.fetch("some query")
                mock_fn.assert_called_once_with("some query")


class WebDiscoveryProviderConformanceTest(unittest.TestCase):
    def test_successful_fetch_returns_ok_true_with_raw_results(self):
        provider = WebDiscoveryProvider()
        with patch.object(
            discovery_engine, "_fetch_web_search", return_value=[_legacy_dict("web_search", "A Web Book")]
        ):
            result = provider.fetch(["query one"], "Some Series", "Some Author")
        self.assertTrue(result.ok)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].title, "A Web Book")

    def test_genuine_empty_result_is_ok_true_not_a_failure(self):
        provider = WebDiscoveryProvider()
        with patch.object(discovery_engine, "_fetch_web_search", return_value=[]):
            result = provider.fetch(["query one"], "Some Series", "Some Author")
        self.assertTrue(result.ok)
        self.assertEqual(result.items, [])

    def test_raising_provider_never_propagates_and_is_ok_false(self):
        provider = WebDiscoveryProvider()
        with patch.object(
            discovery_engine, "_fetch_web_search", side_effect=RuntimeError("web search blew up")
        ):
            result = provider.fetch(["query one"], "Some Series", "Some Author")  # must not raise
        self.assertFalse(result.ok)
        self.assertIn("web search blew up", result.error)


class ApifyProviderConformanceTest(unittest.TestCase):
    def test_successful_fetch_returns_ok_true_with_raw_results(self):
        provider = ApifyProvider()
        with patch.object(
            apify_provider, "fetch_apify_candidates", return_value=[_legacy_dict("apify", "An Apify Book")]
        ):
            result = provider.fetch("some query", budget=apify_provider.ApifyCallBudget())
        self.assertTrue(result.ok)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].title, "An Apify Book")

    def test_already_never_raises_and_stays_ok_true_on_empty(self):
        # Apify's own contract is already "never raises, return []" (see
        # apify_provider.fetch_apify_candidates's docstring) -- this adapter
        # doesn't change that, just wraps it in the same structural shape.
        provider = ApifyProvider()
        with patch.object(apify_provider, "fetch_apify_candidates", return_value=[]):
            result = provider.fetch("some query", budget=apify_provider.ApifyCallBudget())
        self.assertTrue(result.ok)
        self.assertEqual(result.items, [])

    def test_an_unexpected_internal_exception_is_still_caught_and_ok_false(self):
        # Belt-and-suspenders: even though fetch_apify_candidates itself is
        # documented as never-raising, the adapter's _safe_call boundary
        # still guards against a genuinely unexpected exception rather than
        # assuming that contract can never be violated by a future change.
        provider = ApifyProvider()
        with patch.object(
            apify_provider, "fetch_apify_candidates", side_effect=RuntimeError("unexpected bug")
        ):
            result = provider.fetch("some query", budget=apify_provider.ApifyCallBudget())
        self.assertFalse(result.ok)
        self.assertIn("unexpected bug", result.error)


class ProviderTelemetryWiringTest(unittest.TestCase):
    """PB-9: every adapter records a `record_provider_call` entry through
    the shared `_safe_call` boundary, for both success and failure.
    """

    def test_catalog_provider_success_records_an_ok_call(self):
        telemetry = DiscoveryTelemetry()
        with patch.object(discovery_engine, "_fetch_google_books", return_value=[]):
            GoogleBooksProvider().fetch("some query", telemetry=telemetry)
        summary = telemetry.summary()
        self.assertEqual(summary["by_provider"]["google"], {"calls": 1, "ok": 1, "failed": 0, "duration_s": summary["by_provider"]["google"]["duration_s"]})

    def test_catalog_provider_failure_records_a_failed_call(self):
        telemetry = DiscoveryTelemetry()
        with patch.object(discovery_engine, "_fetch_hardcover", side_effect=RuntimeError("boom")):
            HardcoverProvider().fetch("some query", telemetry=telemetry)
        summary = telemetry.summary()
        self.assertEqual(summary["by_provider"]["hardcover"]["calls"], 1)
        self.assertEqual(summary["by_provider"]["hardcover"]["ok"], 0)
        self.assertEqual(summary["by_provider"]["hardcover"]["failed"], 1)

    def test_web_provider_records_a_call_and_still_forwards_telemetry_to_fetch_web_search(self):
        telemetry = DiscoveryTelemetry()
        with patch.object(discovery_engine, "_fetch_web_search", return_value=[]) as mock_fetch:
            WebDiscoveryProvider().fetch(["q"], "Some Series", "Some Author", telemetry=telemetry)
        # Recorded at the adapter level...
        self.assertEqual(telemetry.summary()["by_provider"]["web"]["calls"], 1)
        # ...and still passed through to _fetch_web_search itself, which
        # does its own internal per-query telemetry recording.
        self.assertIs(mock_fetch.call_args.kwargs["telemetry"], telemetry)

    def test_apify_provider_records_a_call(self):
        telemetry = DiscoveryTelemetry()
        with patch.object(apify_provider, "fetch_apify_candidates", return_value=[]):
            ApifyProvider().fetch("some query", telemetry=telemetry, budget=apify_provider.ApifyCallBudget())
        self.assertEqual(telemetry.summary()["by_provider"]["apify"]["calls"], 1)

    def test_no_telemetry_passed_means_no_recording_and_no_crash(self):
        with patch.object(discovery_engine, "_fetch_google_books", return_value=[]):
            result = GoogleBooksProvider().fetch("some query")  # telemetry defaults to None
        self.assertTrue(result.ok)


class RawResultShapeTest(unittest.TestCase):
    def test_from_legacy_dict_ignores_unknown_keys(self):
        raw = RawResult.from_legacy_dict({**_legacy_dict("google_books", "Title"), "some_unrelated_key": 123})
        self.assertEqual(raw.title, "Title")

    def test_from_legacy_dict_defaults_missing_optional_fields_to_none(self):
        raw = RawResult.from_legacy_dict({"source": "google_books", "title": "Title"})
        self.assertIsNone(raw.series_number_hint)
        self.assertEqual(raw.authors, [])

    def test_to_legacy_dict_round_trips(self):
        original = _legacy_dict("hardcover", "Title")
        raw = RawResult.from_legacy_dict(original)
        round_tripped = raw.to_legacy_dict()
        for key, value in original.items():
            self.assertEqual(round_tripped[key], value)


if __name__ == "__main__":
    unittest.main()
