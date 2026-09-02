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
import uuid
from contextlib import contextmanager

from services.llm_pricing import get_price_per_million

logger = logging.getLogger(__name__)

# Phase 9 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_
# evaluation.md`, not re-litigated here): the agentic observability &
# telemetry layer's global, in-memory counters. Deliberately process-
# wide (not per-series, not per-request) and deliberately NOT the same
# thing as `DiscoveryTelemetry` above (a per-*run* object a caller
# constructs and discards) -- these persist for the life of the process,
# the same way a Prometheus-style counter would, so an admin can see
# "how much agentic work has this process done since it started" via
# `GET /admin/agentic/metrics` without needing a request-scoped object
# threaded through every agentic call site.
#
# Guarded by one shared lock (simple, not per-counter -- the spec's own
# "thread-safe via simple locks", and every increment here is O(1) dict
# math, so contention is a non-issue) rather than e.g. `threading.local`
# or `collections.Counter` with no lock at all, since multiple worker
# threads (see this module's own docstring re: `_fetch_all_providers_
# parallel`) can legitimately be mid-turn concurrently across different
# requests.
_agentic_metrics_lock = threading.Lock()
_agentic_metrics: dict[str, int] = {
    "agentic_promotion_attempts": 0,
    "agentic_promotion_use_agentic": 0,
    "agentic_promotion_use_live": 0,
    "agentic_promotion_rejected": 0,
    "agentic_safety_violations": 0,
    "agentic_cache_hits": 0,
    "agentic_cache_misses": 0,
    "agentic_turn_invocations": 0,
    "agentic_turn_failures": 0,
}


def _increment_agentic_metric(name: str, amount: int = 1) -> None:
    """Shared, fail-soft increment behind every `record_agentic_*_metric`
    helper below -- never raises back into its caller (every one of
    those callers is itself a fail-soft side-channel already, same
    convention as every other `record_agentic_*` function in this
    module).
    """
    try:
        with _agentic_metrics_lock:
            _agentic_metrics[name] = _agentic_metrics.get(name, 0) + amount
    except Exception:
        logger.exception("_increment_agentic_metric: failed to increment %s", name)


def get_agentic_metrics() -> dict:
    """Returns every Phase 9 agentic counter as a stable, sorted-by-key
    dict (`{"agentic_cache_hits": 0, "agentic_cache_misses": 0, ...}`) --
    a plain snapshot copy, never the live dict itself, so a caller can't
    accidentally mutate process-wide counters by holding onto the
    returned object. Fail-soft: any unexpected error yields `{}` rather
    than raising.
    """
    try:
        with _agentic_metrics_lock:
            snapshot = dict(_agentic_metrics)
        return dict(sorted(snapshot.items()))
    except Exception:
        logger.exception("get_agentic_metrics: failed to read counters; returning empty dict")
        return {}


def record_agentic_promotion_metric(outcome: str) -> None:
    """Phase 9: called once per `agentic/promotion_evaluator.
    evaluate_promotion` decision that's actually *computed* (i.e. from
    inside `_evaluate_once`, not on a Phase 8 cache hit -- a cache hit
    reuses an already-counted decision, it doesn't make a new one).
    Increments `agentic_promotion_attempts` unconditionally, plus
    whichever of `agentic_promotion_use_agentic`/`agentic_promotion_
    use_live`/`agentic_promotion_rejected` matches `outcome`
    (`"reject_agentic"` maps to the `_rejected` counter's shorter name).
    An unrecognized `outcome` still counts as an attempt, just with no
    matching outcome-specific counter incremented.
    """
    _increment_agentic_metric("agentic_promotion_attempts")
    if outcome == "use_agentic":
        _increment_agentic_metric("agentic_promotion_use_agentic")
    elif outcome == "use_live":
        _increment_agentic_metric("agentic_promotion_use_live")
    elif outcome == "reject_agentic":
        _increment_agentic_metric("agentic_promotion_rejected")


def record_agentic_cache_hit() -> None:
    """Phase 9: called by `agentic/resolution.resolve_routing_
    decisions` each time its optional Phase 8 `cache` already held the
    promotion decision it asked for (no recomputation needed).
    """
    _increment_agentic_metric("agentic_cache_hits")


