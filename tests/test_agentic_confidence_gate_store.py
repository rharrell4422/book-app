"""Phase 2, final scaffolding block: confidence/gate dual-write (shadow
confidence + shadow gate decisions) -- `services/agentic_confidence_gate_
store.py`'s `store_agentic_confidence`/`store_agentic_gate`/`get_agentic_
confidence_history`/`get_agentic_gate_history`, the new
`AgenticConfidenceDecision`/`AgenticGateDecision` models/
`agentic_confidence_decisions`/`agentic_gate_decisions` tables, the
dry-run block's new calls into that store (`agents/series_agent.py`), and
the read-only `/admin/agentic/confidence/{series_id}`/`/admin/agentic/
gate/{series_id}` endpoints.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here), this file needs to prove:

1. `store_agentic_confidence`/`store_agentic_gate` actually insert rows
   into their respective new shadow tables, and the matching `get_
   agentic_*_history` reads them back.
2. Both are fail-soft -- a broken session/write never raises back to the
   caller, and is logged via `record_agentic_confidence_gate_error`.
3. Neither ever touches live confidence (`confidence_engine.py`/
   `SeriesSkeleton.skeleton_json`) or live gate
   (`evaluate_belongs_to_series_gate`) behavior.
4. `run_series_check`'s dry-run block calls both, once per traced book,
   pairing each with the corresponding live snapshot entry, and that
   wiring itself is fail-soft too.
5. Both admin endpoints require owner auth and return stored histories.
"""

import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import main
from agents.series_agent import SeriesIntelligenceAgent
from database import Base
from models import AgenticConfidenceDecision, AgenticGateDecision, Book, Series, SeriesSkeleton
from routers.deps import create_owner_token
from services.agentic_confidence_gate_store import (
    get_agentic_confidence_history,
    get_agentic_gate_history,
    store_agentic_confidence,
    store_agentic_gate,
)


class StoreAgenticConfidenceTest(unittest.TestCase):
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

    def _broken_db(self):
        broken_db = MagicMock()
        broken_db.commit.side_effect = RuntimeError("commit exploded")
        return broken_db

    # -- confidence: write + read --------------------------------------------

    def test_store_agentic_confidence_writes_shadow_table(self):
        live_conf = {"confidence": "medium", "status": "unconfirmed"}
        agentic_conf = {"book_number": 7, "before": {"provider_confidence": "low"}, "after": {"overall": "low"}}
        store_agentic_confidence(self.series.id, 7.0, live_conf, agentic_conf, db_session=self.db)

        rows = (
            self.db.query(AgenticConfidenceDecision)
            .filter(AgenticConfidenceDecision.series_id == self.series.id)
            .all()
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].book_number, 7.0)
        self.assertEqual(rows[0].live_confidence, live_conf)
        self.assertEqual(rows[0].agentic_confidence, agentic_conf)
        self.assertIsNotNone(rows[0].timestamp)

    def test_get_confidence_history_returns_all(self):
        store_agentic_confidence(self.series.id, 7.0, {"turn": 1}, {"turn": "a"}, db_session=self.db)
        store_agentic_confidence(self.series.id, 8.0, {"turn": 2}, {"turn": "b"}, db_session=self.db)

        history = get_agentic_confidence_history(self.series.id, db_session=self.db)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["book_number"], 7.0)
        self.assertEqual(history[0]["live_confidence"], {"turn": 1})
        self.assertEqual(history[0]["agentic_confidence"], {"turn": "a"})
        self.assertEqual(history[1]["book_number"], 8.0)
        for entry in history:
            self.assertIn("id", entry)
            self.assertIn("timestamp", entry)
            self.assertIn("series_id", entry)

    def test_get_confidence_history_returns_empty_list_for_unknown_series(self):
        history = get_agentic_confidence_history(999999, db_session=self.db)
        self.assertEqual(history, [])

    def test_confidence_opens_and_closes_its_own_session_when_none_supplied(self):
        with patch("services.agentic_confidence_gate_store.SessionLocal", self.SessionLocal):
            store_agentic_confidence(self.series.id, 7.0, {"a": 1}, {"b": 2})
            history = get_agentic_confidence_history(self.series.id)

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["live_confidence"], {"a": 1})
        self.assertEqual(history[0]["agentic_confidence"], {"b": 2})

    def test_none_confidence_values_stored_as_empty_dicts(self):
        store_agentic_confidence(self.series.id, 7.0, None, None, db_session=self.db)
        history = get_agentic_confidence_history(self.series.id, db_session=self.db)
        self.assertEqual(history[0]["live_confidence"], {})
        self.assertEqual(history[0]["agentic_confidence"], {})

    # -- confidence: never touches live confidence ---------------------------

    def test_dual_write_does_not_modify_live_confidence(self):
        self.db.add(
            SeriesSkeleton(
                series_id=self.series.id,
                skeleton_json=[{"book_number": 7, "confidence": "high", "status": "confirmed"}],
                schema_version=2,
            )
        )
        self.db.commit()

        before = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        before_json = list(before.skeleton_json)

        store_agentic_confidence(
            self.series.id, 7.0, {"confidence": "high"}, {"overall": "low"}, db_session=self.db
        )

        after = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        self.assertEqual(list(after.skeleton_json), before_json)

    # -- confidence: fail-soft ------------------------------------------------

    def test_confidence_write_failure_is_fail_soft_and_logged(self):
        with patch("services.discovery_telemetry.record_agentic_confidence_gate_error") as mock_record_error:
            store_agentic_confidence(self.series.id, 7.0, {"a": 1}, {"b": 2}, db_session=self._broken_db())

        mock_record_error.assert_called_once()
        call_series_id, call_kind, call_error = mock_record_error.call_args[0]
        self.assertEqual(call_series_id, self.series.id)
        self.assertEqual(call_kind, "confidence")
        self.assertTrue(call_error)

    def test_confidence_write_failure_survives_error_logging_itself_raising(self):
        with patch(
            "services.discovery_telemetry.record_agentic_confidence_gate_error",
            side_effect=RuntimeError("logging exploded"),
        ):
            store_agentic_confidence(self.series.id, 7.0, {"a": 1}, {"b": 2}, db_session=self._broken_db())

    def test_confidence_read_failure_is_fail_soft(self):
        broken_db = MagicMock()
        broken_db.query.side_effect = RuntimeError("query exploded")

        history = get_agentic_confidence_history(self.series.id, db_session=broken_db)
        self.assertEqual(history, [])


