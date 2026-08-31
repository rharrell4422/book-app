"""Regression coverage for the "Unread Fix" (Two-Axis Status Architecture
design chat): reading status (is_read/read_date) and availability status
(availability_status/availability_locked -- see models.Book's docstring)
are independent axes, and once availability_status is set explicitly it
must stick until the user changes it again -- discovery/Check Now/
library_sync may only manage it while unlocked.

The bug report this whole feature exists to fix: toggling a book to
"unread" via Edit Book silently put it back to "available" because a
single overloaded read_status field let date-based inference override an
explicit user choice. TouchedAvailabilityLockingTest below is the direct
regression test for that report.
"""

import unittest
from datetime import date, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import crud
import schemas
from database import Base
from importer.pipeline import import_row
from models import Book, Series
from services.availability_bridge import (
    derive_legacy_fields,
    normalize_availability_status,
    should_self_heal_stale_upcoming,
)


class AvailabilityBridgeHelpersTest(unittest.TestCase):
    def test_normalize_defaults_unrecognized_values_to_available(self):
        for value in [None, "", "reading", "abandoned", "bogus"]:
            self.assertEqual(normalize_availability_status(value), "available")

    def test_normalize_passes_through_recognized_values_case_insensitively(self):
        self.assertEqual(normalize_availability_status("Upcoming"), "upcoming")
        self.assertEqual(normalize_availability_status("OWNED"), "owned")
        self.assertEqual(normalize_availability_status(" available "), "available")

    def test_derive_legacy_fields_read_status_read_always_wins_over_availability(self):
        # read_status specifically is_read-driven regardless of
        # availability_status -- is_upcoming_auto/final are derived purely
        # from the availability axis itself (see the derivation table in
        # services/availability_bridge.py's own docstring), independent of
        # is_read.
        legacy = derive_legacy_fields(is_read=True, availability_status="owned", availability_locked=True)
        self.assertEqual(legacy["read_status"], "read")
        self.assertFalse(legacy["is_upcoming_auto"])
        self.assertFalse(legacy["is_upcoming_final"])

    def test_derive_legacy_fields_unread_owned_maps_to_unread(self):
        legacy = derive_legacy_fields(is_read=False, availability_status="owned", availability_locked=True)
        self.assertEqual(legacy["read_status"], "unread")

    def test_derive_legacy_fields_upcoming_auto_vs_final_tracks_lock(self):
        unlocked = derive_legacy_fields(is_read=False, availability_status="upcoming", availability_locked=False)
        self.assertTrue(unlocked["is_upcoming_auto"])
        self.assertFalse(unlocked["is_upcoming_final"])

        locked = derive_legacy_fields(is_read=False, availability_status="upcoming", availability_locked=True)
        self.assertFalse(locked["is_upcoming_auto"])
        self.assertTrue(locked["is_upcoming_final"])

    def test_self_heal_only_fires_for_upcoming_with_a_passed_date(self):
        today = date(2026, 1, 1)
        self.assertTrue(should_self_heal_stale_upcoming("upcoming", date(2025, 12, 1), today))
        self.assertFalse(should_self_heal_stale_upcoming("upcoming", date(2026, 6, 1), today))
        self.assertFalse(should_self_heal_stale_upcoming("upcoming", None, today))
        self.assertFalse(should_self_heal_stale_upcoming("owned", date(2025, 12, 1), today))
        self.assertFalse(should_self_heal_stale_upcoming("available", date(2025, 12, 1), today))


