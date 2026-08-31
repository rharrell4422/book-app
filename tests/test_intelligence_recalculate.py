"""TG-RT-5: characterization/regression coverage for
intelligence.recalculate_intelligence() -- frozen BEFORE Wave 3's RT-5
dedupes its triple Book-query redundancy (compute_series_intelligence_for_series
is called twice -- once directly, once again inside
recalculate_series_state_for_series -- plus a third, separate Book query in
recount_series_aggregates_for_series), so that fix can be verified as
behavior-preserving against this baseline.

Also freezes a real, currently-live quirk worth calling out explicitly
rather than silently locking in: recalculate_intelligence's return dict is
`{**intelligence, **aggregates}` -- both dicts have a `total_books` key, and
since `aggregates` (from recount_series_aggregates_for_series, which counts
ALL numbered active books including fractional ones, floored via `int()`)
is spread second, it always wins over `intelligence`'s own `total_books`
(from compute_series_intelligence_for_series, which only counts *integer*
book numbers plus any omnibus-range extraction). The two normally agree,
but a fractional companion book higher than the highest integer volume
makes them disagree -- see
test_fractional_companion_above_highest_integer_volume_uses_aggregates_total_books_not_intelligence_total_books.
"""
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import intelligence
from database import Base
from models import Book, Series


