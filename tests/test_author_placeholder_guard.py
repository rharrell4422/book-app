"""Regression coverage for the "Unknown author" placeholder bug (Phase 1 of
the Add Book metadata intake redesign -- see project design chat).

Background: the Add Book save-time fallback and two prefilled-form defaults
used to write the literal string "Unknown author" whenever a locked series
had no author on file yet. That value passes every write path's plain
non-empty check, and normalization makes it *more* dangerous than an empty
value: services/identity.py's _normalize_author_for_identity strips the
literal word "author" as a role descriptor, so "Unknown author" and
"Unknown" collapse to the same non-empty token "unknown" -- which then
compares equal to any *other* placeholder-tainted row, silently fusing
otherwise-unrelated series into the same author identity.
"""

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import crud
import schemas
from database import Base
from models import Book, Series
from services.identity import _authors_match_exact, is_placeholder_author


class IsPlaceholderAuthorTest(unittest.TestCase):
    def test_rejects_every_denylist_value(self):
        for value in ["Unknown", "Unknown author", "UNKNOWN AUTHOR", "N/A", "n/a", "None", "Various", "various"]:
            self.assertTrue(is_placeholder_author(value), f"expected {value!r} to be flagged as a placeholder")

    def test_accepts_real_author_names(self):
        for value in ["Brandon Sanderson", "Rebecca Yarros", "N. K. Jemisin"]:
            self.assertFalse(is_placeholder_author(value))

    def test_empty_value_is_not_a_placeholder(self):
        # Empty is handled separately (as "no signal at all") by every
        # caller -- is_placeholder_author only answers "is this non-empty
        # value actually a disguised placeholder", not "is this blank".
        self.assertFalse(is_placeholder_author(""))
        self.assertFalse(is_placeholder_author(None))

    def test_normalization_laundering_is_exactly_what_gets_caught(self):
        # This is the mechanism that made the bug dangerous: "Unknown
        # author" normalizes to the same token as "Unknown", and two
        # placeholder-tainted authors would otherwise compare as an exact
        # match via _authors_match_exact -- fusing two unrelated series'
        # identities. Confirms both halves of the bug: the false match
        # exists at the identity layer, and is_placeholder_author is what
        # must stop the value from ever being written in the first place.
        self.assertTrue(_authors_match_exact("Unknown author", "Unknown"))
        self.assertTrue(is_placeholder_author("Unknown author"))
        self.assertTrue(is_placeholder_author("Unknown"))


class BackfillSeriesAuthorGuardTest(unittest.TestCase):
    """crud.books._backfill_series_author_if_missing, exercised via
    crud.create_book exactly as the Add Book endpoint calls it."""

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=cls.engine)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setUp(self):
        self.db = self.SessionLocal()

    def tearDown(self):
        self.db.close()

    def _make_authorless_series(self, name: str) -> Series:
        series = Series(name=name, author=None, profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        return series

    def test_placeholder_author_is_not_adopted_into_series(self):
        series = self._make_authorless_series("Placeholder Test Series")
        payload = schemas.BookBase(
            title="Book One",
            author="Unknown author",
            series_id=series.id,
            book_number=1,
        )
        crud.create_book(self.db, payload, profile_id="robbie")

        self.db.refresh(series)
        self.assertIsNone(series.author)

    def test_real_author_is_still_adopted_into_series(self):
        series = self._make_authorless_series("Real Author Test Series")
        payload = schemas.BookBase(
            title="Book One",
            author="Brandon Sanderson",
            series_id=series.id,
            book_number=1,
        )
        crud.create_book(self.db, payload, profile_id="robbie")

        self.db.refresh(series)
        self.assertEqual(series.author, "Brandon Sanderson")

    def test_two_authorless_series_do_not_match_each_other(self):
        # Before the guard, both series could have ended up with
        # Series.author="Unknown author" (or "Unknown") -- an exact match
        # under _authors_match_exact, fusing their discovery identities.
        # With the guard in place, neither series' author is ever set from
        # the placeholder, so the comparison correctly falls back to the
        # "either side empty" case and returns False.
        series_a = self._make_authorless_series("Series A")
        series_b = self._make_authorless_series("Series B")

        crud.create_book(
            self.db,
            schemas.BookBase(title="A1", author="Unknown author", series_id=series_a.id, book_number=1),
            profile_id="robbie",
        )
        crud.create_book(
            self.db,
            schemas.BookBase(title="B1", author="Unknown author", series_id=series_b.id, book_number=1),
            profile_id="robbie",
        )

        self.db.refresh(series_a)
        self.db.refresh(series_b)
        self.assertFalse(_authors_match_exact(series_a.author, series_b.author))


class ImporterBackfillGuardTest(unittest.TestCase):
    """importer/importer.py's near-duplicate backfill in create_or_update_book
    must reject the same denylist as the CRUD path -- it's a separate code
    path, not a shared helper call, so it needs its own coverage."""

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=cls.engine)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setUp(self):
        self.db = self.SessionLocal()

    def tearDown(self):
        self.db.close()

    def test_placeholder_author_is_not_backfilled_via_importer(self):
        from importer.importer import create_or_update_book

        book, _decision = create_or_update_book(
            self.db,
            {
                "title": "Imported Book",
                "author": "N/A",
                "series_name": "Imported Series",
                "book_number": 1,
            },
            profile_id="robbie",
        )

        series = self.db.query(Series).filter(Series.id == book.series_id).first()
        self.assertIsNotNone(series)
        self.assertFalse(str(series.author or "").strip())


