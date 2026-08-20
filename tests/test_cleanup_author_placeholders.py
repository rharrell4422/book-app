"""Regression coverage for scripts/cleanup_author_placeholders.py -- the
one-time repair for series/books already poisoned with a placeholder author
(e.g. "Unknown author") before the write-time guard (see
test_author_placeholder_guard.py) existed.

Patches scripts.cleanup_author_placeholders.SessionLocal (and
scripts.backfill_series_author.SessionLocal, which the cleanup script calls
into for Phase 2) to point at a private in-memory database, the same
pattern test_importer_onboarding.py uses for functions that open their own
session rather than accepting an injected one.
"""

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import Book, Series
from scripts.cleanup_author_placeholders import cleanup_author_placeholders


def _new_in_memory_session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(autocommit=False, autoflush=False, bind=engine)


class CleanupAuthorPlaceholdersTest(unittest.TestCase):
    def setUp(self):
        self.engine, self.SessionLocal = _new_in_memory_session_factory()

    def tearDown(self):
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def _run_cleanup(self):
        with patch("scripts.cleanup_author_placeholders.SessionLocal", self.SessionLocal), patch(
            "scripts.backfill_series_author.SessionLocal", self.SessionLocal
        ):
            return cleanup_author_placeholders()

    def test_series_placeholder_is_cleared_and_backfilled_from_its_own_books(self):
        db = self.SessionLocal()
        series = Series(name="Poisoned Series", author="Unknown author", profile_id="robbie")
        db.add(series)
        db.commit()
        db.refresh(series)
        book = Book(title="Book One", author="Real Author", series_id=series.id, book_number=1, profile_id="robbie")
        db.add(book)
        db.commit()
        series_id = series.id
        db.close()

        result = self._run_cleanup()
        self.assertEqual(result["cleared_series_count"], 1)

        db = self.SessionLocal()
        refreshed = db.query(Series).filter(Series.id == series_id).first()
        self.assertEqual(refreshed.author, "Real Author")
        db.close()

    def test_series_placeholder_with_no_books_ends_up_null_not_reverted(self):
        db = self.SessionLocal()
        series = Series(name="Empty Poisoned Series", author="N/A", profile_id="robbie")
        db.add(series)
        db.commit()
        series_id = series.id
        db.close()

        self._run_cleanup()

        db = self.SessionLocal()
        refreshed = db.query(Series).filter(Series.id == series_id).first()
        self.assertIsNone(refreshed.author)
        db.close()

    def test_book_placeholder_author_is_cleared_to_empty_string(self):
        # Book.author is NOT NULL, so "cleared" means "" rather than NULL --
        # is_placeholder_author("") is False, so it can never re-poison a
        # series' backfilled author the way the placeholder value could.
        db = self.SessionLocal()
        book = Book(title="Standalone Book", author="Various", profile_id="robbie")
        db.add(book)
        db.commit()
        book_id = book.id
        db.close()

        result = self._run_cleanup()
        self.assertEqual(result["cleared_books_count"], 1)

        db = self.SessionLocal()
        refreshed = db.query(Book).filter(Book.id == book_id).first()
        self.assertEqual(refreshed.author, "")
        db.close()

    def test_idempotent_second_run_finds_nothing_left_to_clean(self):
        db = self.SessionLocal()
        series = Series(name="Poisoned Series", author="Unknown", profile_id="robbie")
        db.add(series)
        db.commit()
        db.close()

        first_result = self._run_cleanup()
        second_result = self._run_cleanup()

        self.assertEqual(first_result["cleared_series_count"], 1)
        self.assertEqual(second_result["cleared_series_count"], 0)
        self.assertEqual(second_result["cleared_books_count"], 0)

    def test_real_author_is_left_untouched(self):
        db = self.SessionLocal()
        series = Series(name="Healthy Series", author="Brandon Sanderson", profile_id="robbie")
        db.add(series)
        db.commit()
        db.close()

        result = self._run_cleanup()
        self.assertEqual(result["cleared_series_count"], 0)

        db = self.SessionLocal()
        refreshed = db.query(Series).filter(Series.name == "Healthy Series").first()
        self.assertEqual(refreshed.author, "Brandon Sanderson")
        db.close()


if __name__ == "__main__":
    unittest.main()
