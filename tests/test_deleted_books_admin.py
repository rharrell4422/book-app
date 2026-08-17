"""HTTP-level coverage for the /admin/deleted_books (read-only report) and
/admin/restore_book/{id} (undo a soft-delete) endpoints.

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


class DeletedBooksAdminTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        seed = self.SessionLocal()
        seed.add(Profile(id="robbie", display_name="Robbie's Library", is_default=True))
        series = Series(name="The Empyrean", author="Rebecca Yarros", profile_id="robbie")
        seed.add(series)
        seed.flush()
        seed.add(
            Book(
                title="Onyx Storm",
                author="Rebecca Yarros",
                profile_id="robbie",
                series_id=series.id,
                book_number=3.0,
                is_read=True,
                read_status="read",
                record_status="deleted",
            )
        )
        seed.add(
            Book(
                title="Fourth Wing",
                author="Rebecca Yarros",
                profile_id="robbie",
                series_id=series.id,
                book_number=1.0,
                record_status="active",
            )
        )
        seed.commit()
        self.series_id = series.id
        self.deleted_book_id = seed.query(Book).filter(Book.title == "Onyx Storm").first().id
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
        response = self.client.get("/admin/deleted_books")
        self.assertEqual(response.status_code, 403)

    def test_list_reports_only_soft_deleted_books(self):
        response = self.client.get("/admin/deleted_books", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["entries"][0]["title"], "Onyx Storm")
        self.assertEqual(data["entries"][0]["series_name"], "The Empyrean")
        self.assertTrue(data["entries"][0]["is_read"])

    def test_list_can_filter_by_series_id(self):
        response = self.client.get(
            f"/admin/deleted_books?series_id={self.series_id}", headers=self.owner_headers
        )
        self.assertEqual(response.json()["count"], 1)

        response = self.client.get("/admin/deleted_books?series_id=999999", headers=self.owner_headers)
        self.assertEqual(response.json()["count"], 0)

    def test_restore_sets_record_status_active(self):
        response = self.client.post(
            f"/admin/restore_book/{self.deleted_book_id}", headers=self.owner_headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "restored")

        db = self.SessionLocal()
        try:
            restored = db.query(Book).filter(Book.id == self.deleted_book_id).first()
            self.assertEqual(restored.record_status, "active")
            self.assertTrue(restored.is_read)
        finally:
            db.close()

        # Restoring doesn't touch other rows or fields.
        self.assertEqual(
            self.client.get("/admin/deleted_books", headers=self.owner_headers).json()["count"], 0
        )

    def test_restore_rejects_anonymous_requests(self):
        response = self.client.post(f"/admin/restore_book/{self.deleted_book_id}")
        self.assertEqual(response.status_code, 403)

    def test_restore_unknown_id_reports_not_found_status(self):
        response = self.client.post("/admin/restore_book/999999", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "not_found")


if __name__ == "__main__":
    unittest.main()
