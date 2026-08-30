"""Durable series-level discovery notifications (see the "Durable
Series-Level Discovery Notifications" design chat's finalized spec): the
services/notifications.py CRUD helpers, the aggregation hook inside
services/series_check_engine.py's run_series_check_job_full that creates
one row per series per run, and the GET/POST router endpoints.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import library_sync
import main
import models
import services.series_check_engine as series_check_engine
from database import Base
from routers.deps import create_owner_token
from services.notifications import (
    SERIES_DISCOVERY_DELTA_KIND,
    create_series_discovery_notification,
    dismiss_all_notifications,
    dismiss_notification,
    get_undismissed_notifications,
)


class NotificationCrudTest(unittest.TestCase):
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
        self.db.rollback()
        self.db.query(models.Notification).delete()
        self.db.query(models.Series).delete()
        self.db.commit()
        self.db.close()

    def _make_series(self, profile_id="robbie", name="The First Peacemaker"):
        series = models.Series(name=name, author="Some Author", profile_id=profile_id)
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        return series

    def test_create_and_fetch_undismissed(self):
        series = self._make_series()
        create_series_discovery_notification(
            self.db, profile_id="robbie", series_id=series.id, series_name=series.name, count_new_books=3
        )
        self.db.commit()

        unseen = get_undismissed_notifications(self.db, "robbie")
        self.assertEqual(len(unseen), 1)
        self.assertEqual(unseen[0].series_id, series.id)
        self.assertEqual(unseen[0].series_name, series.name)
        self.assertEqual(unseen[0].count_new_books, 3)
        self.assertEqual(unseen[0].kind, SERIES_DISCOVERY_DELTA_KIND)

    def test_legacy_kind_rows_are_excluded_from_unseen(self):
        # Pre-migration per-book rows (kind="new_book") must never surface
        # in the new series-level view, even if one were somehow still
        # undismissed -- the read path filters by kind, not just
        # dismissed_at, as defense-in-depth alongside the migration's
        # backfill (see alembic/versions/cc28c2fb7b4b_...).
        series = self._make_series()
        legacy = models.Notification(profile_id="robbie", series_id=series.id, kind="new_book")
        self.db.add(legacy)
        self.db.commit()

        self.assertEqual(get_undismissed_notifications(self.db, "robbie"), [])

    def test_dismiss_one_leaves_others_undismissed(self):
        series = self._make_series()
        first = create_series_discovery_notification(
            self.db, profile_id="robbie", series_id=series.id, series_name=series.name, count_new_books=1
        )
        second = create_series_discovery_notification(
            self.db, profile_id="robbie", series_id=series.id, series_name=series.name, count_new_books=2
        )
        self.db.commit()

        dismissed = dismiss_notification(self.db, "robbie", first.id)
        self.assertTrue(dismissed)

        unseen = get_undismissed_notifications(self.db, "robbie")
        self.assertEqual([item.id for item in unseen], [second.id])

    def test_dismiss_one_is_a_noop_for_missing_or_foreign_row(self):
        series = self._make_series()
        mine = create_series_discovery_notification(
            self.db, profile_id="robbie", series_id=series.id, series_name=series.name, count_new_books=1
        )
        other_series = self._make_series(profile_id="other", name="Other Series")
        theirs = create_series_discovery_notification(
            self.db, profile_id="other", series_id=other_series.id, series_name=other_series.name, count_new_books=1
        )
        self.db.commit()

        self.assertFalse(dismiss_notification(self.db, "robbie", theirs.id))
        self.assertFalse(dismiss_notification(self.db, "robbie", 999999))
        self.assertEqual(len(get_undismissed_notifications(self.db, "robbie")), 1)
        self.assertEqual(len(get_undismissed_notifications(self.db, "other")), 1)
        self.assertEqual(get_undismissed_notifications(self.db, "robbie")[0].id, mine.id)

    def test_dismiss_all_clears_the_list(self):
        series = self._make_series()
        create_series_discovery_notification(
            self.db, profile_id="robbie", series_id=series.id, series_name=series.name, count_new_books=1
        )
        self.db.commit()

        dismissed_count = dismiss_all_notifications(self.db, "robbie")
        self.assertEqual(dismissed_count, 1)
        self.assertEqual(get_undismissed_notifications(self.db, "robbie"), [])

    def test_notifications_are_profile_scoped(self):
        robbie_series = self._make_series(profile_id="robbie")
        other_series = self._make_series(profile_id="other", name="Other Series")
        create_series_discovery_notification(
            self.db, profile_id="robbie", series_id=robbie_series.id, series_name=robbie_series.name, count_new_books=1
        )
        create_series_discovery_notification(
            self.db, profile_id="other", series_id=other_series.id, series_name=other_series.name, count_new_books=1
        )
        self.db.commit()

        self.assertEqual(len(get_undismissed_notifications(self.db, "robbie")), 1)
        self.assertEqual(len(get_undismissed_notifications(self.db, "other")), 1)

        dismiss_all_notifications(self.db, "robbie")
        self.assertEqual(get_undismissed_notifications(self.db, "robbie"), [])
        self.assertEqual(len(get_undismissed_notifications(self.db, "other")), 1)


class SeriesCheckNotificationHooksTest(unittest.TestCase):
    """Confirms run_series_check_job_full writes exactly one aggregated
    notification per series per run, counting every newly-inserted book
    (available or upcoming) plus upcoming->available transitions, deduped
    by book id -- never per-book, never for a no-op refresh -- and that
    the notification's book_titles list always has exactly count_new_books
    entries (see the "Durable Notifications: Count Fix + Title List +
    Dedupe" design chat's finalized spec).
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
        series = models.Series(name="The First Peacemaker", author="Some Author", profile_id="robbie")
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
        with patch.object(series_check_engine, "SessionLocal", self.SessionLocal), patch.object(
            library_sync, "SessionLocal", self.SessionLocal
        ), patch.object(series_check_engine.series_agent, "run_series_check", return_value=mocked_result):
            series_check_engine.run_series_check_job_full(self.series.id)

    def _candidate(self, **overrides):
        title = overrides.get("title", "Edge of Shadow")
        book_number = overrides.get("book_number", 8)
        defaults = dict(
            title=title,
            author="Some Author",
            series_name="The First Peacemaker",
            book_number=book_number,
            source_url=None,
            provider="web_search",
            publication_date=None,
            expected_date=None,
            status_hint="available",
            asin_or_id="web_search:edge-of-shadow",
            is_missing=True,
            status="available",
            # title_normalized/book_number_normalized are read preferentially
            # over the top-level title/book_number by series_check_engine's
            # persistence loop -- these must track whatever this candidate's
            # overrides say, or two distinctly-titled test candidates would
            # both resolve to the same identity.
            canonical_metadata={
                "title_normalized": title,
                "series_name_normalized": "The First Peacemaker",
                "book_number_normalized": book_number,
                "publish_date_normalized": None,
                "upcoming_date_normalized": None,
                "availability": "available",
                "edition_type": "unknown",
                "title_selector": None,
            },
        )
        defaults.update(overrides)
        return defaults

    def _notifications_for_this_series(self):
        # Each test method's setUp creates its own fresh Series row, but
        # they all share one class-level in-memory engine/DB (never wiped
        # between tests) -- scope every assertion to *this* test's series
        # so an earlier test's notifications can't leak into this count.
        return self.db.query(models.Notification).filter(models.Notification.series_id == self.series.id).all()

    def test_new_available_book_creates_one_aggregated_notification(self):
        self._run_job_with_mocked_discovery([self._candidate(status_hint="available", status="available")])

        notifications = self._notifications_for_this_series()
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].kind, "series_discovery_delta")
        self.assertEqual(notifications[0].count_new_books, 1)
        self.assertEqual(notifications[0].series_name, self.series.name)
        self.assertIsNone(notifications[0].book_id)
        self.assertEqual(notifications[0].book_titles_json, [{"title": "Edge of Shadow", "status": "available"}])

    def test_multiple_new_books_in_one_run_create_a_single_row_with_the_total_count(self):
        self._run_job_with_mocked_discovery(
            [
                self._candidate(title="Edge of Shadow", book_number=8, asin_or_id="web_search:edge-of-shadow"),
                self._candidate(title="Edge of Dawn", book_number=9, asin_or_id="web_search:edge-of-dawn"),
            ]
        )

        notifications = self._notifications_for_this_series()
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].count_new_books, 2)
        self.assertEqual(len(notifications[0].book_titles_json), 2)
        self.assertEqual(
            {item["title"] for item in notifications[0].book_titles_json},
            {"Edge of Shadow", "Edge of Dawn"},
        )

    def test_new_upcoming_book_creates_a_notification_with_an_upcoming_status_tag(self):
        # Corrected behavior (see the "Durable Notifications: Count Fix +
        # Title List + Dedupe" spec, item 1): a brand-new *upcoming*
        # insert now contributes to the aggregate count and the title
        # list at insert-time, rather than only once/if it later flips to
        # available.
        self._run_job_with_mocked_discovery(
            [self._candidate(status_hint="upcoming", status="upcoming", expected_date="2027-01-01")]
        )
        notifications = self._notifications_for_this_series()
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].count_new_books, 1)
        self.assertEqual(notifications[0].book_titles_json, [{"title": "Edge of Shadow", "status": "upcoming"}])

    def test_upcoming_to_available_transition_creates_a_notification(self):
        existing = models.Book(
            title="Edge of Shadow",
            author="Some Author",
            series_id=self.series.id,
            profile_id=self.series.profile_id,
            book_number=8.0,
            series_order=8,
            record_status="active",
            is_read=False,
            read_status="upcoming",
            is_upcoming_auto=True,
            asin="WEB_SEARCH:EDGE-OF-SHADOW",
        )
        self.db.add(existing)
        self.db.commit()

        self._run_job_with_mocked_discovery([self._candidate(status_hint="available", status="available")])

        self.db.refresh(existing)
        self.assertEqual(existing.read_status, "available")
        notifications = self._notifications_for_this_series()
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].count_new_books, 1)
        self.assertIsNone(notifications[0].book_id)
        self.assertEqual(notifications[0].book_titles_json, [{"title": "Edge of Shadow", "status": "available"}])

    def test_new_insert_and_transition_in_the_same_run_are_summed_into_one_row(self):
        existing = models.Book(
            title="Edge of Shadow",
            author="Some Author",
            series_id=self.series.id,
            profile_id=self.series.profile_id,
            book_number=8.0,
            series_order=8,
            record_status="active",
            is_read=False,
            read_status="upcoming",
            is_upcoming_auto=True,
            asin="WEB_SEARCH:EDGE-OF-SHADOW",
        )
        self.db.add(existing)
        self.db.commit()

        self._run_job_with_mocked_discovery(
            [
                self._candidate(status_hint="available", status="available", book_number=8, asin_or_id="web_search:edge-of-shadow"),
                self._candidate(title="Edge of Dawn", book_number=9, asin_or_id="web_search:edge-of-dawn"),
            ]
        )

        notifications = self._notifications_for_this_series()
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].count_new_books, 2)
        self.assertEqual(
            {item["title"] for item in notifications[0].book_titles_json},
            {"Edge of Shadow", "Edge of Dawn"},
        )

    def test_book_flipping_upcoming_to_available_across_rounds_of_one_run_is_not_double_counted(self):
        # Regression guard for the cross-round edge case the finalized
        # spec's dedupe requirement (item 3) exists to solve: round 1
        # inserts a book as upcoming; round 2 of the *same* Check Now
        # click re-discovers it as available (matched_existing this time,
        # since round 1's insert already committed). Without dedupe-by-
        # book-id, this would double-attribute one book's journey as 2
        # toward count_new_books and list it twice.
        first_round = {
            "series_id": self.series.id,
            "added_books": [
                self._candidate(status_hint="upcoming", status="upcoming", expected_date="2027-01-01")
            ],
            "provider_failures": [],
            "all_providers_failed": False,
        }
        second_round = {
            "series_id": self.series.id,
            "added_books": [self._candidate(status_hint="available", status="available")],
            "provider_failures": [],
            "all_providers_failed": False,
        }
        with patch.object(series_check_engine, "SessionLocal", self.SessionLocal), patch.object(
            library_sync, "SessionLocal", self.SessionLocal
        ), patch.object(
            series_check_engine.series_agent,
            "run_series_check",
            side_effect=[first_round, second_round, {**second_round, "added_books": []}],
        ):
            series_check_engine.run_series_check_job_full(self.series.id)

        notifications = self._notifications_for_this_series()
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].count_new_books, 1)
        self.assertEqual(notifications[0].book_titles_json, [{"title": "Edge of Shadow", "status": "available"}])

    def test_already_available_book_refresh_does_not_create_a_notification(self):
        existing = models.Book(
            title="Edge of Shadow",
            author="Some Author",
            series_id=self.series.id,
            profile_id=self.series.profile_id,
            book_number=8.0,
            series_order=8,
            record_status="active",
            is_read=False,
            read_status="available",
            is_upcoming_auto=False,
            asin="WEB_SEARCH:EDGE-OF-SHADOW",
        )
        self.db.add(existing)
        self.db.commit()

        self._run_job_with_mocked_discovery([self._candidate(status_hint="available", status="available")])

        self.assertEqual(self._notifications_for_this_series(), [])

    def test_completion_response_carries_the_same_discovery_delta_count(self):
        self._run_job_with_mocked_discovery(
            [
                self._candidate(title="Edge of Shadow", book_number=8, asin_or_id="web_search:edge-of-shadow"),
                self._candidate(title="Edge of Dawn", book_number=9, asin_or_id="web_search:edge-of-dawn"),
            ]
        )

        notifications = self._notifications_for_this_series()
        completion = (series_check_engine.series_check_jobs.get(self.series.id) or {}).get("completion") or {}
        self.assertEqual(completion.get("discovery_delta_count"), 2)
        self.assertEqual(notifications[0].count_new_books, completion.get("discovery_delta_count"))


