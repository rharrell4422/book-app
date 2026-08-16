"""HTTP-level coverage for the /admin/ghost_profile_books (read-only report)
and /admin/repair_ghost_profile_books (fix in place) endpoints.

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
from models import Book, Profile, Series
from routers.deps import create_owner_token, get_db


class GhostProfileBooksAdminTest(unittest.TestCase):
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
                Profile(id="mackenzie", display_name="Mackenzie's Library", is_default=False),
            ]
        )
        series = Series(name="The Empyrean", author="Rebecca Yarros", profile_id="mackenzie")
        seed.add(series)
        seed.flush()
        # A correctly-scoped book (should never show up as a ghost).
        seed.add(
            Book(
                title="Fourth Wing",
                author="Rebecca Yarros",
                profile_id="mackenzie",
                series_id=series.id,
                book_number=1.0,
            )
        )
        # Ghost rows: linked to mackenzie's series but tagged profile_id="robbie".
        seed.add(
            Book(
                title="Iron Flame SIGNED",
                author="Rebecca Yarros",
                profile_id="robbie",
                series_id=series.id,
                book_number=2.0,
            )
        )
        seed.add(
            Book(
                title="The Empyrean Series",
                author="Rebecca Yarros",
                profile_id="robbie",
                series_id=series.id,
                book_number=None,
            )
        )
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
        self.owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}

    def tearDown(self):
        main.app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()
        os.remove(self.db_path)

    def test_list_rejects_anonymous_requests(self):
        response = self.client.get("/admin/ghost_profile_books")
        self.assertEqual(response.status_code, 403)

    def test_list_reports_ghost_books_without_changing_them(self):
        response = self.client.get("/admin/ghost_profile_books", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 2)
        titles = {entry["title"] for entry in data["entries"]}
        self.assertEqual(titles, {"Iron Flame SIGNED", "The Empyrean Series"})
        for entry in data["entries"]:
            self.assertEqual(entry["current_profile_id"], "robbie")
            self.assertEqual(entry["correct_profile_id"], "mackenzie")

        db = self.SessionLocal()
        try:
            unchanged = db.query(Book).filter(Book.title == "Iron Flame SIGNED").first()
            self.assertEqual(unchanged.profile_id, "robbie")
        finally:
            db.close()

    def test_repair_reassigns_ghost_books_to_series_profile(self):
        response = self.client.post("/admin/repair_ghost_profile_books", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["repaired_count"], 2)

        db = self.SessionLocal()
        try:
            fixed = db.query(Book).filter(Book.title == "Iron Flame SIGNED").first()
            self.assertEqual(fixed.profile_id, "mackenzie")
            unaffected = db.query(Book).filter(Book.title == "Fourth Wing").first()
            self.assertEqual(unaffected.profile_id, "mackenzie")
        finally:
            db.close()

        # Re-running is a no-op once everything's fixed.
        second_response = self.client.get("/admin/ghost_profile_books", headers=self.owner_headers)
        self.assertEqual(second_response.json()["count"], 0)

    def test_repair_rejects_anonymous_requests(self):
        response = self.client.post("/admin/repair_ghost_profile_books")
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
