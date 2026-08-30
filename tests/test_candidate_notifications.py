"""LitRPG Enhanced Discovery's "Review Candidate Book" notifications: the
services/candidate_notifications.py CRUD/dedupe/ignore/add-to-series
helpers and the GET/POST router endpoints. See models.
SeriesCandidateNotification's docstring for the full design.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import models
from database import Base
from routers.deps import create_owner_token
from services.candidate_notifications import (
    build_review_urls,
    create_or_refresh_candidate_notification,
    get_unresolved_candidate_notifications,
    resolve_add_to_series,
    resolve_do_not_add,
)


class CandidateNotificationDedupeTest(unittest.TestCase):
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
        series = models.Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

    def tearDown(self):
        self.db.rollback()
        self.db.query(models.Book).delete()
        self.db.query(models.SeriesCandidateNotification).delete()
        self.db.query(models.SeriesSkeleton).delete()
        self.db.query(models.Series).delete()
        self.db.commit()
        self.db.close()

    def _canonical(self, **overrides):
        defaults = dict(
            title="Desert Protocol",
            author="Harmon Cooper",
            series_name="Cherry Blossom Girls",
            series_number=7,
            date_iso="2024-02-20",
            url="https://example.com/desert-protocol",
            provider="hardcover",
            identifier="9781111111111",
            isbn13="9781111111111",
            asin=None,
        )
        defaults.update(overrides)
        return defaults

    def test_creates_a_new_unresolved_row(self):
        row = create_or_refresh_candidate_notification(
            self.db,
            profile_id="robbie",
            series_id=self.series.id,
            series_name=self.series.name,
            canonical=self._canonical(),
            overall_confidence="medium",
            provider_confidence="low",
            series_name_hint="Cherry Blossom Girls",
            reason_flags=[],
        )
        self.db.commit()

        self.assertIsNotNone(row)
        self.assertIsNone(row.resolution)
        self.assertEqual(row.candidate_title, "Desert Protocol")
        self.assertEqual(row.isbn13, "9781111111111")

        unresolved = get_unresolved_candidate_notifications(self.db, "robbie")
        self.assertEqual(len(unresolved), 1)

    def test_rediscovering_the_same_isbn_refreshes_in_place_instead_of_duplicating(self):
        create_or_refresh_candidate_notification(
            self.db,
            profile_id="robbie",
            series_id=self.series.id,
            series_name=self.series.name,
            canonical=self._canonical(),
            overall_confidence="medium",
            provider_confidence="low",
            series_name_hint=None,
            reason_flags=[],
        )
        self.db.commit()

        stale_seen_at = datetime.utcnow() - timedelta(days=7)
        first_row = get_unresolved_candidate_notifications(self.db, "robbie")[0]
        first_row.last_seen_at = stale_seen_at
        self.db.commit()

        refreshed = create_or_refresh_candidate_notification(
            self.db,
            profile_id="robbie",
            series_id=self.series.id,
            series_name=self.series.name,
            canonical=self._canonical(source_url="https://example.com/desert-protocol-v2"),
            overall_confidence="medium",
            provider_confidence="low",
            series_name_hint=None,
            reason_flags=[],
        )
        self.db.commit()

        self.assertEqual(refreshed.id, first_row.id)
        self.assertGreater(refreshed.last_seen_at, stale_seen_at)
        self.assertEqual(len(get_unresolved_candidate_notifications(self.db, "robbie")), 1)

    def test_rediscovering_the_same_title_and_number_without_isbn_refreshes_in_place(self):
        # Provider titles for the same real book vary run to run -- this
        # confirms the fallback (title_key + candidate_number) cascade
        # works when isbn13 is absent on both sides, not just the isbn13
        # fast path above.
        create_or_refresh_candidate_notification(
            self.db,
            profile_id="robbie",
            series_id=self.series.id,
            series_name=self.series.name,
            canonical=self._canonical(isbn13=None),
            overall_confidence="medium",
            provider_confidence="low",
            series_name_hint=None,
            reason_flags=[],
        )
        self.db.commit()

        refreshed = create_or_refresh_candidate_notification(
            self.db,
            profile_id="robbie",
            series_id=self.series.id,
            series_name=self.series.name,
            canonical=self._canonical(isbn13=None),
            overall_confidence="medium",
            provider_confidence="low",
            series_name_hint=None,
            reason_flags=[],
        )
        self.db.commit()

        self.assertEqual(len(get_unresolved_candidate_notifications(self.db, "robbie")), 1)
        self.assertEqual(refreshed.candidate_title, "Desert Protocol")

    def test_do_not_add_suppresses_future_rediscovery_of_the_same_candidate(self):
        row = create_or_refresh_candidate_notification(
            self.db,
            profile_id="robbie",
            series_id=self.series.id,
            series_name=self.series.name,
            canonical=self._canonical(),
            overall_confidence="medium",
            provider_confidence="low",
            series_name_hint=None,
            reason_flags=[],
        )
        self.db.commit()

        self.assertTrue(resolve_do_not_add(self.db, profile_id="robbie", notification_id=row.id))
        self.assertEqual(get_unresolved_candidate_notifications(self.db, "robbie"), [])

        # Rediscovering the exact same candidate on a later run must NOT
        # create a brand-new row -- this is the bug the design chat
        # specifically flagged: dedupe alone (which only looks at
        # unresolved rows) would miss an ignored row and recreate it.
        again = create_or_refresh_candidate_notification(
            self.db,
            profile_id="robbie",
            series_id=self.series.id,
            series_name=self.series.name,
            canonical=self._canonical(),
            overall_confidence="medium",
            provider_confidence="low",
            series_name_hint=None,
            reason_flags=[],
        )
        self.db.commit()

        self.assertIsNone(again)
        self.assertEqual(get_unresolved_candidate_notifications(self.db, "robbie"), [])
        all_rows = self.db.query(models.SeriesCandidateNotification).filter(
            models.SeriesCandidateNotification.series_id == self.series.id
        ).all()
        self.assertEqual(len(all_rows), 1)
        self.assertEqual(all_rows[0].resolution, "ignored")

    def test_add_to_series_persists_a_book_row_and_resolves_the_notification(self):
        row = create_or_refresh_candidate_notification(
            self.db,
            profile_id="robbie",
            series_id=self.series.id,
            series_name=self.series.name,
            canonical=self._canonical(asin="B0EXAMPLE1"),
            overall_confidence="medium",
            provider_confidence="low",
            series_name_hint=None,
            reason_flags=[],
        )
        self.db.commit()

        book = resolve_add_to_series(self.db, profile_id="robbie", notification_id=row.id)

        self.assertIsNotNone(book)
        self.assertEqual(book.title, "Desert Protocol")
        self.assertEqual(book.series_id, self.series.id)
        self.assertEqual(book.book_number, 7.0)
        self.assertEqual(book.isbn13, "9781111111111")
        self.assertEqual(book.asin, "B0EXAMPLE1")
        self.assertEqual(book.metadata_source, "discovery")
        self.assertEqual(book.record_status, "active")

        self.db.refresh(row)
        self.assertEqual(row.resolution, "added")
        self.assertIsNotNone(row.resolved_at)
        self.assertEqual(get_unresolved_candidate_notifications(self.db, "robbie"), [])

        # The series' durable skeleton must reflect the newly-owned book
        # right away (resolve_add_to_series calls backfill_skeleton_for_
        # series itself), not wait for the next Check Now.
        skeleton = (
            self.db.query(models.SeriesSkeleton)
            .filter(models.SeriesSkeleton.series_id == self.series.id)
            .first()
        )
        self.assertIsNotNone(skeleton)
        entry = next((e for e in skeleton.skeleton_json if e.get("book_number") == 7.0), None)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.get("status"), "confirmed")
        self.assertEqual(entry.get("source_class"), "library")

    def test_add_to_series_is_a_noop_for_an_already_resolved_notification(self):
        row = create_or_refresh_candidate_notification(
            self.db,
            profile_id="robbie",
            series_id=self.series.id,
            series_name=self.series.name,
            canonical=self._canonical(),
            overall_confidence="medium",
            provider_confidence="low",
            series_name_hint=None,
            reason_flags=[],
        )
        self.db.commit()
        self.assertTrue(resolve_do_not_add(self.db, profile_id="robbie", notification_id=row.id))

        book = resolve_add_to_series(self.db, profile_id="robbie", notification_id=row.id)
        self.assertIsNone(book)
        self.assertEqual(self.db.query(models.Book).count(), 0)

    def test_build_review_urls_includes_optional_asin_lookup_only_when_present(self):
        with_asin = create_or_refresh_candidate_notification(
            self.db,
            profile_id="robbie",
            series_id=self.series.id,
            series_name=self.series.name,
            canonical=self._canonical(asin="B0EXAMPLE1"),
            overall_confidence="medium",
            provider_confidence="low",
            series_name_hint=None,
            reason_flags=[],
        )
        self.db.commit()
        urls = build_review_urls(with_asin)
        self.assertIn("amazon.com", urls["amazon_ku_search"])
        self.assertIn("google.com", urls["google_search"])
        self.assertEqual(urls["asin_lookup"], "https://www.amazon.com/dp/B0EXAMPLE1")

        resolve_do_not_add(self.db, profile_id="robbie", notification_id=with_asin.id)
        without_asin = create_or_refresh_candidate_notification(
            self.db,
            profile_id="robbie",
            series_id=self.series.id,
            series_name=self.series.name,
            canonical=self._canonical(asin=None, isbn13="9782222222222", title="A Different Book"),
            overall_confidence="medium",
            provider_confidence="low",
            series_name_hint=None,
            reason_flags=[],
        )
        self.db.commit()
        urls = build_review_urls(without_asin)
        self.assertIsNone(urls["asin_lookup"])

    def test_notifications_are_profile_scoped(self):
        other_series = models.Series(name="Other Series", author="Other Author", profile_id="other")
        self.db.add(other_series)
        self.db.commit()
        self.db.refresh(other_series)

        create_or_refresh_candidate_notification(
            self.db,
            profile_id="robbie",
            series_id=self.series.id,
            series_name=self.series.name,
            canonical=self._canonical(),
            overall_confidence="medium",
            provider_confidence="low",
            series_name_hint=None,
            reason_flags=[],
        )
        create_or_refresh_candidate_notification(
            self.db,
            profile_id="other",
            series_id=other_series.id,
            series_name=other_series.name,
            canonical=self._canonical(isbn13="9783333333333", title="Other Book"),
            overall_confidence="medium",
            provider_confidence="low",
            series_name_hint=None,
            reason_flags=[],
        )
        self.db.commit()

        self.assertEqual(len(get_unresolved_candidate_notifications(self.db, "robbie")), 1)
        self.assertEqual(len(get_unresolved_candidate_notifications(self.db, "other")), 1)


class CandidateNotificationEndpointTest(unittest.TestCase):
    """Router-level wiring only -- mirrors NotificationEndpointTest's
    pattern in tests/test_notifications.py.
    """

    def setUp(self):
        self.client = TestClient(main.app)
        self.token = create_owner_token()

    def test_list_requires_reader_auth(self):
        response = self.client.get("/notifications/candidates")
        self.assertEqual(response.status_code, 401)

    def test_add_requires_owner_auth(self):
        response = self.client.post("/notifications/candidates/1/add")
        self.assertEqual(response.status_code, 403)

    def test_ignore_requires_owner_auth(self):
        response = self.client.post("/notifications/candidates/1/ignore")
        self.assertEqual(response.status_code, 403)

    def test_list_returns_an_empty_list_when_authenticated(self):
        with patch("routers.candidate_notifications.get_unresolved_candidate_notifications", return_value=[]):
            response = self.client.get(
                "/notifications/candidates", headers={"Authorization": f"Bearer {self.token}"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_add_returns_404_when_not_found(self):
        with patch("routers.candidate_notifications.resolve_add_to_series", return_value=None):
            response = self.client.post(
                "/notifications/candidates/999999/add", headers={"Authorization": f"Bearer {self.token}"}
            )
        self.assertEqual(response.status_code, 404)

    def test_add_returns_book_details_when_found(self):
        from types import SimpleNamespace

        fake_book = SimpleNamespace(id=5, series_id=9, title="Desert Protocol")
        with patch("routers.candidate_notifications.resolve_add_to_series", return_value=fake_book):
            response = self.client.post(
                "/notifications/candidates/1/add", headers={"Authorization": f"Bearer {self.token}"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"book_id": 5, "series_id": 9, "title": "Desert Protocol"})

    def test_ignore_returns_404_when_not_found(self):
        with patch("routers.candidate_notifications.resolve_do_not_add", return_value=False):
            response = self.client.post(
                "/notifications/candidates/999999/ignore", headers={"Authorization": f"Bearer {self.token}"}
            )
        self.assertEqual(response.status_code, 404)

    def test_ignore_returns_success_when_found(self):
        with patch("routers.candidate_notifications.resolve_do_not_add", return_value=True):
            response = self.client.post(
                "/notifications/candidates/42/ignore", headers={"Authorization": f"Bearer {self.token}"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["resolved"], True)


if __name__ == "__main__":
    unittest.main()
