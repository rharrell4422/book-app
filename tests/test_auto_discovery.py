"""Auto Discovery MVP: eligibility filter (§2) and the rate-limited batch
button's job runner + endpoints (§4).
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import models
import services.auto_discovery as auto_discovery
from database import Base
from routers.deps import create_owner_token
from services.auto_discovery import (
    AUTO_DISCOVERY_COOLDOWN,
    cooldown_remaining_seconds,
    discovery_batch_jobs,
    get_eligible_series,
    is_series_eligible_for_auto_discovery,
    run_full_auto_discovery_job,
)


def _make_eligible_series(**overrides) -> models.Series:
    defaults = dict(
        name="Clean Series",
        author="Real Author",
        is_finished=False,
        is_caught_up=True,
        has_unread_books=False,
        has_upcoming_books=False,
        missing_books=[],
    )
    defaults.update(overrides)
    return models.Series(**defaults)


class IsSeriesEligibleForAutoDiscoveryTest(unittest.TestCase):
    def test_clean_series_is_eligible(self):
        self.assertTrue(is_series_eligible_for_auto_discovery(_make_eligible_series()))

    def test_finished_series_is_not_eligible(self):
        self.assertFalse(is_series_eligible_for_auto_discovery(_make_eligible_series(is_finished=True)))

    def test_not_caught_up_series_is_not_eligible(self):
        self.assertFalse(is_series_eligible_for_auto_discovery(_make_eligible_series(is_caught_up=False)))

    def test_series_with_unread_books_is_not_eligible(self):
        self.assertFalse(is_series_eligible_for_auto_discovery(_make_eligible_series(has_unread_books=True)))

    def test_series_with_upcoming_books_is_not_eligible(self):
        self.assertFalse(is_series_eligible_for_auto_discovery(_make_eligible_series(has_upcoming_books=True)))

    def test_series_with_missing_books_is_not_eligible(self):
        self.assertFalse(is_series_eligible_for_auto_discovery(_make_eligible_series(missing_books=[4])))

    def test_series_with_no_author_is_not_eligible(self):
        self.assertFalse(is_series_eligible_for_auto_discovery(_make_eligible_series(author=None)))
        self.assertFalse(is_series_eligible_for_auto_discovery(_make_eligible_series(author="  ")))

    def test_series_with_placeholder_author_is_not_eligible(self):
        for placeholder in ("Unknown", "N/A", "Unknown Author"):
            self.assertFalse(
                is_series_eligible_for_auto_discovery(_make_eligible_series(author=placeholder)),
                msg=f"placeholder author '{placeholder}' should not be eligible",
            )


class ActiveBookScopingTest(unittest.TestCase):
    """needs_reresolution must only be checked against active (non-deleted)
    books -- spec §2's explicit correction over an earlier draft that would
    have queried all Book rows.
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

    def _persist_eligible_series_with_book(self, **book_overrides):
        series = _make_eligible_series()
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)

        book_defaults = dict(profile_id=series.profile_id, title="Some Book", author="Real Author", series_id=series.id)
        book_defaults.update(book_overrides)
        book = models.Book(**book_defaults)
        self.db.add(book)
        self.db.commit()
        self.db.refresh(series)
        return series

    def test_active_book_needing_reresolution_blocks_eligibility(self):
        series = self._persist_eligible_series_with_book(needs_reresolution=True, record_status="active")
        self.assertFalse(is_series_eligible_for_auto_discovery(series))

    def test_soft_deleted_book_needing_reresolution_does_not_block_eligibility(self):
        series = self._persist_eligible_series_with_book(needs_reresolution=True, record_status="deleted")
        self.assertTrue(is_series_eligible_for_auto_discovery(series))

    def test_get_eligible_series_scopes_by_profile(self):
        series_a = _make_eligible_series(name="Series A", profile_id="robbie")
        series_b = _make_eligible_series(name="Series B", profile_id="other")
        self.db.add_all([series_a, series_b])
        self.db.commit()

        self.assertEqual([s.name for s in get_eligible_series(self.db, "robbie")], ["Series A"])
        self.assertEqual([s.name for s in get_eligible_series(self.db, "other")], ["Series B"])