def record_agentic_cache_miss() -> None:
    """Phase 9: called by `agentic/resolution.resolve_routing_
    decisions` each time its optional Phase 8 `cache` did NOT already
    hold the promotion decision it asked for (so it had to be looked up/
    computed via `cache`'s own `compute_fn`, then cached for next time).
    """
    _increment_agentic_metric("agentic_cache_misses")


def record_agentic_turn_invocation() -> None:
    """Phase 9: called by `agents/series_agent.py`'s `_run_agentic_turn_
    guarded` each time it actually invokes `agents/agentic_series_agent.
    run_agentic_turn` for real (never on a Phase 8 shared-state reuse --
    see that helper's own docstring). Watching this counter stay at (at
    most) one increment per `run_series_check` call is exactly how an
    admin would confirm Phase 8's once-per-turn guard is holding.
    """
    _increment_agentic_metric("agentic_turn_invocations")


def record_agentic_turn_failure() -> None:
    """Phase 9: called by `agents/series_agent.py`'s `_run_agentic_turn_
    guarded` each time the one real `run_agentic_turn` invocation it just
    made (see `record_agentic_turn_invocation` above) raised.
    """
    _increment_agentic_metric("agentic_turn_failures")


class DiscoveryTelemetry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current_pass = "unlabeled"
        # HTA Orchestrator Step 3: tier/correlation_id context, set for the
        # duration of a `pass_scope()` block the same way `_current_pass`
        # already is -- see `pass_scope`'s own docstring for why both are
        # tracked as nested, save/restore state rather than being passed
        # explicitly to every `record_llm_call()` call site.
        self._current_tier: str | None = None
        self._current_correlation_id: str | None = None
        self.passes: list[dict] = []
        self.web_search_calls: list[dict] = []
        self.llm_calls: list[dict] = []
        # HTA Orchestrator Step 4: shadow-mode LLM calls (`call_llm(...,
        # shadow=True)`), recorded via `record_shadow_llm_call` -- kept as
        # its own list, entirely separate from `llm_calls` above, so a
        # shadow call can never be double-counted into (or silently
        # inflate) production `total_llm_calls`/`total_cost_usd`. See
        # `record_shadow_llm_call`'s docstring for why this mirrors
        # `record_llm_call` field-for-field rather than reusing it.
        self.shadow_llm_calls: list[dict] = []
        # HTA Orchestrator Step 7: Tier C shadow responses scored against
        # the deterministic gate they shadowed -- kept as its own list,
        # separate from `shadow_llm_calls` above (which is cost/token-
        # shaped only), since this holds the semantic agreement/
        # disagreement comparison instead. Per-run, in-memory only; see
        # `record_tier_c_shadow_score`'s docstring for why no cross-run
        # aggregate (e.g. "per-series accuracy") is computed here.
        self.tier_c_shadow_scores: list[dict] = []
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
    def pass_scope(self, name: str, *, tier: str | None = None):
        """`tier` (HTA Orchestrator Step 3, e.g. `"A"` for a structuring
        pass, `"B"` for reconciliation) is an explicit label attached to
        every `record_llm_call()` made while this scope is active -- not
        inferred from `name`, since `name` already varies dynamically
        (`targeted`/`author_fallback`/`missing_volume`/their `_refinement`
        siblings/`reconciliation`) in ways that don't map 1:1 onto a tier.

        A fresh `correlation_id` is generated on every entry into this
        context manager -- once per actual pass *invocation*, not once per
        pass name/type -- so a job with multiple rounds produces multiple
        correlation_ids per pass type, one per real firing (see `summary()`'s
        `by_correlation_id` breakdown). `tier`/`correlation_id` nest and
        restore exactly like `_current_pass` already does, so a caller that
        doesn't pass `tier` (or that nests scopes) sees unchanged behavior.
        """
        correlation_id = uuid.uuid4().hex
        with self._lock:
            previous_pass = self._current_pass
            previous_tier = self._current_tier
            previous_correlation_id = self._current_correlation_id
            self._current_pass = name
            self._current_tier = tier
            self._current_correlation_id = correlation_id
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
                self._current_pass = previous_pass
                self._current_tier = previous_tier
                self._current_correlation_id = previous_correlation_id

    def record_web_search_call(self, *, query: str, duration_s: float) -> None:
        with self._lock:
            self.web_search_calls.append(
                {"pass": self._current_pass, "query": query, "duration_s": round(duration_s, 3)}
            )

    def record_llm_call(
        self,
        *,
        duration_s: float,
        tokens_in: int = 0,
        tokens_out: int = 0,
        model_id: str | None = None,
    ) -> None:
        """`model_id` (HTA Orchestrator Step 2) is optional and purely
        additive -- an existing caller that doesn't pass it keeps working
        exactly as before, with `cost_usd` recorded as `0.0` and no tier/
        model attribution. `tier`/`correlation_id` are never passed in
        directly here; they're read from whatever `pass_scope()` is
        currently active (see that method's docstring) so this call shape
        doesn't grow a parameter for every future piece of pass-scoped
        context.

        Cost is computed here, not by the caller, so the fail-soft
        guarantee for an unrecognized `model_id` (`cost_usd=0.0`, a logged
        warning, never a raised exception -- see `services/llm_pricing.py`)
        has exactly one implementation rather than being duplicated at
        every call site.
        """
        tokens_in = int(tokens_in or 0)
        tokens_out = int(tokens_out or 0)
        cost_usd = 0.0
        if model_id:
            pricing = get_price_per_million(model_id)
            if pricing is None:
                logger.warning(
                    "record_llm_call: no pricing entry for model_id=%s; recording cost_usd=0.0", model_id
                )
            else:
                price_in_per_million, price_out_per_million = pricing
                cost_usd = (tokens_in * price_in_per_million + tokens_out * price_out_per_million) / 1_000_000
        with self._lock:
            self.llm_calls.append(
                {
                    "pass": self._current_pass,
                    "duration_s": round(duration_s, 3),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "model_id": model_id,
                    "tier": self._current_tier,
                    "correlation_id": self._current_correlation_id,
                    "cost_usd": cost_usd,
                }
            )

    def record_shadow_llm_call(
        self,
        *,
        duration_s: float,
        tokens_in: int = 0,
        tokens_out: int = 0,
        model_id: str | None = None,
    ) -> None:
        """HTA Orchestrator Step 4: shadow-mode counterpart to `record_
        llm_call` above -- same fields, same fail-soft `cost_usd`
        computation, same `pass`/`tier`/`correlation_id` read from
        whichever `pass_scope()` is currently active. The only
        difference is *where* the entry lands: `self.shadow_llm_calls`
        instead of `self.llm_calls`, so `summary()` can report shadow
        totals in their own section without ever mixing them into
        production `total_llm_calls`/`total_cost_usd`/`per_model`/
        `per_tier`.

        Callers choose this over `record_llm_call` themselves -- this
        module has no opinion on when a call is "shadow"; that's decided
        entirely by whoever calls `call_llm(..., shadow=True)` and then
        picks which of these two recording functions to call with the
        result (see `llm_client.call_llm`'s `shadow` docstring).
        """
        tokens_in = int(tokens_in or 0)
        tokens_out = int(tokens_out or 0)
        cost_usd = 0.0
        if model_id:
            pricing = get_price_per_million(model_id)
            if pricing is None:
                logger.warning(
                    "record_shadow_llm_call: no pricing entry for model_id=%s; recording cost_usd=0.0", model_id
                )
            else:
                price_in_per_million, price_out_per_million = pricing
                cost_usd = (tokens_in * price_in_per_million + tokens_out * price_out_per_million) / 1_000_000
        with self._lock:
            self.shadow_llm_calls.append(
                {
                    "pass": self._current_pass,
                    "duration_s": round(duration_s, 3),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "model_id": model_id,
                    "tier": self._current_tier,
                    "correlation_id": self._current_correlation_id,
                    "cost_usd": cost_usd,
                }
            )

    def record_tier_c_shadow_score(
        self,
        *,
        parsed_ok: bool,
        belongs_to_series_agreement: bool | None = None,
        inferred_number_agreement: bool | None = None,
        tier_c_confidence: str | None = None,
        confidence_aligned: bool | None = None,
        tier_c_alternate_title_flag: bool | None = None,
    ) -> None:
        """HTA Orchestrator Step 7: records one Tier C shadow call's
        agreement/disagreement against the deterministic gate it shadowed
        (see `agents/series_agent.py`'s `_score_tier_c_shadow_response`,
        which computes every field this accepts). Same `pass`/
        `correlation_id` attribution convention as `record_llm_call`/
        `record_shadow_llm_call` -- read from whichever `pass_scope()` is
        currently active, not passed in directly.

        Deliberately per-run, in-memory only, same as every other list on
        this object -- `summary()`'s `tier_c_shadow` section below reports
        totals for *this* run's candidates, not a cross-run/per-series
        accuracy figure. Computing that requires a persisted store (a
        `shadow_llm_calls` DB table, per Step 7's architectural diff,
        section 5) that does not exist yet; until it does, promotion
        decisions must not be based on any aggregate derived from this
        method across multiple runs.
        """
        with self._lock:
            self.tier_c_shadow_scores.append(
                {
                    "pass": self._current_pass,
                    "correlation_id": self._current_correlation_id,
                    "parsed_ok": bool(parsed_ok),
                    "belongs_to_series_agreement": belongs_to_series_agreement,
                    "inferred_number_agreement": inferred_number_agreement,
                    "tier_c_confidence": tier_c_confidence,
                    "confidence_aligned": confidence_aligned,
                    "tier_c_alternate_title_flag": tier_c_alternate_title_flag,
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
            shadow_llm_calls = list(self.shadow_llm_calls)
            tier_c_shadow_scores = list(self.tier_c_shadow_scores)
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
                    "cost_usd": 0.0,
                },
            )

        for entry in passes:
            bucket = _bucket(entry["pass"])
            bucket["pass_duration_s"] = round(bucket["pass_duration_s"] + entry["duration_s"], 3)
        for call in web_search_calls:
            bucket = _bucket(call["pass"])
            bucket["web_search_calls"] += 1
            bucket["web_search_duration_s"] = round(bucket["web_search_duration_s"] + call["duration_s"], 3)
        # HTA Orchestrator Step 2/3: model_id/tier/cost_usd/correlation_id
        # are all optional -- entries recorded before this change (or by a
        # caller that still doesn't pass model_id) simply have `None` for
        # each and fall into the "unknown"/"none" buckets below, exactly
        # like an untagged `by_provider`/`by_gate` entry would.
        per_model: dict[str, dict] = {}
        per_tier: dict[str, dict] = {}
        by_correlation_id: dict[str, dict] = {}
        total_cost_usd = 0.0

        def _cost_bucket(bucket_map: dict[str, dict], key: str) -> dict:
            return bucket_map.setdefault(
                key, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
            )

        for call in llm_calls:
            bucket = _bucket(call["pass"])
            bucket["llm_calls"] += 1
            bucket["llm_duration_s"] = round(bucket["llm_duration_s"] + call["duration_s"], 3)
            bucket["tokens_in"] += call["tokens_in"]
            bucket["tokens_out"] += call["tokens_out"]
            bucket["cost_usd"] = round(bucket["cost_usd"] + call["cost_usd"], 6)

            total_cost_usd += call["cost_usd"]

            for bucket_map, key in (
                (per_model, call.get("model_id") or "unknown"),
                (per_tier, call.get("tier") or "none"),
            ):
                dim_bucket = _cost_bucket(bucket_map, key)
                dim_bucket["calls"] += 1
                dim_bucket["tokens_in"] += call["tokens_in"]
                dim_bucket["tokens_out"] += call["tokens_out"]
                dim_bucket["cost_usd"] = round(dim_bucket["cost_usd"] + call["cost_usd"], 6)

            # by_correlation_id is inherently single-tier per key -- a
            # correlation_id is minted once per pass_scope() entry, and
            # `tier` doesn't change mid-scope -- so a plain `tier` string
            # is stored here rather than a tier_counts breakdown (see the
            # Step 3 final-precision review for why the conditional shape
            # had no reachable case to serve).
            corr_key = call.get("correlation_id") or "none"
            corr_bucket = by_correlation_id.setdefault(
                corr_key,
                {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "tier": call.get("tier")},
            )
            corr_bucket["calls"] += 1
            corr_bucket["tokens_in"] += call["tokens_in"]
            corr_bucket["tokens_out"] += call["tokens_out"]
            corr_bucket["cost_usd"] = round(corr_bucket["cost_usd"] + call["cost_usd"], 6)

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

        # HTA Orchestrator Step 4: shadow-mode LLM calls get their own,
        # separate aggregation -- deliberately NOT folded into `per_model`/
        # `per_tier`/`total_llm_calls`/`total_cost_usd` above (those stay
        # exactly what they were pre-Step-4: production-only), so a shadow
        # call can never inflate what an admin reads as "real" cost/call
        # counts. Same per_model/per_tier shape as the production
        # breakdown, reusing `_cost_bucket` for identical bucket shape.
        shadow_per_model: dict[str, dict] = {}
        shadow_per_tier: dict[str, dict] = {}
        shadow_total_cost_usd = 0.0
        for call in shadow_llm_calls:
            shadow_total_cost_usd += call["cost_usd"]
            for bucket_map, key in (
                (shadow_per_model, call.get("model_id") or "unknown"),
                (shadow_per_tier, call.get("tier") or "none"),
            ):
                dim_bucket = _cost_bucket(bucket_map, key)
                dim_bucket["calls"] += 1
                dim_bucket["tokens_in"] += call["tokens_in"]
                dim_bucket["tokens_out"] += call["tokens_out"]
                dim_bucket["cost_usd"] = round(dim_bucket["cost_usd"] + call["cost_usd"], 6)

        # HTA Orchestrator Step 7: per-run-only Tier C shadow scoring
        # totals -- counts, not a persisted/cross-run accuracy percentage
        # (see `record_tier_c_shadow_score`'s docstring for why). Every
        # `*_agreements`/`*_disagreements` pair only counts entries where
        # that specific comparison was actually possible (`agreement is
        # not None`), so an unparseable or partially-missing response
        # contributes to `parse_failures` without silently skewing the
        # agreement rate for the fields it did resolve.
        tier_c_parse_failures = sum(1 for s in tier_c_shadow_scores if not s["parsed_ok"])
        tier_c_belongs_agreements = sum(
            1 for s in tier_c_shadow_scores if s["belongs_to_series_agreement"] is True
        )
        tier_c_belongs_disagreements = sum(
            1 for s in tier_c_shadow_scores if s["belongs_to_series_agreement"] is False
        )
        tier_c_number_agreements = sum(
            1 for s in tier_c_shadow_scores if s["inferred_number_agreement"] is True
        )
        tier_c_number_disagreements = sum(
            1 for s in tier_c_shadow_scores if s["inferred_number_agreement"] is False
        )
        tier_c_confidence_aligned = sum(1 for s in tier_c_shadow_scores if s["confidence_aligned"] is True)
        tier_c_confidence_misaligned = sum(
            1 for s in tier_c_shadow_scores if s["confidence_aligned"] is False
        )
        tier_c_alternate_title_flagged = sum(
            1 for s in tier_c_shadow_scores if s["tier_c_alternate_title_flag"] is True
        )

        return {
            "by_pass": by_pass,
            "by_provider": by_provider,
            "by_gate": by_gate,
            # HTA Orchestrator Step 2/3: per-model and per-tier cost/token
            # breakdowns (mirrors by_provider's shape, including a "calls"
            # count so average-cost-per-call is computable), plus a
            # per-correlation_id breakdown keyed to a single pass_scope()
            # invocation -- see pass_scope()'s docstring.
            "per_model": per_model,
            "per_tier": per_tier,
            "by_correlation_id": by_correlation_id,
            "total_web_search_calls": len(web_search_calls),
            "total_llm_calls": len(llm_calls),
            "total_tokens_in": sum(c["tokens_in"] for c in llm_calls),
            "total_tokens_out": sum(c["tokens_out"] for c in llm_calls),
            "total_cost_usd": round(total_cost_usd, 6),
            # RT-1b: additive-only counter for agentic_hooks.py's tool-call
            # tracing -- not consumed by anything routing/confidence-related,
            # purely informational.
            "total_tool_calls": len(tool_calls),
            # HTA Orchestrator Step 4: shadow-mode LLM call totals, kept in
            # their own section rather than merged into any of the
            # production keys above -- see the aggregation loop's own
            # comment for why. A run with no shadow calls at all reports
            # zeroed-out counts here, exactly like an empty `llm_calls`
            # would for the production section.
            "shadow": {
                "total_llm_calls": len(shadow_llm_calls),
                "total_tokens_in": sum(c["tokens_in"] for c in shadow_llm_calls),
                "total_tokens_out": sum(c["tokens_out"] for c in shadow_llm_calls),
                "total_cost_usd": round(shadow_total_cost_usd, 6),
                "per_model": shadow_per_model,
                "per_tier": shadow_per_tier,
            },
            # HTA Orchestrator Step 7: Tier C shadow-vs-gate agreement
            # scoring, this run only -- see `record_tier_c_shadow_score`'s
            # docstring for why this is deliberately not a cross-run/
            # per-series accuracy percentage yet.
            "tier_c_shadow": {
                "total_scored": len(tier_c_shadow_scores),
                "parse_failures": tier_c_parse_failures,
                "belongs_to_series_agreements": tier_c_belongs_agreements,
                "belongs_to_series_disagreements": tier_c_belongs_disagreements,
                "inferred_number_agreements": tier_c_number_agreements,
                "inferred_number_disagreements": tier_c_number_disagreements,
                "confidence_aligned_on_disagreement": tier_c_confidence_aligned,
                "confidence_misaligned_on_disagreement": tier_c_confidence_misaligned,
                "alternate_title_flagged": tier_c_alternate_title_flagged,
            },
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


def record_agentic_promotion_plan(series_id: int, plan: dict) -> None:
    """Phase 2 kickoff (`services/agentic_promotion_plan.py`): logs one
    series' Phase 2 promotion plan for auditing -- what promotion would
    require, the current alignment/requirement signals, and the derived
    risk level, at the moment this plan was generated.

    Same log-only fallback as the other `record_agentic_*` helpers above
    (tagged `agentic_promotion_plan`), fail-soft. No structured/persisted
    store exists yet -- see `record_agentic_evaluation`'s docstring for
    why; wiring one in is explicit future work, not this ticket.
    """
    try:
        logger.info(
            "agentic_promotion_plan series_id=%s plan=%s",
            series_id,
            json.dumps(plan, default=str),
        )
    except Exception:
        logger.exception("record_agentic_promotion_plan: failed to log plan for series_id=%s", series_id)


def record_agentic_skeleton_preview_error(series_id: int, error: str) -> None:
    """Phase 2 dual-write (`services/agentic_skeleton_preview_store.py`):
    logs a failure to insert into the `agentic_skeleton_previews` shadow
    table for `series_id`. That store performs a real write (unlike the
    log-only `record_agentic_*` helpers above), so this is specifically
    for surfacing write failures against that shadow table -- it is not
    itself a fallback persistence layer.

    Same log-only, fail-soft convention as every other helper in this
    module (tagged `agentic_skeleton_preview_error`): a failure here must
    never raise back into `store_agentic_skeleton_preview`, which already
    guards its own call to this.
    """
    try:
        logger.info("agentic_skeleton_preview_error series_id=%s error=%s", series_id, error)
    except Exception:
        logger.exception(
            "record_agentic_skeleton_preview_error: failed to log preview-store error for series_id=%s", series_id
        )


def record_agentic_confidence_gate_error(series_id: int, decision_kind: str, error: str) -> None:
    """Phase 2 dual-write (`agentic/confidence_gate_store.py`):
    logs a failure to insert into the `agentic_confidence_decisions` or
    `agentic_gate_decisions` shadow table for `series_id`.
    `decision_kind` is `"confidence"` or `"gate"`, distinguishing which
    store call failed. Those stores perform real writes (unlike the
    log-only `record_agentic_*` helpers above), so this is specifically
    for surfacing write failures against those shadow tables -- it is
    not itself a fallback persistence layer.

    Same log-only, fail-soft convention as every other helper in this
    module (tagged `agentic_confidence_gate_error`): a failure here must
    never raise back into `store_agentic_confidence`/`store_agentic_
    gate`, which already guard their own calls to this.
    """
    try:
        logger.info(
            "agentic_confidence_gate_error series_id=%s decision_kind=%s error=%s",
            series_id,
            decision_kind,
            error,
        )
    except Exception:
        logger.exception(
            "record_agentic_confidence_gate_error: failed to log %s-decision store error for series_id=%s",
            decision_kind,
            series_id,
        )


def record_agentic_promotion_error(series_id: int, error: str) -> None:
    """Phase 3 (`agentic/promotion_evaluator.py`): logs a
    failure to insert into the `agentic_promotion_decisions` shadow
    table for `series_id`. That store performs a real write (unlike the
    log-only `record_agentic_*` helpers above), so this is specifically
    for surfacing write failures against that table -- it is not itself
    a fallback persistence layer.

    Same log-only, fail-soft convention as every other helper in this
    module (tagged `agentic_promotion_error`): a failure here must never
    raise back into `store_promotion_decision`, which already guards its
    own call to this.
    """
    try:
        logger.info("agentic_promotion_error series_id=%s error=%s", series_id, error)
    except Exception:
        logger.exception(
            "record_agentic_promotion_error: failed to log promotion-store error for series_id=%s", series_id
        )


def record_agentic_dry_run(series_id: int, payload: dict) -> None:
    """Phase 2 dual execution mode (`agents/series_agent.py`'s `run_
    series_check`): logs one live discovery turn's dry-run agentic
    comparison -- the live snapshot alongside `agents/agentic_series_
    agent.run_agentic_turn`'s freshly-computed shadow trace, or (on
    failure) just the error -- for later inspection via `/admin/agentic/
    dry-run/{series_id}`.

    Purely diagnostic. No writes -- logging one more JSON blob is the
    only side effect. Same log-only fallback as every other `record_
    agentic_*` helper above (tagged `agentic_dry_run`), fail-soft: a
    logging/serialization failure here must never propagate back into
    `run_series_check`, which already wraps its own call to this in a
    try/except, but this function guards itself too so it's safe to call
    from anywhere.
    """
    try:
        logger.info(
            "agentic_dry_run series_id=%s payload=%s",
            series_id,
            json.dumps(payload, default=str),
        )
    except Exception:
        logger.exception("record_agentic_dry_run: failed to log dry-run payload for series_id=%s", series_id)


def record_agentic_safety_violation(series_id: int, book_number, reason: str) -> None:
    """Phase 7 (`agentic/safety.py`'s guardrail layer): logs one
    rejection of an otherwise-eligible agentic decision because `agentic.
    safety.validate_agentic_decision` (or `validate_promotion_
    outcome`) judged it unsafe -- called from both `agentic.
    promotion_evaluator.evaluate_promotion` (before it would have
    returned `"use_agentic"`) and `agentic.resolution.
    resolve_routing_decisions` (its defense-in-depth re-check), so the
    same book/series can log this twice per turn if both layers agree
    it's unsafe -- that's expected, not a bug, since each call site logs
    its own point of rejection independently.

    Purely diagnostic. No writes -- logging one line is the only side
    effect (Phase 9 adds a second, equally side-effect-only one: bumping
    the in-memory `agentic_safety_violations` counter below). Same
    log-only, fail-soft convention as every other `record_agentic_*`
    helper in this module (tagged `agentic_safety_violation`): a failure
    here must never raise back into its caller, both of which already
    guard their own call to this, but this function guards itself too
    so it's safe to call from anywhere.

    Phase 9: incrementing the counter is intentionally placed here
    rather than inside `agentic.safety.validate_agentic_
    decision`/`validate_promotion_outcome` themselves -- those two stay
    exactly as pure as their own docstrings already promise (no I/O, not
    even an in-memory counter), and this function is only ever called at
    the two real "a violation just changed what happens" moments
    (`evaluate_promotion`'s own veto, `resolve_routing_decisions`'
    defense-in-depth veto), not from `tests/test_agentic_safety.py`'s
    many direct, non-production calls to those two pure functions -- so
    the counter reflects genuine production rejections, not every unit
    test assertion about them.
    """
    try:
        logger.info(
            "agentic_safety_violation series_id=%s book_number=%s reason=%s",
            series_id,
            book_number,
            reason,
        )
    except Exception:
        logger.exception(
            "record_agentic_safety_violation: failed to log safety violation for series_id=%s book_number=%s",
            series_id,
            book_number,
        )
    try:
        _increment_agentic_metric("agentic_safety_violations")
    except Exception:
        logger.exception(
            "record_agentic_safety_violation: failed to increment agentic_safety_violations counter for "
            "series_id=%s book_number=%s",
            series_id,
            book_number,
        )


@contextmanager
def maybe_pass_scope(telemetry: "DiscoveryTelemetry | None", name: str, *, tier: str | None = None):
    """No-op passthrough when telemetry is None, so call sites don't need
    an `if telemetry:` guard around every pass boundary. `tier` is forwarded
    to `DiscoveryTelemetry.pass_scope` unchanged -- see that method's
    docstring (HTA Orchestrator Step 3).
    """
    if telemetry is None:
        yield
    else:
        with telemetry.pass_scope(name, tier=tier):
            yield
