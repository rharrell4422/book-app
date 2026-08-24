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

import threading
import time
from contextlib import contextmanager


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
        }


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
