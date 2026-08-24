import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from intelligence import compute_series_intelligence_for_series, recount_series_aggregates_for_series
from models import Book, Series


class IntelligenceProfileScopingTest(unittest.TestCase):
    """Regression tests for a profile-isolation bug: a Book could end up
    linked to a series_id belonging to a *different* profile than the
    book's own profile_id (e.g. via a code path that forgot to set
    profile_id explicitly -- see services/series_check_engine.py; CR-10
    removed the Book/Series model's implicit "robbie" fallback for exactly
    this failure mode, so the ghost row below is now constructed
    explicitly instead of relying on that default). Such a "ghost" book is
    invisible to every profile-scoped books query, yet these two
    intelligence functions used to query by series_id alone and would
    still count it, inflating total_books/upcoming counts for a series
    with a book nobody could actually see. Both functions must ignore
    books whose profile_id doesn't match the series' own profile_id.
    """

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=cls.engine)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setUp(self):
        self.db = self.SessionLocal()
        self.series = Series(name="The Empyrean", author="Rebecca Yarros", profile_id="mackenzie")
        self.db.add(self.series)
        self.db.commit()
        self.db.refresh(self.series)

        for number in (1, 2, 3):
            self.db.add(
                Book(
                    title=f"Book {number}",
                    author="Rebecca Yarros",
                    series_id=self.series.id,
                    book_number=float(number),
                    series_order=number,
                    record_status="active",
                    is_read=True,
                    read_status="read",
                    profile_id="mackenzie",
                )
            )
        # A ghost row: linked to mackenzie's series_id but tagged with a
        # different profile_id ("robbie") -- reproduces the bug.
        self.db.add(
            Book(
                title="Some Future Book",
                author="Rebecca Yarros",
                series_id=self.series.id,
                profile_id="robbie",
                book_number=4.0,
                series_order=4,
                record_status="active",
                is_read=False,
                read_status="upcoming",
                is_upcoming_auto=True,
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_compute_series_intelligence_ignores_other_profile_ghost_book(self):
        intelligence = compute_series_intelligence_for_series(self.db, self.series.id)
        self.assertEqual(intelligence["total_books"], 3)
        self.assertIsNone(intelligence["next_upcoming_book_number"])

    def test_recount_series_aggregates_ignores_other_profile_ghost_book(self):
        aggregates = recount_series_aggregates_for_series(self.db, self.series.id)
        self.assertEqual(aggregates.get("total_books"), 3)


if __name__ == "__main__":
    unittest.main()
