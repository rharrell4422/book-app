"""Phase 2, third implementation block: dual-write skeleton preview
(shadow writes only) -- `services/agentic_skeleton_preview_store.py`'s
`store_agentic_skeleton_preview`/`get_agentic_skeleton_previews`, the new
`AgenticSkeletonPreview` model/`agentic_skeleton_previews` table, the
dry-run block's new call into that store (`agents/series_agent.py`), and
the read-only `/admin/agentic/previews/{series_id}` endpoint.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here), this file needs to prove:

1. `store_agentic_skeleton_preview` actually inserts a row into the new
   shadow table, and `get_agentic_skeleton_previews` reads it back.
2. It is fail-soft -- a broken session/write never raises back to the
   caller, and is logged via `record_agentic_skeleton_preview_error`.
3. It never touches `SeriesSkeleton.skeleton_json` (the live table).
4. `run_series_check`'s dry-run block calls it with the dry run turn's
   `skeleton_merge_previews`, and that call itself is fail-soft too.
5. The admin endpoint requires owner auth and returns stored previews.
"""

import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
from agents.series_agent import SeriesIntelligenceAgent
from database import Base
from models import AgenticSkeletonPreview, Book, Series, SeriesSkeleton
from routers.deps import create_owner_token
from services.agentic_skeleton_preview_store import (
    get_agentic_skeleton_previews,
    store_agentic_skeleton_preview,
)


class StoreAgenticSkeletonPreviewTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_stores_and_reads_back_a_preview(self):
        preview = {"book_number": 7, "before": {"title": "old"}, "after": {"title": "new"}}
        store_agentic_skeleton_preview(self.series.id, preview, db_session=self.db)

        rows = self.db.query(AgenticSkeletonPreview).filter(AgenticSkeletonPreview.series_id == self.series.id).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].preview_json, preview)
        self.assertIsNotNone(rows[0].timestamp)

        previews = get_agentic_skeleton_previews(self.series.id, db_session=self.db)
        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0]["series_id"], self.series.id)
        self.assertEqual(previews[0]["preview_json"], preview)
        self.assertIn("timestamp", previews[0])
        self.assertIn("id", previews[0])

    def test_multiple_previews_accumulate_oldest_first(self):
        store_agentic_skeleton_preview(self.series.id, {"turn": 1}, db_session=self.db)
        store_agentic_skeleton_preview(self.series.id, {"turn": 2}, db_session=self.db)
        store_agentic_skeleton_preview(self.series.id, {"turn": 3}, db_session=self.db)

        previews = get_agentic_skeleton_previews(self.series.id, db_session=self.db)
        self.assertEqual([p["preview_json"]["turn"] for p in previews], [1, 2, 3])

    def test_get_previews_returns_empty_list_for_unknown_series(self):
        previews = get_agentic_skeleton_previews(999999, db_session=self.db)
        self.assertEqual(previews, [])

    def test_opens_and_closes_its_own_session_when_none_supplied(self):
        with patch("services.agentic_skeleton_preview_store.SessionLocal", self.SessionLocal):
            store_agentic_skeleton_preview(self.series.id, {"turn": 1})
            previews = get_agentic_skeleton_previews(self.series.id)

        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0]["preview_json"], {"turn": 1})

    def test_none_preview_stored_as_empty_dict(self):
        store_agentic_skeleton_preview(self.series.id, None, db_session=self.db)
        previews = get_agentic_skeleton_previews(self.series.id, db_session=self.db)
        self.assertEqual(previews[0]["preview_json"], {})

    def test_never_touches_live_skeleton(self):
        self.db.add(SeriesSkeleton(series_id=self.series.id, skeleton_json=[{"book_number": 1}], schema_version=2))
        self.db.commit()

        before = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        before_json = list(before.skeleton_json)

        store_agentic_skeleton_preview(self.series.id, {"book_number": 1, "title": "shadow-only"}, db_session=self.db)

        after = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        self.assertEqual(list(after.skeleton_json), before_json)

    def _broken_db(self):
        broken_db = MagicMock()
        broken_db.commit.side_effect = RuntimeError("commit exploded")
        return broken_db

    def test_write_failure_is_fail_soft_and_logged(self):
        with patch("services.discovery_telemetry.record_agentic_skeleton_preview_error") as mock_record_error:
            # Should not raise despite the broken session.
            store_agentic_skeleton_preview(self.series.id, {"turn": 1}, db_session=self._broken_db())

        mock_record_error.assert_called_once()
        call_series_id, call_error = mock_record_error.call_args[0]
        self.assertEqual(call_series_id, self.series.id)
        self.assertTrue(call_error)

    def test_write_failure_survives_error_logging_itself_raising(self):
        with patch(
            "services.discovery_telemetry.record_agentic_skeleton_preview_error",
            side_effect=RuntimeError("logging exploded"),
        ):
            # Must not raise even if the fail-soft error logger itself blows up.
            store_agentic_skeleton_preview(self.series.id, {"turn": 1}, db_session=self._broken_db())

    def test_read_failure_is_fail_soft(self):
        broken_db = MagicMock()
        broken_db.query.side_effect = RuntimeError("query exploded")

        previews = get_agentic_skeleton_previews(self.series.id, db_session=broken_db)
        self.assertEqual(previews, [])


