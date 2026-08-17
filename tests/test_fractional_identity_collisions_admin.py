"""HTTP-level coverage for the /admin/fractional_identity_collisions
(read-only, narrow diagnostic) endpoint.

Unlike /admin/deleted_books, this only reports soft-deletes caused by the
fixed services/identity.py truncation bug -- i.e. a soft-deleted book whose
book_number collides, under the old int(float()) truncation, with a
different-numbered sibling in the same series. Ordinary/legitimate
duplicate collapses (same book_number, or no book_number) must not show up
here.

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


class FractionalIdentityCollisionsAdminTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        seed = self.SessionLocal()
        seed.add(Profile(id="robbie", display_name="Robbie's Library", is_default=True))

        collided_series = Series(name="The Empyrean", author="Rebecca Yarros", profile_id="robbie")
        clean_series = Series(name="Some Other Series", author="Someone Else", profile_id="robbie")
        seed.add_all([collided_series, clean_series])
        seed.flush()

        # Collision: book 3 wrongly soft-deleted when 3.5 was discovered
        # (old code: int(float(3.5)) == int(float(3)) == 3).
        seed.add(
            Book(
                title="Onyx Storm",
                author="Rebecca Yarros",
                profile_id="robbie",
                series_id=collided_series.id,
                book_number=3.0,
                is_read=True,
                read_status="read",
                record_status="deleted",
            )
        )
        seed.add(
            Book(
                title="Threshing Day",
                author="Rebecca Yarros",
                profile_id="robbie",
                series_id=collided_series.id,
                book_number=3.5,
                record_status="active",
            )
        )
        seed.add(
            Book(
                title="Fourth Wing",
                author="Rebecca Yarros",
                profile_id="robbie",
                series_id=collided_series.id,
                book_number=1.0,
                record_status="active",
            )
        )

        # Not a collision: ordinary duplicate with the SAME book_number,
        # correctly collapsed -- must not appear in the report.
        seed.add(
            Book(
                title="Some Book (Duplicate Import)",
                author="Someone Else",
                profile_id="robbie",
                series_id=clean_series.id,
                book_number=1.0,
                record_status="deleted",
            )
        )
        seed.add(
            Book(
                title="Some Book",
                author="Someone Else",
                profile_id="robbie",
                series_id=clean_series.id,
                book_number=1.0,
                record_status="active",
            )
        )
        seed.commit()
        self.collided_series_id = collided_series.id
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

    def test_rejects_anonymous_requests(self):
        response = self.client.get("/admin/fractional_identity_collisions")
        self.assertEqual(response.status_code, 403)

    def test_reports_only_the_fractional_collision_group(self):
        response = self.client.get("/admin/fractional_identity_collisions", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["count"], 1)

        entry = data["entries"][0]
        self.assertEqual(entry["series_id"], self.collided_series_id)
        self.assertEqual(entry["series_name"], "The Empyrean")
        self.assertEqual(entry["collided_truncated_number"], 3)

        titles_by_status = {m["title"]: m["record_status"] for m in entry["members"]}
        self.assertEqual(titles_by_status["Onyx Storm"], "deleted")
        self.assertEqual(titles_by_status["Threshing Day"], "active")
        self.assertNotIn("Fourth Wing", titles_by_status)

    def test_does_not_flag_ordinary_same_number_duplicate_collapse(self):
        response = self.client.get("/admin/fractional_identity_collisions", headers=self.owner_headers)
        data = response.json()
        series_ids_reported = {entry["series_id"] for entry in data["entries"]}
        self.assertNotIn(
            "Some Other Series",
            [entry["series_name"] for entry in data["entries"]],
        )
        self.assertEqual(len(series_ids_reported), 1)


if __name__ == "__main__":
    unittest.main()
