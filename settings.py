"""Phase 3 kickoff (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
phase1_evaluation.md`'s settled architecture, not re-litigated here): the
project's first centralized feature-flag module. Every other env-driven
toggle in this codebase (`AUTH_DISABLED`, `HARDCOVER_API_KEY`, etc.) is
read ad hoc, module-level, wherever it's needed (see `routers/deps.py`,
`provider_io.py`) -- this module exists specifically for
`AGENTIC_ROUTING_ENABLED`, the first flag that gates a *behavior*
(candidate promotion in `agents/series_agent.py`'s live routing path)
rather than just an integration's availability, and is deliberately kept
to that one flag.

Read as a module attribute (`settings.AGENTIC_ROUTING_ENABLED`), not
imported by value (`from settings import AGENTIC_ROUTING_ENABLED`), by
every caller -- the value is computed once at import time from the
environment, same as every other flag in this codebase, but tests need
to flip it without touching `os.environ`/process restarts, which only
works if callers re-read the module attribute each time rather than
capturing a stale copy at their own import time.

Defaults to `False`: this is a feature-flagged, gradual promotion
mechanism (`agentic/promotion_evaluator.py`), not a default-on
behavior change. With the flag unset/off, `agents/series_agent.py`'s
live routing path is byte-for-byte identical to before this flag
existed.

Phase 4 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_
evaluation.md`, not re-litigated here) adds `AGENTIC_SERIES_ACTIVATION`
+ `is_agentic_activated`: a *second*, per-series gate layered on top of
`AGENTIC_ROUTING_ENABLED`. The distinction matters --

- `AGENTIC_ROUTING_ENABLED` alone (Phase 3): the promotion evaluator
  runs and every decision is recorded to the `agentic_promotion_
  decisions` shadow table, but `agents/series_agent.py`'s live routing
  path still only ever resolves to the *live* confidence/gate --
  "record, don't apply".
- `is_agentic_activated(series_id)` additionally `True` for a given
  series (Phase 4): that series' resolved confidence/gate can actually
  become the *agentic* decision when the promotion evaluator chose
  `"use_agentic"` -- "record AND apply", for that series only.

This lets a small, explicit, reversible (env-var-driven, no code
change/deploy) allowlist of series be the only ones where agentic
decisions ever reach live routing output, while every other activated-
routing series keeps recording decisions for later comparison.
"""

import os

AGENTIC_ROUTING_ENABLED = bool(os.getenv("AGENTIC_ROUTING_ENABLED", "false").lower() == "true")

# Comma-separated list of series_ids that may use agentic routing, e.g.
# "12,47,203". Empty/unset means no series is activated, even if
# AGENTIC_ROUTING_ENABLED is on (see is_agentic_activated below).
AGENTIC_SERIES_ACTIVATION = os.getenv("AGENTIC_SERIES_ACTIVATION", "")


def is_agentic_activated(series_id: int) -> bool:
    """Phase 4's per-series activation gate (see module docstring for
    the full rationale): `False` whenever `AGENTIC_ROUTING_ENABLED` is
    off (per-series activation can never re-enable a globally-disabled
    feature), and otherwise `True` only if `series_id` appears in the
    comma-separated `AGENTIC_SERIES_ACTIVATION` allowlist.

    Reads both module attributes fresh on every call (not a value
    captured once at import time) so tests -- and, in production, an
    env-var change followed by a process restart -- take effect without
    needing to reload this module. Fail-soft: a malformed entry in
    `AGENTIC_SERIES_ACTIVATION` (not an int) is skipped rather than
    raised, since this is called from `agents/series_agent.py`'s live
    routing path, which must never fail because of a typo in an
    allowlist env var.
    """
    if not AGENTIC_ROUTING_ENABLED:
        return False
    activated: set[int] = set()
    for raw in AGENTIC_SERIES_ACTIVATION.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            activated.add(int(raw))
        except ValueError:
            continue
    return series_id in activated


