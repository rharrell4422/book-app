"""HTTP-level coverage for the onboarding upload endpoints added to
routers/imports.py: POST /import/preview, POST /import/upload, and
POST /import/reset_profile.

Uses a private file-backed SQLite database (not the real books.db) for
the FastAPI `get_db` dependency plus `importer.pipeline.SessionLocal` and
`importer.preview.SessionLocal` -- `run_import`/`preview_import` open
their own session internally, so a plain `:memory:` engine wouldn't be
visible across the two separately opened sessions the way a real
deployment's single shared engine is.
"""

import os
import tempfile
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
from database import Base
from models import Profile
from routers.deps import create_owner_token, get_db


class ImportUploadEndpointTest(unittest.TestCase):
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
        seed.commit()
        seed.close()

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        main.app.dependency_overrides[get_db] = override_get_db

        self.session_local_patchers = self._patch_importer_session_locals()
        for patcher in self.session_local_patchers:
            patcher.start()

        self.client = TestClient(main.app)
        self.token = create_owner_token()
        self.owner_headers = {"Authorization": f"Bearer {self.token}", "X-Profile-Id": "daughter"}

    def _patch_importer_session_locals(self):
        from unittest.mock import patch

        return [
            patch("importer.pipeline.SessionLocal", self.SessionLocal),
            patch("importer.preview.SessionLocal", self.SessionLocal),
        ]

    def tearDown(self):
        main.app.dependency_overrides.pop(get_db, None)
        for patcher in self.session_local_patchers:
            patcher.stop()
        self.engine.dispose()
        os.remove(self.db_path)

    def _csv_bytes(self, rows: list[str]) -> bytes:
        return ("\n".join(rows) + "\n").encode("utf-8")

    def test_preview_requires_owner_auth(self):
        response = self.client.post(
            "/import/preview",
            files={"file": ("books.csv", self._csv_bytes(["Title,Author", "A Book,Someone"]), "text/csv")},
        )
        self.assertEqual(response.status_code, 403)

    def test_preview_rejects_unsupported_file_type(self):
        response = self.client.post(
            "/import/preview",
            headers=self.owner_headers,
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        self.assertEqual(response.status_code, 422)

    def test_preview_parses_without_writing_to_the_database(self):
        response = self.client.post(
            "/import/preview",
            headers=self.owner_headers,
            files={
                "file": (
                    "books.csv",
                    self._csv_bytes(["Title,Author,Series", "Chronicles Book 3,Author One,"]),
                    "text/csv",
                )
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["row_count"], 1)
        self.assertEqual(body["valid_row_count"], 1)

        db = self.SessionLocal()
        try:
            from models import Book

            self.assertEqual(db.query(Book).count(), 0)
        finally:
            db.close()

    def test_upload_imports_books_scoped_to_the_active_profile(self):
        response = self.client.post(
            "/import/upload",
            headers=self.owner_headers,
            files={
                "file": (
                    "books.csv",
                    self._csv_bytes(
                        [
                            "Title,Author,Series",
                            "Chronicles Book 3,Author One,",
                            "Book Four,,",  # missing author -- should be skipped, not fatal
                        ]
                    ),
                    "text/csv",
                )
            },
        )
        self.assertEqual(response.status_code, 200)
        summary = response.json()["import_summary"]
        self.assertEqual(summary["imported_count"], 1)
        self.assertEqual(summary["failed_count"], 1)

        db = self.SessionLocal()
        try:
            from models import Book

            daughter_books = db.query(Book).filter(Book.profile_id == "daughter").all()
            self.assertEqual([b.title for b in daughter_books], ["Chronicles Book 3"])
            self.assertEqual(db.query(Book).filter(Book.profile_id == "robbie").count(), 0)
        finally:
            db.close()

    def test_reset_profile_only_clears_the_active_profiles_data(self):
        self.client.post(
            "/import/upload",
            headers=self.owner_headers,
            files={
                "file": (
                    "books.csv",
                    self._csv_bytes(["Title,Author", "Chronicles Book 3,Author One"]),
                    "text/csv",
                )
            },
        )
        self.client.post(
            "/import/upload",
            headers={**self.owner_headers, "X-Profile-Id": "robbie"},
            files={
                "file": (
                    "books.csv",
                    self._csv_bytes(["Title,Author", "Robbie Book Book 1,Robbie Author"]),
                    "text/csv",
                )
            },
        )

        reset_response = self.client.post("/import/reset_profile", headers=self.owner_headers)
        self.assertEqual(reset_response.status_code, 200)
        self.assertEqual(reset_response.json()["deleted_books"], 1)

        db = self.SessionLocal()
        try:
            from models import Book

            self.assertEqual(db.query(Book).filter(Book.profile_id == "daughter").count(), 0)
            self.assertEqual(db.query(Book).filter(Book.profile_id == "robbie").count(), 1)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
