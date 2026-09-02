"""Step 10 Phase 3 (Multi-Provider Tier C, orchestrator extraction):
pulls the Tier C shadow LLM call -- dispatch, telemetry recording,
response scoring, cost computation, and persistence -- out of
`agents/series_agent.py`'s classification loop into its own named seam.

This is a pure, behavior-preserving extraction, not a new feature: every
line of logic in `run_tier_c_shadow_call` below is moved verbatim from
that loop (previously inlined directly after `tier_c_prompt` was built),
with loop-local closures turned into explicit keyword arguments instead.
Single-provider (Anthropic, via `llm_client.call_llm(tier="C", ...)`)
and fully sequential, identical to pre-Phase-3 behavior -- Phase 4 is
where this same function grows a `ThreadPoolExecutor`-based fan-out to
Groq/OpenAI; nothing about that exists here yet.

`agents/series_agent.py` keeps everything this module doesn't own:
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
"""

from __future__ import annotations

import json
import time

import discovery_engine
import settings
from llm_client import TIER_MODEL_MAP, LLMCallError, call_llm
from services import tier_c_shadow_store
from services.llm_pricing import get_price_per_million


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
    """Step 10 Phase 3: the Tier C shadow call site, extracted verbatim
    from `agents/series_agent.py`'s classification loop (previously
    inlined immediately after `tier_c_prompt` was built). Callers must
    already have decided Tier C should fire for this candidate --
    `tier_c_shadow_predicate` and the prompt-building inputs it depends
    on stay in `series_agent.py`; this function starts from an
    already-built `prompt` string.

    `telemetry` is typed as `object | None` rather than importing
    `DiscoveryTelemetry` here purely to avoid a needless import for a
    duck-typed parameter (only `.record_shadow_llm_call`/`.record_tier_c_
    shadow_score` are ever called on it, both already optional no-ops
    per `maybe_pass_scope`'s own documented contract) -- callers still
    pass a real `DiscoveryTelemetry` instance exactly as before.

    Single-provider (Anthropic, via `call_llm(tier="C", ...)`), fully
    sequential -- identical to every call site this replaces. Returns
    the `_score_tier_c_shadow_response` dict on a successful, scoreable
    call, or `None` when the call failed/timed out (fail-soft, same
    convention as every other LLM call site in this codebase -- see
    provider_io.py's CR-2 comment). Callers use `None` to mean "Tier C
    unavailable for this candidate," exactly as the inlined code did.

    Persists one `shadow_llm_calls` row via `tier_c_shadow_store.persist_
    tier_c_shadow_call` on every successful, scoreable call -- this
    function's own independent-session/fail-soft semantics (see that
    function's docstring), unchanged from before the extraction.
    """
    _console_log(f"Tier C shadow triggered for candidate_id={candidate_id!r} (reason=ambiguity)")

    # Step 8, section 5.1: only "live" state gets an explicit timeout --
    # every other state keeps today's best-effort, no-timeout behavior
    # unchanged (a slow shadow call in shadow_only/shadow_advisory costs
    # latency, not correctness, since nothing downstream waits on its
    # result to make a decision). "live" state's Tier C call sits on the
    # actual decision path for this candidate, so it needs the same
    # explicit bound every other live, user-visible outbound call in
    # this codebase gets (see settings.TIER_C_LIVE_TIMEOUT_SECONDS's
    # docstring) -- a timeout here raises LLMCallError exactly like any
    # other provider failure, so it's handled by the same except clause
    # below with no special-casing.
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
    finally:
        if telemetry is not None:
            telemetry.record_shadow_llm_call(
                duration_s=time.monotonic() - started_tier_c,
                tokens_in=tier_c_response.tokens_in if tier_c_response is not None else 0,
                tokens_out=tier_c_response.tokens_out if tier_c_response is not None else 0,
                # HTA Orchestrator Step 7: TIER_MODEL_MAP["C"] is now a
                # {"provider", "model_id"} dict, not a bare model_id
                # string -- prefer the response's own resolved model_id
                # when the call succeeded, falling back to the tier's
                # mapped model_id on failure (mirrors every other call
                # site's "record a zero-token entry attributed to what
                # would have been called" convention).
                model_id=(
                    tier_c_response.model_id if tier_c_response is not None else TIER_MODEL_MAP["C"]["model_id"]
                ),
            )

    if tier_c_response is None:
        return None

    # HTA Orchestrator Step 7 / Step 8: scores the shadow response
    # against the deterministic gate's already-computed belongs_to_
    # series/inferred_number_int for this same candidate. Only attempted
    # when the call actually succeeded; a failed call has nothing to
    # score beyond the zero-token entry already recorded above.
    #
    # Deliberately NOT gated on `telemetry is not None` (Step 7's
    # original gate) -- `maybe_pass_scope`'s own docstring guarantees "a
    # caller that doesn't pass a DiscoveryTelemetry instance ... changes
    # no behavior", and the "live" override the caller applies to this
    # function's return value is real behavior, not observability.
    # Scoring itself must not depend on whether telemetry happens to be
    # attached; only *recording* it (below) does.
    tier_c_score = _score_tier_c_shadow_response(
        tier_c_response.text,
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

    # Step 8, section 1: persist this shadow call -- own independent DB
    # session (see tier_c_shadow_store's module docstring for why),
    # fail-soft, never raises back into this loop. Cost is computed the
    # same way record_shadow_llm_call already computes it (services.
    # llm_pricing), just also captured here since that function doesn't
    # return its own cost_usd.
    tier_c_cost_usd = 0.0
    tier_c_pricing = get_price_per_million(tier_c_response.model_id)
    if tier_c_pricing is not None:
        price_in, price_out = tier_c_pricing
        tier_c_cost_usd = (
            tier_c_response.tokens_in * price_in + tier_c_response.tokens_out * price_out
        ) / 1_000_000
    tier_c_shadow_store.persist_tier_c_shadow_call(
        series_id=series_id,
        run_id=run_id or "unknown",
        gate_belongs_to_series=gate_belongs_to_series,
        gate_inferred_number=gate_inferred_number_int,
        gate_confidence=gate_confidence,
        shadow_provider=TIER_MODEL_MAP["C"]["provider"],
        shadow_model_id=tier_c_response.model_id,
        shadow_belongs_to_series=tier_c_score["tier_c_belongs_to_series"],
        shadow_inferred_number=tier_c_score["tier_c_inferred_number"],
        shadow_confidence=tier_c_score["tier_c_confidence"],
        shadow_is_alternate_title_of_known_book=tier_c_score["tier_c_alternate_title_flag"],
        parsed_ok=tier_c_score["parsed_ok"],
        belongs_to_series_agreement=tier_c_score["belongs_to_series_agreement"],
        inferred_number_agreement=tier_c_score["inferred_number_agreement"],
        confidence_aligned=tier_c_score["confidence_aligned"],
        prompt_tokens=tier_c_response.tokens_in,
        completion_tokens=tier_c_response.tokens_out,
        total_cost_usd=tier_c_cost_usd,
        # Step 9: both already available locally at this call site --
        # duration_ms from the same started_tier_c/time.monotonic() pair
        # record_shadow_llm_call above already uses, tier_c_state_at_call
        # from the tier_c_state the caller read once per run_series_check
        # call. See models.ShadowLLMCall's docstring for why both are
        # needed by TierCPromotionPolicyEngine.
        duration_ms=(time.monotonic() - started_tier_c) * 1000,
        tier_c_state_at_call=tier_c_state,
    )

    return tier_c_score