class CooldownRemainingSecondsTest(unittest.TestCase):
    def test_no_prior_run_has_zero_cooldown(self):
        profile = models.Profile(id="robbie", display_name="Robbie", last_full_discovery_run_at=None)
        self.assertEqual(cooldown_remaining_seconds(profile), 0)

    def test_recent_run_has_remaining_cooldown(self):
        profile = models.Profile(
            id="robbie", display_name="Robbie", last_full_discovery_run_at=datetime.utcnow() - timedelta(days=1)
        )
        remaining = cooldown_remaining_seconds(profile)
        self.assertGreater(remaining, 0)
        self.assertLessEqual(remaining, AUTO_DISCOVERY_COOLDOWN.total_seconds())

    def test_run_older_than_cooldown_window_has_zero_remaining(self):
        profile = models.Profile(
            id="robbie", display_name="Robbie", last_full_discovery_run_at=datetime.utcnow() - timedelta(days=8)
        )
        self.assertEqual(cooldown_remaining_seconds(profile), 0)


class RunFullAutoDiscoveryJobTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()
        discovery_batch_jobs.clear()

        self.profile = models.Profile(id="robbie", display_name="Robbie")
        self.db.add(self.profile)
        series_one = _make_eligible_series(name="Series One")
        series_two = _make_eligible_series(name="Series Two")
        self.db.add_all([series_one, series_two])
        self.db.commit()
        self.db.refresh(series_one)
        self.db.refresh(series_two)
        self.series_ids = [series_one.id, series_two.id]

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()
        discovery_batch_jobs.clear()
        # series_check_jobs is a *global* module-level dict shared with the
        # real "Check Now" feature -- these series_ids are only meaningful
        # for this test's own in-memory DB, so any leftover entries for
        # them must not leak into other test files running in the same
        # process.
        for series_id in self.series_ids:
            auto_discovery.series_check_jobs.pop(series_id, None)

    def test_successful_sweep_stamps_cooldown_and_reports_new_books(self):
        def fake_check(series_id):
            auto_discovery.series_check_jobs[series_id] = {
                "status": "completed",
                "completion": {"new_books": [{"id": 1}] if series_id == self.series_ids[0] else []},
            }

        with patch.object(auto_discovery, "SessionLocal", self.SessionLocal), patch.object(
            auto_discovery, "run_series_check_job_full", side_effect=fake_check
        ):
            run_full_auto_discovery_job("robbie", "job-1", self.series_ids)

        self.db.refresh(self.profile)
        self.assertIsNotNone(self.profile.last_full_discovery_run_at)

        job = discovery_batch_jobs["robbie"]
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["completed"], 2)
        self.assertEqual(job["new_books_found"], 1)

    def test_series_already_running_via_check_now_is_skipped_not_double_run(self):
        auto_discovery.series_check_jobs[self.series_ids[0]] = {"status": "running"}

        with patch.object(auto_discovery, "SessionLocal", self.SessionLocal), patch.object(
            auto_discovery, "run_series_check_job_full"
        ) as mock_check:
            run_full_auto_discovery_job("robbie", "job-1", self.series_ids)

        # Only series_ids[1] should have actually been run.
        mock_check.assert_called_once_with(self.series_ids[1])
        job = discovery_batch_jobs["robbie"]
        self.assertEqual(job["results"][0]["outcome"], "skipped_already_running")

    def test_hard_crash_mid_sweep_does_not_stamp_cooldown(self):
        with patch.object(auto_discovery, "SessionLocal", self.SessionLocal), patch.object(
            auto_discovery, "run_series_check_job_full", side_effect=RuntimeError("boom")
        ):
            run_full_auto_discovery_job("robbie", "job-1", self.series_ids)

        self.db.refresh(self.profile)
        self.assertIsNone(self.profile.last_full_discovery_run_at)
        self.assertEqual(discovery_batch_jobs["robbie"]["status"], "completed")
        self.assertIn("error", discovery_batch_jobs["robbie"])

    def test_zero_eligible_series_still_completes_and_stamps_cooldown(self):
        with patch.object(auto_discovery, "SessionLocal", self.SessionLocal):
            run_full_auto_discovery_job("robbie", "job-1", [])

        self.db.refresh(self.profile)
        self.assertIsNotNone(self.profile.last_full_discovery_run_at)
        self.assertEqual(discovery_batch_jobs["robbie"]["completed"], 0)


