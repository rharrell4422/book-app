"""HTTP-level coverage for POST /series/{id}/verify -- the Two-Timestamp UI
Adjustments spec's (locked 2026-09-04) "Search Book Online" backend stamp.

Uses a private file-backed SQLite database instead of the real books.db,
overriding the FastAPI `get_db` dependency for the duration of each test,
mirroring tests/test_light_list_endpoints.py.
"""

import os
import tempfile
import unittest
from datetime import date

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
from database import Base
from models import Profile, Series
from routers.deps import create_owner_token, get_db


class SeriesVerifyEndpointTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        seed = self.SessionLocal()
        seed.add(Profile(id="robbie", display_name="Robbie's Library", is_default=True))
        series = Series(name="Mistborn", author="Brandon Sanderson", profile_id="robbie")
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

    def test_verify_stamps_last_verified_at_to_today_and_leaves_last_synced_at_alone(self):
        db = self.SessionLocal()
        series = db.query(Series).filter(Series.id == self.series_id).first()
        self.assertIsNone(series.last_verified_at)
        self.assertIsNone(series.last_synced_at)
        self.assertIsNone(series.last_checked)
        db.close()

        response = self.client.post(f"/series/{self.series_id}/verify", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["series_id"], self.series_id)
        self.assertEqual(body["last_verified_at"], date.today().isoformat())

        db = self.SessionLocal()
        series = db.query(Series).filter(Series.id == self.series_id).first()
        self.assertEqual(series.last_verified_at, date.today())
        # Unrelated timestamps must stay untouched -- verifying is a
        # deliberately separate signal from syncing/checking.
        self.assertIsNone(series.last_synced_at)
        self.assertIsNone(series.last_checked)
        db.close()

    def test_verify_is_reflected_on_series_detail_and_list_endpoints(self):
        self.client.post(f"/series/{self.series_id}/verify", headers=self.owner_headers)

        detail = self.client.get(f"/series/{self.series_id}", headers=self.owner_headers).json()
        self.assertEqual(detail["last_verified_at"], date.today().isoformat())
        self.assertIsNone(detail["last_synced_at"])

        listing = self.client.get("/series/", headers=self.owner_headers).json()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["last_verified_at"], date.today().isoformat())
        self.assertIsNone(listing[0]["last_synced_at"])

        light = self.client.get("/series/light", headers=self.owner_headers).json()
        self.assertEqual(len(light), 1)
        self.assertEqual(light[0]["last_verified_at"], date.today().isoformat())
        self.assertIsNone(light[0]["last_synced_at"])

    def test_verify_returns_404_for_missing_series(self):
        response = self.client.post("/series/999999/verify", headers=self.owner_headers)
        self.assertEqual(response.status_code, 404)

    def test_verify_is_idempotent_across_repeated_clicks(self):
        first = self.client.post(f"/series/{self.series_id}/verify", headers=self.owner_headers)
        second = self.client.post(f"/series/{self.series_id}/verify", headers=self.owner_headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["last_verified_at"], second.json()["last_verified_at"])


if __name__ == "__main__":
    unittest.main()
