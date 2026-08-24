"""PB-3b + PP-6: conformance and behavior tests for `FixtureBackedProvider`.

Uses a small, self-contained temp recordings directory (not the shared
`fixtures/provider_recordings/`) so this suite's assertions don't depend on
-- or need updating for -- whatever real recordings happen to exist there.
"""
import json
import tempfile
import unittest
from pathlib import Path

from fixture_provider import FixtureBackedProvider
from provider_protocol import ProviderFetchResult, RawResult


class FixtureBackedProviderTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.recordings_dir = Path(self._tmpdir.name)
        (self.recordings_dir / "sample.json").write_text(
            json.dumps(
                {
                    "id": "sample",
                    "recordings": [
                        {
                            "query": "Some Series Some Author",
                            "items": [
                                {
                                    "source": "web_search",
                                    "title": "Some Book",
                                    "authors": ["Some Author"],
                                    "source_url": "https://example.com/some-book",
                                }
                            ],
                        }
                    ],
                }
            )
        )

    def test_known_query_returns_recorded_items_with_ok_true(self):
        provider = FixtureBackedProvider(self.recordings_dir)
        result = provider.fetch("Some Series Some Author")
        self.assertIsInstance(result, ProviderFetchResult)
        self.assertTrue(result.ok)
        self.assertIsNone(result.error)
        self.assertEqual(len(result.items), 1)
        self.assertIsInstance(result.items[0], RawResult)
        self.assertEqual(result.items[0].title, "Some Book")

    def test_query_normalization_matches_regardless_of_case_or_whitespace(self):
        provider = FixtureBackedProvider(self.recordings_dir)
        result = provider.fetch("  some SERIES   some author  ")
        self.assertEqual(len(result.items), 1)

    def test_unknown_query_is_a_deterministic_empty_success_not_a_failure(self):
        provider = FixtureBackedProvider(self.recordings_dir)
        result = provider.fetch("a query with no recording")
        self.assertTrue(result.ok)
        self.assertIsNone(result.error)
        self.assertEqual(result.items, [])

    def test_never_raises_even_when_recordings_dir_does_not_exist(self):
        provider = FixtureBackedProvider(self.recordings_dir / "does-not-exist")
        result = provider.fetch("anything")  # must not raise
        self.assertTrue(result.ok)
        self.assertEqual(result.items, [])

    def test_max_results_truncates_recorded_items(self):
        (self.recordings_dir / "many.json").write_text(
            json.dumps(
                {
                    "id": "many",
                    "recordings": [
                        {
                            "query": "many results query",
                            "items": [
                                {"source": "web_search", "title": f"Book {i}"} for i in range(5)
                            ],
                        }
                    ],
                }
            )
        )
        provider = FixtureBackedProvider(self.recordings_dir)
        result = provider.fetch("many results query", max_results=2)
        self.assertEqual(len(result.items), 2)

    def test_malformed_recording_file_is_skipped_not_raised(self):
        (self.recordings_dir / "broken.json").write_text("{not valid json")
        provider = FixtureBackedProvider(self.recordings_dir)  # must not raise during construction
        result = provider.fetch("Some Series Some Author")
        self.assertEqual(len(result.items), 1)  # the valid sample.json recording still loads


class RealRecordingsDirectoryTest(unittest.TestCase):
    """Exercises the actual fixtures/provider_recordings/ directory shipped
    with the repo, guarding against a recording file silently regressing
    into something FixtureBackedProvider can't load.
    """

    def test_default_recordings_directory_loads_without_error_and_has_entries(self):
        provider = FixtureBackedProvider()
        result = provider.fetch("Jonathan Hunt Georgia Wagner")
        self.assertTrue(result.ok)
        self.assertGreaterEqual(len(result.items), 1)


if __name__ == "__main__":
    unittest.main()
