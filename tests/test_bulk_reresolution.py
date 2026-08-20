"""Regression coverage for Phase 7 of the Add Book metadata intake redesign
(bulk re-resolution -- see services/bulk_reresolution.py's module docstring
and services/metadata_provenance.py's bind-time provenance rules, which
this reuses rather than re-deriving.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
import models
from database import Base
from routers.deps import create_owner_token
from services.bulk_reresolution import bulk_reresolve, count_eligible_books


def _make_candidate(confidence: str, title: str = "Resolved Title", isbn13: str | None = "9780000000001"):
    return {
        "candidate_id": f"cand-{confidence}",
        "title": title,
        "author": "Some Author",
        "authors": ["Some Author"],
        "isbn13": isbn13,
        "description": None,
        "source_url": "https://example.com/book",
        "published_date": None,
        "providers": ["google_books"],
        "confidence": confidence,
        "signals": {"author_match": True, "isbn_present": bool(isbn13), "strong_title_match": True},
    }


class BulkReresolutionServiceTest(unittest.TestCase):
    # A fresh in-memory engine per test (rather than setUpClass) since
    # count_eligible_books' assertions below are global per-profile counts
    # -- sharing one engine across tests would leak rows between them.
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def _make_book(self, **overrides) -> models.Book:
        defaults = dict(profile_id="robbie", title="Some Title", author="Some Author", metadata_source=None)
        defaults.update(overrides)
        book = models.Book(**defaults)
        self.db.add(book)
        self.db.commit()
        self.db.refresh(book)
        return book

    def test_never_verified_rows_are_eligible(self):
        for source in (None, "user", "import"):
            self._make_book(title=f"Book {source}", metadata_source=source)
        self.assertEqual(count_eligible_books(self.db, "robbie"), 3)

    def test_discovery_and_confident_provider_rows_are_not_eligible(self):
        self._make_book(title="Discovered", metadata_source="discovery")
        self._make_book(title="Confidently Bound", metadata_source="provider", needs_reresolution=False)
        self.assertEqual(count_eligible_books(self.db, "robbie"), 0)

    def test_low_confidence_provider_row_is_eligible(self):
        self._make_book(title="Weakly Bound", metadata_source="provider", needs_reresolution=True)
        self.assertEqual(count_eligible_books(self.db, "robbie"), 1)

    def test_deleted_rows_are_excluded(self):
        self._make_book(title="Deleted Book", metadata_source="user", record_status="deleted")
        self.assertEqual(count_eligible_books(self.db, "robbie"), 0)

    def test_high_confidence_match_updates_row_and_clears_flag(self):
        book = self._make_book(title="Fourth Wing", metadata_source="user")
        with patch(
            "services.bulk_reresolution.find_book_candidates",
            return_value={"candidates": [_make_candidate("high", title="Fourth Wing")]},
        ):
            summary = bulk_reresolve(self.db, "robbie")

        self.db.refresh(book)
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(summary["remaining"], 0)
        self.assertEqual(book.metadata_source, "provider")
        self.assertFalse(book.needs_reresolution)
        self.assertEqual(book.canonical_title, "Fourth Wing")
        self.assertEqual(book.isbn13, "9780000000001")

    def test_low_confidence_match_stays_eligible_for_next_pass(self):
        book = self._make_book(title="Ambiguous Book", metadata_source="import")
        with patch(
            "services.bulk_reresolution.find_book_candidates",
            return_value={"candidates": [_make_candidate("low")]},
        ):
            summary = bulk_reresolve(self.db, "robbie")

        self.db.refresh(book)
        self.assertEqual(summary["updated"], 1)
        # Still provider-sourced (a low-confidence bind is genuinely a real
        # catalog match, not down-weighted or excluded) but flagged, so it
        # remains in the eligible queue for a future pass.
        self.assertEqual(book.metadata_source, "provider")
        self.assertTrue(book.needs_reresolution)
        self.assertEqual(summary["remaining"], 1)

    def test_no_candidates_leaves_row_untouched(self):
        book = self._make_book(title="Obscure Book", metadata_source="user")
        with patch("services.bulk_reresolution.find_book_candidates", return_value={"candidates": []}):
            summary = bulk_reresolve(self.db, "robbie")

        self.db.refresh(book)
        self.assertEqual(summary["no_match"], 1)
        self.assertEqual(book.metadata_source, "user")
        self.assertEqual(summary["remaining"], 1)

    def test_one_row_erroring_does_not_prevent_others_from_processing(self):
        self._make_book(title="Will Error", metadata_source="user")
        good_book = self._make_book(title="Will Succeed", metadata_source="user")

        def fake_find(title, *args, **kwargs):
            if title == "Will Error":
                raise RuntimeError("provider timeout")
            return {"candidates": [_make_candidate("high", title=title)]}

        with patch("services.bulk_reresolution.find_book_candidates", side_effect=fake_find):
            summary = bulk_reresolve(self.db, "robbie")

        self.db.refresh(good_book)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(good_book.metadata_source, "provider")

    def test_limit_caps_rows_processed_per_call(self):
        for i in range(5):
            self._make_book(title=f"Book {i}", metadata_source="user")

        with patch(
            "services.bulk_reresolution.find_book_candidates",
            return_value={"candidates": [_make_candidate("high")]},
        ):
            summary = bulk_reresolve(self.db, "robbie", limit=2)

        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["remaining"], 3)

    def test_profiles_are_isolated(self):
        self._make_book(profile_id="robbie", title="Robbie Book", metadata_source="user")
        self._make_book(profile_id="daughter", title="Daughter Book", metadata_source="user")
        self.assertEqual(count_eligible_books(self.db, "robbie"), 1)
        self.assertEqual(count_eligible_books(self.db, "daughter"), 1)


class BulkReresolveEndpointAuthTest(unittest.TestCase):
    """Router-level wiring only -- exercised against the real app's DB
    session dependency, so every case here mocks the service layer to
    avoid ever touching the actual library data.
    """

    def setUp(self):
        self.client = TestClient(main.app)
        self.token = create_owner_token()

    def test_requires_owner_auth(self):
        # POST routes go through require_owner (not require_reader) --
        # unauthenticated is still rejected, but with 403 rather than the
        # 401 a GET-only route like /books/find returns.
        response = self.client.post("/books/bulk_reresolve")
        self.assertEqual(response.status_code, 403)

    def test_owner_can_trigger_and_limit_is_forwarded(self):
        with patch(
            "routers.books.bulk_reresolve",
            return_value={"processed": 0, "updated": 0, "no_match": 0, "errors": 0, "remaining": 0, "results": []},
        ) as mock_bulk:
            response = self.client.post(
                "/books/bulk_reresolve",
                headers={"Authorization": f"Bearer {self.token}"},
                params={"limit": 5},
            )

        self.assertEqual(response.status_code, 200)
        args, kwargs = mock_bulk.call_args
        self.assertEqual(kwargs.get("limit", args[-1] if args else None), 5)

    def test_invalid_limit_is_rejected(self):
        response = self.client.post(
            "/books/bulk_reresolve",
            headers={"Authorization": f"Bearer {self.token}"},
            params={"limit": 0},
        )
        self.assertEqual(response.status_code, 422)

    def test_queue_count_requires_reader_auth(self):
        response = self.client.get("/books/reresolution_queue_count")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
