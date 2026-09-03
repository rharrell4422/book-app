import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import crud
import schemas
from database import Base
from models import Book, Series


class SeriesCrudTest(unittest.TestCase):
    """Regression coverage for crud.create_series/update_series.

    SeriesBase (the request schema used for both create and update) includes
    derived, read-only fields -- read_count, unread_count, series_state --
    that only exist as @property on the Series model for API responses, not
    as real columns. Naively passing schema.model_dump() straight into the
    Series(**kwargs) constructor (or setattr-ing every dumped key) blows up
    with "property 'x' of 'Series' object has no setter" the moment those
    fields are present with any value, including the schema's own defaults.
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

    def test_create_series_with_full_schema_dump_does_not_crash(self):
        # This is exactly what the "/series/" POST endpoint passes through:
        # a full SeriesBase, including its None-valued derived fields.
        payload = schemas.SeriesBase(name="Koban", author="Stephen W Bennett", is_finished=True)
        series = crud.create_series(self.db, payload, profile_id="robbie")

        self.assertIsNotNone(series.id)
        self.assertEqual(series.name, "Koban")
        self.assertEqual(series.author, "Stephen W Bennett")
        self.assertTrue(series.is_finished)
        # Derived properties should still work normally afterwards.
        self.assertEqual(series.read_count, 0)
        self.assertEqual(series.unread_count, 0)

    def test_update_series_with_full_schema_dump_does_not_crash(self):
        series = Series(name="Placeholder", author="Someone", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)

        payload = schemas.SeriesBase(name="Renamed", author="Someone Else", is_finished=True)
        updated = crud.update_series(self.db, series.id, payload, profile_id="robbie")

        self.assertIsNotNone(updated)
        self.assertEqual(updated.name, "Renamed")
        self.assertEqual(updated.author, "Someone Else")
        self.assertTrue(updated.is_finished)

    def test_delete_series_cascade_does_not_touch_a_ghost_cross_profile_book(self):
        # CR-9 regression: the Series row lookup was profile-checked, but
        # the cascade delete of its Book rows filtered by series_id alone
        # -- a "ghost" Book row that somehow ended up pointed at this
        # series_id under a *different* profile_id would be silently
        # deleted as a side effect of deleting someone else's series.
        series = Series(name="Robbie's Series", author="Someone", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)

        owned_book = Book(
            title="Robbie's Book",
            author="Someone",
            series_id=series.id,
            profile_id="robbie",
            record_status="active",
        )
        ghost_book = Book(
            title="Ghost Book From Another Profile",
            author="Someone",
            series_id=series.id,
            profile_id="daughter",
            record_status="active",
        )
        self.db.add_all([owned_book, ghost_book])
        self.db.commit()
        self.db.refresh(ghost_book)

        result = crud.delete_series(self.db, series.id, profile_id="robbie")

        self.assertEqual(result["deleted_books"], 1)
        surviving_ghost = self.db.query(Book).filter(Book.id == ghost_book.id).first()
        self.assertIsNotNone(surviving_ghost)
        self.assertEqual(surviving_ghost.record_status, "active")


class JonathanHuntDuplicateSeriesIncidentTest(unittest.TestCase):
    """Jonathan Hunt Thriller Series incident (2026-09-02): three separate
    attempts to track the same series -- each typed slightly differently
    ("Jonathan Hunt Thriller Series" / "Jonathon Hunt Thriller", "Georgia
    Wagner" / "Georgia Wagner; Scott Cook;") -- each created a brand-new,
    permanently-0-book `Series` row instead of being recognized as the one
    already tracked, because `create_series` had no dedup check at all.
    End-to-end regression coverage for `_find_series_for_dedup`'s fix.

    Deliberately a fresh in-memory engine per test (setUp/tearDown), not a
    shared setUpClass one -- unlike SeriesCrudTest above, every test method
    here reuses the exact same "Jonathan Hunt Thriller Series" name/author
    on purpose (that's what's under test), so sharing one DB across methods
    would let an earlier test's row leak into a later one and silently
    change what "existing" the dedup lookup finds.
    """

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_typo_and_missing_suffix_reuses_existing_series_not_a_duplicate(self):
        first = crud.create_series(
            self.db, schemas.SeriesBase(name="Jonathan Hunt Thriller Series", author="Georgia Wagner"), profile_id="robbie"
        )

        second = crud.create_series(
            self.db,
            schemas.SeriesBase(name="Jonathan Hunt Thriller Series", author="Georgia Wagner; Scott Cook;"),
            profile_id="robbie",
        )
        third = crud.create_series(
            self.db, schemas.SeriesBase(name="Jonathon Hunt Thriller", author="Georgia Wagner; Scott Cook"), profile_id="robbie"
        )

        self.assertEqual(second.id, first.id)
        self.assertEqual(third.id, first.id)
        self.assertEqual(self.db.query(Series).filter(Series.profile_id == "robbie").count(), 1)

    def test_blank_author_on_existing_series_is_backfilled_not_left_blank(self):
        first = crud.create_series(self.db, schemas.SeriesBase(name="Jonathan Hunt Thriller Series", author="Georgia Wagner"), profile_id="robbie")
        self.assertEqual(first.author, "Georgia Wagner")

        # Simulate the empty-shell case: no author recorded at all yet.
        first.author = None
        self.db.commit()

        reused = crud.create_series(
            self.db, schemas.SeriesBase(name="Jonathan Hunt Thriller Series", author="Georgia Wagner; Scott Cook"), profile_id="robbie"
        )
        self.assertEqual(reused.id, first.id)
        self.assertEqual(reused.author, "Georgia Wagner; Scott Cook")

    def test_existing_nonblank_author_is_not_overwritten_by_a_differently_formatted_retry(self):
        first = crud.create_series(self.db, schemas.SeriesBase(name="Jonathan Hunt Thriller Series", author="Georgia Wagner"), profile_id="robbie")

        reused = crud.create_series(
            self.db, schemas.SeriesBase(name="Jonathan Hunt Thriller Series", author="Georgia Wagner; Scott Cook"), profile_id="robbie"
        )
        self.assertEqual(reused.id, first.id)
        self.assertEqual(reused.author, "Georgia Wagner")  # untouched, not clobbered

    def test_genuinely_different_series_is_not_merged(self):
        first = crud.create_series(self.db, schemas.SeriesBase(name="Jonathan Hunt Thriller Series", author="Georgia Wagner"), profile_id="robbie")
        other = crud.create_series(self.db, schemas.SeriesBase(name="Percy Jackson & The Olympians", author="Rick Riordan"), profile_id="robbie")

        self.assertNotEqual(other.id, first.id)
        self.assertEqual(self.db.query(Series).filter(Series.profile_id == "robbie").count(), 2)

    def test_same_name_and_author_under_a_different_profile_is_not_merged(self):
        first = crud.create_series(self.db, schemas.SeriesBase(name="Jonathan Hunt Thriller Series", author="Georgia Wagner"), profile_id="robbie")
        other_profile = crud.create_series(self.db, schemas.SeriesBase(name="Jonathan Hunt Thriller Series", author="Georgia Wagner"), profile_id="daughter")

        self.assertNotEqual(other_profile.id, first.id)


class GuidedDiscoverySeriesFieldsTest(unittest.TestCase):
    """Guided Discovery (locked 2026-09-03, iterations 1-5): create_series
    persists canonical_url/canonical_source/verified_volume_count on a
    genuinely new series, and backfills them (fill-only-if-empty, same
    convention as author -- see JonathanHuntDuplicateSeriesIncidentTest
    above) when a request matches an already-tracked series instead.
    """

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.db = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_new_series_persists_all_three_fields(self):
        series = crud.create_series(
            self.db,
            schemas.SeriesBase(
                name="Jonathan Hunt Thriller Series",
                author="Georgia Wagner",
                canonical_url="https://www.amazon.com/dp/B000TEST",
                canonical_source="KU",
                verified_volume_count=12,
            ),
            profile_id="robbie",
        )
        self.assertEqual(series.canonical_url, "https://www.amazon.com/dp/B000TEST")
        self.assertEqual(series.canonical_source, "KU")
        self.assertEqual(series.verified_volume_count, 12)

    def test_omitting_fields_leaves_them_null_existing_series_unaffected(self):
        # Every series created before Guided Discovery existed (or that
        # simply never filled these in) must behave identically.
        series = crud.create_series(
            self.db, schemas.SeriesBase(name="Some Other Series", author="Some Author"), profile_id="robbie"
        )
        self.assertIsNone(series.canonical_url)
        self.assertIsNone(series.canonical_source)
        self.assertIsNone(series.verified_volume_count)

    def test_matching_existing_series_backfills_blank_canonical_fields(self):
        first = crud.create_series(
            self.db, schemas.SeriesBase(name="Jonathan Hunt Thriller Series", author="Georgia Wagner"), profile_id="robbie"
        )
        self.assertIsNone(first.canonical_url)

        reused = crud.create_series(
            self.db,
            schemas.SeriesBase(
                name="Jonathan Hunt Thriller Series",
                author="Georgia Wagner",
                canonical_url="https://www.amazon.com/dp/B000TEST",
                canonical_source="KU",
                verified_volume_count=12,
            ),
            profile_id="robbie",
        )
        self.assertEqual(reused.id, first.id)
        self.assertEqual(reused.canonical_url, "https://www.amazon.com/dp/B000TEST")
        self.assertEqual(reused.canonical_source, "KU")
        self.assertEqual(reused.verified_volume_count, 12)

    def test_matching_existing_series_never_clobbers_already_set_canonical_fields(self):
        first = crud.create_series(
            self.db,
            schemas.SeriesBase(
                name="Jonathan Hunt Thriller Series",
                author="Georgia Wagner",
                canonical_url="https://www.amazon.com/dp/ORIGINAL",
                canonical_source="KU",
                verified_volume_count=12,
            ),
            profile_id="robbie",
        )

        reused = crud.create_series(
            self.db,
            schemas.SeriesBase(
                name="Jonathan Hunt Thriller Series",
                author="Georgia Wagner",
                canonical_url="https://www.amazon.com/dp/DIFFERENT",
                canonical_source="Goodreads",
                verified_volume_count=99,
            ),
            profile_id="robbie",
        )
        self.assertEqual(reused.id, first.id)
        self.assertEqual(reused.canonical_url, "https://www.amazon.com/dp/ORIGINAL")
        self.assertEqual(reused.canonical_source, "KU")
        self.assertEqual(reused.verified_volume_count, 12)


if __name__ == "__main__":
    unittest.main()