class AutoDiscoveryEndpointTest(unittest.TestCase):
    """Router-level wiring, exercised through the real app's DB session
    dependency (a GET-only read for the profile row) -- every scenario that
    would actually mutate anything or run a real sweep mocks the service
    layer, same pattern as BulkReresolveEndpointAuthTest.
    """

    def setUp(self):
        self.client = TestClient(main.app)
        self.token = create_owner_token()
        discovery_batch_jobs.clear()

    def tearDown(self):
        discovery_batch_jobs.clear()

    def test_post_requires_owner_auth(self):
        response = self.client.post("/discovery/auto_run_mvp")
        self.assertEqual(response.status_code, 403)

    def test_get_status_requires_reader_auth(self):
        response = self.client.get("/discovery/auto_run_mvp/status", params={"job_id": "whatever"})
        self.assertEqual(response.status_code, 401)

    def test_already_running_guard_returns_running_without_starting_a_new_job(self):
        discovery_batch_jobs["robbie"] = {"job_id": "existing-job", "status": "running", "total": 3, "completed": 1}

        with patch("routers.discovery.run_full_auto_discovery_job") as mock_job:
            response = self.client.post("/discovery/auto_run_mvp", headers={"Authorization": f"Bearer {self.token}"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "running")
        self.assertEqual(body["job_id"], "existing-job")
        mock_job.assert_not_called()

    def test_cooldown_blocks_a_new_run(self):
        with patch("routers.discovery.cooldown_remaining_seconds", return_value=3600), patch(
            "routers.discovery.run_full_auto_discovery_job"
        ) as mock_job:
            response = self.client.post("/discovery/auto_run_mvp", headers={"Authorization": f"Bearer {self.token}"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "cooldown")
        self.assertEqual(body["remaining_seconds"], 3600)
        mock_job.assert_not_called()

    def test_starting_a_fresh_run_returns_started_with_job_id(self):
        with patch("routers.discovery.cooldown_remaining_seconds", return_value=0), patch(
            "routers.discovery.get_eligible_series", return_value=[]
        ), patch("routers.discovery.run_full_auto_discovery_job") as mock_job:
            response = self.client.post("/discovery/auto_run_mvp", headers={"Authorization": f"Bearer {self.token}"})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "started")
        self.assertIsNotNone(body["job_id"])
        self.assertEqual(body["total"], 0)
        mock_job.assert_called_once()

    def test_status_for_unknown_job_id_is_interrupted(self):
        response = self.client.get(
            "/discovery/auto_run_mvp/status",
            params={"job_id": "never-existed"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "interrupted")

    def test_status_mismatched_job_id_for_this_profile_is_interrupted(self):
        discovery_batch_jobs["robbie"] = {"job_id": "job-a", "status": "completed", "total": 1, "completed": 1}
        response = self.client.get(
            "/discovery/auto_run_mvp/status",
            params={"job_id": "job-b"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        self.assertEqual(response.json()["status"], "interrupted")

    def test_status_returns_current_job_state_when_job_id_matches(self):
        discovery_batch_jobs["robbie"] = {
            "job_id": "job-a",
            "status": "completed",
            "total": 2,
            "completed": 2,
            "new_books_found": 3,
            "results": [],
        }
        response = self.client.get(
            "/discovery/auto_run_mvp/status",
            params={"job_id": "job-a"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["new_books_found"], 3)

    def test_status_response_includes_discovery_delta_count(self):
        # CR-11 regression: the job dict has always carried this field (see
        # services/auto_discovery.py), but the status endpoint's response
        # never forwarded it -- the frontend polling this endpoint always
        # saw 0/null regardless of the job's actual value.
        discovery_batch_jobs["robbie"] = {
            "job_id": "job-a",
            "status": "completed",
            "total": 2,
            "completed": 2,
            "new_books_found": 3,
            "discovery_delta_count": 5,
            "results": [],
        }
        response = self.client.get(
            "/discovery/auto_run_mvp/status",
            params={"job_id": "job-a"},
            headers={"Authorization": f"Bearer {self.token}"},
        )
        body = response.json()
        self.assertEqual(body["discovery_delta_count"], 5)


if __name__ == "__main__":
    unittest.main()
