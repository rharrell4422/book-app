"""Step 10 Phase 3 (Multi-Provider Tier C, orchestrator extraction) +
Phase 4 (parallel fan-out): pulls the Tier C shadow LLM call -- dispatch,
telemetry recording, response scoring, cost computation, and persistence
-- out of `agents/series_agent.py`'s classification loop into its own
named seam, then (Phase 4) grows that seam a `ThreadPoolExecutor`-based
fan-out to Anthropic + Groq + OpenAI in parallel, sampled at
`settings.TIER_C_PARALLEL_SHADOW_SAMPLE_RATE`.

Phase 3 was a pure, behavior-preserving extraction: every line of the
single-provider path below was moved verbatim out of that loop (formerly
inlined directly after `tier_c_prompt` was built), with loop-local
closures turned into explicit keyword arguments instead. Phase 4 is the
first phase where `agents/series_agent.py`'s call site can start
producing real behavior differences in production -- but only once
`settings.TIER_C_PARALLEL_SHADOW_SAMPLE_RATE` is raised above its
Phase-1-shipped default of `0.0` (Phase 6's job, not this one). At
`0.0`, `run_tier_c_shadow_call` always takes the single-provider branch,
identical to Phase 3 -- see `_should_fan_out`.

`agents/series_agent.py`'s call site is completely unchanged by Phase 4
-- the sampling gate (single-provider vs. fan-out) lives entirely inside
this module, exactly per Step 10's finalized spec ("series_agent remains
responsible for deciding whether Tier C is invoked at all; the
orchestrator decides how"). `agents/series_agent.py` keeps everything
this module doesn't own:
  - Deciding whether Tier C fires at all (`tier_c_shadow_predicate`).
  - Building the Tier C prompt itself (`build_belongs_to_series_prompt`
    and its inputs -- `known_series_titles`, `sibling_candidates`,
    `provider_metadata`, etc. -- stay loop-local; threading all of that
    through this module's signature instead of a single pre-built
    `prompt` string would make this seam wider than it needs to be for
    Phase 3's single-provider scope).
  - Consuming this function's returned `tier_c_score` dict (or `None`)
    to decide the "live"-state override and "shadow_advisory" disagreement
    payload -- both stay routing decisions, not orchestrator concerns,
    exactly as every prior Step 10 spec round insisted they must.

`_score_tier_c_shadow_response` moves here too (previously a module-level
helper in `agents/series_agent.py`, added in HTA Orchestrator Step 7) --
it has no dependency on anything series_agent-specific, and its only
real caller is `run_tier_c_shadow_call` immediately below it. `agents/
series_agent.py` re-exports it (`from services.tier_c_orchestrator import
_score_tier_c_shadow_response`) purely so `tests/test_series_discovery.py`'s
existing `from agents.series_agent import (..., _score_tier_c_shadow_
response, ...)` import keeps working completely unmodified -- Phase 3's
acceptance bar is "all existing Tier C shadow tests pass unchanged," and
that includes import statements, not just runtime behavior.

Phase 4 decisions (Step 10's finalized spec, "final tie-break rule"
round), all implemented below:
  - Concurrency: `ThreadPoolExecutor(max_workers=3)` around `call_llm`,
    one call per `_TIER_C_FAN_OUT_PROVIDERS` entry. `call_llm` itself
    stays synchronous; only the scheduling is threaded (same pattern as
    `provider_io._fetch_all_providers_parallel`'s existing provider-fetch
    concurrency).
  - Timeout: every fan-out call gets `settings.TIER_C_PARALLEL_CALL_
    TIMEOUT_SECONDS` regardless of `tier_c_state` (moot in practice,
    since "live" never fans out at all -- see below -- but stated
    explicitly per the spec's own reasoning: uniform timeouts prevent
    one hung provider from blocking collection of the other two already-
    finished results).
  - Live-state exclusion: `tier_c_state == "live"` forces single-provider
    and skips the sample-rate roll entirely (`_should_fan_out` short-
    circuits on `and`, so `random.random()` is never even called for a
    "live" candidate) -- "live" stays exactly as safe/deterministic as
    every prior step left it.
  - Partial-failure + majority-vote tie-break: `_aggregate_gate_
    comparison_votes` collapses however many providers actually
    responded (0/1/2/3) into the single `tier_c_score`-shaped dict
    `agents/series_agent.py`'s "live"-override/"shadow_advisory"-
    disagreement logic already expects -- 0 responses -> `None`
    (Tier C unavailable, same as a single failed call always meant);
    1 response -> that response verbatim (today's single-provider
    behavior); 2-3 responses -> majority vote on `belongs_to_series_
    agreement`, with an exact 2-way tie resolved as disagreement (the
    "disagreement is the safety-relevant direction, resolve first"
    philosophy already load-bearing in `tier_c_promotion_engine.
    _decide_transition`, applied one level up: per-candidate vote
    resolution instead of per-window promotion/demotion resolution).
  - Persistence: every provider that actually responded (successful
    `call_llm`, regardless of whether its text parsed) gets its own
    `shadow_llm_calls` row via `persist_tier_c_shadow_call`, all sharing
    one `candidate_request_id` (minted once per `run_tier_c_shadow_call`
    invocation, single-provider or fan-out alike) -- the join key a
    later phase's `get_recent_candidate_aggregates` will group on.
  - NOT built here (explicitly deferred to Phase 5): cross-provider
    consensus/conflict scoring and anything touching `TierCPromotion
    History.metrics_snapshot` or `tier_c_promotion_engine.py` -- this
    module's aggregation is only ever the immediate, in-process,
    single-candidate gate-comparison vote described above, not the
    read-time, across-candidates window `evaluate_tier_c_promotion`
    consumes.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import discovery_engine
import settings
from llm_client import PROVIDER_METADATA, TIER_MODEL_MAP, LLMCallError, LLMResponse, call_llm
from services import tier_c_shadow_store
from services.llm_pricing import get_price_per_million

# Step 10 Phase 4: the fixed provider/model set a sampled fan-out calls
# out to (Anthropic -- whatever TIER_MODEL_MAP["C"] currently points at,
# kept as the first/primary entry for log/ordering readability only --
# order has no effect on the majority-vote outcome -- plus Groq/OpenAI).
# Groq/OpenAI use the same ad hoc smoke-test models `services.llm_
# pricing.PRICING_PER_MILLION_TOKENS` and `tests/test_llm_client.py`
# already established ahead of any tier routing to them (Steps 7/10
# Phase 2) -- not re-derived from TIER_MODEL_MAP since neither provider
# has a tier entry yet. A hardcoded tuple, not a setting, because
# changing *which* three providers/models fan out is a code change
# (prompt/scoring compatibility must be verified per model), unlike
# *whether* fan-out happens at all (settings.TIER_C_PARALLEL_SHADOW_
# SAMPLE_RATE) or *how long* to wait for it (settings.TIER_C_PARALLEL_
# CALL_TIMEOUT_SECONDS).
_TIER_C_FAN_OUT_PROVIDERS: tuple[dict[str, str], ...] = (
    {"provider": TIER_MODEL_MAP["C"]["provider"], "model_id": TIER_MODEL_MAP["C"]["model_id"]},
    {"provider": "groq", "model_id": "llama-3.3-70b-versatile"},
    {"provider": "openai", "model_id": "gpt-4o-mini"},
)


def _console_log(message: str) -> None:
    print(f"[tier_c_orchestrator] {message}", flush=True)


def _score_tier_c_shadow_response(
    response_text: str,
    *,
    gate_belongs_to_series: bool,
    gate_inferred_number_int: int | None,
) -> dict:
    """HTA Orchestrator Step 7: parses one Tier C shadow LLM response
    (`build_belongs_to_series_prompt`'s documented JSON shape -- see
    prompts.py) and scores it against the deterministic gate's
    already-computed decision for the same candidate. Per-run, in-memory
    only -- the caller (`run_tier_c_shadow_call` below) hands this
    straight to `DiscoveryTelemetry.record_tier_c_shadow_score`, which
    never persists across runs (see Step 7's architectural diff, section
    5, for why cross-run/per-series accuracy is explicit future work, not
    this one).

    Fail-soft on a malformed/unparseable response, same convention as
    every other LLM response parse site in this codebase (see
    provider_io.py's CR-2 comment): returns `{"parsed_ok": False, ...}`
    with every other field `None` rather than raising, so a single bad
    shadow response can never sink the classification loop it's shadowing.

    Field-by-field scoring rationale (Step 7 architectural diff,
    clarification 1):
      - `belongs_to_series`/`inferred_number` both have a deterministic-
        gate counterpart, so both get a real agreement/disagreement bool.
        `inferred_number` agreement is exact-match (both `None` counts as
        agreement; one `None` and the other not counts as disagreement) --
        these are discrete series-position numbers, not a continuous
        quantity a fuzzy tolerance would make sense for.
      - `confidence` is scored relative to disagreement, not compared to
        anything on the gate (the gate has no confidence-grade concept of
        its own at this layer -- see `confidence_engine` for the
        separate, unrelated overall_grade this doesn't touch):
        `confidence_aligned` is only meaningful when Tier C disagreed
        with the gate on `belongs_to_series`, and asks whether Tier C's
        own self-reported confidence was "medium"/"high" for that
        disagreement (as opposed to "low", which would mean Tier C
        wasn't even confident in its own dissent). `None` when Tier C
        agreed (the question doesn't apply).
      - `is_alternate_title_of_known_book` is recorded only -- the gate
        has no alternate-title concept to compare against, so this is
        never folded into the agreement/disagreement metrics above.

    Step 8 ("Tier C Shadow Scoring Persistence + Promotion Path", section
    1.2) addition: also returns Tier C's own raw, normalized
    `belongs_to_series`/`inferred_number` values (`tier_c_belongs_to_
    series`/`tier_c_inferred_number`), not just the agreement booleans
    above. Needed so the shadow call site's persistence write and "live"
    promotion-state override can use Tier C's actual decision without
    re-parsing `response_text` a second time -- this function stays the
    single source of truth for everything derived from one shadow
    response (section 1.2's "do not re-implement scoring" rule extends to
    "do not re-parse the response", not just the comparison booleans).
    """
    text = (response_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        return {
            "parsed_ok": False,
            "belongs_to_series_agreement": None,
            "inferred_number_agreement": None,
            "tier_c_confidence": None,
            "confidence_aligned": None,
            "tier_c_alternate_title_flag": None,
            "tier_c_belongs_to_series": None,
            "tier_c_inferred_number": None,
        }

    tier_c_belongs_to_series = parsed.get("belongs_to_series")
    if not isinstance(tier_c_belongs_to_series, bool):
        tier_c_belongs_to_series = None
    belongs_to_series_agreement = (
        bool(tier_c_belongs_to_series) == bool(gate_belongs_to_series)
        if tier_c_belongs_to_series is not None
        else None
    )

    tier_c_inferred_number_int = discovery_engine._to_int_or_none(parsed.get("inferred_number"))
    inferred_number_agreement = tier_c_inferred_number_int == gate_inferred_number_int

    tier_c_confidence = parsed.get("confidence") if isinstance(parsed.get("confidence"), str) else None
    confidence_aligned = None
    if belongs_to_series_agreement is False and tier_c_confidence is not None:
        confidence_aligned = tier_c_confidence in ("medium", "high")

    tier_c_alternate_title_flag = parsed.get("is_alternate_title_of_known_book")
    if not isinstance(tier_c_alternate_title_flag, bool):
        tier_c_alternate_title_flag = None

    return {
        "parsed_ok": True,
        "belongs_to_series_agreement": belongs_to_series_agreement,
        "inferred_number_agreement": inferred_number_agreement,
        "tier_c_confidence": tier_c_confidence,
        "confidence_aligned": confidence_aligned,
        "tier_c_alternate_title_flag": tier_c_alternate_title_flag,
        "tier_c_belongs_to_series": tier_c_belongs_to_series,
        "tier_c_inferred_number": tier_c_inferred_number_int,
    }


def _record_and_score_response(
    *,
    response: "LLMResponse | None",
    provider: str,
    fallback_model_id: str,
    duration_s: float,
    series_id: int,
    run_id: str | None,
    tier_c_state: str,
    candidate_request_id: str,
    gate_belongs_to_series: bool,
    gate_inferred_number_int: int | None,
    gate_confidence: str | None,
    telemetry: object | None,
) -> dict | None:
    """One provider's worth of telemetry-recording + scoring +
    cost-computation + persistence -- shared by both the single-provider
    and Phase 4 parallel-fan-out paths below so neither has to duplicate
    it. `response` is `None` on a failed/timed-out call; every other
    argument is identical across both call sites.

    Records a zero-token telemetry entry (attributed to `fallback_model_
    id`) and returns `None` when `response` is `None` -- otherwise scores
    the response, records the real telemetry entry, computes cost, and
    persists one `shadow_llm_calls` row tagged with `candidate_request_
    id`, returning the `_score_tier_c_shadow_response` dict. This is
    exactly Phase 3's single-provider body, just parameterized by
    provider/model_id instead of hardcoding `TIER_MODEL_MAP["C"]`.
    """
    if telemetry is not None:
        telemetry.record_shadow_llm_call(
            duration_s=duration_s,
            tokens_in=response.tokens_in if response is not None else 0,
            tokens_out=response.tokens_out if response is not None else 0,
            model_id=response.model_id if response is not None else fallback_model_id,
        )

    if response is None:
        return None

    tier_c_score = _score_tier_c_shadow_response(
        response.text,
        gate_belongs_to_series=gate_belongs_to_series,
        gate_inferred_number_int=gate_inferred_number_int,
    )
    if telemetry is not None:
        telemetry.record_tier_c_shadow_score(
            parsed_ok=tier_c_score["parsed_ok"],
            belongs_to_series_agreement=tier_c_score["belongs_to_series_agreement"],
            inferred_number_agreement=tier_c_score["inferred_number_agreement"],
            tier_c_confidence=tier_c_score["tier_c_confidence"],
            confidence_aligned=tier_c_score["confidence_aligned"],
            tier_c_alternate_title_flag=tier_c_score["tier_c_alternate_title_flag"],
        )

    tier_c_cost_usd = 0.0
    tier_c_pricing = get_price_per_million(response.model_id)
    if tier_c_pricing is not None:
        price_in, price_out = tier_c_pricing
        tier_c_cost_usd = (response.tokens_in * price_in + response.tokens_out * price_out) / 1_000_000

    tier_c_shadow_store.persist_tier_c_shadow_call(
        series_id=series_id,
        run_id=run_id or "unknown",
        gate_belongs_to_series=gate_belongs_to_series,
        gate_inferred_number=gate_inferred_number_int,
        gate_confidence=gate_confidence,
        shadow_provider=provider,
        shadow_model_id=response.model_id,
        shadow_belongs_to_series=tier_c_score["tier_c_belongs_to_series"],
        shadow_inferred_number=tier_c_score["tier_c_inferred_number"],
        shadow_confidence=tier_c_score["tier_c_confidence"],
        shadow_is_alternate_title_of_known_book=tier_c_score["tier_c_alternate_title_flag"],
        parsed_ok=tier_c_score["parsed_ok"],
        belongs_to_series_agreement=tier_c_score["belongs_to_series_agreement"],
        inferred_number_agreement=tier_c_score["inferred_number_agreement"],
        confidence_aligned=tier_c_score["confidence_aligned"],
        prompt_tokens=response.tokens_in,
        completion_tokens=response.tokens_out,
        total_cost_usd=tier_c_cost_usd,
        duration_ms=duration_s * 1000,
        tier_c_state_at_call=tier_c_state,
        candidate_request_id=candidate_request_id,
    )

    return tier_c_score


def _should_fan_out(tier_c_state: str) -> bool:
    """Step 10 Phase 4 sampling gate. `tier_c_state == "live"` forces
    single-provider and short-circuits before `random.random()` is ever
    called (Python's `and` never evaluates the right operand once the
    left is `False`) -- "live" stays exactly as deterministic/safe as
    every prior step left it, with zero chance of a sample-rate roll
    consuming a draw for it. Every other state rolls `settings.TIER_C_
    PARALLEL_SHADOW_SAMPLE_RATE`, which defaults to `0.0` (Phase 1) --
    `random.random()` returns `[0.0, 1.0)`, so `< 0.0` can never be
    `True`, making fan-out unconditionally inactive until Phase 6 raises
    the rate above zero.
    """
    return tier_c_state != "live" and random.random() < settings.TIER_C_PARALLEL_SHADOW_SAMPLE_RATE


def _call_one_provider(
    provider: str, model_id: str, prompt: str, timeout: float
) -> tuple[str, str, "LLMResponse | None", float]:
    """One fan-out provider's call, run inside a `ThreadPoolExecutor`
    worker by `_run_parallel_fan_out` below. Always returns a 4-tuple
    (never raises) so the caller's `future.result()` never needs its own
    try/except beyond a belt-and-suspenders guard -- `LLMCallError` is
    caught here exactly like the single-provider path catches it,
    logged, and turned into a `None` response (Phase 4's "0/1/2/3
    responses" partial-failure accounting downstream treats a `None`
    response as "this provider did not respond," identical to how a
    failed single-provider call has always meant "Tier C unavailable").

    JSON mode (`response_format="json"`) is requested whenever `llm_
    client.PROVIDER_METADATA` says the provider supports it (Groq/
    OpenAI as of Phase 2) -- Anthropic gets no `response_format` and
    keeps relying on the prompt's own instructions plus `_score_tier_c_
    shadow_response`'s existing markdown-fence-stripping parse, exactly
    as the single-provider path always has. All three providers' raw
    text is scored by that same function uniformly either way.
    """
    response_format = "json" if PROVIDER_METADATA.get(provider, {}).get("supports_json_mode") else None
    started = time.monotonic()
    response: "LLMResponse | None" = None
    try:
        response = call_llm(
            model_id=model_id,
            provider=provider,
            prompt=prompt,
            shadow=True,
            max_tokens=500,
            temperature=0,
            timeout=timeout,
            response_format=response_format,
        )
    except LLMCallError as exc:
        _console_log(f"Tier C parallel shadow call failed (provider={provider!r}): {exc}")
    return provider, model_id, response, time.monotonic() - started


def _aggregate_gate_comparison_votes(scored: list[dict]) -> dict | None:
    """Step 10 Phase 4's partial-failure + majority-vote rule, applied to
    however many providers actually produced a `_score_tier_c_shadow_
    response` dict (`scored` only ever contains successful calls --
    `_record_and_score_response` already returns `None` for a failed
    one, filtered out by `_run_parallel_fan_out` before this is called):

      - 0 responses -> `None` ("Tier C unavailable", same meaning a
        failed single-provider call has always had).
      - 1 response -> that response verbatim (today's single-provider
        behavior -- there is nothing to vote on with only one voice).
      - >=2 responses -> majority vote on `belongs_to_series_agreement`
        among whichever of those responses actually produced a
        comparable (non-`None`) agreement value -- an unparseable
        response (`parsed_ok=False`, `belongs_to_series_agreement=None`)
        doesn't get a vote, exactly like it doesn't get one anywhere
        else in this codebase's Tier C aggregation. An exact 2-way tie
        (one agree, one disagree) resolves to disagreement, matching
        Step 10's finalized tie-break rule and the same "disagreement is
        the safety-relevant direction, resolve first" philosophy already
        load-bearing in `tier_c_promotion_engine._decide_transition`.

    The returned dict is always one of `scored`'s own entries, verbatim
    -- never a synthesized blend -- so every field `agents/series_
    agent.py`'s "live"-override/"shadow_advisory"-disagreement logic
    already reads off a `tier_c_score` dict (`parsed_ok`, `tier_c_
    belongs_to_series`, `tier_c_confidence`, ...) stays internally
    self-consistent, exactly as it would for a genuine single-provider
    response.
    """
    if not scored:
        return None
    if len(scored) == 1:
        return scored[0]

    voters = [entry for entry in scored if entry["belongs_to_series_agreement"] is not None]
    if len(voters) <= 1:
        # 0 comparable votes despite >=2 raw responses: every response
        # was unparseable, so there's nothing to aggregate -- return the
        # first one (its parsed_ok=False/agreement=None already makes it
        # a no-op for every downstream consumer, same as `None` would
        # be). 1 comparable vote: treat as single-provider, same as the
        # top-level `len(scored) == 1` case above.
        return voters[0] if voters else scored[0]

    agree_count = sum(1 for entry in voters if entry["belongs_to_series_agreement"] is True)
    disagree_count = len(voters) - agree_count
    aggregate_agreement = agree_count > disagree_count

    for entry in voters:
        if entry["belongs_to_series_agreement"] == aggregate_agreement:
            return entry
    return voters[0]  # unreachable in practice -- kept as a safe fallback


def _run_single_provider(
    *,
    series_id: int,
    run_id: str | None,
    tier_c_state: str,
    prompt: str,
    candidate_request_id: str,
    gate_belongs_to_series: bool,
    gate_inferred_number_int: int | None,
    gate_confidence: str | None,
    telemetry: object | None,
) -> dict | None:
    """Phase 3's original single-provider body (Anthropic, via `call_llm
    (tier="C", ...)`), unchanged in every observable way -- still the
    path every non-sampled candidate takes (i.e. all of them, until
    Phase 6 raises `settings.TIER_C_PARALLEL_SHADOW_SAMPLE_RATE` above
    `0.0`). Only the persistence call has grown a `candidate_request_id`
    (Phase 1's schema addition, now actually populated for every
    invocation -- single-provider included -- so Phase 5's aggregation
    query can group all of a candidate_request_id's rows uniformly
    whether they came from a fan-out or not).

    Step 8, section 5.1: only "live" state gets an explicit timeout --
    every other state keeps today's best-effort, no-timeout behavior
    unchanged (a slow shadow call in shadow_only/shadow_advisory costs
    latency, not correctness, since nothing downstream waits on its
    result to make a decision). "live" state's Tier C call sits on the
    actual decision path for this candidate, so it needs the same
    explicit bound every other live, user-visible outbound call in this
    codebase gets (see settings.TIER_C_LIVE_TIMEOUT_SECONDS's docstring)
    -- a timeout here raises LLMCallError exactly like any other
    provider failure, handled by the same except clause with no
    special-casing.
    """
    tier_c_call_timeout = settings.TIER_C_LIVE_TIMEOUT_SECONDS if tier_c_state == "live" else None

    started_tier_c = time.monotonic()
    tier_c_response = None
    try:
        tier_c_response = call_llm(
            tier="C",
            prompt=prompt,
            shadow=True,
            max_tokens=500,
            temperature=0,
            timeout=tier_c_call_timeout,
        )
    except LLMCallError as exc:
        # Fail-soft, same convention as every other LLM call site in
        # this codebase (see provider_io.py's CR-2 comment) -- a
        # shadow-only call must never sink a real Check Now run. In
        # "live" state this is also exactly "Tier C unavailable" (Step
        # 8, section 5.1) -- the caller's override only ever fires when
        # this function's return value is not None, so a timeout/failure
        # here always falls back to the deterministic gate's own
        # belongs_to_series, with no separate handling needed.
        _console_log(f"Tier C shadow LLM call failed: {exc}")

    return _record_and_score_response(
        response=tier_c_response,
        provider=TIER_MODEL_MAP["C"]["provider"],
        fallback_model_id=TIER_MODEL_MAP["C"]["model_id"],
        duration_s=time.monotonic() - started_tier_c,
        series_id=series_id,
        run_id=run_id,
        tier_c_state=tier_c_state,
        candidate_request_id=candidate_request_id,
        gate_belongs_to_series=gate_belongs_to_series,
        gate_inferred_number_int=gate_inferred_number_int,
        gate_confidence=gate_confidence,
        telemetry=telemetry,
    )


def _run_parallel_fan_out(
    *,
    series_id: int,
    run_id: str | None,
    tier_c_state: str,
    prompt: str,
    candidate_id: str,
    candidate_request_id: str,
    gate_belongs_to_series: bool,
    gate_inferred_number_int: int | None,
    gate_confidence: str | None,
    telemetry: object | None,
) -> dict | None:
    """Step 10 Phase 4: calls every `_TIER_C_FAN_OUT_PROVIDERS` entry
    (Anthropic + Groq + OpenAI) concurrently via `ThreadPoolExecutor`,
    each bounded by `settings.TIER_C_PARALLEL_CALL_TIMEOUT_SECONDS` so
    one hung provider can never block collection of the other two
    already-finished results. `_should_fan_out` already guarantees this
    is only ever reached for shadow_only/shadow_advisory candidates
    (never "live"), so uniformly applying the same timeout to all three
    calls -- rather than Phase 3's "live" state alone -- changes nothing
    about "live"'s own behavior.

    Persists one row per provider that actually responded (via
    `_record_and_score_response`, same helper the single-provider path
    uses), all sharing `candidate_request_id`, then collapses however
    many of those responses were scoreable into one dict via `_aggregate
    _gate_comparison_votes` -- see that function's docstring for the
    partial-failure/majority-vote rule itself.
    """
    _console_log(f"Tier C parallel fan-out triggered for candidate_id={candidate_id!r}")
    timeout = settings.TIER_C_PARALLEL_CALL_TIMEOUT_SECONDS

    call_results: list[tuple[str, str, "LLMResponse | None", float]] = []
    with ThreadPoolExecutor(max_workers=len(_TIER_C_FAN_OUT_PROVIDERS)) as executor:
        futures = [
            executor.submit(_call_one_provider, entry["provider"], entry["model_id"], prompt, timeout)
            for entry in _TIER_C_FAN_OUT_PROVIDERS
        ]
        for future in futures:
            try:
                call_results.append(future.result())
            except Exception as exc:  # belt-and-suspenders only -- _call_one_provider itself never raises
                _console_log(f"Tier C parallel shadow call raised unexpectedly: {exc}")

    scored: list[dict] = []
    for provider, model_id, response, duration_s in call_results:
        tier_c_score = _record_and_score_response(
            response=response,
            provider=provider,
            fallback_model_id=model_id,
            duration_s=duration_s,
            series_id=series_id,
            run_id=run_id,
            tier_c_state=tier_c_state,
            candidate_request_id=candidate_request_id,
            gate_belongs_to_series=gate_belongs_to_series,
            gate_inferred_number_int=gate_inferred_number_int,
            gate_confidence=gate_confidence,
            telemetry=telemetry,
        )
        if tier_c_score is not None:
            scored.append(tier_c_score)

    return _aggregate_gate_comparison_votes(scored)


def run_tier_c_shadow_call(
    *,
    series_id: int,
    run_id: str | None,
    tier_c_state: str,
    prompt: str,
    candidate_id: str,
    gate_belongs_to_series: bool,
    gate_inferred_number_int: int | None,
    gate_confidence: str | None,
    telemetry: object | None = None,
) -> dict | None:
    """Step 10 Phase 3/4: the Tier C shadow call site, extracted from
    `agents/series_agent.py`'s classification loop (originally inlined
    immediately after `tier_c_prompt` was built). Callers must already
    have decided Tier C should fire for this candidate --
    `tier_c_shadow_predicate` and the prompt-building inputs it depends
    on stay in `series_agent.py`; this function starts from an
    already-built `prompt` string.

    `telemetry` is typed as `object | None` rather than importing
    `DiscoveryTelemetry` here purely to avoid a needless import for a
    duck-typed parameter (only `.record_shadow_llm_call`/`.record_tier_c_
    shadow_score` are ever called on it, both already optional no-ops
    per `maybe_pass_scope`'s own documented contract) -- callers still
    pass a real `DiscoveryTelemetry` instance exactly as before.

    Mints one `candidate_request_id` (uuid4 hex) per invocation --
    single-provider or fan-out alike -- so every `shadow_llm_calls` row
    this call produces (one row for single-provider, up to three for a
    sampled fan-out) can be grouped back together later (Phase 5's
    aggregation query). Then rolls `_should_fan_out(tier_c_state)`:
    `False` (today, always, since `settings.TIER_C_PARALLEL_SHADOW_
    SAMPLE_RATE` defaults to `0.0`) takes `_run_single_provider`, Phase
    3's original body unchanged; `True` takes Phase 4's `_run_parallel_
    fan_out`. Either branch returns the same `_score_tier_c_shadow_
    response`-shaped dict (or `None`), so this function's own contract
    with `agents/series_agent.py` -- and therefore series_agent.py
    itself -- needs zero changes for Phase 4.
    """
    candidate_request_id = uuid.uuid4().hex

    if _should_fan_out(tier_c_state):
        return _run_parallel_fan_out(
            series_id=series_id,
            run_id=run_id,
            tier_c_state=tier_c_state,
            prompt=prompt,
            candidate_id=candidate_id,
            candidate_request_id=candidate_request_id,
            gate_belongs_to_series=gate_belongs_to_series,
            gate_inferred_number_int=gate_inferred_number_int,
            gate_confidence=gate_confidence,
            telemetry=telemetry,
        )

    _console_log(f"Tier C shadow triggered for candidate_id={candidate_id!r} (reason=ambiguity)")
    return _run_single_provider(
        series_id=series_id,
        run_id=run_id,
        tier_c_state=tier_c_state,
        prompt=prompt,
        candidate_request_id=candidate_request_id,
        gate_belongs_to_series=gate_belongs_to_series,
        gate_inferred_number_int=gate_inferred_number_int,
        gate_confidence=gate_confidence,
        telemetry=telemetry,
    )
