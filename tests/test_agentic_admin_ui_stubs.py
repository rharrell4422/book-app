"""Phase 1, eleventh implementation block (second half):
`services/agentic_admin_ui_stubs.py`'s `list_agentic_series`/
`get_agentic_history`.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here), this file needs to prove:

1. `list_agentic_series` correctly lists every series with a
   `SeriesSkeleton` row, and returns an honest empty result when there
   are none.
2. `get_agentic_history` always returns the documented shape with an
   empty `history` and an explanatory `note` (there is no persisted
   evaluation-history store yet -- see that module's docstring).
3. Neither writes anything.
4. `list_agentic_series` opens/closes its own session when none is
   supplied, matching every other Phase 1 harness function's convention.
"""

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from models import Book, Series, SeriesSkeleton
from services.agentic_admin_ui_stubs import get_agentic_history, list_agentic_series
from services.skeleton_store import backfill_skeleton_for_series


class ListAgenticSeriesTest(unittest.TestCase):
    # Deliberately a fresh engine per test method (not shared via
    # setUpClass) -- several methods below add series/skeleton rows, and
    # `test_returns_empty_when_no_skeletons_exist` needs to observe a
    # genuinely empty table rather than whatever earlier test methods
    # already inserted into a shared in-memory DB.
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def _make_series_with_skeleton(self, name: str) -> Series:
        series = Series(name=name, author="Some Author", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.db.add(
            Book(
                title=f"{name} Book 1",
                author="Some Author",
                series_id=series.id,
                profile_id=series.profile_id,
                series_order=1,
                book_number=1.0,
                record_status="active",
                is_read=False,
            )
        )
        self.db.commit()
        backfill_skeleton_for_series(self.db, series.id)
        return series

    def test_returns_empty_when_no_skeletons_exist(self):
        result = list_agentic_series(db_session=self.db)
        self.assertEqual(result, {"series_ids": [], "count": 0, "timestamp": result["timestamp"]})
        self.assertIn("timestamp", result)

    def test_lists_series_ids_with_a_skeleton_row(self):
        series_a = self._make_series_with_skeleton("Cherry Blossom Girls")
        series_b = self._make_series_with_skeleton("Another Series")

        result = list_agentic_series(db_session=self.db)

        self.assertEqual(set(result["series_ids"]), {series_a.id, series_b.id})
        self.assertEqual(result["count"], 2)

    def test_never_writes_anything(self):
        self._make_series_with_skeleton("Cherry Blossom Girls")
        before = self.db.query(SeriesSkeleton).count()

        list_agentic_series(db_session=self.db)

        after = self.db.query(SeriesSkeleton).count()
        self.assertEqual(before, after)

    def test_opens_and_closes_its_own_session_when_none_supplied(self):
        series = self._make_series_with_skeleton("Cherry Blossom Girls")

        with patch("services.agentic_admin_ui_stubs.SessionLocal", self.SessionLocal):
            result = list_agentic_series()

        self.assertIn(series.id, result["series_ids"])


class GetAgenticHistoryTest(unittest.TestCase):
    def test_returns_documented_shape_with_empty_history_and_a_note(self):
        result = get_agentic_history(42)

        self.assertEqual(result["series_id"], 42)
        self.assertEqual(result["history"], [])
        self.assertIn("note", result)
        self.assertIn("no persisted store", result["note"])
        self.assertIn("timestamp", result)

    def test_accepts_but_does_not_require_a_db_session(self):
        # db_session is accepted for interface parity/future-proofing but
        # unused today -- passing one (even a nonsense value) must not
        # raise, since nothing here touches it.
        result = get_agentic_history(1, db_session="not-a-real-session")
        self.assertEqual(result["series_id"], 1)
        self.assertEqual(result["history"], [])


if __name__ == "__main__":
    unittest.main()