class TouchedAvailabilityLockingTest(unittest.TestCase):
    """crud.create_book / crud.update_book's "touched key" locking rule --
    availability_locked only ever flips because a request explicitly
    included availability_status, never as a side effect of any other
    field changing."""

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

    def test_create_without_availability_status_defaults_unlocked_available(self):
        book = crud.create_book(self.db, schemas.BookBase(title="Fresh Add", author="A"), profile_id="robbie")
        self.assertEqual(book.availability_status, "available")
        self.assertFalse(book.availability_locked)
        self.assertEqual(book.read_status, "available")

    def test_create_with_explicit_availability_status_locks_it(self):
        book = crud.create_book(
            self.db,
            schemas.BookBase(title="Explicit Add", author="A", availability_status="owned", is_read=False),
            profile_id="robbie",
        )
        self.assertEqual(book.availability_status, "owned")
        self.assertTrue(book.availability_locked)
        self.assertEqual(book.read_status, "unread")

    def test_regression_toggling_to_unread_sticks_even_with_a_past_release_date(self):
        """The exact bug report: "I just toggled this book...to 'unread'
        with the edit book card and it put into Library (and series view)
        as Available." A book with a release_date in the past used to get
        silently re-classified "available" by date-based inference the
        moment it was saved as "unread" -- this asserts that no longer
        happens even one layer below the UI (the API/crud layer itself).
        """
        book = crud.create_book(
            self.db,
            schemas.BookBase(
                title="Here We Go Again",
                author="A",
                availability_status="available",
                release_date=date.today() - timedelta(days=400),
            ),
            profile_id="robbie",
        )
        self.assertEqual(book.availability_status, "available")

        updated = crud.update_book(
            self.db,
            book.id,
            schemas.BookUpdate(availability_status="owned", is_read=False),
            profile_id="robbie",
        )
        self.assertEqual(updated.availability_status, "owned")
        self.assertTrue(updated.availability_locked)
        self.assertEqual(updated.read_status, "unread")

    def test_untouched_update_never_flips_the_lock_or_the_value(self):
        book = crud.create_book(
            self.db,
            schemas.BookBase(title="Locked Book", author="A", availability_status="owned"),
            profile_id="robbie",
        )
        self.assertTrue(book.availability_locked)

        # A title-only edit must never re-derive/relock availability_status
        # from anything else -- it should be a complete no-op on this axis.
        updated = crud.update_book(self.db, book.id, schemas.BookUpdate(title="Locked Book (Renamed)"), profile_id="robbie")
        self.assertEqual(updated.availability_status, "owned")
        self.assertTrue(updated.availability_locked)
        self.assertEqual(updated.read_status, "unread")

    def test_untouched_update_on_unlocked_book_leaves_it_unlocked(self):
        book = crud.create_book(self.db, schemas.BookBase(title="Unlocked Book", author="A"), profile_id="robbie")
        self.assertFalse(book.availability_locked)

        updated = crud.update_book(self.db, book.id, schemas.BookUpdate(title="Unlocked Book (Renamed)"), profile_id="robbie")
        self.assertFalse(updated.availability_locked)
        self.assertEqual(updated.availability_status, "available")

    def test_marking_read_forces_read_status_regardless_of_availability_status(self):
        book = crud.create_book(
            self.db,
            schemas.BookBase(title="To Be Read", author="A", availability_status="upcoming"),
            profile_id="robbie",
        )
        updated = crud.update_book(self.db, book.id, schemas.BookUpdate(is_read=True), profile_id="robbie")
        self.assertEqual(updated.read_status, "read")
        self.assertTrue(updated.is_read)


