import unittest
from unittest.mock import patch

import intelligence


class LookupBookSummaryTest(unittest.TestCase):
    """intelligence.lookup_book_summary asks Google Books' intitle: search,
    which is a relevance-ranked text match, not an exact-phrase lookup -- it
    doesn't reliably rank the exact requested volume first. These tests
    guard against blindly trusting result order when we already know which
    book number we're looking for.
    """

    def _mock_results(self, results):
        return patch.object(intelligence.discovery_engine, "_fetch_google_books", return_value=results)

    def test_wrong_volume_ranked_first_is_skipped_in_favor_of_the_right_one(self):
        # Regression (live bug): looking up book 1 of "1% Lifesteal" returned
        # Google's "Volume 4" result (ranked first) instead of the real book
        # 1 match ("Book one", ranked second), silently attaching book 4's
        # summary to book 1.
        results = [
            {
                "title": "1% Lifesteal (Volume 4): A LitRPG Adventure",
                "description": "Book four's description.",
                "authors": ["Robert Blaise"],
                "source_url": None,
            },
            {
                "title": "1% Lifesteal: A LitRPG Adventure. Book one",
                "description": "Book one's description.",
                "authors": ["Robert Blaise"],
                "source_url": None,
            },
        ]
        with self._mock_results(results):
            result = intelligence.lookup_book_summary(
                "1% Lifesteal: A LitRPG: (1% Lifesteal Book 1)",
                "Robert Blaise",
                book_number=1,
                series_name="1% Lifesteal",
            )

        self.assertTrue(result["found"])
        self.assertEqual(result["summary"], "Book one's description.")
        self.assertEqual(result["matched_title"], "1% Lifesteal: A LitRPG Adventure. Book one")

    def test_only_wrong_volume_available_returns_not_found_rather_than_wrong_summary(self):
        # If the only numbered match Google has is for a *different* book,
        # returning it would silently mislabel it as this book's summary --
        # better to report nothing than something confidently wrong.
        results = [
            {
                "title": "1% Lifesteal (Volume 4): A LitRPG Adventure",
                "description": "Book four's description.",
                "authors": ["Robert Blaise"],
                "source_url": None,
            }
        ]
        with self._mock_results(results):
            result = intelligence.lookup_book_summary(
                "1% Lifesteal (Volume 2): A LitRPG: (1% Lifesteal Book 2)",
                "Robert Blaise",
                book_number=2,
                series_name="1% Lifesteal",
            )

        self.assertFalse(result["found"])
        self.assertIsNone(result["summary"])

    def test_unnumbered_lookup_still_accepts_first_described_result(self):
        # When we don't know the book's number (book_number=None), fall back
        # to the original behavior of trusting the first described result.
        results = [
            {
                "title": "Some Standalone Novel",
                "description": "A description.",
                "authors": ["Some Author"],
                "source_url": None,
            }
        ]
        with self._mock_results(results):
            result = intelligence.lookup_book_summary("Some Standalone Novel", "Some Author")

        self.assertTrue(result["found"])
        self.assertEqual(result["summary"], "A description.")


if __name__ == "__main__":
    unittest.main()
