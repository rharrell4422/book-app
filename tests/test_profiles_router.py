"""HTTP-level coverage for routers/profiles.py: listing (with book_count/
has_data), creating, and renaming profiles.

Uses a private file-backed SQLite database instead of the real books.db,
overriding the FastAPI `get_db` dependency for the duration of each test.
"""

import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
from database import Base
from models import Book, Profile
from routers.deps import create_owner_token, get_db


class ProfilesRouterTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        seed = self.SessionLocal()
        seed.add_all(
            [
                Profile(id="robbie", display_name="Robbie's Library", is_default=True),
                Profile(id="daughter", display_name="Daughter's Library", is_default=False),
            ]
        )
        seed.add(Book(title="Robbie Book", author="A", profile_id="robbie"))
        seed.commit()
        seed.close()

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        main.app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(main.app)
        self.owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}

    def tearDown(self):
        main.app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()
        os.remove(self.db_path)

    def test_list_profiles_reports_book_count_and_has_data(self):
        response = self.client.get("/profiles", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        by_id = {p["id"]: p for p in response.json()}

        self.assertEqual(by_id["robbie"]["book_count"], 1)
        self.assertTrue(by_id["robbie"]["has_data"])
        self.assertEqual(by_id["daughter"]["book_count"], 0)
        self.assertFalse(by_id["daughter"]["has_data"])

    def test_rename_requires_owner_auth(self):
        response = self.client.patch("/profiles/daughter", json={"display_name": "New Name"})
        self.assertEqual(response.status_code, 403)

    def test_rename_updates_display_name_without_touching_the_id_or_data(self):
        response = self.client.patch(
            "/profiles/daughter",
            headers=self.owner_headers,
            json={"display_name": "  Emma's Library  "},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], "daughter")
        self.assertEqual(body["display_name"], "Emma's Library")

        listing = self.client.get("/profiles", headers=self.owner_headers).json()
        renamed = next(p for p in listing if p["id"] == "daughter")
        self.assertEqual(renamed["display_name"], "Emma's Library")

    def test_rename_rejects_blank_name(self):
        response = self.client.patch(
            "/profiles/daughter", headers=self.owner_headers, json={"display_name": "   "}
        )
        self.assertEqual(response.status_code, 422)

    def test_rename_rejects_unknown_profile(self):
        response = self.client.patch(
            "/profiles/nonexistent", headers=self.owner_headers, json={"display_name": "Whatever"}
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