class StoreAgenticGateTest(unittest.TestCase):
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

    def _broken_db(self):
        broken_db = MagicMock()
        broken_db.commit.side_effect = RuntimeError("commit exploded")
        return broken_db

    # -- gate: write + read ---------------------------------------------------

    def test_store_agentic_gate_writes_shadow_table(self):
        live_gate = {"belongs_to_series": True, "source_class": "library"}
        agentic_gate = {"book_number": 7, "gate_input": {"title": "x"}, "gate_output": {"belongs": True}}
        store_agentic_gate(self.series.id, 7.0, live_gate, agentic_gate, db_session=self.db)

        rows = self.db.query(AgenticGateDecision).filter(AgenticGateDecision.series_id == self.series.id).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].book_number, 7.0)
        self.assertEqual(rows[0].live_gate, live_gate)
        self.assertEqual(rows[0].agentic_gate, agentic_gate)
        self.assertIsNotNone(rows[0].timestamp)

    def test_get_gate_history_returns_all(self):
        store_agentic_gate(self.series.id, 7.0, {"turn": 1}, {"turn": "a"}, db_session=self.db)
        store_agentic_gate(self.series.id, 8.0, {"turn": 2}, {"turn": "b"}, db_session=self.db)

        history = get_agentic_gate_history(self.series.id, db_session=self.db)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["book_number"], 7.0)
        self.assertEqual(history[0]["live_gate"], {"turn": 1})
        self.assertEqual(history[0]["agentic_gate"], {"turn": "a"})
        self.assertEqual(history[1]["book_number"], 8.0)
        for entry in history:
            self.assertIn("id", entry)
            self.assertIn("timestamp", entry)
            self.assertIn("series_id", entry)

    def test_get_gate_history_returns_empty_list_for_unknown_series(self):
        history = get_agentic_gate_history(999999, db_session=self.db)
        self.assertEqual(history, [])

    def test_gate_opens_and_closes_its_own_session_when_none_supplied(self):
        with patch("services.agentic_confidence_gate_store.SessionLocal", self.SessionLocal):
            store_agentic_gate(self.series.id, 7.0, {"a": 1}, {"b": 2})
            history = get_agentic_gate_history(self.series.id)

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["live_gate"], {"a": 1})
        self.assertEqual(history[0]["agentic_gate"], {"b": 2})

    def test_none_gate_values_stored_as_empty_dicts(self):
        store_agentic_gate(self.series.id, 7.0, None, None, db_session=self.db)
        history = get_agentic_gate_history(self.series.id, db_session=self.db)
        self.assertEqual(history[0]["live_gate"], {})
        self.assertEqual(history[0]["agentic_gate"], {})

    # -- gate: never touches live gate/skeleton -------------------------------

    def test_dual_write_does_not_modify_live_gate(self):
        self.db.add(
            SeriesSkeleton(
                series_id=self.series.id,
                skeleton_json=[{"book_number": 7, "confidence": "high", "status": "confirmed"}],
                schema_version=2,
            )
        )
        self.db.commit()

        before = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        before_json = list(before.skeleton_json)

        store_agentic_gate(
            self.series.id, 7.0, {"belongs_to_series": True}, {"gate_output": {"belongs": False}}, db_session=self.db
        )

        after = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        self.assertEqual(list(after.skeleton_json), before_json)

    # -- gate: fail-soft -------------------------------------------------------

    def test_gate_write_failure_is_fail_soft_and_logged(self):
        with patch("services.discovery_telemetry.record_agentic_confidence_gate_error") as mock_record_error:
            store_agentic_gate(self.series.id, 7.0, {"a": 1}, {"b": 2}, db_session=self._broken_db())

        mock_record_error.assert_called_once()
        call_series_id, call_kind, call_error = mock_record_error.call_args[0]
        self.assertEqual(call_series_id, self.series.id)
        self.assertEqual(call_kind, "gate")
        self.assertTrue(call_error)

    def test_gate_write_failure_survives_error_logging_itself_raising(self):
        with patch(
            "services.discovery_telemetry.record_agentic_confidence_gate_error",
            side_effect=RuntimeError("logging exploded"),
        ):
            store_agentic_gate(self.series.id, 7.0, {"a": 1}, {"b": 2}, db_session=self._broken_db())

    def test_gate_read_failure_is_fail_soft(self):
        broken_db = MagicMock()
        broken_db.query.side_effect = RuntimeError("query exploded")

        history = get_agentic_gate_history(self.series.id, db_session=broken_db)
        self.assertEqual(history, [])


