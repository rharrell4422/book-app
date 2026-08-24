"""Discovery Health Indicator (Auto Discovery MVP spec, §1)."""

import os
import tempfile
import unittest
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
from database import Base
from models import Profile, Series
from routers.deps import create_owner_token, get_db
from services.discovery_health import compute_discovery_health


class ComputeDiscoveryHealthTest(unittest.TestCase):
    def setUp(self):
        self.today = date(2026, 8, 20)

    def test_never_checked_when_last_checked_is_none(self):
        self.assertEqual(compute_discovery_health(None, is_finished=False, today=self.today), "never_checked")

    def test_healthy_within_six_months(self):
        recent = self.today - timedelta(days=30)
        self.assertEqual(compute_discovery_health(recent, is_finished=False, today=self.today), "healthy")

    def test_healthy_at_exactly_the_six_month_boundary(self):
        boundary = self.today - timedelta(days=183)
        self.assertEqual(compute_discovery_health(boundary, is_finished=False, today=self.today), "healthy")

    def test_stale_between_six_and_twelve_months(self):
        stale = self.today - timedelta(days=200)
        self.assertEqual(compute_discovery_health(stale, is_finished=False, today=self.today), "stale")

    def test_very_stale_beyond_twelve_months(self):
        very_stale = self.today - timedelta(days=400)
        self.assertEqual(compute_discovery_health(very_stale, is_finished=False, today=self.today), "very_stale")

    def test_a_future_last_checked_date_is_treated_as_healthy_not_negative(self):
        future = self.today + timedelta(days=5)
        self.assertEqual(compute_discovery_health(future, is_finished=False, today=self.today), "healthy")


class SeriesDiscoveryHealthPropertyTest(unittest.TestCase):
    """Series.discovery_health is a plain @property (see models.py) --
    confirms it's wired up and reachable off a real ORM instance, not just
    the pure function above.
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

    def test_never_checked_series_reports_never_checked(self):
        series = Series(name="Untouched Series", author="Someone", profile_id="robbie", last_checked=None)
        self.db.add(series)
        self.db.commit()
        self.assertEqual(series.discovery_health, "never_checked")

    def test_recently_checked_series_reports_healthy(self):
        series = Series(name="Fresh Series", author="Someone", profile_id="robbie", last_checked=date.today())
        self.db.add(series)
        self.db.commit()
        self.assertEqual(series.discovery_health, "healthy")


class DiscoveryHealthApiExposureTest(unittest.TestCase):
    """last_checked/discovery_health must actually reach the frontend on
    every Series list/detail response, per the spec -- not just exist as a
    model property nobody serializes.
    """

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        seed = self.SessionLocal()
        seed.add(Profile(id="robbie", display_name="Robbie's Library", is_default=True))
        series = Series(name="Mistborn", author="Brandon Sanderson", profile_id="robbie", last_checked=None)
        seed.add(series)
        seed.commit()
        self.series_id = series.id
        seed.close()

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        main.app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(main.app)
        self.owner_headers = {"Authorization": f"Bearer {create_owner_token()}", "X-Profile-Id": "robbie"}

    def tearDown(self):
        main.app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()
        os.remove(self.db_path)

    def test_series_light_exposes_discovery_health(self):
        response = self.client.get("/series/light", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        row = response.json()[0]
        self.assertIsNone(row["last_checked"])
        self.assertEqual(row["discovery_health"], "never_checked")

    def test_series_detail_exposes_discovery_health(self):
        response = self.client.get(f"/series/{self.series_id}", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["last_checked"])
        self.assertEqual(body["discovery_health"], "never_checked")

    def test_full_series_list_exposes_discovery_health(self):
        response = self.client.get("/series/", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        row = response.json()[0]
        self.assertEqual(row["discovery_health"], "never_checked")


if __name__ == "__main__":
    unittest.main()
