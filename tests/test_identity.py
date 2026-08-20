import unittest
from types import SimpleNamespace

from services.identity import (
    _canonical_title_identity_key,
    _normalized_book_number_value,
    _series_book_identity_key,
    owned_title_for_identity,
)


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


class CanonicalTitleIdentityKeyTest(unittest.TestCase):
    """Regression coverage for a live bug: a discovered "Iron Flame SIGNED"
    listing carried its own real ISBN, so it skipped the ASIN- and
    series+number-based dedupe paths, and _canonical_title_identity_key
    didn't know "signed" was a non-distinctive printing variant the way it
    already knew about format markers like "(Kindle Edition)". The result
    was a duplicate book persisted alongside the already-owned "Iron Flame"
    instead of being recognized as the same title.
    """

    def test_signed_edition_qualifier_matches_the_plain_title(self):
        self.assertEqual(
            _canonical_title_identity_key("Iron Flame SIGNED"),
            _canonical_title_identity_key("Iron Flame"),
        )
        self.assertEqual(
            _canonical_title_identity_key("Iron Flame (Signed Edition)"),
            _canonical_title_identity_key("Iron Flame"),
        )
        self.assertEqual(
            _canonical_title_identity_key("Iron Flame - Signed Copy"),
            _canonical_title_identity_key("Iron Flame"),
        )
        self.assertEqual(
            _canonical_title_identity_key("Iron Flame Autographed"),
            _canonical_title_identity_key("Iron Flame"),
        )

    def test_does_not_strip_a_distinctive_title_containing_the_word(self):
        # "signed" only gets stripped as a trailing/bracketed edition
        # qualifier -- it must not corrupt a title where the word is
        # actually part of the story's own name.
        self.assertNotEqual(
            _canonical_title_identity_key("The Signed Confession"),
            _canonical_title_identity_key("The Confession"),
        )


class OwnedTitleForIdentityTest(unittest.TestCase):
    """Regression coverage for the two-column title model's identity
    coalescing rule (see project design chat, §3.3): identity/discovery
    matching against an *existing* owned book must key off canonical_title
    when present, falling back to title -- otherwise a resolved title (e.g.
    a corrected "Volume 4" -> "Book 4") would stay keyed off whatever the
    user originally typed and never get recognized under its resolved
    identity.
    """

    def test_falls_back_to_title_when_canonical_title_is_none(self):
        book = SimpleNamespace(title="1% Lifesteal Volume 4", canonical_title=None)
        self.assertEqual(owned_title_for_identity(book), "1% Lifesteal Volume 4")

    def test_prefers_canonical_title_when_present(self):
        book = SimpleNamespace(title="1% Lifesteal Volume 4", canonical_title="1% Lifesteal Book 4")
        self.assertEqual(owned_title_for_identity(book), "1% Lifesteal Book 4")

    def test_blank_canonical_title_falls_back_to_title(self):
        book = SimpleNamespace(title="1% Lifesteal Volume 4", canonical_title="   ")
        self.assertEqual(owned_title_for_identity(book), "1% Lifesteal Volume 4")

    def test_title_never_read_from_canonical_title_side(self):
        # Confirms the coalesced value actually changes downstream identity
        # keys, not just that the function returns something plausible.
        raw = SimpleNamespace(title="1% Lifesteal Volume 4", canonical_title=None)
        resolved = SimpleNamespace(title="1% Lifesteal Volume 4", canonical_title="1% Lifesteal Book 4")
        self.assertNotEqual(
            _canonical_title_identity_key(owned_title_for_identity(raw)),
            _canonical_title_identity_key(owned_title_for_identity(resolved)),
        )


if __name__ == "__main__":
    unittest.main()