class RecalculateIntelligenceTest(unittest.TestCase):
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

    def _make_series(self, **overrides) -> Series:
        defaults = {"name": "Some Series", "author": "Some Author", "profile_id": "robbie"}
        defaults.update(overrides)
        series = Series(**defaults)
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        return series

    def _add_book(self, series: Series, number, **overrides) -> Book:
        # These fixtures predate the availability_status axis and still pass
        # legacy read_status kwargs -- compute_series_intelligence_for_series
        # now reads availability_status as its source of truth (see the
        # "Two-Axis Status Architecture" design chat's finalized Phase-3
        # decision), so backfill it here from the same legacy kwargs using
        # the same mapping the real migration uses (see
        # f1a2b3c4d5e6_add_availability_status_axis_to_books.py), unless a
        # test explicitly overrides it directly.
        if "availability_status" not in overrides:
            read_status = str(overrides.get("read_status") or "").strip().lower()
            if overrides.get("is_read") or read_status in ("read", "unread"):
                overrides["availability_status"] = "owned"
            elif read_status == "upcoming":
                overrides["availability_status"] = "upcoming"
            elif read_status == "available":
                overrides["availability_status"] = "available"

        defaults = {
            "title": f"Some Series Book {number}",
            "author": series.author,
            "series_id": series.id,
            "profile_id": series.profile_id,
            "series_order": number,
            "book_number": number,
            "record_status": "active",
            "is_read": False,
        }
        defaults.update(overrides)
        book = Book(**defaults)
        self.db.add(book)
        self.db.commit()
        self.db.refresh(book)
        return book

    def test_no_gaps_all_read_series(self):
        series = self._make_series()
        for number in (1, 2, 3):
            self._add_book(series, number, is_read=True)

        result = intelligence.recalculate_intelligence(self.db, series.id)

        self.assertEqual(result["series_id"], series.id)
        self.assertEqual(result["total_books"], 3)
        self.assertEqual(result["missing_orders"], [])
        self.assertIsNone(result["next_unread_book_number"])
        self.assertIsNone(result["next_upcoming_book_number"])
        self.assertFalse(result["is_series_finished"])
        self.assertEqual(result["read_count"], 3)
        self.assertEqual(result["unread_count"], 0)
        self.assertEqual(result["active_count"], 3)
        self.assertEqual(result["deleted_count"], 0)
        self.assertEqual(result["upcoming_count"], 0)

        self.db.refresh(series)
        self.assertEqual(series.total_books, 3)
        self.assertEqual(series.missing_books, [])
        self.assertFalse(series.has_unread_books)
        self.assertFalse(series.has_upcoming_books)
        self.assertTrue(series.is_caught_up)
        self.assertEqual(series.series_status, "ongoing")

    def test_a_numbering_gap_is_reported_and_written(self):
        series = self._make_series()
        self._add_book(series, 1, is_read=True)
        self._add_book(series, 3, is_read=False)

        result = intelligence.recalculate_intelligence(self.db, series.id)

        self.assertEqual(result["total_books"], 3)
        self.assertEqual(result["missing_orders"], [2])
        self.assertEqual(result["next_unread_book_number"], 3)

        self.db.refresh(series)
        self.assertEqual(series.missing_books, [2])
        self.assertTrue(series.has_unread_books)
        self.assertFalse(series.is_caught_up)

    def test_soft_deleted_book_is_excluded_from_active_counts_but_counted_as_deleted(self):
        series = self._make_series()
        self._add_book(series, 1, is_read=True)
        self._add_book(series, 2, is_read=True)
        self._add_book(series, 3, record_status="deleted")

        result = intelligence.recalculate_intelligence(self.db, series.id)

        self.assertEqual(result["total_books"], 2)
        self.assertEqual(result["missing_orders"], [])
        self.assertEqual(result["active_count"], 2)
        self.assertEqual(result["deleted_count"], 1)

    def test_upcoming_book_populates_next_upcoming_and_upcoming_count(self):
        series = self._make_series()
        self._add_book(series, 1, is_read=True)
        self._add_book(series, 2, read_status="upcoming")

        result = intelligence.recalculate_intelligence(self.db, series.id)

        self.assertEqual(result["next_upcoming_book_number"], 2)
        self.assertEqual(result["upcoming_count"], 1)

        self.db.refresh(series)
        self.assertTrue(series.has_upcoming_books)
        self.assertEqual(series.next_upcoming_book_number, 2)

    def test_available_not_yet_owned_book_counts_as_unread_option_a_semantics(self):
        # Phase-3 "Two-Axis Status Architecture" decision, option A: switching
        # the source field from legacy read_status to availability_status
        # must not redefine what counts as "unread" -- a discovered-but-not-
        # owned "available" book still counts toward next_unread_book_number/
        # unread_count, exactly as it did when this was driven by
        # read_status == "available".
        series = self._make_series()
        self._add_book(series, 1, is_read=True)
        self._add_book(series, 2, availability_status="available")

        result = intelligence.recalculate_intelligence(self.db, series.id)

        self.assertEqual(result["next_unread_book_number"], 2)
        self.assertIsNone(result["next_upcoming_book_number"])
        self.assertEqual(result["upcoming_count"], 0)

    def test_upcoming_classification_uses_availability_status_not_legacy_read_status(self):
        # A book with a blank/stale read_status (the exact shape of the
        # contaminated-row bug this phase's migration backfills) must still
        # be classified correctly, because compute_series_intelligence_for_
        # series now reads availability_status directly rather than
        # depending on read_status having been derived.
        series = self._make_series()
        self._add_book(series, 1, is_read=True)
        book = self._add_book(series, 2, availability_status="upcoming")
        book.read_status = ""
        self.db.commit()

        result = intelligence.recalculate_intelligence(self.db, series.id)

        self.assertEqual(result["next_upcoming_book_number"], 2)
        self.assertEqual(result["upcoming_count"], 1)
        self.assertIsNone(result["next_unread_book_number"])

    def test_is_finished_flows_through_to_series_status(self):
        series = self._make_series(is_finished=True)
        self._add_book(series, 1, is_read=True)

        result = intelligence.recalculate_intelligence(self.db, series.id)

        self.assertTrue(result["is_series_finished"])
        self.db.refresh(series)
        self.assertEqual(series.series_status, "finished")

    def test_scan_result_added_books_sets_has_new_books(self):
        series = self._make_series()
        self._add_book(series, 1, is_read=True)

        intelligence.recalculate_intelligence(
            self.db, series.id, scan_result={"added_books": [{"title": "New Book"}]}
        )

        self.db.refresh(series)
        self.assertTrue(series.has_new_books)

    def test_has_new_books_is_sticky_once_true_even_without_a_scan_result(self):
        series = self._make_series(has_new_books=True)
        self._add_book(series, 1, is_read=True)

        intelligence.recalculate_intelligence(self.db, series.id)

        self.db.refresh(series)
        self.assertTrue(series.has_new_books)

    def test_cross_profile_ghost_book_is_excluded_from_every_count(self):
        # Defense-in-depth check: both compute_series_intelligence_for_series
        # and recount_series_aggregates_for_series filter by the series' own
        # profile_id, not just series_id -- a "ghost" book sharing this
        # series_id under a different profile_id (the failure mode CR-10's
        # removed default used to cause) must not inflate any count.
        series = self._make_series()
        self._add_book(series, 1, is_read=True)
        self._add_book(series, 2, profile_id="some_other_profile")

        result = intelligence.recalculate_intelligence(self.db, series.id)

        self.assertEqual(result["total_books"], 1)
        self.assertEqual(result["active_count"], 1)
        self.assertEqual(result["missing_orders"], [])

    def test_fractional_companion_above_highest_integer_volume_uses_aggregates_total_books_not_intelligence_total_books(
        self,
    ):
        # Characterizes a real, currently-live quirk (see module docstring):
        # compute_series_intelligence_for_series only counts *integer* book
        # numbers toward total_books (a 3.5-style companion doesn't count as
        # a "volume"), but recount_series_aggregates_for_series counts ANY
        # numbered active book (floored via int()) -- and since
        # recalculate_intelligence's return dict is `{**intelligence,
        # **aggregates}`, aggregates' total_books (3, from int(3.5)) wins
        # over intelligence's own total_books (2, integer-only) in the
        # final merged result.
        series = self._make_series()
        self._add_book(series, 1, is_read=True)
        self._add_book(series, 2, is_read=True)
        self._add_book(series, 3.5, is_read=False)

        intelligence_only = intelligence.compute_series_intelligence_for_series(self.db, series.id)
        aggregates_only = intelligence.recount_series_aggregates_for_series(self.db, series.id)
        self.assertEqual(intelligence_only["total_books"], 2)
        self.assertEqual(aggregates_only["total_books"], 3)

        result = intelligence.recalculate_intelligence(self.db, series.id)
        self.assertEqual(result["total_books"], 3)
        # missing_orders, however, still comes from the (unmerged-over)
        # intelligence dict -- it stays [] here since covered_numbers is
        # {1, 2} and intelligence's own total_books cap is 2, not 3.
        self.assertEqual(result["missing_orders"], [])

    def test_nonexistent_series_returns_the_documented_empty_defaults(self):
        result = intelligence.recalculate_intelligence(self.db, series_id=999999)
        self.assertEqual(result["series_id"], 999999)
        self.assertEqual(result["total_books"], 0)
        self.assertEqual(result["missing_orders"], [])
        self.assertIsNone(result["next_unread_book_number"])
        self.assertFalse(result["is_series_finished"])
        # aggregates still runs its own (profile-unscoped-when-series-missing)
        # query and merges in -- series_id-keyed, zeroed out since no Book
        # rows reference a nonexistent series_id.
        self.assertEqual(result["active_count"], 0)
        self.assertEqual(result["deleted_count"], 0)


if __name__ == "__main__":
    unittest.main()
