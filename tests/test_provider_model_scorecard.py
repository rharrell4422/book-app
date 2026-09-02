"""Step 11 (Provider/Model Scorecard & Tier C Confidence Signals): unit
coverage for `services.provider_model_scorecard`. Phase 1:
`get_provider_model_scorecard` -- a read-only, computed-on-read
aggregation over `shadow_llm_calls`, grouped across ALL series by
`(shadow_provider, shadow_model_id)`. Phase 3: `check_parse_failure_
spikes`, an alert-only detector built on top of it. No behavior change
to any routing/promotion decision from either phase; `evaluate_tier_c_
promotion` calls the Phase 3 detector as of Step 11 Phase 3 (see
tests/test_tier_c_multi_provider.py for that wiring's own coverage), but
nothing consumes its return value to change behavior.
"""

import logging
import unittest
import uuid
from datetime import datetime, timedelta

from database import Base
from models import Series, ShadowLLMCall
from services.provider_model_scorecard import (
    ProviderModelMetricsAlert,
    check_parse_failure_spikes,
    get_provider_model_scorecard,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


class ProviderModelScorecardTest(unittest.TestCase):
    """Unlike every Step 10 test class this mirrors, this one creates a
    fresh in-memory engine PER TEST (not per class via setUpClass) --
    get_provider_model_scorecard deliberately aggregates across ALL
    series with no series_id scoping, so a shared class-level engine
    would leak rows between test methods (nothing here can filter them
    back out the way series_id-scoped tests do).
    """

    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()
        series_a = Series(name="Series A", author="Author A", profile_id="robbie")
        series_b = Series(name="Series B", author="Author B", profile_id="robbie")
        self.db.add_all([series_a, series_b])
        self.db.commit()
        self.db.refresh(series_a)
        self.db.refresh(series_b)
        self.series_a = series_a
        self.series_b = series_b

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def _add_row(
        self,
        *,
        series_id: int,
        provider: str = "anthropic",
        model_id: str = "claude-haiku-4-5-20251001",
        candidate_request_id: str | None = None,
        belongs_to_series_agreement: bool | None = True,
        shadow_belongs_to_series: bool | None = None,
        parsed_ok: bool = True,
        duration_ms: float | None = 100.0,
        total_cost_usd: float = 0.01,
        created_at: datetime | None = None,
    ) -> None:
        self.db.add(
            ShadowLLMCall(
                series_id=series_id,
                run_id="job-1",
                tier="C",
                gate_belongs_to_series=False,
                shadow_provider=provider,
                shadow_model_id=model_id,
                shadow_belongs_to_series=(
                    shadow_belongs_to_series if shadow_belongs_to_series is not None else belongs_to_series_agreement
                ),
                parsed_ok=parsed_ok,
                belongs_to_series_agreement=belongs_to_series_agreement,
                candidate_request_id=candidate_request_id,
                duration_ms=duration_ms,
                total_cost_usd=total_cost_usd,
                created_at=created_at or datetime.utcnow(),
            )
        )
        self.db.commit()

    def _entry_for(self, scorecard: list[dict], provider: str, model_id: str) -> dict:
        for entry in scorecard:
            if entry["provider"] == provider and entry["model_id"] == model_id:
                return entry
        self.fail(f"no scorecard entry for provider={provider!r} model_id={model_id!r}")

    def test_no_rows_returns_empty_list(self):
        self.assertEqual(get_provider_model_scorecard(self.db), [])

    def test_aggregates_across_series_not_scoped_to_one(self):
        """The whole point of this being its own module: one call
        surfaces both series' anthropic rows in a single entry, unlike
        every function in tier_c_shadow_store.py which takes a
        series_id.
        """
        self._add_row(series_id=self.series_a.id, belongs_to_series_agreement=True)
        self._add_row(series_id=self.series_b.id, belongs_to_series_agreement=True)

        scorecard = get_provider_model_scorecard(self.db)

        self.assertEqual(len(scorecard), 1)
        entry = scorecard[0]
        self.assertEqual(entry["provider"], "anthropic")
        self.assertEqual(entry["call_count"], 2)

    def test_distinct_provider_model_pairs_get_separate_entries(self):
        self._add_row(series_id=self.series_a.id, provider="anthropic", model_id="claude-haiku-4-5-20251001")
        self._add_row(series_id=self.series_a.id, provider="groq", model_id="llama-3.1-8b-instant")
        self._add_row(series_id=self.series_a.id, provider="openai", model_id="gpt-4o-mini")

        scorecard = get_provider_model_scorecard(self.db)

        self.assertEqual(len(scorecard), 3)
        providers = {entry["provider"] for entry in scorecard}
        self.assertEqual(providers, {"anthropic", "groq", "openai"})

    def test_gate_agreement_rate_excludes_unparseable_rows_from_denominator(self):
        self._add_row(series_id=self.series_a.id, belongs_to_series_agreement=True)
        self._add_row(series_id=self.series_a.id, belongs_to_series_agreement=False)
        # Unparseable: no comparable agreement value -- must not count
        # toward the denominator (it would wrongly dilute the rate).
        self._add_row(series_id=self.series_a.id, belongs_to_series_agreement=None, parsed_ok=False)

        entry = self._entry_for(get_provider_model_scorecard(self.db), "anthropic", "claude-haiku-4-5-20251001")

        self.assertEqual(entry["call_count"], 3)
        self.assertEqual(entry["gate_agreement_rate"], 0.5)  # 1 agree / 2 voters, not / 3

    def test_gate_agreement_rate_is_none_when_every_row_is_unparseable(self):
        self._add_row(series_id=self.series_a.id, belongs_to_series_agreement=None, parsed_ok=False)

        entry = self._entry_for(get_provider_model_scorecard(self.db), "anthropic", "claude-haiku-4-5-20251001")

        self.assertIsNone(entry["gate_agreement_rate"])

    def test_parse_failure_rate_counts_over_all_rows_in_window(self):
        self._add_row(series_id=self.series_a.id, parsed_ok=True, belongs_to_series_agreement=True)
        self._add_row(series_id=self.series_a.id, parsed_ok=True, belongs_to_series_agreement=True)
        self._add_row(series_id=self.series_a.id, parsed_ok=False, belongs_to_series_agreement=None)
        self._add_row(series_id=self.series_a.id, parsed_ok=False, belongs_to_series_agreement=None)

        entry = self._entry_for(get_provider_model_scorecard(self.db), "anthropic", "claude-haiku-4-5-20251001")

        self.assertEqual(entry["parse_failure_rate"], 0.5)  # 2/4, unlike gate_agreement_rate's 2-voter denominator

    def test_conflict_involvement_rate_is_none_with_no_candidate_request_id(self):
        self._add_row(series_id=self.series_a.id, candidate_request_id=None, belongs_to_series_agreement=True)

        entry = self._entry_for(get_provider_model_scorecard(self.db), "anthropic", "claude-haiku-4-5-20251001")

        self.assertIsNone(entry["conflict_involvement_rate"])

    def test_conflict_involvement_rate_reads_conflict_flag_from_sibling_providers(self):
        """anthropic's own row never disagrees with itself -- the
        conflict must be detected by looking at groq's sibling row for
        the SAME candidate_request_id, proving the cross-provider join
        actually happens rather than just inspecting each row in
        isolation.
        """
        cand = uuid.uuid4().hex
        self._add_row(
            series_id=self.series_a.id,
            provider="anthropic",
            candidate_request_id=cand,
            belongs_to_series_agreement=True,
            shadow_belongs_to_series=True,
        )
        self._add_row(
            series_id=self.series_a.id,
            provider="groq",
            model_id="llama-3.1-8b-instant",
            candidate_request_id=cand,
            belongs_to_series_agreement=False,
            shadow_belongs_to_series=False,
        )

        scorecard = get_provider_model_scorecard(self.db)
        anthropic_entry = self._entry_for(scorecard, "anthropic", "claude-haiku-4-5-20251001")
        groq_entry = self._entry_for(scorecard, "groq", "llama-3.1-8b-instant")

        self.assertEqual(anthropic_entry["conflict_involvement_rate"], 1.0)
        self.assertEqual(groq_entry["conflict_involvement_rate"], 1.0)

    def test_conflict_involvement_rate_zero_when_candidates_are_unanimous(self):
        cand = uuid.uuid4().hex
        self._add_row(
            series_id=self.series_a.id,
            provider="anthropic",
            candidate_request_id=cand,
            belongs_to_series_agreement=True,
            shadow_belongs_to_series=True,
        )
        self._add_row(
            series_id=self.series_a.id,
            provider="groq",
            model_id="llama-3.1-8b-instant",
            candidate_request_id=cand,
            belongs_to_series_agreement=True,
            shadow_belongs_to_series=True,
        )

        entry = self._entry_for(get_provider_model_scorecard(self.db), "anthropic", "claude-haiku-4-5-20251001")

        self.assertEqual(entry["conflict_involvement_rate"], 0.0)

    def test_conflict_involvement_rate_excludes_rows_with_no_candidate_request_id_from_denominator(self):
        cand = uuid.uuid4().hex
        # One conflicted candidate (2 rows, one per provider)...
        self._add_row(
            series_id=self.series_a.id,
            provider="anthropic",
            candidate_request_id=cand,
            belongs_to_series_agreement=True,
            shadow_belongs_to_series=True,
        )
        self._add_row(
            series_id=self.series_a.id,
            provider="groq",
            model_id="llama-3.1-8b-instant",
            candidate_request_id=cand,
            belongs_to_series_agreement=False,
            shadow_belongs_to_series=False,
        )
        # ...plus an anthropic row with no candidate_request_id at all,
        # which must be excluded from both numerator and denominator.
        self._add_row(series_id=self.series_a.id, provider="anthropic", candidate_request_id=None)

        entry = self._entry_for(get_provider_model_scorecard(self.db), "anthropic", "claude-haiku-4-5-20251001")

        self.assertEqual(entry["call_count"], 2)  # both anthropic rows count here
        self.assertEqual(entry["conflict_involvement_rate"], 1.0)  # but only 1 of 2 has a candidate_request_id

    def test_avg_latency_ms_ignores_null_rows(self):
        self._add_row(series_id=self.series_a.id, duration_ms=100.0)
        self._add_row(series_id=self.series_a.id, duration_ms=200.0)
        self._add_row(series_id=self.series_a.id, duration_ms=None)

        entry = self._entry_for(get_provider_model_scorecard(self.db), "anthropic", "claude-haiku-4-5-20251001")

        self.assertEqual(entry["avg_latency_ms"], 150.0)  # (100+200)/2, not /3

    def test_avg_latency_ms_is_none_when_every_row_predates_duration_tracking(self):
        self._add_row(series_id=self.series_a.id, duration_ms=None)

        entry = self._entry_for(get_provider_model_scorecard(self.db), "anthropic", "claude-haiku-4-5-20251001")

        self.assertIsNone(entry["avg_latency_ms"])

    def test_avg_cost_usd_averages_over_all_rows(self):
        self._add_row(series_id=self.series_a.id, total_cost_usd=0.01)
        self._add_row(series_id=self.series_a.id, total_cost_usd=0.03)

        entry = self._entry_for(get_provider_model_scorecard(self.db), "anthropic", "claude-haiku-4-5-20251001")

        self.assertAlmostEqual(entry["avg_cost_usd"], 0.02)

    def test_window_limits_rows_considered_to_the_most_recent(self):
        now = datetime.utcnow()
        # 3 old rows that disagree, 2 recent rows that agree -- with
        # window=2, only the 2 recent (agreeing) rows should count.
        self._add_row(
            series_id=self.series_a.id,
            belongs_to_series_agreement=False,
            created_at=now - timedelta(days=3),
        )
        self._add_row(
            series_id=self.series_a.id,
            belongs_to_series_agreement=False,
            created_at=now - timedelta(days=2),
        )
        self._add_row(
            series_id=self.series_a.id,
            belongs_to_series_agreement=False,
            created_at=now - timedelta(days=1),
        )
        self._add_row(series_id=self.series_a.id, belongs_to_series_agreement=True, created_at=now)
        self._add_row(
            series_id=self.series_a.id,
            belongs_to_series_agreement=True,
            created_at=now - timedelta(hours=1),
        )

        entry = self._entry_for(
            get_provider_model_scorecard(self.db, window=2), "anthropic", "claude-haiku-4-5-20251001"
        )

        self.assertEqual(entry["call_count"], 2)
        self.assertEqual(entry["gate_agreement_rate"], 1.0)

    def test_default_window_is_100(self):
        from services.provider_model_scorecard import DEFAULT_SCORECARD_WINDOW

        self.assertEqual(DEFAULT_SCORECARD_WINDOW, 100)


class ParseFailureSpikeDetectorTest(unittest.TestCase):
    """Step 11 Phase 3: unit coverage for `check_parse_failure_spikes` --
    alert-only, built directly on `get_provider_model_scorecard`'s
    `parse_failure_rate`. Same per-test fresh-engine pattern as
    `ProviderModelScorecardTest` above, for the same reason (no
    series_id scoping to isolate test data by).
    """

    def setUp(self):
        # Any test module that imports `main` runs Alembic on import, and
        # Alembic's fileConfig() disables every logger that already
        # exists -- including this one, which assertLogs (used by the
        # alert test below) cannot see through. So whether that test
        # passes would otherwise depend on which other files pytest
        # happened to collect alongside this one (same fix already
        # applied in tests/test_skeleton_store.py/tests/test_series_
        # discovery.py for their own loggers).
        scorecard_logger = logging.getLogger("services.provider_model_scorecard")
        was_disabled = scorecard_logger.disabled
        scorecard_logger.disabled = False
        self.addCleanup(setattr, scorecard_logger, "disabled", was_disabled)

        self.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.db = self.SessionLocal()
        series = Series(name="Test Series", author="Test Author", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def _add_row(self, *, provider: str, model_id: str, parsed_ok: bool) -> None:
        self.db.add(
            ShadowLLMCall(
                series_id=self.series.id,
                run_id="job-1",
                tier="C",
                gate_belongs_to_series=False,
                shadow_provider=provider,
                shadow_model_id=model_id,
                shadow_belongs_to_series=True if parsed_ok else None,
                parsed_ok=parsed_ok,
                belongs_to_series_agreement=True if parsed_ok else None,
                created_at=datetime.utcnow(),
            )
        )
        self.db.commit()

    def test_no_alert_when_parse_failure_rate_is_at_or_below_threshold(self):
        for _ in range(9):
            self._add_row(provider="anthropic", model_id="claude-haiku-4-5-20251001", parsed_ok=True)
        self._add_row(provider="anthropic", model_id="claude-haiku-4-5-20251001", parsed_ok=False)
        # 1/10 = 0.10 failure rate, exactly at a 0.10 threshold -- ">"
        # threshold means this must NOT alert (see docstring's ">="-vs-">"
        # note).
        alerts = check_parse_failure_spikes(self.db, threshold=0.10)

        self.assertEqual(alerts, [])

    def test_alert_when_parse_failure_rate_exceeds_threshold(self):
        for _ in range(7):
            self._add_row(provider="groq", model_id="llama-3.1-8b-instant", parsed_ok=True)
        for _ in range(3):
            self._add_row(provider="groq", model_id="llama-3.1-8b-instant", parsed_ok=False)
        # 3/10 = 0.30 > 0.15 threshold.

        with self.assertLogs("services.provider_model_scorecard", level="WARNING") as captured:
            alerts = check_parse_failure_spikes(self.db, threshold=0.15)

        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertIsInstance(alert, ProviderModelMetricsAlert)
        self.assertEqual(alert.provider, "groq")
        self.assertEqual(alert.model_id, "llama-3.1-8b-instant")
        self.assertEqual(alert.call_count, 10)
        self.assertAlmostEqual(alert.parse_failure_rate, 0.3)
        self.assertEqual(alert.threshold, 0.15)
        self.assertTrue(any("ProviderModelMetricsAlert" in message for message in captured.output))

    def test_only_offending_providers_are_included_when_multiple_exist(self):
        for _ in range(10):
            self._add_row(provider="anthropic", model_id="claude-haiku-4-5-20251001", parsed_ok=True)
        for _ in range(5):
            self._add_row(provider="openai", model_id="gpt-4o-mini", parsed_ok=True)
        for _ in range(5):
            self._add_row(provider="openai", model_id="gpt-4o-mini", parsed_ok=False)

        alerts = check_parse_failure_spikes(self.db, threshold=0.15)

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].provider, "openai")

    def test_uses_settings_defaults_when_window_and_threshold_omitted(self):
        import settings
        from unittest.mock import patch

        for _ in range(3):
            self._add_row(provider="anthropic", model_id="claude-haiku-4-5-20251001", parsed_ok=True)
        for _ in range(2):
            self._add_row(provider="anthropic", model_id="claude-haiku-4-5-20251001", parsed_ok=False)
        # 2/5 = 0.40 -- exceeds the real default threshold (0.15) but
        # would NOT exceed an artificially high one, proving the default
        # is actually read from settings rather than some other constant.
        with patch.object(settings, "PARSE_FAILURE_ALERT_THRESHOLD", 0.9):
            self.assertEqual(check_parse_failure_spikes(self.db), [])
        with patch.object(settings, "PARSE_FAILURE_ALERT_THRESHOLD", 0.15):
            self.assertEqual(len(check_parse_failure_spikes(self.db)), 1)

    def test_no_rows_produces_no_alerts(self):
        self.assertEqual(check_parse_failure_spikes(self.db), [])


if __name__ == "__main__":
    unittest.main()