class DryRunWiresIntoPreviewStoreTest(unittest.TestCase):
    """`agents/series_agent.py`'s dry-run block (added in the previous
    Phase 2 block) now also calls `store_agentic_skeleton_preview` with
    the dry-run turn's `skeleton_merge_previews`.
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
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series
        for number in [1, 2, 3, 4, 5, 6, 8, 9]:
            self.db.add(
                Book(
                    title=f"Cherry Blossom Girls Book {number}",
                    author="Harmon Cooper",
                    series_id=series.id,
                    profile_id=series.profile_id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=False,
                )
            )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _mock_discovery(self, candidates, **overrides):
        result = {
            "candidates": candidates,
            "provider_failures": [],
            "all_providers_failed": False,
            "used_author_fallback": False,
        }
        result.update(overrides)
        return patch("discovery_engine.discover_candidates_for_series", return_value=result)

    def _candidate(self):
        return {
            "source": "hardcover",
            "source_id": "hc-7",
            "title": "Cherry Blossom Girls Book 7",
            "authors": ["Harmon Cooper"],
            "published_date": "2024-02-20",
            "isbn13": None,
            "source_url": None,
            "language": "",
            "confidence": "targeted",
            "series_number_hint": 7,
            "upcoming_hint": False,
        }

    def test_run_series_check_stores_a_preview_row(self):
        with self._mock_discovery([self._candidate()]):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        # Live result unaffected.
        self.assertTrue(result["found"])

        previews = get_agentic_skeleton_previews(self.series.id, db_session=self.db)
        self.assertEqual(len(previews), 1)
        self.assertIn("preview_json", previews[0])

    def test_stored_preview_matches_agentic_trace_skeleton_merge_previews(self):
        import agents.agentic_series_agent as agentic_series_agent_module

        original_run_agentic_turn = agentic_series_agent_module.run_agentic_turn
        captured = {}

        def _wrapped(*args, **kwargs):
            trace = original_run_agentic_turn(*args, **kwargs)
            captured["trace"] = trace
            return trace

        with self._mock_discovery([self._candidate()]), patch(
            "agents.agentic_series_agent.run_agentic_turn", side_effect=_wrapped
        ):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False)

        previews = get_agentic_skeleton_previews(self.series.id, db_session=self.db)
        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0]["preview_json"], captured["trace"].get("skeleton_merge_previews", {}))

    def test_preview_store_failure_does_not_block_dry_run_or_live_result(self):
        with self._mock_discovery([self._candidate()]), patch(
            "services.agentic_skeleton_preview_store.store_agentic_skeleton_preview",
            side_effect=RuntimeError("shadow write exploded"),
        ), patch("services.discovery_telemetry.record_agentic_dry_run") as mock_record_dry_run:
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        # Live result still returned normally.
        self.assertTrue(result["found"])
        # The dry-run trace itself was still logged despite the shadow-write failure.
        mock_record_dry_run.assert_called_once()
        call_series_id, payload = mock_record_dry_run.call_args[0]
        self.assertNotIn("error", payload)
        self.assertIn("agentic_trace", payload)

    def test_run_series_check_never_writes_probes_json_or_changes_skeleton_confidence(self):
        with self._mock_discovery([self._candidate()]):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False)

        row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        self.assertIsNotNone(row)
        # No probes_json column/behavior exists anywhere (see services/
        # agentic_ttl_validator.py's own note) -- confirms this block
        # didn't introduce one either.
        self.assertFalse(hasattr(row, "probes_json"))


class AdminAgenticPreviewsEndpointTest(unittest.TestCase):
    """TestClient dispatches requests through `main.app`'s own dependency-
    injected DB session (backed by the real `database.SessionLocal`/
    `engine`, same as tests/test_admin_agentic_endpoints.py's routes),
    not this file's other classes' isolated in-memory engines. So this
    class writes/reads through that same real `SessionLocal` directly --
    matching what the endpoint itself uses -- against a series_id chosen
    to not collide with real data, cleaning up its one inserted row
    afterward.
    """

    SERIES_ID = -999999997

    def setUp(self):
        from database import SessionLocal as RealSessionLocal

        self.RealSessionLocal = RealSessionLocal
        store_agentic_skeleton_preview(self.SERIES_ID, {"book_number": 7})

    def tearDown(self):
        from models import AgenticSkeletonPreview

        db = self.RealSessionLocal()
        try:
            db.query(AgenticSkeletonPreview).filter(AgenticSkeletonPreview.series_id == self.SERIES_ID).delete()
            db.commit()
        finally:
            db.close()

    def test_previews_endpoint_owner_only(self):
        client = TestClient(main.app)

        anonymous_response = client.get(f"/admin/agentic/previews/{self.SERIES_ID}")
        self.assertEqual(anonymous_response.status_code, 403)

        owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}
        owner_response = client.get(f"/admin/agentic/previews/{self.SERIES_ID}", headers=owner_headers)

        self.assertEqual(owner_response.status_code, 200)
        body = owner_response.json()
        self.assertEqual(body["series_id"], self.SERIES_ID)
        self.assertEqual(len(body["previews"]), 1)
        self.assertEqual(body["previews"][0]["preview_json"], {"book_number": 7})


if __name__ == "__main__":
    unittest.main()
