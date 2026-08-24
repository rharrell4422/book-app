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


if __name__ == "__main__":
    unittest.main()
