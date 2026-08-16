"""Coverage for the local-dev-only AUTH_DISABLED bypass (routers/deps.py).

The autouse fixture in tests/conftest.py forces AUTH_DISABLED off for every
test by default, so the first test here is really asserting that fixture
is doing its job (i.e. a developer's local .env can safely set
AUTH_DISABLED=true without silently defeating this whole suite's auth
assertions). The rest of the tests explicitly opt back in to prove the
bypass itself works when it's on.
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
from routers.deps import get_db


class AuthDisabledTest(unittest.TestCase):
    def setUp(self):
        # A private file-backed SQLite database, not the real books.db --
        # test_write_request_also_succeeds_when_auth_disabled actually
        # creates a book, which must never land in real library data.
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_engine(f"sqlite:///{self.db_path}", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

        seed = self.SessionLocal()
        seed.add(Profile(id="robbie", display_name="Robbie's Library", is_default=True))
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

    def tearDown(self):
        main.app.dependency_overrides.pop(get_db, None)
        self.engine.dispose()
        os.remove(self.db_path)

    def test_unauthenticated_request_is_rejected_by_default(self):
        # AUTH_DISABLED is forced off by the autouse conftest fixture, so
        # this should behave exactly like production: no token, no access.
        self.assertIsNone(os.environ.get("AUTH_DISABLED"))
        response = self.client.get("/books/")
        self.assertEqual(response.status_code, 401)

    def test_unauthenticated_request_succeeds_when_auth_disabled(self):
        os.environ["AUTH_DISABLED"] = "true"
        try:
            response = self.client.get("/books/")
        finally:
            os.environ.pop("AUTH_DISABLED", None)
        self.assertEqual(response.status_code, 200)

    def test_write_request_also_succeeds_when_auth_disabled(self):
        os.environ["AUTH_DISABLED"] = "true"
        try:
            response = self.client.post(
                "/books/", json={"title": "Bypassed Auth Book", "author": "Someone"}
            )
        finally:
            os.environ.pop("AUTH_DISABLED", None)
        self.assertEqual(response.status_code, 200)

    def test_bypass_accepts_common_truthy_spellings(self):
        for value in ("1", "true", "True", "yes", "YES"):
            with self.subTest(value=value):
                os.environ["AUTH_DISABLED"] = value
                try:
                    response = self.client.get("/books/")
                finally:
                    os.environ.pop("AUTH_DISABLED", None)
                self.assertEqual(response.status_code, 200)

    def test_bypass_ignores_falsy_or_blank_values(self):
        for value in ("", "0", "false"):
            with self.subTest(value=value):
                os.environ["AUTH_DISABLED"] = value
                try:
                    response = self.client.get("/books/")
                finally:
                    os.environ.pop("AUTH_DISABLED", None)
                self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