class NumberInferenceProvenanceTest(unittest.TestCase):
    """Regression coverage for the Phase 4 number-inference unification's
    provenance wiring: crud.create_book now delegates to
    discovery_engine.infer_number_from_title (the same extractor Check Now
    uses) instead of its own narrower pattern set, and stamps
    book_number_source so a title-inferred number can be told apart from
    one the user actually typed.
    """

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=cls.engine)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setUp(self):
        self.db = self.SessionLocal()
        series = Series(name="Cherry Blossom Girls", author="Some Author", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

    def tearDown(self):
        self.db.close()

    def test_number_inferred_from_title_is_stamped_title_inferred(self):
        book = crud.create_book(
            self.db,
            schemas.BookBase(title="Cherry Blossom Girls Book 7", author="Some Author", series_id=self.series.id),
            profile_id="robbie",
        )
        self.assertEqual(book.book_number, 7.0)
        self.assertEqual(book.book_number_source, "title_inferred")

    def test_explicit_user_supplied_number_is_stamped_user(self):
        book = crud.create_book(
            self.db,
            schemas.BookBase(
                title="Some Standalone Title", author="Some Author", series_id=self.series.id, book_number=3
            ),
            profile_id="robbie",
        )
        self.assertEqual(book.book_number, 3.0)
        self.assertEqual(book.book_number_source, "user")

    def test_volume_and_hash_forms_are_now_recognized_via_the_unified_extractor(self):
        # crud's old narrower pattern (bare "book N" only) would have missed
        # both of these -- confirms the delegation to
        # discovery_engine.infer_number_from_title actually widened
        # recognition rather than just changing call sites.
        volume_book = crud.create_book(
            self.db,
            schemas.BookBase(title="Cherry Blossom Girls Volume 7", author="Some Author", series_id=self.series.id),
            profile_id="robbie",
        )
        self.assertEqual(volume_book.book_number, 7.0)

        hash_book = crud.create_book(
            self.db,
            schemas.BookBase(title="Cherry Blossom Girls #8", author="Some Author", series_id=self.series.id),
            profile_id="robbie",
        )
        self.assertEqual(hash_book.book_number, 8.0)

    def test_fractional_title_inferred_number_is_preserved(self):
        book = crud.create_book(
            self.db,
            schemas.BookBase(
                title="Threshing Day (Empyrean Book 3.5)", author="Some Author", series_id=self.series.id
            ),
            profile_id="robbie",
        )
        self.assertEqual(book.book_number, 3.5)
        self.assertIsNone(book.series_order)
        self.assertEqual(book.book_number_source, "title_inferred")


class CreateBookProvenanceDerivationTest(unittest.TestCase):
    """Phase 6: crud.create_book derives metadata_source/needs_reresolution
    itself from the transient find_confidence signal (see schemas.BookBase's
    docstring and services/metadata_provenance.py) -- a client-supplied
    metadata_source is never trusted directly on the create path, even
    though the schema also exposes it as a raw settable field for other,
    non-API write paths.
    """

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=cls.engine)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setUp(self):
        self.db = self.SessionLocal()

    def tearDown(self):
        self.db.close()

    def test_no_find_confidence_is_stamped_user(self):
        book = crud.create_book(self.db, schemas.BookBase(title="Manual Entry", author="A"), profile_id="robbie")
        self.assertEqual(book.metadata_source, "user")
        self.assertIsNone(book.needs_reresolution)

    def test_high_confidence_find_bind_is_stamped_provider_verified(self):
        book = crud.create_book(
            self.db,
            schemas.BookBase(title="Bound High", author="A", find_confidence="high"),
            profile_id="robbie",
        )
        self.assertEqual(book.metadata_source, "provider")
        self.assertFalse(book.needs_reresolution)

    def test_low_confidence_find_bind_is_provider_but_flagged_for_reresolution(self):
        book = crud.create_book(
            self.db,
            schemas.BookBase(title="Bound Low", author="A", find_confidence="low"),
            profile_id="robbie",
        )
        self.assertEqual(book.metadata_source, "provider")
        self.assertTrue(book.needs_reresolution)

    def test_client_supplied_metadata_source_is_ignored_on_create(self):
        # A client trying to claim metadata_source="provider" directly
        # (without an actual find_confidence signal) must not succeed --
        # this is exactly the trust boundary this derivation exists for.
        book = crud.create_book(
            self.db,
            schemas.BookBase(title="Spoofed", author="A", metadata_source="provider", needs_reresolution=False),
            profile_id="robbie",
        )
        self.assertEqual(book.metadata_source, "user")
        self.assertIsNone(book.needs_reresolution)

    def test_invalid_find_confidence_raises(self):
        with self.assertRaises(ValueError):
            crud.create_book(
                self.db,
                schemas.BookBase(title="Bad Signal", author="A", find_confidence="not-a-tier"),
                profile_id="robbie",
            )


if __name__ == "__main__":
    unittest.main()