class ImporterTokenNormalizationTest(unittest.TestCase):
    """importer/pipeline.py's explicit-token normalization -- recognized
    tokens lock the availability axis (as authoritative as a manual edit);
    everything else stays unlocked, matching a freshly-added book."""

    def _row(self, read_status: str):
        book_data, _unknown = import_row(["Title", "Author", "Read Status"], ["Some Book", "Some Author", read_status])
        return book_data

    def test_explicit_read_token_locks_owned(self):
        row = self._row("Read")
        self.assertTrue(row["is_read"])
        self.assertEqual(row["availability_status"], "owned")
        self.assertTrue(row["availability_locked"])
        self.assertEqual(row["read_status"], "read")
        self.assertFalse(row["is_missing"])

    def test_explicit_unread_token_locks_owned_but_not_read(self):
        row = self._row("Unread")
        self.assertFalse(row["is_read"])
        self.assertEqual(row["availability_status"], "owned")
        self.assertTrue(row["availability_locked"])
        self.assertEqual(row["read_status"], "unread")

    def test_explicit_available_token_locks_available(self):
        row = self._row("Available")
        self.assertEqual(row["availability_status"], "available")
        self.assertTrue(row["availability_locked"])
        self.assertEqual(row["read_status"], "available")

    def test_explicit_upcoming_token_locks_upcoming(self):
        row = self._row("TBR")
        self.assertEqual(row["availability_status"], "upcoming")
        self.assertTrue(row["availability_locked"])
        self.assertEqual(row["read_status"], "upcoming")
        self.assertTrue(row["is_upcoming_final"])
        self.assertFalse(row["is_upcoming_auto"])

    def test_ambiguous_token_does_not_lock(self):
        row = self._row("Currently Reading")
        self.assertFalse(row["is_read"])
        self.assertEqual(row["availability_status"], "available")
        self.assertFalse(row["availability_locked"])

    def test_blank_token_does_not_lock(self):
        row = self._row("")
        self.assertEqual(row["availability_status"], "available")
        self.assertFalse(row["availability_locked"])
        self.assertFalse(row["is_missing"])


class DiscoveryHonorsAvailabilityLockTest(unittest.TestCase):
    """library_sync.update_from_series must never overwrite a locked
    availability_status, except the one narrow stale-upcoming self-heal."""

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
        series = Series(name="Locked Axis Series", author="Some Author", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

    def tearDown(self):
        self.db.close()

    def _sync(self):
        import library_sync
        from unittest.mock import patch

        with patch.object(library_sync, "SessionLocal", self.SessionLocal):
            return library_sync.update_from_series(self.series.id)

    def test_locked_unread_owned_book_with_past_release_date_is_left_alone(self):
        # This is the library_sync-layer half of the "Unread Fix" regression:
        # a locked "owned" book with a long-past release_date must not be
        # pulled back to "available" by the exact date-based logic that
        # self-heals a genuinely stale *upcoming* book.
        book = Book(
            title="Here We Go Again",
            author="Some Author",
            series_id=self.series.id,
            profile_id=self.series.profile_id,
            record_status="active",
            is_read=False,
            availability_status="owned",
            availability_locked=True,
            release_date=date.today() - timedelta(days=400),
        )
        self.db.add(book)
        self.db.commit()

        self._sync()
        self.db.refresh(book)

        self.assertEqual(book.availability_status, "owned")
        self.assertTrue(book.availability_locked)
        self.assertEqual(book.read_status, "unread")

    def test_locked_upcoming_with_passed_date_self_heals_to_available_and_unlocks(self):
        book = Book(
            title="Stale Preorder",
            author="Some Author",
            series_id=self.series.id,
            profile_id=self.series.profile_id,
            record_status="active",
            is_read=False,
            availability_status="upcoming",
            availability_locked=True,
            release_date=date.today() - timedelta(days=10),
        )
        self.db.add(book)
        self.db.commit()

        self._sync()
        self.db.refresh(book)

        self.assertEqual(book.availability_status, "available")
        self.assertFalse(book.availability_locked)
        self.assertEqual(book.read_status, "available")

    def test_locked_upcoming_with_future_date_stays_locked(self):
        book = Book(
            title="Real Preorder",
            author="Some Author",
            series_id=self.series.id,
            profile_id=self.series.profile_id,
            record_status="active",
            is_read=False,
            availability_status="upcoming",
            availability_locked=True,
            release_date=date.today() + timedelta(days=30),
        )
        self.db.add(book)
        self.db.commit()

        self._sync()
        self.db.refresh(book)

        self.assertEqual(book.availability_status, "upcoming")
        self.assertTrue(book.availability_locked)


if __name__ == "__main__":
    unittest.main()