# Series Fingerprint system (see discovery_agentic_fingerprint_
# recommendation.md for the full ten-round design chain). A dedicated
# two-tier gate, deliberately NOT a reuse of AGENTIC_ROUTING_ENABLED/
# is_agentic_activated above -- those gate a different subsystem (the
# agentic promotion evaluator's shadow-vs-live override), which does not
# govern the always-live confidence_engine.py code path fingerprint
# influence plugs into. Mirrors the same env-var-only shape (no DB-backed
# activation table -- see the design chain's Round 3 catch: there is no
# precedent anywhere in this codebase for a DB-driven flag, so this isn't
# the place to introduce one).
#
# The fingerprint itself is always built, unconditionally, for every
# series (Builder cost is zero -- it only reads this round's already-
# computed delta/confidence output). This flag pair governs only whether
# confidence_engine.compute_confidence is ever handed a non-None
# `fingerprint` argument -- i.e. "shadow-first": compute and persist it
# regardless, but only let it influence live scoring once explicitly
# turned on.
FINGERPRINT_INFLUENCE_ENABLED = bool(os.getenv("FINGERPRINT_INFLUENCE_ENABLED", "false").lower() == "true")

# Comma-separated list of series_ids that may have fingerprint influence
# active, e.g. "12,47,203". Empty/unset means no series is activated, even
# if FINGERPRINT_INFLUENCE_ENABLED is on (see is_fingerprint_activated).
FINGERPRINT_SERIES_ACTIVATION = os.getenv("FINGERPRINT_SERIES_ACTIVATION", "")


def is_fingerprint_activated(series_id: int) -> bool:
    """Per-series fingerprint-influence activation gate -- same shape and
    same fail-soft rationale as is_agentic_activated above, but reading
    the FINGERPRINT_* pair instead. `False` whenever
    FINGERPRINT_INFLUENCE_ENABLED is off; otherwise `True` only if
    `series_id` appears in the comma-separated FINGERPRINT_SERIES_
    ACTIVATION allowlist. Reads both module attributes fresh on every
    call, same reason as is_agentic_activated: tests need to flip them
    without a process restart.
    """
    if not FINGERPRINT_INFLUENCE_ENABLED:
        return False
    activated: set[int] = set()
    for raw in FINGERPRINT_SERIES_ACTIVATION.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            activated.add(int(raw))
        except ValueError:
            continue
    return series_id in activated


# Step 8 (Tier C Shadow Scoring Persistence + Promotion Path): budget
# ceilings for Tier C shadow calls, enforced via Mechanism B (Step 8 diff,
# "Mechanism B Selection" revision) -- a single aggregation of persisted
# `shadow_llm_calls` cost for the relevant window, checked once at the
# start of a Check Now job (services/series_check_engine.run_series_check_
# job_full) and cached for that job's duration, not re-checked per
# candidate. `None` (the default, unset) means "no ceiling" -- same
# opt-in-by-default-off philosophy as every other flag in this module;
# setting one of these to a positive float is what actually turns
# enforcement on for that window. See services/tier_c_shadow_store.
# check_tier_c_shadow_budget.
TIER_C_SHADOW_MAX_DAILY_COST_USD: float | None = (
    float(os.environ["TIER_C_SHADOW_MAX_DAILY_COST_USD"])
    if os.getenv("TIER_C_SHADOW_MAX_DAILY_COST_USD", "").strip()
    else None
)
TIER_C_SHADOW_MAX_MONTHLY_COST_USD: float | None = (
    float(os.environ["TIER_C_SHADOW_MAX_MONTHLY_COST_USD"])
    if os.getenv("TIER_C_SHADOW_MAX_MONTHLY_COST_USD", "").strip()
    else None
)

# Step 8: only consulted when a series' Tier C promotion state is "live"
# (see agents/series_agent.py's Tier C shadow call site) -- in every other
# state, Tier C stays the existing best-effort, no-timeout shadow call
# (behavior-preserving). Mirrors provider_io.WEB_SEARCH_TIMEOUT_SECONDS'
# role: the one place in this codebase a live, user-visible decision path
# waits on a single outbound call, it gets an explicit bound. A timeout
# (or any other LLMCallError) in "live" state is treated as "Tier C
# unavailable" and falls back to the deterministic gate's own decision --
# see that call site's own comment for why this is safe.
TIER_C_LIVE_TIMEOUT_SECONDS = float(os.environ.get("TIER_C_LIVE_TIMEOUT_SECONDS", "20"))

