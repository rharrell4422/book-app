import unittest
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch

import library_sync
from database import Base
from models import Book, Series


class LibrarySyncUpcomingHealingTest(unittest.TestCase):
    """Covers a real bug report: a book imported from a spreadsheet (or an old
    discovery run) as read_status="upcoming" with a release date that has
    since passed used to stay "upcoming" forever, because the old logic let
    the stale `read_status == "upcoming"` flag itself justify staying
    upcoming -- a self-fulfilling condition that no date could ever override.
    update_from_series() is called after every Check Now run
    (services/series_check_engine.py), so this is also what "self-heals"
    these books the next time a series is checked.
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
        series = Series(name="Axel Blaze", author="Some Author")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

    def tearDown(self):
        self.db.close()

    def _add_book(self, **kwargs) -> Book:
        defaults = dict(
            title="Some Book",
            author="Some Author",
            series_id=self.series.id,
            record_status="active",
            is_read=False,
        )
        defaults.update(kwargs)
        book = Book(**defaults)
        self.db.add(book)
        self.db.commit()
        self.db.refresh(book)
        return book

    def _sync(self) -> dict:
        with patch.object(library_sync, "SessionLocal", self.SessionLocal):
            return library_sync.update_from_series(self.series.id)

    def test_stale_upcoming_book_with_past_release_date_flips_to_available(self):
        past_date = date.today() - timedelta(days=30)
        book = self._add_book(
            title="Axel Blaze 13",
            book_number=13.0,
            read_status="upcoming",
            is_upcoming_auto=True,
            release_date=past_date,
        )

        self._sync()
        self.db.refresh(book)

        self.assertEqual(book.read_status, "available")
        self.assertFalse(book.is_upcoming_auto)
        self.assertFalse(book.is_upcoming_final)

    def test_stale_upcoming_book_with_past_publication_date_flips_to_available(self):
        past_date = date.today() - timedelta(days=90)
        book = self._add_book(
            title="Axel Blaze 12",
            book_number=12.0,
            read_status="upcoming",
            is_upcoming_final=True,
            publication_date=past_date,
        )

        self._sync()
        self.db.refresh(book)

        self.assertEqual(book.read_status, "available")
        self.assertFalse(book.is_upcoming_auto)
        self.assertFalse(book.is_upcoming_final)

    def test_upcoming_book_with_future_release_date_stays_upcoming(self):
        future_date = date.today() + timedelta(days=30)
        book = self._add_book(
            title="Axel Blaze 14",
            book_number=14.0,
            read_status="upcoming",
            is_upcoming_auto=True,
            release_date=future_date,
        )

        self._sync()
        self.db.refresh(book)

        self.assertEqual(book.read_status, "upcoming")
        self.assertTrue(book.is_upcoming_auto)

    def test_upcoming_book_with_no_date_at_all_stays_upcoming(self):
        # Mirrors an announced-but-undated preorder (e.g. Peacemaker 9 before
        # its release date was known) -- with no date to check against, the
        # stale-date healing logic must not kick in and demote it.
        book = self._add_book(
            title="Embers of the Ancients",
            book_number=9.0,
            read_status="upcoming",
            is_upcoming_auto=True,
            release_date=None,
            publication_date=None,
        )

        self._sync()
        self.db.refresh(book)

        self.assertEqual(book.read_status, "upcoming")
        self.assertTrue(book.is_upcoming_auto)

    def test_read_book_is_untouched_regardless_of_dates(self):
        past_date = date.today() - timedelta(days=400)
        book = self._add_book(
            title="Axel Blaze 10",
            book_number=10.0,
            is_read=True,
            read_status="read",
            release_date=past_date,
        )

        self._sync()
        self.db.refresh(book)

        self.assertEqual(book.read_status, "read")

    def test_already_available_book_is_left_unchanged(self):
        past_date = date.today() - timedelta(days=10)
        book = self._add_book(
            title="Axel Blaze 11",
            book_number=11.0,
            read_status="available",
            release_date=past_date,
        )

        result = self._sync()
        self.db.refresh(book)

        self.assertEqual(book.read_status, "available")
        self.assertEqual(result["updated_rows"], 0)

    def test_profile_id_scoping_excludes_a_ghost_cross_profile_book(self):
        # CR-9 regression: series_id alone doesn't guarantee a Book row
        # belongs to the series' own profile -- a "ghost" cross-profile row
        # sharing this series_id used to get silently mutated by whichever
        # profile's Check Now happened to trigger this sync.
        past_date = date.today() - timedelta(days=30)
        ghost_book = self._add_book(
            title="Ghost Book From Another Profile",
            book_number=13.0,
            read_status="upcoming",
            is_upcoming_auto=True,
            release_date=past_date,
            profile_id="daughter",
        )

        with patch.object(library_sync, "SessionLocal", self.SessionLocal):
            result = library_sync.update_from_series(self.series.id, profile_id="robbie")
        self.db.refresh(ghost_book)

        self.assertEqual(result["mirrored_rows"], 0)
        self.assertEqual(ghost_book.read_status, "upcoming")
        self.assertTrue(ghost_book.is_upcoming_auto)


if __name__ == "__main__":
    unittest.main()
