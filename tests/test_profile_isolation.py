"""Regression coverage for multi-profile library isolation (see
alembic/versions/a4f27c81de93_*.py and the profiles table in models.py).

Covers the three risk areas called out in the multi-profile plan:
  1. Plain CRUD scoping -- a profile can't list/read/edit/delete another
     profile's rows, and two profiles can independently use the same
     series name.
  2. The "link into another profile's series via a client-settable
     series_id" gap (crud.books.InvalidSeriesForProfileError).
  3. Discovery/dedup-by-author -- the highest-risk area for a silent
     cross-profile leak, since it matches by author name/title text
     rather than by id.
"""

import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import crud
import schemas
from agents.series_agent import discover_more_by_author
from crud.books import InvalidSeriesForProfileError
from database import Base
from models import Book, Profile, Series
from routers.deps import get_current_profile_id


def _new_in_memory_session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(autocommit=False, autoflush=False, bind=engine)


class ProfileScopedCrudTest(unittest.TestCase):
    # A fresh in-memory DB per test method (not per class): several tests
    # here assert an exact row set for a given profile, which would flake
    # depending on test execution order if rows from an earlier test in
    # the same class were still sitting in a shared database.
    def setUp(self):
        self.engine, SessionLocal = _new_in_memory_session_factory()
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_get_all_series_and_books_only_return_the_requested_profile(self):
        crud.create_series(self.db, schemas.SeriesBase(name="Robbie Series"), profile_id="robbie")
        crud.create_series(self.db, schemas.SeriesBase(name="Daughter Series"), profile_id="daughter")
        crud.create_book(self.db, schemas.BookBase(title="Robbie Book", author="A"), profile_id="robbie")
        crud.create_book(self.db, schemas.BookBase(title="Daughter Book", author="B"), profile_id="daughter")

        self.assertEqual([s.name for s in crud.get_all_series(self.db, "robbie")], ["Robbie Series"])
        self.assertEqual([s.name for s in crud.get_all_series(self.db, "daughter")], ["Daughter Series"])
        self.assertEqual([b.title for b in crud.get_all_books(self.db, "robbie")], ["Robbie Book"])
        self.assertEqual([b.title for b in crud.get_all_books(self.db, "daughter")], ["Daughter Book"])

    def test_get_series_by_name_is_scoped_per_profile_not_global(self):
        # This is a deliberate behavior change from the pre-profile global
        # lookup: two profiles can now each track a same-named series
        # without colliding.
        robbie_series = crud.create_series(self.db, schemas.SeriesBase(name="Shared Name"), profile_id="robbie")
        daughter_series = crud.create_series(self.db, schemas.SeriesBase(name="Shared Name"), profile_id="daughter")

        self.assertNotEqual(robbie_series.id, daughter_series.id)
        self.assertEqual(crud.get_series_by_name(self.db, "Shared Name", "robbie").id, robbie_series.id)
        self.assertEqual(crud.get_series_by_name(self.db, "Shared Name", "daughter").id, daughter_series.id)
        self.assertIsNone(crud.get_series_by_name(self.db, "Nonexistent Name", "robbie"))

    def test_cannot_read_edit_or_delete_another_profiles_rows_by_guessing_id(self):
        robbie_series = crud.create_series(self.db, schemas.SeriesBase(name="Robbie Only"), profile_id="robbie")
        robbie_book = crud.create_book(
            self.db,
            schemas.BookBase(title="Robbie Book", author="A", series_id=robbie_series.id),
            profile_id="robbie",
        )

        # Daughter can't see or touch Robbie's rows just by guessing the id.
        self.assertIsNone(crud.get_series(self.db, robbie_series.id, "daughter"))
        self.assertIsNone(crud.get_book(self.db, robbie_book.id, "daughter"))
        self.assertEqual(crud.get_books_by_series(self.db, robbie_series.id, "daughter"), [])
        self.assertFalse(crud.delete_book(self.db, robbie_book.id, "daughter"))
        self.assertIsNone(crud.delete_series(self.db, robbie_series.id, "daughter"))
        self.assertIsNone(
            crud.update_series(self.db, robbie_series.id, schemas.SeriesBase(name="Hijacked"), "daughter")
        )
        self.assertIsNone(
            crud.update_book(self.db, robbie_book.id, schemas.BookUpdate(title="Hijacked"), "daughter")
        )

        # Robbie can, of course.
        self.assertIsNotNone(crud.get_series(self.db, robbie_series.id, "robbie"))
        self.assertIsNotNone(crud.get_book(self.db, robbie_book.id, "robbie"))

    def test_create_book_rejects_series_id_belonging_to_another_profile(self):
        daughter_series = crud.create_series(self.db, schemas.SeriesBase(name="Daughter Series"), profile_id="daughter")

        with self.assertRaises(InvalidSeriesForProfileError):
            crud.create_book(
                self.db,
                schemas.BookBase(title="Sneaky Link", author="A", series_id=daughter_series.id),
                profile_id="robbie",
            )

    def test_update_book_rejects_moving_into_another_profiles_series(self):
        robbie_series = crud.create_series(self.db, schemas.SeriesBase(name="Robbie Series 2"), profile_id="robbie")
        daughter_series = crud.create_series(self.db, schemas.SeriesBase(name="Daughter Series 2"), profile_id="daughter")
        robbie_book = crud.create_book(
            self.db,
            schemas.BookBase(title="Robbie Book 2", author="A", series_id=robbie_series.id),
            profile_id="robbie",
        )

        with self.assertRaises(InvalidSeriesForProfileError):
            crud.update_book(
                self.db, robbie_book.id, schemas.BookUpdate(series_id=daughter_series.id), "robbie"
            )

        # The book must be left untouched by the rejected update.
        unchanged = crud.get_book(self.db, robbie_book.id, "robbie")
        self.assertEqual(unchanged.series_id, robbie_series.id)


