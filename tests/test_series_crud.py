import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import crud
import schemas
from database import Base
from models import Series


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
        series = crud.create_series(self.db, payload)

        self.assertIsNotNone(series.id)
        self.assertEqual(series.name, "Koban")
        self.assertEqual(series.author, "Stephen W Bennett")
        self.assertTrue(series.is_finished)
        # Derived properties should still work normally afterwards.
        self.assertEqual(series.read_count, 0)
        self.assertEqual(series.unread_count, 0)

    def test_update_series_with_full_schema_dump_does_not_crash(self):
        series = Series(name="Placeholder", author="Someone")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)

        payload = schemas.SeriesBase(name="Renamed", author="Someone Else", is_finished=True)
        updated = crud.update_series(self.db, series.id, payload)

        self.assertIsNotNone(updated)
        self.assertEqual(updated.name, "Renamed")
        self.assertEqual(updated.author, "Someone Else")
        self.assertTrue(updated.is_finished)


if __name__ == "__main__":
    unittest.main()
