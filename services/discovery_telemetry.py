"""Per-job instrumentation for a single Check Now run -- pass-level timing,
web-search/LLM call counts, and LLM token usage.

Built to answer one concrete question empirically instead of by estimate:
how long does one discovery round actually take, and where does that time
go, for a long/under-indexed series (the case that motivated the multi-round
catch-up loop and its cache design)? See discovery_catchup_architecture_spec.md.

Threading note: `_fetch_all_providers_parallel` runs Google Books/
OpenLibrary/Hardcover/web-search concurrently across worker threads, but
only the "web" task ever touches a DiscoveryTelemetry instance (the other
three providers make no web-search/LLM calls), and passes are otherwise
strictly sequential within one `run_series_check` call (targeted ->
author-fallback -> reconciliation -> missing-volume). So a single shared
`_current_pass` label, guarded by a lock, is safe: no two passes are ever
actually mid-flight on web-search/LLM at the same wall-clock moment, despite
the object being shared across threads.

Every parameter that touches this module elsewhere is optional and defaults
to None, matching the existing `diagnostics` parameter convention already
used throughout discovery_engine.py -- a caller that doesn't pass a
DiscoveryTelemetry instance pays no cost and changes no behavior.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class DiscoveryTelemetry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current_pass = "unlabeled"
        self.passes: list[dict] = []
        self.web_search_calls: list[dict] = []
        self.llm_calls: list[dict] = []
        # PB-9: per-provider call/failure counts (google/openlibrary/
        # hardcover/web/apify/fixture -- whatever `provider_protocol.py`
        # adapter name is passed) and named decision-point outcomes
        # (confidence-grade distribution, all_providers_failed occurrences,
        # author-fallback trigger rate, precheck short-circuit vs full-loop
        # rate, FIX-PB-7's skeleton-update-failure counter, etc), for
        # cost/quality comparison across discovery runs.
        self.provider_calls: list[dict] = []
        self.gate_outcomes: list[dict] = []
        # RT-1b: agentic_hooks.py's side-channel record of a tool/provider
        # call, kept as its own list rather than folded into
        # `provider_calls` -- deliberately not "ok"/"duration_s" shaped
        # like that PB-9 counter (it doesn't have a pass/fail outcome, just
        # a query + result size), and additive-only: nothing here reads or
        # mutates `provider_calls`/`gate_outcomes`, so this can never change
        # PB-9's existing counts.
        self.tool_calls: list[dict] = []

    @contextmanager
    def pass_scope(self, name: str):
        with self._lock:
            previous = self._current_pass
            self._current_pass = name
        started = time.monotonic()
        started_wall = time.time()
        try:
            yield
        finally:
            duration = time.monotonic() - started
            with self._lock:
                self.passes.append(
                    {
                        "pass": name,
                        "started_at": started_wall,
                        "duration_s": round(duration, 3),
                    }
                )
                self._current_pass = previous

    def record_web_search_call(self, *, query: str, duration_s: float) -> None:
        with self._lock:
            self.web_search_calls.append(
                {"pass": self._current_pass, "query": query, "duration_s": round(duration_s, 3)}
            )

    def record_llm_call(self, *, duration_s: float, tokens_in: int = 0, tokens_out: int = 0) -> None:
        with self._lock:
            self.llm_calls.append(
                {
                    "pass": self._current_pass,
                    "duration_s": round(duration_s, 3),
                    "tokens_in": int(tokens_in or 0),
                    "tokens_out": int(tokens_out or 0),
                }
            )

    def record_provider_call(self, provider: str, *, ok: bool, duration_s: float) -> None:
        """PB-9: one entry per `provider_protocol.py` adapter call --
        `ok` mirrors that adapter's own `ProviderFetchResult.ok` (see
        provider_protocol.py's module docstring for why that's the sole
        failure signal now), independent of this pass's per-query
        `record_web_search_call` bookkeeping below (that one already
        existed for Serper specifically; this one is the general,
        every-provider counterpart PP-2/PP-3 made possible).
        """
        with self._lock:
            self.provider_calls.append(
                {"pass": self._current_pass, "provider": provider, "ok": bool(ok), "duration_s": round(duration_s, 3)}
            )

    def record_tool_call(self, *, provider: str, query: str, result_size: int) -> None:
        """RT-1b: called by `agentic_hooks.record_tool_call` -- one entry
        per agentic-substrate-observed provider/tool invocation. Purely
        additive bookkeeping for the agentic tracing layer; nothing in the
        existing PB-9 `record_provider_call`/`record_gate_outcome` counters
        or their `by_provider`/`by_gate` summary breakdowns reads this.
        """
        with self._lock:
            self.tool_calls.append(
                {
                    "pass": self._current_pass,
                    "provider": str(provider),
                    "query": str(query),
                    "result_size": int(result_size),
                }
            )

    def record_gate_outcome(self, gate: str, outcome: str) -> None:
        """PB-9: a labeled decision point resolved to `outcome` --
        e.g. record_gate_outcome("confidence_grade", "high"),
        record_gate_outcome("all_providers_failed", "true"),
        record_gate_outcome("author_fallback", "triggered"),
        record_gate_outcome("precheck", "short_circuited"),
        record_gate_outcome("skeleton_update", "failed"). Purely additive
        counting -- callers choose their own gate/outcome vocabulary, this
        module just tallies it in summary()'s by_gate breakdown.
        """
        with self._lock:
            self.gate_outcomes.append({"pass": self._current_pass, "gate": str(gate), "outcome": str(outcome)})

    def summary(self) -> dict:
        with self._lock:
            passes = list(self.passes)
            web_search_calls = list(self.web_search_calls)
            llm_calls = list(self.llm_calls)
            provider_calls = list(self.provider_calls)
            gate_outcomes = list(self.gate_outcomes)
            tool_calls = list(self.tool_calls)

        by_pass: dict[str, dict] = {}

        def _bucket(name: str) -> dict:
            return by_pass.setdefault(
                name,
                {
                    "pass_duration_s": 0.0,
                    "web_search_calls": 0,
                    "web_search_duration_s": 0.0,
                    "llm_calls": 0,
                    "llm_duration_s": 0.0,
                    "tokens_in": 0,
                    "tokens_out": 0,
                },
            )

        for entry in passes:
            bucket = _bucket(entry["pass"])
            bucket["pass_duration_s"] = round(bucket["pass_duration_s"] + entry["duration_s"], 3)
        for call in web_search_calls:
            bucket = _bucket(call["pass"])
            bucket["web_search_calls"] += 1
            bucket["web_search_duration_s"] = round(bucket["web_search_duration_s"] + call["duration_s"], 3)
        for call in llm_calls:
            bucket = _bucket(call["pass"])
            bucket["llm_calls"] += 1
            bucket["llm_duration_s"] = round(bucket["llm_duration_s"] + call["duration_s"], 3)
            bucket["tokens_in"] += call["tokens_in"]
            bucket["tokens_out"] += call["tokens_out"]

        by_provider: dict[str, dict] = {}
        for call in provider_calls:
            bucket = by_provider.setdefault(
                call["provider"], {"calls": 0, "ok": 0, "failed": 0, "duration_s": 0.0}
            )
            bucket["calls"] += 1
            bucket["ok" if call["ok"] else "failed"] += 1
            bucket["duration_s"] = round(bucket["duration_s"] + call["duration_s"], 3)

        by_gate: dict[str, dict[str, int]] = {}
        for entry in gate_outcomes:
            outcomes = by_gate.setdefault(entry["gate"], {})
            outcomes[entry["outcome"]] = outcomes.get(entry["outcome"], 0) + 1

        return {
            "by_pass": by_pass,
            "by_provider": by_provider,
            "by_gate": by_gate,
            "total_web_search_calls": len(web_search_calls),
            "total_llm_calls": len(llm_calls),
            "total_tokens_in": sum(c["tokens_in"] for c in llm_calls),
            "total_tokens_out": sum(c["tokens_out"] for c in llm_calls),
            # RT-1b: additive-only counter for agentic_hooks.py's tool-call
            # tracing -- not consumed by anything routing/confidence-related,
            # purely informational.
            "total_tool_calls": len(tool_calls),
        }


def record_agentic_evaluation(series_id: int, report: dict) -> None:
    """Phase 1 evaluation harness (`services/agentic_evaluation_harness.py`):
    logs one completed shadow-mode agentic-vs-live comparison report for
    `series_id`.

    This module has no structured/persisted JSON-log store of its own --
    `DiscoveryTelemetry` above is a per-run, in-memory counters object,
    never written anywhere durable. Per the harness's own spec ("if no
    structured storage exists yet, log via the existing logging mechanism
    with a clear tag"), this is that log-only fallback, tagged
    `agentic_evaluation` so it's easy to grep/filter for later analysis.
    Wiring in real structured storage (a table, a file, etc.) is explicit
    future work, not this ticket.

    Fail-soft: a logging/serialization failure here must never propagate
    back into the harness that called this -- `run_agentic_evaluation_
    for_series` already wraps its own call to this in a try/except, but
    this function guards itself too so it's safe to call from anywhere.
    """
    try:
        logger.info(
            "agentic_evaluation series_id=%s report=%s",
            series_id,
            json.dumps(report, default=str),
        )
    except Exception:
        logger.exception("record_agentic_evaluation: failed to log report for series_id=%s", series_id)


def record_agentic_batch(series_ids: list[int], batch_report: dict) -> None:
    """Phase 1 batch orchestrator (`services/agentic_batch_orchestrator.py`):
    logs one completed batch of shadow-mode agentic-vs-live comparison
    reports for `series_ids`.

    Purely diagnostic, same log-only fallback as `record_agentic_
    evaluation` above (tagged `agentic_batch` instead, so a batch summary
    is distinguishable from an individual per-series report when
    grepping/filtering logs) -- see that function's docstring for why
    there's no structured/persisted store here yet. Deliberately does
    NOT call `record_agentic_evaluation` per series itself -- `services.
    agentic_batch_orchestrator.run_batch_agentic_evaluations` uses
    `services.agentic_replay_runner.replay_and_compare` (not `services.
    agentic_evaluation_harness.run_agentic_evaluation_for_series`)
    precisely so each series' report is logged exactly once, as part of
    this one batch-level call, rather than once per series here plus
    once again at the batch level.

    Fail-soft: a logging/serialization failure here must never propagate
    back into the batch orchestrator that called this.
    """
    try:
        logger.info(
            "agentic_batch series_ids=%s count=%s batch_report=%s",
            list(series_ids or []),
            len(series_ids or []),
            json.dumps(batch_report, default=str),
        )
    except Exception:
        logger.exception("record_agentic_batch: failed to log batch report for series_ids=%s", series_ids)


def record_agentic_drift(series_id: int, drift_report: dict) -> None:
    """Phase 1 drift detector (`services/agentic_drift_detector.py`, called
    from `services/agentic_evaluation_harness.py`): logs one series'
    live-vs-agentic-preview skeleton drift report.

    Same log-only fallback as `record_agentic_evaluation`/`record_agentic_
    batch` above (tagged `agentic_drift`), fail-soft.
    """
    try:
        logger.info(
            "agentic_drift series_id=%s drift_report=%s",
            series_id,
            json.dumps(drift_report, default=str),
        )
    except Exception:
        logger.exception("record_agentic_drift: failed to log drift report for series_id=%s", series_id)


def record_agentic_ttl(series_id: int, ttl_report: dict) -> None:
    """Phase 1 TTL sweep validator (`services/agentic_ttl_validator.py`,
    called from `services/agentic_evaluation_harness.py`): logs one
    series' discovered/probe TTL validation report.

    Same log-only fallback as the other `record_agentic_*` helpers above
    (tagged `agentic_ttl`), fail-soft.
    """
    try:
        logger.info(
            "agentic_ttl series_id=%s ttl_report=%s",
            series_id,
            json.dumps(ttl_report, default=str),
        )
    except Exception:
        logger.exception("record_agentic_ttl: failed to log TTL report for series_id=%s", series_id)


def record_agentic_full_report(series_id: int, report: dict) -> None:
    """Phase 1 report generator (`services/agentic_report_generator.py`,
    called from `services/agentic_evaluation_harness.generate_full_
    agentic_report`): logs one series' consolidated JSON evaluation
    report (live + agentic + comparison + drift + TTL, merged).

    Same log-only fallback as the other `record_agentic_*` helpers above
    (tagged `agentic_full_report`), fail-soft.
    """
    try:
        logger.info(
            "agentic_full_report series_id=%s report=%s",
            series_id,
            json.dumps(report, default=str),
        )
    except Exception:
        logger.exception("record_agentic_full_report: failed to log report for series_id=%s", series_id)


def record_agentic_full_html(series_id: int, html: str) -> None:
    """Phase 1 report generator (`services/agentic_report_generator.py`,
    called from `services/agentic_evaluation_harness.generate_full_
    agentic_html`): logs one series' HTML-style evaluation report.

    `html` is already a plain, pre-escaped string (see `services/
    agentic_report_generator.py`'s module docstring) -- logged as-is,
    tagged `agentic_full_html`, fail-soft like every other `record_
    agentic_*` helper above.
    """
    try:
        logger.info("agentic_full_html series_id=%s html=%s", series_id, html)
    except Exception:
        logger.exception("record_agentic_full_html: failed to log HTML report for series_id=%s", series_id)


@contextmanager
def maybe_pass_scope(telemetry: "DiscoveryTelemetry | None", name: str):
    """No-op passthrough when telemetry is None, so call sites don't need
    an `if telemetry:` guard around every pass boundary.
    """
    if telemetry is None:
        yield
    else:
        with telemetry.pass_scope(name):
            yield
