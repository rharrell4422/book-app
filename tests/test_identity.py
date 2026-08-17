import unittest

from services.identity import _normalized_book_number_value, _series_book_identity_key


class SeriesBookIdentityKeyTest(unittest.TestCase):
    """Regression coverage for a live bug: _normalized_book_number_value used
    to truncate (int(float(value))), not round, a fractional book_number --
    so a companion/side-story book at position 3.5 (e.g. Rebecca Yarros's
    "Threshing Day" in The Empyrean) produced the *same* identity key as the
    real book 3 ("Onyx Storm"). Every matching/dedup pass keyed off of
    _series_book_identity_key then treated them as the same row, merging the
    companion book's fields into (or collapsing away) the real numbered
    entry -- i.e. "Check for New" adding Threshing Day silently destroyed
    the existing Onyx Storm row.
    """

    def test_fractional_and_whole_number_produce_different_keys(self):
        whole = _series_book_identity_key("The Empyrean", 3)
        fractional = _series_book_identity_key("The Empyrean", 3.5)
        self.assertIsNotNone(whole)
        self.assertIsNotNone(fractional)
        self.assertNotEqual(whole, fractional)

    def test_whole_number_key_is_stable_whether_int_or_float(self):
        # 3 and 3.0 are the same book number and must still collapse to one
        # key -- only genuinely fractional positions should differ.
        self.assertEqual(
            _series_book_identity_key("The Empyrean", 3),
            _series_book_identity_key("The Empyrean", 3.0),
        )

    def test_normalized_book_number_value_preserves_fraction(self):
        self.assertEqual(_normalized_book_number_value(3.5), 3.5)
        self.assertEqual(_normalized_book_number_value(3), 3.0)
        self.assertIsNone(_normalized_book_number_value(None))
        self.assertIsNone(_normalized_book_number_value(0))


if __name__ == "__main__":
    unittest.main()
