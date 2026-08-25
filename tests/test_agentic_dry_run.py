"""Phase 2, second implementation block: dual execution mode -- `agents/
series_agent.py`'s `run_series_check` now also runs the Phase 1 shadow
loop (`agents/agentic_series_agent.run_agentic_turn`) once, in parallel,
on every live discovery turn, purely for diagnostics via `services/
discovery_telemetry.record_agentic_dry_run` and the read-only `/admin/
agentic/dry-run/{series_id}` endpoint.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(not re-litigated here; this is Phase 2 dual execution on top of that
settled Phase 1 architecture), this file needs to prove:

1. The dry-run hook executes without changing `run_series_check`'s live
   result (same routing outcome as before this block existed).
2. It logs a structured `{"live_snapshot", "agentic_trace", "timestamp"}`
   payload via `record_agentic_dry_run`.
3. Any exception from either shadow-loop call is caught and logged as an
   `{"error", "timestamp"}` payload instead of propagating to the caller.
4. The admin inspection endpoint requires owner auth.
5. Nothing here writes any additional persistent state beyond what the
   live pipeline already, independently, writes.
6. Skeleton content (`SeriesSkeleton.skeleton_json`) and Book rows are
   unaffected by the dry-run block specifically.
"""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import agents.agentic_series_agent as agentic_series_agent_module
import main
import services.agentic_evaluation_harness as agentic_evaluation_harness_module
from agents.series_agent import SeriesIntelligenceAgent
from database import Base
from models import Book, Series, SeriesSkeleton
from routers.deps import create_owner_token


