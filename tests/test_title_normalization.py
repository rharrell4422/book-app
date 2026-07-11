import unittest

from services.title_normalization import (
    _apply_custom_title_pattern,
    _normalize_title_clean_up,
    _normalize_title_new_clean,
)


class CleanUpLitrpgSubtitleTest(unittest.TestCase):
    """Regression coverage for the "Clean Up" mode bug where the generic
    LitRPG marketing-subtitle collapse only fired when that phrase was the
    literal end of the title -- which almost never happens on real titles,
    since they're usually followed by a "(Series Name Book #)" suffix.
    """

    def test_strips_sandwiched_descriptor_phrase_before_series_suffix(self):
        # Real title from the Completionist Chronicles series: "LitRPG" is
        # sandwiched between descriptor words on both sides ("Epic Fantasy"
        # before, "Adventure" after), and is followed by a series suffix.
        title = "Unmapped: An Epic Fantasy LitRPG Adventure (The Completionist Chronicles Book 13)"
        result = _normalize_title_clean_up(title, "The Completionist Chronicles")
        self.assertEqual(result, "Unmapped: A LitRPG (The Completionist Chronicles Book 13)")

    def test_strips_bare_a_litrpg_descriptor_with_no_suffix(self):
        result = _normalize_title_clean_up("Some Book: A LitRPG Apocalypse")
        self.assertEqual(result, "Some Book: A LitRPG")

    def test_strips_descriptor_only_after_litrpg_with_no_article(self):
        result = _normalize_title_clean_up("Some Book: LitRPG Novel")
        self.assertEqual(result, "Some Book: LitRPG")

    def test_does_not_touch_a_genuinely_distinct_subtitle(self):
        title = "Some Book: A Completely Different Subtitle (Series Book 5)"
        result = _normalize_title_clean_up(title, "Series")
        self.assertEqual(result, title)

    def test_new_clean_title_rebuilds_full_suffix_from_sandwiched_phrase(self):
        title = "Unmapped: An Epic Fantasy LitRPG Adventure (The Completionist Chronicles Book 13)"
        result = _normalize_title_new_clean(title, "The Completionist Chronicles", 13)
        self.assertEqual(result, "Unmapped (The Completionist Chronicles Book 13)")


class CustomTitlePatternTest(unittest.TestCase):
    """The custom pattern mode was simplified from a mini templating
    language (with [[optional blocks]] and || fallback chains) down to
    plain token substitution with automatic cleanup of stray leftover
    punctuation when a token is blank.
    """

    def test_plain_token_substitution(self):
        result = _apply_custom_title_pattern(
            "{book_title} ({series_name} Book {book_number})",
            "Some Book: A Subtitle",
            "My Series",
            3,
            None,
        )
        self.assertEqual(result, "Some Book (My Series Book 3)")

    def test_blank_subtitle_token_leaves_no_dangling_dash(self):
        result = _apply_custom_title_pattern(
            "{book_title} - {book_subtitle}",
            "Some Book",
            "My Series",
            1,
            None,
        )
        self.assertEqual(result, "Some Book")

    def test_blank_series_name_leaves_no_leading_space_in_parens(self):
        result = _apply_custom_title_pattern(
            "{book_title} ({series_name} Book {book_number})",
            "Some Book",
            "",
            2,
            None,
        )
        self.assertEqual(result, "Some Book (Book 2)")


if __name__ == "__main__":
    unittest.main()
