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
        series = Series(name="The First Peacemaker", author="Some Author", profile_id="robbie")
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

    def test_new_book_gets_discovery_provenance_and_matching_canonical_title(self):
        # Check Now has no user-entered title to preserve, so both title
        # columns get the same resolved value, metadata_source is stamped
        # "discovery" (verified by construction), and book_number_source is
        # "provider" since the number came from canonical_metadata, never
        # from a user typing it in.
        self._run_job_with_mocked_discovery(
            [
                {
                    "title": "Edge of Shadow",
                    "author": "Some Author",
                    "series_name": "The First Peacemaker",
                    "book_number": 8,
                    "source_url": None,
                    "provider": "web_search",
                    "publication_date": None,
                    "expected_date": None,
                    "status_hint": "available",
                    "asin_or_id": "web_search:edge-of-shadow",
                    "is_missing": True,
                    "status": "available",
                    "canonical_metadata": {
                        "title_normalized": "Edge of Shadow",
                        "series_name_normalized": "The First Peacemaker",
                        "book_number_normalized": 8,
                        "publish_date_normalized": None,
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
        self.assertEqual(book.canonical_title, "Edge of Shadow")
        self.assertEqual(book.title, book.canonical_title)
        self.assertEqual(book.metadata_source, "discovery")
        self.assertEqual(book.book_number_source, "provider")

    def test_new_book_inherits_series_profile_id_not_default(self):
        # Regression test (CR-10 removed the fallback this originally
        # guarded against -- Book.profile_id used to default to "robbie"
        # when not set explicitly). A newly discovered book must inherit
        # the *series'* own profile_id -- otherwise it becomes an invisible
        # "ghost" row that no profile-scoped query can see, while still
        # counting toward that series' aggregates (total_books,
        # has_new_upcoming_books, etc.).
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
            profile_id=self.series.profile_id,
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

    def test_companion_book_with_fractional_number_does_not_clobber_real_book(self):
        # Regression (live bug): a discovered companion/side-story book at a
        # fractional series position (e.g. "Threshing Day" at 3.5 in The
        # Empyrean) used to share an identity key with the real numbered
        # book it truncated down to (book 3, "Onyx Storm") -- see
        # services/identity.py's _normalized_book_number_value. That made
        # the persistence layer treat the new companion candidate as an
        # *update* to the existing book-3 row (overwriting its title/status)
        # instead of inserting a distinct new row, effectively destroying
        # the real book 3 the moment the companion book was discovered.
        onyx_storm = Book(
            title="Onyx Storm",
            author="Some Author",
            series_id=self.series.id,
            profile_id=self.series.profile_id,
            book_number=3.0,
            series_order=3,
            record_status="active",
            is_read=True,
            read_status="read",
        )
        self.db.add(onyx_storm)
        self.db.commit()
        onyx_storm_id = onyx_storm.id

        self._run_job_with_mocked_discovery(
            [
                {
                    "title": "Threshing Day: (The First Peacemaker Book 3.5)",
                    "author": "Some Author",
                    "series_name": "The First Peacemaker",
                    "book_number": 3.5,
                    "source_url": None,
                    "provider": "hardcover",
                    "publication_date": None,
                    "expected_date": "2026-09-29",
                    "status_hint": "upcoming",
                    "asin_or_id": "hardcover:threshing-day",
                    "is_missing": False,
                    "status": "upcoming",
                    "canonical_metadata": {
                        "title_normalized": "Threshing Day: (The First Peacemaker Book 3.5)",
                        "series_name_normalized": "The First Peacemaker",
                        "book_number_normalized": 3.5,
                        "publish_date_normalized": None,
                        "upcoming_date_normalized": "2026-09-29",
                        "availability": "upcoming",
                        "edition_type": "unknown",
                        "title_selector": None,
                    },
                }
            ]
        )

        self.db.refresh(onyx_storm)
        self.assertEqual(onyx_storm.id, onyx_storm_id)
        self.assertEqual(onyx_storm.title, "Onyx Storm")
        self.assertTrue(onyx_storm.is_read)
        self.assertEqual(onyx_storm.read_status, "read")
        self.assertEqual(str((onyx_storm.record_status or "active")), "active")

        companion = (
            self.db.query(Book)
            .filter(Book.series_id == self.series.id, Book.book_number == 3.5)
            .first()
        )
        self.assertIsNotNone(companion)
        self.assertNotEqual(companion.id, onyx_storm_id)
        self.assertEqual(companion.read_status, "upcoming")

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
            profile_id=self.series.profile_id,
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
            profile_id=self.series.profile_id,
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


class SeriesCheckPrecheckTest(unittest.TestCase):
    """Tests the catalog-only pre-check short-circuit (architecture spec
    #7.2/#7.3) at the job level -- a series checked within the staleness
    window skips the full discovery loop entirely when the pre-check finds
    nothing new, and falls through to the normal loop when it does.
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

    def _make_series(self, *, last_checked, book_number=1.0):
        series = Series(name="Jonathan Hunt", author="Georgia Wagner", profile_id="robbie", last_checked=last_checked)
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.db.add(
            Book(
                title="The Jericho Siege",
                author="Georgia Wagner",
                series_id=series.id,
                profile_id=series.profile_id,
                book_number=book_number,
                record_status="active",
                read_status="available",
            )
        )
        self.db.commit()
        return series

    def test_recently_checked_series_short_circuits_when_precheck_finds_nothing(self):
        # SERIES_CHECK_PRECHECK_ENABLED is temporarily off by default (see
        # its own comment) while discovery itself is under active
        # development -- explicitly re-enabled here so this mechanism's own
        # behavior stays covered for whenever it's flipped back on.
        from datetime import date

        series = self._make_series(last_checked=date.today())

        with patch.object(series_check_engine, "SessionLocal", self.SessionLocal), patch.object(
            library_sync, "SessionLocal", self.SessionLocal
        ), patch.object(
            series_check_engine, "SERIES_CHECK_PRECHECK_ENABLED", True
        ), patch.object(
            series_check_engine.discovery_engine, "precheck_for_new_volumes", return_value=False
        ) as mock_precheck, patch.object(
            series_check_engine.series_agent, "run_series_check"
        ) as mock_run_series_check:
            series_check_engine.run_series_check_job_full(series.id)

        mock_precheck.assert_called_once()
        mock_run_series_check.assert_not_called()

        job = series_check_engine.series_check_jobs[series.id]
        self.assertTrue(job["result"]["idle_check"])
        self.assertEqual(job["result"]["rounds_run"], 0)
        self.assertEqual(job["completion"]["status"], "no_new_books")

    def test_recently_checked_series_falls_through_to_full_loop_when_precheck_finds_something(self):
        from datetime import date

        series = self._make_series(last_checked=date.today())
        mocked_result = {
            "series_id": series.id,
            "added_books": [],
            "provider_failures": [],
            "all_providers_failed": False,
        }

        with patch.object(series_check_engine, "SessionLocal", self.SessionLocal), patch.object(
            library_sync, "SessionLocal", self.SessionLocal
        ), patch.object(
            series_check_engine, "SERIES_CHECK_PRECHECK_ENABLED", True
        ), patch.object(
            series_check_engine.discovery_engine, "precheck_for_new_volumes", return_value=True
        ) as mock_precheck, patch.object(
            series_check_engine.series_agent, "run_series_check", return_value=mocked_result
        ) as mock_run_series_check:
            series_check_engine.run_series_check_job_full(series.id)

        mock_precheck.assert_called_once()
        mock_run_series_check.assert_called()

        job = series_check_engine.series_check_jobs[series.id]
        self.assertFalse(job["result"]["idle_check"])
        self.assertGreaterEqual(job["result"]["rounds_run"], 1)

    def test_precheck_disabled_by_default_always_runs_full_loop_even_when_recently_checked(self):
        # Regression guard for the temporary dev-mode disable: a
        # recently-checked series (which would have short-circuited if the
        # precheck were on) must still run the real full discovery loop
        # while SERIES_CHECK_PRECHECK_ENABLED is False, and must never call
        # precheck_for_new_volumes at all.
        from datetime import date

        self.assertFalse(series_check_engine.SERIES_CHECK_PRECHECK_ENABLED)
        series = self._make_series(last_checked=date.today())
        mocked_result = {
            "series_id": series.id,
            "added_books": [],
            "provider_failures": [],
            "all_providers_failed": False,
        }

        with patch.object(series_check_engine, "SessionLocal", self.SessionLocal), patch.object(
            library_sync, "SessionLocal", self.SessionLocal
        ), patch.object(
            series_check_engine.discovery_engine, "precheck_for_new_volumes"
        ) as mock_precheck, patch.object(
            series_check_engine.series_agent, "run_series_check", return_value=mocked_result
        ) as mock_run_series_check:
            series_check_engine.run_series_check_job_full(series.id)

        mock_precheck.assert_not_called()
        mock_run_series_check.assert_called()

        job = series_check_engine.series_check_jobs[series.id]
        self.assertFalse(job["result"]["idle_check"])
        self.assertGreaterEqual(job["result"]["rounds_run"], 1)

    def test_series_with_no_check_history_always_runs_full_loop(self):
        series = self._make_series(last_checked=None)
        mocked_result = {
            "series_id": series.id,
            "added_books": [],
            "provider_failures": [],
            "all_providers_failed": False,
        }

        with patch.object(series_check_engine, "SessionLocal", self.SessionLocal), patch.object(
            library_sync, "SessionLocal", self.SessionLocal
        ), patch.object(
            series_check_engine.discovery_engine, "precheck_for_new_volumes"
        ) as mock_precheck, patch.object(
            series_check_engine.series_agent, "run_series_check", return_value=mocked_result
        ) as mock_run_series_check:
            series_check_engine.run_series_check_job_full(series.id)

        mock_precheck.assert_not_called()
        mock_run_series_check.assert_called()

        job = series_check_engine.series_check_jobs[series.id]
        self.assertFalse(job["result"]["idle_check"])

    def test_staleness_window_boundary_beyond_threshold_runs_full_loop(self):
        from datetime import date, timedelta

        series = self._make_series(
            last_checked=date.today() - timedelta(days=series_check_engine.SERIES_CHECK_PRECHECK_STALENESS_DAYS + 1)
        )
        mocked_result = {
            "series_id": series.id,
            "added_books": [],
            "provider_failures": [],
            "all_providers_failed": False,
        }

        with patch.object(series_check_engine, "SessionLocal", self.SessionLocal), patch.object(
            library_sync, "SessionLocal", self.SessionLocal
        ), patch.object(
            series_check_engine.discovery_engine, "precheck_for_new_volumes"
        ) as mock_precheck, patch.object(
            series_check_engine.series_agent, "run_series_check", return_value=mocked_result
        ) as mock_run_series_check:
            series_check_engine.run_series_check_job_full(series.id)

        mock_precheck.assert_not_called()
        mock_run_series_check.assert_called()


class SkeletonUpdateFailureVisibilityTest(unittest.TestCase):
    """FIX-PB-7: a failure inside apply_skeleton_updates must (1) never
    roll back the already-committed Book persistence for that round, and
    (2) be visible outside server logs -- on the job dict's result and
    completion payloads (what services/series.py's status endpoint
    forwards verbatim) -- rather than only reaching logger.exception.
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
        series = Series(name="Some Series", author="Some Author", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

    def tearDown(self):
        self.db.close()

    def test_skeleton_update_failure_is_surfaced_without_rolling_back_the_new_book(self):
        mocked_result = {
            "series_id": self.series.id,
            "added_books": [
                {
                    "title": "Some New Book",
                    "author": "Some Author",
                    "series_name": "Some Series",
                    "book_number": 2,
                    "source_url": None,
                    "provider": "web_search",
                    "publication_date": "2026-01-01",
                    "expected_date": None,
                    "status_hint": "available",
                    "asin_or_id": "web_search:https://example.com/book-2",
                    "is_missing": True,
                    "status": "available",
                    "canonical_metadata": {
                        "title_normalized": "Some New Book",
                        "series_name_normalized": "Some Series",
                        "book_number_normalized": 2,
                        "publish_date_normalized": "2026-01-01",
                        "upcoming_date_normalized": None,
                        "availability": "available",
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
        ), patch.object(
            series_check_engine.series_agent, "run_series_check", return_value=mocked_result
        ), patch.object(
            series_check_engine, "apply_skeleton_updates", side_effect=RuntimeError("skeleton boom")
        ):
            series_check_engine.run_series_check_job_full(self.series.id)

        # The new Book row still persisted -- no rollback of that
        # already-committed work.
        book = self.db.query(Book).filter(Book.series_id == self.series.id, Book.book_number == 2.0).first()
        self.assertIsNotNone(book)

        job = series_check_engine.series_check_jobs[self.series.id]
        # The mocked discovery result is identical every round (a
        # persistent stub), so the loop runs more than one round before its
        # own "nothing new persisted" stop condition kicks in -- every one
        # of those rounds' skeleton-update attempts still fails and gets
        # recorded, so this asserts "at least one", not an exact count tied
        # to the round loop's own unrelated stop-condition timing.
        failures = job["result"]["skeleton_update_failures"]
        self.assertGreaterEqual(len(failures), 1)
        self.assertTrue(all("skeleton boom" in f for f in failures))
        # completion is what the /check status endpoint forwards verbatim.
        self.assertEqual(job["completion"]["skeleton_update_failures"], failures)

    def test_no_failure_means_an_empty_list_not_a_missing_key(self):
        mocked_result = {
            "series_id": self.series.id,
            "added_books": [],
            "provider_failures": [],
            "all_providers_failed": False,
        }
        with patch.object(series_check_engine, "SessionLocal", self.SessionLocal), patch.object(
            library_sync, "SessionLocal", self.SessionLocal
        ), patch.object(series_check_engine.series_agent, "run_series_check", return_value=mocked_result):
            series_check_engine.run_series_check_job_full(self.series.id)

        job = series_check_engine.series_check_jobs[self.series.id]
        self.assertEqual(job["result"]["skeleton_update_failures"], [])
        self.assertEqual(job["completion"]["skeleton_update_failures"], [])


class CrossProfileCheckNowIsolationTest(unittest.TestCase):
    """TG-5: two profiles independently tracking a same-named series (a
    realistic multi-profile scenario, not a contrived id collision) must
    stay fully isolated through a real Check Now job run -- profile B's
    Series/Book rows are untouched by profile A's job, even though both
    series share a name/author and overlapping book numbers.
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

        self.series_a = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
        self.series_b = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="other_profile")
        self.db.add(self.series_a)
        self.db.add(self.series_b)
        self.db.commit()
        self.db.refresh(self.series_a)
        self.db.refresh(self.series_b)

        for series in (self.series_a, self.series_b):
            for number in (1, 2, 3):
                self.db.add(
                    Book(
                        title=f"Cherry Blossom Girls Book {number}",
                        author="Harmon Cooper",
                        series_id=series.id,
                        profile_id=series.profile_id,
                        series_order=number,
                        book_number=float(number),
                        record_status="active",
                        is_read=False,
                    )
                )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_check_now_for_profile_a_never_touches_profile_bs_rows(self):
        mocked_result = {
            "series_id": self.series_a.id,
            "added_books": [
                {
                    "title": "Cherry Blossom Girls Book 4",
                    "author": "Harmon Cooper",
                    "series_name": "Cherry Blossom Girls",
                    "book_number": 4,
                    "source_url": None,
                    "provider": "web_search",
                    "publication_date": "2026-01-01",
                    "expected_date": None,
                    "status_hint": "available",
                    "asin_or_id": "web_search:https://example.com/book-4",
                    "is_missing": True,
                    "status": "available",
                    "canonical_metadata": {
                        "title_normalized": "Cherry Blossom Girls Book 4",
                        "series_name_normalized": "Cherry Blossom Girls",
                        "book_number_normalized": 4,
                        "publish_date_normalized": "2026-01-01",
                        "upcoming_date_normalized": None,
                        "availability": "available",
                        "edition_type": "unknown",
                        "title_selector": None,
                    },
                }
            ],
            "provider_failures": [],
            "all_providers_failed": False,
        }

        profile_b_books_before = [
            (b.id, b.title, b.book_number, b.record_status)
            for b in self.db.query(Book).filter(Book.profile_id == "other_profile").order_by(Book.id).all()
        ]
        profile_b_last_checked_before = self.series_b.last_checked
        profile_b_has_new_books_before = self.series_b.has_new_books

        with patch.object(series_check_engine, "SessionLocal", self.SessionLocal), patch.object(
            library_sync, "SessionLocal", self.SessionLocal
        ), patch.object(
            series_check_engine.series_agent, "run_series_check", return_value=mocked_result
        ):
            series_check_engine.run_series_check_job_full(self.series_a.id)

        # Profile A got its new book.
        new_book = (
            self.db.query(Book)
            .filter(Book.series_id == self.series_a.id, Book.book_number == 4.0)
            .first()
        )
        self.assertIsNotNone(new_book)
        self.assertEqual(new_book.profile_id, "robbie")

        # Profile B's books are byte-for-byte unchanged -- no phantom
        # "Book 4" leaked into the other profile's series, and none of its
        # existing rows were touched.
        profile_b_books_after = [
            (b.id, b.title, b.book_number, b.record_status)
            for b in self.db.query(Book).filter(Book.profile_id == "other_profile").order_by(Book.id).all()
        ]
        self.assertEqual(profile_b_books_before, profile_b_books_after)

        self.db.refresh(self.series_b)
        self.assertEqual(self.series_b.last_checked, profile_b_last_checked_before)
        self.assertEqual(self.series_b.has_new_books, profile_b_has_new_books_before)

        # The job itself is keyed/scoped to profile A's series_id only.
        job = series_check_engine.series_check_jobs[self.series_a.id]
        self.assertEqual(job["result"]["series_id"], self.series_a.id)


if __name__ == "__main__":
    unittest.main()
