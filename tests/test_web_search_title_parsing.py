"""Regression coverage for provider_io._parse_web_search_structured_items'
bare-series-title backstop.

"Defiance of the Fall 17" investigation (2026-08-30/31): the web-search
structuring LLM prompt asks for a "clean" title with the series name and
any "Book N" suffix stripped. For a book whose ENTIRE title is just
"<Series Name> <N>" (common in progression-fantasy/LitRPG series, no
distinct subtitle at all), stripping both leaves nothing distinguishing,
and the model fell back to echoing the bare series name while still
correctly reporting book_number in its own JSON field. That bare title then
carries no number for core_title_key to fold in, so it can't be recognized
as the same book as a same-numbered candidate from another provider (no
shared title_key to merge on) -- delta_engine's duplicate_number check then
flags the resulting two title-distinguishable-looking, unmerged candidates
as a real conflict, tanking confidence and dropping the whole thing. It can
also fall through persistence's bare-title identity fallback straight onto
an unrelated owned book (most often book 1, whose real title very often
really is just the bare series name -- see
tests/test_series_check_persistence.py's
test_bare_titled_candidate_with_mismatched_number_does_not_clobber_book_one
for that half of the incident).

_parse_web_search_structured_items reconstructs the title (folding the
number back in) whenever it detects this exact collapse: book_number is
set, isn't 1 (book 1 legitimately IS often just the bare series name with
no number -- reconstructing "<Series> 1" there would be wrong, not a fix),
and the LLM's title carries zero content beyond the bare series name.
"""
import unittest

from provider_io import _parse_web_search_structured_items


def _pair(item: dict, url: str = "https://example.com/book") -> tuple[dict, dict]:
    source = {"url": url, "description": "a search result snippet"}
    return (item, source)


class ParseWebSearchStructuredItemsBareTitleBackstopTest(unittest.TestCase):
    def test_bare_series_title_with_higher_number_is_reconstructed(self):
        item = {
            "title": "Defiance of the Fall",
            "series_name": "Defiance of the Fall",
            "book_number": 17,
            "author_names": ["JF Brink"],
            "published_date": "2026-09-23",
            "is_upcoming": True,
            "isbn13": None,
        }
        results = _parse_web_search_structured_items([_pair(item)], author="JF Brink")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Defiance of the Fall 17")
        self.assertEqual(results[0]["series_number_hint"], 17.0)

    def test_bare_series_title_at_book_one_is_left_alone(self):
        # Book 1 legitimately being titled with nothing but the bare series
        # name is the expected, common case -- must NOT get "1" appended.
        item = {
            "title": "Defiance of the Fall",
            "series_name": "Defiance of the Fall",
            "book_number": 1,
            "author_names": ["JF Brink"],
            "published_date": "2020-01-01",
            "is_upcoming": False,
            "isbn13": None,
        }
        results = _parse_web_search_structured_items([_pair(item)], author="JF Brink")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Defiance of the Fall")

    def test_title_that_already_carries_its_own_number_is_left_alone(self):
        item = {
            "title": "Defiance of the Fall 17",
            "series_name": "Defiance of the Fall",
            "book_number": 17,
            "author_names": ["JF Brink"],
            "published_date": None,
            "is_upcoming": True,
            "isbn13": None,
        }
        results = _parse_web_search_structured_items([_pair(item)], author="JF Brink")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Defiance of the Fall 17")

    def test_genuinely_distinct_subtitle_is_left_alone(self):
        # A real, distinct subtitle unrelated to the series name must never
        # get a number appended just because it lacks one of its own --
        # that's the normal, correct "Book N" stripping case, not the bug.
        item = {
            "title": "The Great Beyond",
            "series_name": "Some Series",
            "book_number": 4,
            "author_names": ["Some Author"],
            "published_date": "2024-01-01",
            "is_upcoming": False,
            "isbn13": None,
        }
        results = _parse_web_search_structured_items([_pair(item)], author="Some Author")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "The Great Beyond")

    def test_no_series_name_hint_is_left_alone(self):
        # Nothing to compare the bare title against -- can't tell whether
        # it collapsed, so must not guess.
        item = {
            "title": "Some Standalone Title",
            "series_name": None,
            "book_number": 5,
            "author_names": ["Some Author"],
            "published_date": "2024-01-01",
            "is_upcoming": False,
            "isbn13": None,
        }
        results = _parse_web_search_structured_items([_pair(item)], author="Some Author")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "Some Standalone Title")


if __name__ == "__main__":
    unittest.main()