class DiscoveryDedupeCrossProfileTest(unittest.TestCase):
    """The plan's highest-risk area: 'More by this author' matches by
    author name and title text, not by id, so it's the easiest place to
    introduce a silent cross-profile leak.
    """

    def setUp(self):
        self.engine, SessionLocal = _new_in_memory_session_factory()
        self.db = SessionLocal()
        # Robbie already owns this exact book; Daughter owns nothing by
        # this author yet.
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.db.add(
            Book(
                title="Cherry Blossom Girls Book 7",
                author="Harmon Cooper",
                series_id=series.id,
                series_order=7,
                book_number=7.0,
                record_status="active",
                is_read=True,
                isbn13="9781111111111",
                profile_id="robbie",
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def _mock_discovery(self, candidates):
        return patch(
            "discovery_engine.discover_candidates_for_author",
            return_value={"candidates": candidates, "provider_failures": [], "all_providers_failed": False},
        )

    def test_book_owned_by_one_profile_does_not_suppress_discovery_for_another(self):
        candidates = [
            {
                "source": "hardcover",
                "title": "Cherry Blossom Girls Book 7",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": "9781111111111",
                "source_url": None,
                "series_number_hint": 7,
                "upcoming_hint": False,
                "series_name_hint": "Cherry Blossom Girls",
            }
        ]

        with self._mock_discovery(candidates):
            robbie_result = discover_more_by_author(self.db, "Harmon Cooper", "robbie")
            daughter_result = discover_more_by_author(self.db, "Harmon Cooper", "daughter")

        # Robbie already owns it -- correctly excluded from her results.
        self.assertEqual(robbie_result["candidates"], [])
        # Daughter doesn't own it -- it must still surface as new for her,
        # not get silently suppressed by Robbie's copy.
        self.assertEqual(len(daughter_result["candidates"]), 1)
        self.assertEqual(daughter_result["candidates"][0]["title"], "Cherry Blossom Girls Book 7")


class GetCurrentProfileIdDependencyTest(unittest.TestCase):
    """Unit coverage for routers.deps.get_current_profile_id, independent
    of any specific router -- the header-with-default-fallback contract
    every library-scoped endpoint relies on.
    """

    def setUp(self):
        self.engine, SessionLocal = _new_in_memory_session_factory()
        self.db = SessionLocal()
        self.db.add_all(
            [
                Profile(id="robbie", display_name="Robbie's Library", is_default=True),
                Profile(id="daughter", display_name="Daughter's Library", is_default=False),
            ]
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    class _FakeRequest:
        def __init__(self, headers):
            self.headers = headers or {}

    def test_missing_header_resolves_to_default_profile(self):
        resolved = get_current_profile_id(self._FakeRequest({}), self.db)
        self.assertEqual(resolved, "robbie")

    def test_explicit_header_resolves_to_that_profile(self):
        resolved = get_current_profile_id(self._FakeRequest({"x-profile-id": "daughter"}), self.db)
        self.assertEqual(resolved, "daughter")

    def test_unknown_profile_header_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            get_current_profile_id(self._FakeRequest({"x-profile-id": "nonexistent"}), self.db)
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