# Step 9 (Tier C Promotion Policy Engine): thresholds for services/tier_c_
# promotion_engine.py's state-transition rules -- see that module's
# docstring for the full rule table. Same opt-in-by-value-not-by-code
# philosophy as every other setting in this module: these have sane
# defaults so the engine is live and evaluating from the moment it ships,
# not gated behind a separate on/off flag (unlike AGENTIC_ROUTING_ENABLED,
# this isn't a behavior change to a *live* routing path -- shadow_only is
# always the safe starting state, and the engine can only ever move a
# series one step at a time from wherever it already is).
#
# TIER_C_PROMOTION_MIN_CALLS is deliberately both the lookback window size
# AND the minimum sample size required to decide anything -- "last N
# shadow_llm_calls rows" from the Step 9 spec, not two separate knobs.
# Below this many scored calls, the engine always HOLDs
# ("insufficient_evidence"), regardless of how few/many Check Now jobs
# that spans (Tier C shadow calls are sparse -- only ambiguous candidates
# trigger one -- so counting by job would be meaningless for a low-
# activity series; see the Step 9 design chat's resolution).
#
# Step 10 Phase 5 (Multi-Provider Tier C, per-candidate aggregation): now
# counts distinct Tier C *candidates* (`get_recent_candidate_aggregates`),
# not raw `shadow_llm_calls` rows -- a sampled multi-provider fan-out
# candidate contributes up to 3 rows but still only 1 unit toward this
# count. Numerically identical to the pre-Phase-5 row-count meaning for
# every single-provider candidate (today's only kind, since
# TIER_C_PARALLEL_SHADOW_SAMPLE_RATE still defaults to 0.0), so this is a
# no-op for existing deployments and only starts to matter once Phase 6
# raises the sample rate above zero.
TIER_C_PROMOTION_MIN_CALLS = int(os.environ.get("TIER_C_PROMOTION_MIN_CALLS", "10"))

# Promote (shadow_only -> shadow_advisory, or shadow_advisory -> live)
# when the agreement rate over the last TIER_C_PROMOTION_MIN_CALLS scored
# calls is at or above this threshold.
TIER_C_PROMOTION_AGREEMENT_THRESHOLD = float(
    os.environ.get("TIER_C_PROMOTION_AGREEMENT_THRESHOLD", "0.9")
)

# Demote (live -> shadow_advisory, or shadow_advisory -> shadow_only) when
# the disagreement rate over the same window is at or above this
# threshold. Deliberately not required to be `1 - TIER_C_PROMOTION_
# AGREEMENT_THRESHOLD` -- promotion and demotion sensitivity are
# independent knobs (asymmetric hysteresis is the point: a series should
# be harder to knock out of "live" than it was to promote into it, or
# vice versa, depending on how these two are tuned).
TIER_C_DEMOTION_DISAGREEMENT_THRESHOLD = float(
    os.environ.get("TIER_C_DEMOTION_DISAGREEMENT_THRESHOLD", "0.3")
)

# Global kill-switch for whether the engine honors TierCPromotionState.
# is_manual_override at all -- distinct from that per-series column
# itself. Defaults to True (freezes are honored, as intended); sits
# behind its own flag only so a stuck/mis-set freeze can be neutralized
# codebase-wide via one env var without needing per-series DB writes to
# undo it.
TIER_C_MANUAL_OVERRIDE_HONORED = bool(
    os.getenv("TIER_C_MANUAL_OVERRIDE_HONORED", "true").lower() == "true"
)

