"""Minimal "New Books Added to Library" notification system (Auto Discovery
MVP spec, §3): the services/notifications.py CRUD helpers, the persistence
hooks inside services/series_check_engine.py that create them, and the
GET/POST router endpoints.
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
    create_new_book_notification,
    dismiss_all_notifications,
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
        self.db.query(models.Book).delete()
        self.db.query(models.Series).delete()
        self.db.commit()
        self.db.close()

    def _make_book(self, profile_id="robbie", **overrides):
        defaults = dict(profile_id=profile_id, title="Some Book", author="Some Author")
        defaults.update(overrides)
        book = models.Book(**defaults)
        self.db.add(book)
        self.db.commit()
        self.db.refresh(book)
        return book

    def test_create_and_fetch_undismissed(self):
        book = self._make_book()
        create_new_book_notification(self.db, book)
        self.db.commit()

        unseen = get_undismissed_notifications(self.db, "robbie")
        self.assertEqual(len(unseen), 1)
        self.assertEqual(unseen[0].book_id, book.id)
        self.assertEqual(unseen[0].kind, "new_book")

    def test_dismiss_all_clears_the_list(self):
        book = self._make_book()
        create_new_book_notification(self.db, book)
        self.db.commit()

        dismissed_count = dismiss_all_notifications(self.db, "robbie")
        self.assertEqual(dismissed_count, 1)
        self.assertEqual(get_undismissed_notifications(self.db, "robbie"), [])

    def test_notifications_are_profile_scoped(self):
        robbie_book = self._make_book(profile_id="robbie")
        other_book = self._make_book(profile_id="other")
        create_new_book_notification(self.db, robbie_book)
        create_new_book_notification(self.db, other_book)
        self.db.commit()

        self.assertEqual(len(get_undismissed_notifications(self.db, "robbie")), 1)
        self.assertEqual(len(get_undismissed_notifications(self.db, "other")), 1)

        dismiss_all_notifications(self.db, "robbie")
        self.assertEqual(get_undismissed_notifications(self.db, "robbie"), [])
        self.assertEqual(len(get_undismissed_notifications(self.db, "other")), 1)


class SeriesCheckNotificationHooksTest(unittest.TestCase):
    """Confirms the two trigger points wired into run_series_check_job_full
    fire exactly when the spec says they should, and not otherwise.
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
        series = models.Series(name="The First Peacemaker", author="Some Author")
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
        defaults = dict(
            title="Edge of Shadow",
            author="Some Author",
            series_name="The First Peacemaker",
            book_number=8,
            source_url=None,
            provider="web_search",
            publication_date=None,
            expected_date=None,
            status_hint="available",
            asin_or_id="web_search:edge-of-shadow",
            is_missing=True,
            status="available",
            canonical_metadata={
                "title_normalized": "Edge of Shadow",
                "series_name_normalized": "The First Peacemaker",
                "book_number_normalized": 8,
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

    def test_new_available_book_creates_a_notification(self):
        self._run_job_with_mocked_discovery([self._candidate(status_hint="available", status="available")])

        notifications = self._notifications_for_this_series()
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].kind, "new_book")

        book = self.db.query(models.Book).filter(models.Book.series_id == self.series.id).first()
        self.assertEqual(notifications[0].book_id, book.id)

    def test_new_upcoming_book_does_not_create_a_notification(self):
        # Dropped trigger, per the spec's final decisions (§B) -- a brand
        # new upcoming book is not itself a notification event.
        self._run_job_with_mocked_discovery(
            [self._candidate(status_hint="upcoming", status="upcoming", expected_date="2027-01-01")]
        )
        self.assertEqual(self._notifications_for_this_series(), [])

    def test_upcoming_to_available_transition_creates_a_notification(self):
        existing = models.Book(
            title="Edge of Shadow",
            author="Some Author",
            series_id=self.series.id,
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
        self.assertEqual(notifications[0].book_id, existing.id)

    def test_already_available_book_refresh_does_not_create_a_duplicate_notification(self):
        existing = models.Book(
            title="Edge of Shadow",
            author="Some Author",
            series_id=self.series.id,
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


class NotificationEndpointTest(unittest.TestCase):
    """Router-level wiring only -- exercised against the real app's DB
    session dependency for the GET (a read), with the mutating POST mocked
    at the service layer, same pattern as BulkReresolveEndpointAuthTest.
    """

    def setUp(self):
        self.client = TestClient(main.app)
        self.token = create_owner_token()

    def test_unseen_requires_reader_auth(self):
        response = self.client.get("/notifications/unseen")
        self.assertEqual(response.status_code, 401)

    def test_dismiss_requires_owner_auth(self):
        response = self.client.post("/notifications/dismiss")
        self.assertEqual(response.status_code, 403)

    def test_unseen_returns_a_list_when_authenticated(self):
        with patch("routers.notifications.get_undismissed_notifications", return_value=[]):
            response = self.client.get("/notifications/unseen", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_dismiss_returns_the_dismissed_count(self):
        with patch("routers.notifications.dismiss_all_notifications", return_value=4) as mock_dismiss:
            response = self.client.post("/notifications/dismiss", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["dismissed_count"], 4)
        mock_dismiss.assert_called_once()


if __name__ == "__main__":
    unittest.main()
