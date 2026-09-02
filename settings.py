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
