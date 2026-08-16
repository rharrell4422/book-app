import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import library_sync
import services.series_check_engine as series_check_engine
from database import Base
from models import Book, Series


class SeriesCheckPersistenceTest(unittest.TestCase):
    """Tests the "Check Now" job's persistence layer (run_series_check_job_full)
    against an in-memory database, with the discovery agent itself mocked out
    so this is deterministic and doesn't depend on live third-party APIs.
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
        series = Series(name="The First Peacemaker", author="Some Author")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

    def tearDown(self):
        self.db.close()

    def _run_job_with_mocked_discovery(self, added_books: list[dict]):
        mocked_result = {
            "series_id": self.series.id,
            "added_books": added_books,
            "provider_failures": [],
            "all_providers_failed": False,
        }
        # run_series_check_job_full calls library_sync.update_from_series(),
        # which opens its own db session via a module-level `SessionLocal`
        # imported directly from database.py -- patching only
        # series_check_engine's SessionLocal leaves that call pointed at the
        # real on-disk dev database (sqlite:///./books.db) instead of this
        # test's isolated in-memory one. Locally that "worked" by accident
        # because a real books.db with a books table happens to exist on
        # disk; a clean checkout (e.g. CI) has no such file/table and
        # library_sync raises "no such table: books" instead.
        with patch.object(series_check_engine, "SessionLocal", self.SessionLocal), patch.object(
            library_sync, "SessionLocal", self.SessionLocal
        ), patch.object(series_check_engine.series_agent, "run_series_check", return_value=mocked_result):
            series_check_engine.run_series_check_job_full(self.series.id)

    def test_new_book_persists_source_url_from_discovery_candidate(self):
        self._run_job_with_mocked_discovery(
            [
                {
                    "title": "Edge of Shadow",
                    "author": "Some Author",
                    "series_name": "The First Peacemaker",
                    "book_number": 8,
                    "source_url": "https://www.amazon.com/dp/EXAMPLE8",
                    "provider": "web_search",
                    "publication_date": "2026-08-09",
                    "expected_date": None,
                    "status_hint": "available",
                    "asin_or_id": "web_search:https://www.amazon.com/dp/EXAMPLE8",
                    "is_missing": True,
                    "status": "available",
                    "canonical_metadata": {
                        "title_normalized": "Edge of Shadow",
                        "series_name_normalized": "The First Peacemaker",
                        "book_number_normalized": 8,
                        "publish_date_normalized": "2026-08-09",
                        "upcoming_date_normalized": None,
                        "availability": "available",
                        "edition_type": "unknown",
                        "title_selector": None,
                    },
                }
            ]
        )

        book = self.db.query(Book).filter(Book.series_id == self.series.id, Book.book_number == 8.0).first()
        self.assertIsNotNone(book)
        self.assertEqual(book.source_url, "https://www.amazon.com/dp/EXAMPLE8")

    def test_new_book_inherits_series_profile_id_not_default(self):
        # Regression test: Book.profile_id defaults to "robbie" when not set
        # explicitly (see models.py). A newly discovered book must inherit
        # the *series'* own profile_id instead of silently falling back to
        # that default -- otherwise it becomes an invisible "ghost" row that
        # no profile-scoped query can see, while still counting toward that
        # series' aggregates (total_books, has_new_upcoming_books, etc.).
        other_profile_series = Series(name="Mackenzie's Series", author="Some Author", profile_id="mackenzie")
        self.db.add(other_profile_series)
        self.db.commit()
        self.db.refresh(other_profile_series)

        mocked_result = {
            "series_id": other_profile_series.id,
            "added_books": [
                {
                    "title": "Some Future Book",
                    "author": "Some Author",
                    "series_name": "Mackenzie's Series",
                    "book_number": 4,
                    "source_url": None,
                    "provider": "web_search",
                    "publication_date": None,
                    "expected_date": None,
                    "status_hint": "upcoming",
                    "asin_or_id": "web_search:example4",
                    "is_missing": False,
                    "status": "upcoming",
                    "canonical_metadata": {
                        "title_normalized": "Some Future Book",
                        "series_name_normalized": "Mackenzie's Series",
                        "book_number_normalized": 4,
                        "publish_date_normalized": None,
                        "upcoming_date_normalized": None,
                        "availability": "upcoming",
                        "edition_type": "unknown",
                        "title_selector": None,
                    },
                }
            ],
            "provider_failures": [],
            "all_providers_failed": False,
        }
        with patch.object(series_check_engine, "SessionLocal", self.SessionLocal), patch.object(
            library_sync, "SessionLocal", self.SessionLocal
        ), patch.object(series_check_engine.series_agent, "run_series_check", return_value=mocked_result):
            series_check_engine.run_series_check_job_full(other_profile_series.id)

        book = (
            self.db.query(Book)
            .filter(Book.series_id == other_profile_series.id, Book.book_number == 4.0)
            .first()
        )
        self.assertIsNotNone(book)
        self.assertEqual(book.profile_id, "mackenzie")

    def test_new_book_with_no_source_url_leaves_it_null(self):
        self._run_job_with_mocked_discovery(
            [
                {
                    "title": "Some Book",
                    "author": "Some Author",
                    "series_name": "The First Peacemaker",
                    "book_number": 2,
                    "source_url": None,
                    "provider": "hardcover",
                    "publication_date": "2026-01-01",
                    "expected_date": None,
                    "status_hint": "available",
                    "asin_or_id": "hardcover:123",
                    "is_missing": True,
                    "status": "available",
                    "canonical_metadata": {
                        "title_normalized": "Some Book",
                        "series_name_normalized": "The First Peacemaker",
                        "book_number_normalized": 2,
                        "publish_date_normalized": "2026-01-01",
                        "upcoming_date_normalized": None,
                        "availability": "available",
                        "edition_type": "unknown",
                        "title_selector": None,
                    },
                }
            ]
        )

        book = self.db.query(Book).filter(Book.series_id == self.series.id, Book.book_number == 2.0).first()
        self.assertIsNotNone(book)
        self.assertIsNone(book.source_url)

    def test_existing_book_without_source_url_gets_backfilled_on_recheck(self):
        # The persistence layer only matches a re-discovered candidate back
        # to an existing row via the synthetic ASIN-style identifier it
        # stored on first insert (asin_or_id == f"{source}:{source_id}") --
        # series+number/title dicts are tracked but not currently consulted
        # for matching -- so a realistic re-check candidate carries the same
        # identifier as before.
        existing = Book(
            title="Edge of Shadow",
            author="Some Author",
            series_id=self.series.id,
            series_order=8,
            book_number=8.0,
            record_status="active",
            is_read=False,
            read_status="available",
            asin="WEB_SEARCH:HTTPS://WWW.AMAZON.COM/DP/EXAMPLE8",
            source_url=None,
        )
        self.db.add(existing)
        self.db.commit()

        self._run_job_with_mocked_discovery(
            [
                {
                    "title": "Edge of Shadow",
                    "author": "Some Author",
                    "series_name": "The First Peacemaker",
                    "book_number": 8,
                    "source_url": "https://www.amazon.com/dp/EXAMPLE8",
                    "provider": "web_search",
                    "publication_date": "2026-08-09",
                    "expected_date": None,
                    "status_hint": "available",
                    "asin_or_id": "web_search:https://www.amazon.com/dp/EXAMPLE8",
                    "is_missing": True,
                    "status": "available",
                    "canonical_metadata": {
                        "title_normalized": "Edge of Shadow",
                        "series_name_normalized": "The First Peacemaker",
                        "book_number_normalized": 8,
                        "publish_date_normalized": "2026-08-09",
                        "upcoming_date_normalized": None,
                        "availability": "available",
                        "edition_type": "unknown",
                        "title_selector": None,
                    },
                }
            ]
        )

        self.db.refresh(existing)
        self.assertEqual(existing.source_url, "https://www.amazon.com/dp/EXAMPLE8")

    def test_dedupe_collapse_merges_release_date_from_loser_into_keeper(self):
        # Reproduces the real-world "Quest Academy" / "Ultimate Level" /
        # "The Bad Guys" data loss: two active rows end up sharing the same
        # series+book_number (e.g. an older enriched row plus a
        # re-discovered duplicate with a cleaner title but no dates). The
        # dedupe collapse passes correctly pick a single survivor, but used
        # to just mark the loser "deleted" without copying over any date
        # the *keeper* was missing -- silently reverting a confirmed
        # release_date back to "Needs Date Verification" on the surviving
        # row. Both rows lack publication_date/asin/is_read so the
        # keeper_score/existing_score tuples tie and the *second* row
        # (`richer`) wins as keeper via score comparison order; either way,
        # the release_date from the loser must survive onto whichever row
        # is kept.
        richer = Book(
            title="Quest Academy: Scavengers",
            author="Some Author",
            series_id=self.series.id,
            book_number=2.0,
            series_order=2,
            record_status="active",
            is_read=False,
            read_status="available",
            release_date=None,
            source_url=None,
        )
        from datetime import date

        richer.release_date = date(2026, 6, 30)
        thinner = Book(
            title="Scavengers: Quest Academy, Book 2",
            author="Some Author",
            series_id=self.series.id,
            book_number=2.0,
            series_order=2,
            record_status="active",
            is_read=False,
            read_status="available",
            release_date=None,
            source_url=None,
        )
        self.db.add_all([richer, thinner])
        self.db.commit()

        self._run_job_with_mocked_discovery([])

        survivors = (
            self.db.query(Book)
            .filter(Book.series_id == self.series.id, Book.book_number == 2.0)
            .filter(Book.record_status != "deleted")
            .all()
        )
        self.assertEqual(len(survivors), 1, "dedupe collapse should leave exactly one active row")
        self.assertEqual(survivors[0].release_date, date(2026, 6, 30))

        deleted = (
            self.db.query(Book)
            .filter(Book.series_id == self.series.id, Book.book_number == 2.0)
            .filter(Book.record_status == "deleted")
            .all()
        )
        self.assertEqual(len(deleted), 1)


if __name__ == "__main__":
    unittest.main()
