import unittest

from new_book_checker import (
    _micro_filter_reasons,
    _rank_candidate,
    parse_amazon_candidates,
    parse_fantastic_fiction_candidates,
    parse_google_organic_candidates,
    parse_publisher_or_author_candidates,
)


class NewBookCheckerParsingTests(unittest.TestCase):
    def test_parse_amazon_candidates_extracts_title_author_number_url(self):
        html = '''
        <div data-component-type="s-search-result">
          <h2><a href="https://www.amazon.com/dp/abc"><span>The Expanse Book 10</span></a></h2>
          <div>by James S. A. Corey</div>
        </div>
        '''

        candidates = parse_amazon_candidates(html, "The Expanse")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["title"], "The Expanse Book 10")
        self.assertEqual(candidates[0]["book_number"], 10)
        self.assertIn("James", candidates[0]["author"])
        self.assertEqual(candidates[0]["url"], "https://www.amazon.com/dp/abc")

    def test_parse_fantasticfiction_candidates_extracts_series_number(self):
        html = '''
        <div class="bookitem">
          <a href="https://www.fantasticfiction.com/c/corey-james-sa/book10.htm">Leviathan Falls</a>
          <span>The Expanse series - Book 10 by James S. A. Corey</span>
        </div>
        '''

        candidates = parse_fantastic_fiction_candidates(html, "The Expanse")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["title"], "Leviathan Falls")
        self.assertEqual(candidates[0]["book_number"], 10)
        self.assertIn("James", candidates[0]["author"])
        self.assertIn("fantasticfiction.com", candidates[0]["url"])

    def test_parse_publisher_or_author_candidates_pattern(self):
        html = '''
        <html>
          <head><title>Leviathan Falls | Publisher</title></head>
          <body>
            <h1>Leviathan Falls</h1>
            <p>Book #10 in the The Expanse series by James S. A. Corey.</p>
          </body>
        </html>
        '''

        candidates = parse_publisher_or_author_candidates(html, "The Expanse")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["title"], "Leviathan Falls")
        self.assertEqual(candidates[0]["book_number"], 10)
        self.assertIn("James", candidates[0]["author"])

    def test_parse_google_candidates_top_results(self):
        html = '''
        <div class="g">
          <a href="/url?q=https%3A%2F%2Fexample.com%2Fbook10&sa=U"><h3>The Expanse Book 10 announced</h3></a>
          <div class="VwiC3b">Book 10 in The Expanse series by James S. A. Corey.</div>
        </div>
        <div class="g">
          <a href="/url?q=https%3A%2F%2Fexample.org%2Fother&sa=U"><h3>Unrelated result</h3></a>
          <div class="VwiC3b">No series info.</div>
        </div>
        '''

        candidates = parse_google_organic_candidates(html, "The Expanse")

        self.assertGreaterEqual(len(candidates), 1)
        first = candidates[0]
        self.assertEqual(first["title"], "The Expanse Book 10 announced")
        self.assertEqual(first["book_number"], 10)
        self.assertIn("example.com/book10", first["url"])


class NewBookCheckerFilterAndRankingTests(unittest.TestCase):
    def test_micro_filters_reject_bad_candidate(self):
        bad = {
            "title": "Some Other Series Novel",
            "book_number": None,
            "author": "Nobody",
            "url": "https://openlibrary.org/work/OL123W",
        }

        reasons = _micro_filter_reasons(bad, "The Expanse", "search")

        self.assertIn("metadata-domain", reasons)
        self.assertIn("missing-book-number", reasons)
        self.assertIn("series-not-in-title", reasons)

    def test_ranking_prefers_higher_priority_provider_when_matches_equal(self):
        candidate = {
            "title": "The Expanse Book 10",
            "author": "James S. A. Corey",
            "book_number": 10,
            "url": "https://example.com/book10",
        }

        publisher_score = _rank_candidate(candidate, "The Expanse", "James S. A. Corey", 10, "publisher_site")
        amazon_score = _rank_candidate(candidate, "The Expanse", "James S. A. Corey", 10, "amazon_books")

        self.assertGreater(publisher_score, amazon_score)


if __name__ == "__main__":
    unittest.main()
