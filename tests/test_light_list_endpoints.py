"""HTTP-level coverage for the slim, paginated list endpoints added for
mobile/list-view clients: GET /books/light and GET /series/light.

These are purely additive alongside the existing GET /books/ and
GET /series/ endpoints, so this suite also asserts the original endpoints
are completely unaffected (still unpaginated, still full-fidelity, still
nesting books under series).

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


class LightListEndpointsTest(unittest.TestCase):
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
        series = Series(name="Mistborn", author="Brandon Sanderson", profile_id="robbie", total_books=3)
        seed.add(series)
        seed.flush()
        for i in range(1, 6):
            seed.add(
                Book(
                    title=f"Robbie Book {i}",
                    author="Author",
                    profile_id="robbie",
                    series_id=series.id if i <= 3 else None,
                    book_number=float(i) if i <= 3 else None,
                )
            )
        seed.add(Book(title="Daughter Book", author="Author", profile_id="daughter"))
        seed.add(Series(name="Daughter Series", author="Someone", profile_id="daughter"))
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
        self.owner_headers = {"Authorization": f"Bearer {create_owner_token()}", "X-Profile-Id": "robbie"}

    def tearDown(self):
        main.app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()
        os.remove(self.db_path)

    # ---------------------------------------------------------------
    # /books/light
    # ---------------------------------------------------------------

    def test_books_light_returns_slim_fields_only(self):
        response = self.client.get("/books/light", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 5)
        row = body[0]
        self.assertEqual(
            set(row.keys()),
            {
                "id",
                "title",
                "author",
                "series_id",
                "series_name",
                "book_number",
                "read_status",
                "is_read",
                "is_upcoming_final",
                "rating",
            },
        )
        # Heavy/full-fidelity-only fields must not leak into the slim response.
        self.assertNotIn("auto_summary", row)
        self.assertNotIn("review", row)
        self.assertNotIn("notes", row)

    def test_books_light_paginates_with_limit_and_offset(self):
        page1 = self.client.get("/books/light?limit=2&offset=0", headers=self.owner_headers).json()
        page2 = self.client.get("/books/light?limit=2&offset=2", headers=self.owner_headers).json()
        page3 = self.client.get("/books/light?limit=2&offset=4", headers=self.owner_headers).json()

        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 2)
        self.assertEqual(len(page3), 1)

        all_ids = [row["id"] for row in page1 + page2 + page3]
        self.assertEqual(len(all_ids), len(set(all_ids)), "pages must not overlap or repeat rows")

    def test_books_light_rejects_out_of_range_limit(self):
        response = self.client.get("/books/light?limit=0", headers=self.owner_headers)
        self.assertEqual(response.status_code, 422)

        response = self.client.get("/books/light?limit=201", headers=self.owner_headers)
        self.assertEqual(response.status_code, 422)

    def test_books_light_rejects_negative_offset(self):
        response = self.client.get("/books/light?offset=-1", headers=self.owner_headers)
        self.assertEqual(response.status_code, 422)

    def test_books_light_is_scoped_to_the_active_profile(self):
        response = self.client.get(
            "/books/light", headers={"Authorization": f"Bearer {create_owner_token()}", "X-Profile-Id": "daughter"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["title"], "Daughter Book")

    def test_full_books_endpoint_is_unaffected_by_the_new_light_endpoint(self):
        response = self.client.get("/books/", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 5)
        # Full endpoint still returns every field, unpaginated.
        self.assertIn("auto_summary", body[0])
        self.assertIn("review", body[0])

    # ---------------------------------------------------------------
    # /series/light
    # ---------------------------------------------------------------

    def test_series_light_returns_slim_fields_without_nested_books(self):
        response = self.client.get("/series/light", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        row = body[0]
        self.assertEqual(row["name"], "Mistborn")
        self.assertNotIn("books", row)

    def test_series_light_paginates(self):
        response = self.client.get("/series/light?limit=1&offset=0", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 1)

        response = self.client.get("/series/light?limit=1&offset=1", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 0)

    def test_series_light_rejects_out_of_range_limit(self):
        response = self.client.get("/series/light?limit=500", headers=self.owner_headers)
        self.assertEqual(response.status_code, 422)

    def test_full_series_endpoint_still_nests_books(self):
        response = self.client.get("/series/", headers=self.owner_headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body), 1)
        self.assertIn("books", body[0])
        self.assertEqual(len(body[0]["books"]), 3)


if __name__ == "__main__":
    unittest.main()