# Step 10 Phase 1 (Multi-Provider Tier C, schema/settings scaffolding):
# both defaulted to fully inactive -- no call site read either of these
# until Step 10 Phase 4 (fan-out) existed, and even once that code
# existed, this value stayed 0.0 until the per-candidate aggregation
# layer (Step 10 Phase 5) shipped, so Step 9's promotion engine was never
# exposed to multi-provider rows while it was still reading them as flat
# per-call rows.
#
# Step 10 Phase 6 (activation): now that Phase 5 has shipped, this is
# raised from 0.0 to a deliberately small, non-zero value -- per the
# finalized Step 10 plan's own instruction ("start with a low rate, e.g.
# 0.05, to cap cost") -- rather than jumping straight to a large fraction
# of traffic. At 0.05, roughly 1 in 20 Tier-C-shadow-eligible candidates
# (excluding "live" state, which never fans out regardless of this value
# -- see below) pays for 2 extra provider calls (Groq + OpenAI) instead
# of the existing single Anthropic call; the other ~19 in 20 are
# unaffected. Same opt-in-by-value convention as every other Tier C
# setting in this module (see TIER_C_SHADOW_MAX_DAILY_COST_USD above),
# just no longer defaulting to fully off -- raise further (or back to
# 0.0 to fully disable again) independently of any code change, purely
# by adjusting this env var.
#
# Fraction (0.0-1.0) of Tier-C-shadow-eligible candidates that fan out to
# every configured provider in parallel, rather than the existing
# single-provider Anthropic-only shadow call. Never consulted for a
# candidate whose TierCPromotionState is "live" -- see that call site's
# own future comment for why live-state fan-out is out of scope for
# Step 10 entirely (safety-critical routing stays single-provider).
TIER_C_PARALLEL_SHADOW_SAMPLE_RATE = float(
    os.environ.get("TIER_C_PARALLEL_SHADOW_SAMPLE_RATE", "0.05")
)

# Per-call timeout applied to EVERY provider call in a parallel Tier C
# fan-out, regardless of tier_c_state -- deliberately not reusing
# TIER_C_LIVE_TIMEOUT_SECONDS's semantics (that one bounds a single,
# no-fan-out call on the live decision path). A fan-out orchestrator
# waits on the slowest of several concurrent calls, so an unbounded call
# here would block collection of already-finished sibling providers'
# results too, not just its own -- shorter than the live timeout on
# purpose, to limit how much any one slow provider can eat into the
# shared per-job round budget (SERIES_CHECK_HARD_TIMEOUT_SECONDS) now
# that up to 3 provider calls are in flight at once instead of one.
TIER_C_PARALLEL_CALL_TIMEOUT_SECONDS = float(
    os.environ.get("TIER_C_PARALLEL_CALL_TIMEOUT_SECONDS", "8")
)

# Step 11 Phase 3 (Provider/Model Scorecard & Tier C Confidence Signals,
# parse-failure spike detector): global, not per-series or per-tenant --
# `services.provider_model_scorecard.get_provider_model_scorecard`
# aggregates across every series, and this detector piggybacks on
# `services.tier_c_promotion_engine.evaluate_tier_c_promotion`'s existing
# per-Check-Now-job cadence (Step 9's own precedent for "no new
# scheduler/cron infrastructure") rather than introducing a second
# trigger point. Alert-only in Step 11: a provider/model crossing this
# threshold is logged for a human to notice, never auto-demoted or
# routed around -- see `services.provider_model_scorecard.
# ProviderModelMetricsAlert`'s docstring.
#
# PARSE_FAILURE_WINDOW_SIZE deliberately matches `services.provider_
# model_scorecard.DEFAULT_SCORECARD_WINDOW`'s own default (100) -- not a
# coincidence, just two independent settings that happen to agree today;
# each can be tuned independently without affecting the other.
PARSE_FAILURE_WINDOW_SIZE = int(os.environ.get("PARSE_FAILURE_WINDOW_SIZE", "100"))

# Fraction (0.0-1.0) of a provider/model's last PARSE_FAILURE_WINDOW_SIZE
# shadow calls that must be unparseable (`parsed_ok=False`) before an
# alert is logged for that provider/model. 0.15 (the finalized spec's own
# example) is a deliberately loose default -- occasional unparseable
# responses are already expected/tolerated by every downstream consumer
# of `parsed_ok` (see `_score_tier_c_shadow_response`'s docstring); this
# threshold is meant to catch a genuine, sustained spike (a provider
# consistently ignoring the JSON-mode/prompt instructions), not
# individual flaky responses.
PARSE_FAILURE_ALERT_THRESHOLD = float(os.environ.get("PARSE_FAILURE_ALERT_THRESHOLD", "0.15"))