class DryRunWiresIntoConfidenceGateStoreTest(unittest.TestCase):
    """`agents/series_agent.py`'s dry-run block (added in earlier Phase 2
    blocks) now also calls `store_agentic_confidence`/`store_agentic_
    gate` once per traced book.
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

    def test_dry_run_path_stores_confidence_and_gate(self):
        # agents/series_agent.py's dry-run block calls run_agentic_turn
        # with a context that has no "db" key (per the prior Phase 2
        # block's spec), so run_agentic_turn always opens its own fresh
        # session via agents.agentic_series_agent.SessionLocal -- for a
        # real deployment that's just another session against the same
        # database, but this test's isolated in-memory engine needs that
        # name patched too, or run_agentic_turn would see an empty (or
        # the real dev) database instead of this test's fixture data.
        with self._mock_discovery([self._candidate()]), patch(
            "agents.agentic_series_agent.SessionLocal", self.SessionLocal
        ):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        # Live result unaffected.
        self.assertTrue(result["found"])

        confidence_history = get_agentic_confidence_history(self.series.id, db_session=self.db)
        gate_history = get_agentic_gate_history(self.series.id, db_session=self.db)

        # The shadow loop's candidate_numbers come from the *skeleton*
        # (agents/agentic_series_agent.run_agentic_turn's own docstring),
        # i.e. the library-owned books [1,2,3,4,5,6,8,9] set up in setUp
        # -- not book 7, the newly-discovered candidate this run just
        # found (which only lands in the skeleton later, via
        # apply_skeleton_updates in services/series_check_engine.py,
        # outside run_series_check entirely). So one row per owned book.
        self.assertEqual(len(confidence_history), 8)
        self.assertEqual(len(gate_history), 8)

        book_numbers = {entry["book_number"] for entry in confidence_history}
        self.assertEqual(book_numbers, {1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 9.0})

        one_confidence_entry = next(entry for entry in confidence_history if entry["book_number"] == 1.0)
        self.assertIn("live_confidence", one_confidence_entry)
        self.assertIn("agentic_confidence", one_confidence_entry)

        one_gate_entry = next(entry for entry in gate_history if entry["book_number"] == 1.0)
        self.assertIn("live_gate", one_gate_entry)
        self.assertIn("agentic_gate", one_gate_entry)

    def test_confidence_gate_store_failure_does_not_block_dry_run_or_live_result(self):
        with self._mock_discovery([self._candidate()]), patch(
            "services.agentic_confidence_gate_store.store_agentic_confidence",
            side_effect=RuntimeError("shadow confidence write exploded"),
        ), patch(
            "services.agentic_confidence_gate_store.store_agentic_gate",
            side_effect=RuntimeError("shadow gate write exploded"),
        ), patch("services.discovery_telemetry.record_agentic_dry_run") as mock_record_dry_run:
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        # Live result still returned normally.
        self.assertTrue(result["found"])
        # The dry-run trace itself was still logged despite the shadow-write failures.
        mock_record_dry_run.assert_called_once()
        call_series_id, payload = mock_record_dry_run.call_args[0]
        self.assertNotIn("error", payload)
        self.assertIn("agentic_trace", payload)

    def test_run_series_check_does_not_change_live_skeleton_confidence(self):
        with self._mock_discovery([self._candidate()]):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False)

        skeleton_row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        self.assertIsNotNone(skeleton_row)
        # Every entry is still a library-owned/confirmed entry, exactly
        # what the live pipeline alone (without this dual-write block)
        # would have produced -- confidence/gate values are untouched.
        for entry in skeleton_row.skeleton_json:
            if entry.get("source_class", "library") == "library":
                self.assertEqual(entry.get("status"), "confirmed")
                self.assertEqual(entry.get("confidence"), "high")


class AdminAgenticConfidenceGateEndpointsTest(unittest.TestCase):
    """Same reasoning as the equivalent previews-endpoint test in
    tests/test_agentic_skeleton_preview_store.py: TestClient dispatches
    through `main.app`'s real `database.SessionLocal`/`engine`, not this
    file's other classes' isolated in-memory engines, so this class
    writes/reads through that same real SessionLocal directly, against
    series_ids chosen not to collide with real data, cleaning up
    afterward.
    """

    SERIES_ID = -999999996

    def setUp(self):
        from database import SessionLocal as RealSessionLocal

        self.RealSessionLocal = RealSessionLocal
        store_agentic_confidence(self.SERIES_ID, 7.0, {"confidence": "medium"}, {"overall": "low"})
        store_agentic_gate(self.SERIES_ID, 7.0, {"belongs_to_series": True}, {"gate_output": {"belongs": True}})

    def tearDown(self):
        db = self.RealSessionLocal()
        try:
            db.query(AgenticConfidenceDecision).filter(
                AgenticConfidenceDecision.series_id == self.SERIES_ID
            ).delete()
            db.query(AgenticGateDecision).filter(AgenticGateDecision.series_id == self.SERIES_ID).delete()
            db.commit()
        finally:
            db.close()

    def test_admin_endpoints_owner_only(self):
        client = TestClient(main.app)

        anon_confidence = client.get(f"/admin/agentic/confidence/{self.SERIES_ID}")
        self.assertEqual(anon_confidence.status_code, 403)
        anon_gate = client.get(f"/admin/agentic/gate/{self.SERIES_ID}")
        self.assertEqual(anon_gate.status_code, 403)

        owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}

        owner_confidence = client.get(f"/admin/agentic/confidence/{self.SERIES_ID}", headers=owner_headers)
        self.assertEqual(owner_confidence.status_code, 200)
        confidence_body = owner_confidence.json()
        self.assertEqual(confidence_body["series_id"], self.SERIES_ID)
        self.assertEqual(len(confidence_body["confidence_history"]), 1)
        self.assertEqual(confidence_body["confidence_history"][0]["live_confidence"], {"confidence": "medium"})

        owner_gate = client.get(f"/admin/agentic/gate/{self.SERIES_ID}", headers=owner_headers)
        self.assertEqual(owner_gate.status_code, 200)
        gate_body = owner_gate.json()
        self.assertEqual(gate_body["series_id"], self.SERIES_ID)
        self.assertEqual(len(gate_body["gate_history"]), 1)
        self.assertEqual(gate_body["gate_history"][0]["live_gate"], {"belongs_to_series": True})


if __name__ == "__main__":
    unittest.main()