class NotificationEndpointTest(unittest.TestCase):
    """Router-level wiring only -- exercised against the real app's DB
    session dependency for the GET (a read), with the mutating POSTs mocked
    at the service layer, same pattern as BulkReresolveEndpointAuthTest.
    """

    def setUp(self):
        self.client = TestClient(main.app)
        self.token = create_owner_token()

    def test_unseen_requires_reader_auth(self):
        response = self.client.get("/notifications/unseen")
        self.assertEqual(response.status_code, 401)

    def test_dismiss_all_requires_owner_auth(self):
        response = self.client.post("/notifications/dismiss")
        self.assertEqual(response.status_code, 403)

    def test_dismiss_one_requires_owner_auth(self):
        response = self.client.post("/notifications/1/dismiss")
        self.assertEqual(response.status_code, 403)

    def test_unseen_returns_a_list_when_authenticated(self):
        with patch("routers.notifications.get_undismissed_notifications", return_value=[]):
            response = self.client.get("/notifications/unseen", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_dismiss_all_returns_the_dismissed_count(self):
        with patch("routers.notifications.dismiss_all_notifications", return_value=4) as mock_dismiss:
            response = self.client.post("/notifications/dismiss", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dismissed_count"], 4)
        mock_dismiss.assert_called_once()

    def test_dismiss_one_returns_success_when_found(self):
        with patch("routers.notifications.dismiss_notification", return_value=True) as mock_dismiss:
            response = self.client.post("/notifications/42/dismiss", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dismissed_count"], 1)
        mock_dismiss.assert_called_once()

    def test_dismiss_one_returns_404_when_not_found(self):
        with patch("routers.notifications.dismiss_notification", return_value=False):
            response = self.client.post("/notifications/999999/dismiss", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