class AgenticDryRunTest(unittest.TestCase):
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

    def _row_counts(self):
        return {
            "series": self.db.query(Series).count(),
            "books": self.db.query(Book).count(),
            "skeletons": self.db.query(SeriesSkeleton).count(),
        }

    def _skeleton_json(self):
        row = self.db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == self.series.id).first()
        return list(row.skeleton_json) if row else None

    def _skeleton_json_stable(self):
        # `backfill_skeleton_for_series` (the LIVE pipeline's own,
        # pre-existing write -- unrelated to the dry-run addition this
        # file tests) re-stamps timestamps fresh on every rebuild, so a
        # byte-for-byte comparison across two calls would always differ
        # even with zero meaningful change. Strips exactly those volatile
        # fields, keeping everything substantive (book_number/title/
        # status/confidence/source_class/etc.).
        entries = self._skeleton_json() or []
        stable = []
        for entry in entries:
            trimmed = {k: v for k, v in entry.items() if k not in ("first_seen_at", "last_confirmed_at", "sources")}
            stable.append(trimmed)
        return stable

    # -- 1: no live behavior change --------------------------------------

    def test_dry_run_executes_without_affecting_live_behavior(self):
        with self._mock_discovery([self._candidate()]), patch(
            "agents.agentic_series_agent.run_agentic_turn", wraps=agentic_series_agent_module.run_agentic_turn
        ) as spy_run_agentic_turn, patch(
            "services.agentic_evaluation_harness._observe_live_pipeline",
            wraps=agentic_evaluation_harness_module._observe_live_pipeline,
        ) as spy_observe_live:
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        # Dual execution actually happened...
        spy_run_agentic_turn.assert_called_once()
        spy_observe_live.assert_called_once()

        # ...but the live result is exactly what it would be without it
        # (same assertions as tests/test_agentic_hooks.py's baseline
        # "no behavior change" case for an identical scenario).
        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)
        self.assertEqual(result["available_missing"][0]["series_number"], 7)

    # -- 2: logs a structured trace ----------------------------------------

    def test_dry_run_logs_agentic_trace(self):
        with self._mock_discovery([self._candidate()]), patch(
            "services.discovery_telemetry.record_agentic_dry_run"
        ) as mock_record:
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False)

        mock_record.assert_called_once()
        call_series_id, payload = mock_record.call_args[0]
        self.assertEqual(call_series_id, self.series.id)
        self.assertIn("live_snapshot", payload)
        self.assertIn("agentic_trace", payload)
        self.assertIn("timestamp", payload)
        self.assertNotIn("error", payload)

        agentic_trace = payload["agentic_trace"]
        for key in (
            "provider_calls",
            "probes",
            "confidence_traces",
            "gate_traces",
            "skeleton_merge_previews",
            "reasoning_steps",
        ):
            self.assertIn(key, agentic_trace)

        live_snapshot = payload["live_snapshot"]
        for key in ("skeleton_snapshot", "confidence_snapshot", "gate_snapshot"):
            self.assertIn(key, live_snapshot)

    # -- 3: fail-soft on error -----------------------------------------------

    def test_dry_run_logs_errors_fail_soft(self):
        with self._mock_discovery([self._candidate()]), patch(
            "agents.agentic_series_agent.run_agentic_turn", side_effect=RuntimeError("shadow loop exploded")
        ), patch("services.discovery_telemetry.record_agentic_dry_run") as mock_record:
            agent = SeriesIntelligenceAgent()
            # Must not raise despite the dry-run hook's own call blowing up.
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        # The live result is still returned normally.
        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)

        mock_record.assert_called_once()
        call_series_id, payload = mock_record.call_args[0]
        self.assertEqual(call_series_id, self.series.id)
        self.assertIn("error", payload)
        self.assertIn("shadow loop exploded", payload["error"])
        self.assertIn("timestamp", payload)
        self.assertNotIn("agentic_trace", payload)

    def test_dry_run_survives_telemetry_logging_itself_raising(self):
        # Even if record_agentic_dry_run itself blows up on both the
        # success and the error-logging attempt, run_series_check must
        # still complete and return its live result.
        with self._mock_discovery([self._candidate()]), patch(
            "agents.agentic_series_agent.run_agentic_turn", side_effect=RuntimeError("shadow loop exploded")
        ), patch("services.discovery_telemetry.record_agentic_dry_run", side_effect=RuntimeError("logging exploded")):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)

    # -- 4: owner-only admin endpoint --------------------------------------

    def test_dry_run_endpoint_owner_only(self):
        client = TestClient(main.app)

        anonymous_response = client.get(f"/admin/agentic/dry-run/{self.series.id}")
        self.assertEqual(anonymous_response.status_code, 403)

        owner_headers = {"Authorization": f"Bearer {create_owner_token()}"}
        owner_response = client.get(f"/admin/agentic/dry-run/{self.series.id}", headers=owner_headers)
        self.assertEqual(owner_response.status_code, 200)
        body = owner_response.json()
        self.assertEqual(body["series_id"], self.series.id)
        self.assertIn("history", body)
        self.assertIn("note", body)

    # -- 5: no additional persistent state ------------------------------------

    def test_dry_run_no_state_changes(self):
        with self._mock_discovery([]):
            agent = SeriesIntelligenceAgent()
            # Settle the series into steady state first -- backfilling the
            # skeleton and updating Series.last_checked are the live
            # pipeline's own, pre-existing writes, independent of the
            # dry-run addition this file tests.
            agent.run_series_check(self.db, self.series.id, emit_summary=False)

            before_counts = self._row_counts()
            before_skeleton = self._skeleton_json_stable()

            # A second call, same no-new-candidates input: if the dry-run
            # block (which runs on every call) introduced any extra write,
            # row counts/skeleton content would drift here.
            agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(self._row_counts(), before_counts)
        self.assertEqual(self._skeleton_json_stable(), before_skeleton)

    # -- 6: skeleton/confidence specifically unaffected -----------------------

    def test_dry_run_does_not_modify_skeleton_or_confidence(self):
        with self._mock_discovery([self._candidate()]):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False)

        skeleton_after_live_call = self._skeleton_json_stable()
        confidences_after_live_call = {
            entry["book_number"]: entry.get("confidence") for entry in skeleton_after_live_call
        }

        # Re-running with the exact same input must not perturb the
        # skeleton or any entry's confidence grade -- the dry-run block
        # (agents/agentic_series_agent.run_agentic_turn + services/
        # agentic_evaluation_harness._observe_live_pipeline) never calls
        # apply_skeleton_updates/_upsert_skeleton_row (see those modules'
        # own no-write docstrings); only the live pipeline's own
        # backfill_skeleton_for_series call touches skeleton_json, and
        # that's idempotent for identical Book rows.
        with self._mock_discovery([self._candidate()]):
            agent = SeriesIntelligenceAgent()
            agent.run_series_check(self.db, self.series.id, emit_summary=False)

        skeleton_after_second_call = self._skeleton_json_stable()
        confidences_after_second_call = {
            entry["book_number"]: entry.get("confidence") for entry in skeleton_after_second_call
        }
        self.assertEqual(skeleton_after_live_call, skeleton_after_second_call)
        self.assertEqual(confidences_after_live_call, confidences_after_second_call)


if __name__ == "__main__":
    unittest.main()
