# Agentic System Overview

Static documentation only -- describes the system as built across Phases
1-10. This file has no code behind it; it is not imported, executed, or
tested. If behavior described here and the code ever disagree, the code
is correct and this file is stale and should be updated.

## 1. Summary of Phases 1-10

| Phase | Theme | What it added |
|---|---|---|
| 1 | Agentic substrate + shadow diagnostics | `agents/agentic_series_agent.py`'s deterministic shadow loop (`run_agentic_turn`), tracing hooks (`agentic_hooks.py`), the evaluation harness, replay runner, drift/TTL validators, report generator, and the first read-only `/admin/agentic/*` endpoints. Everything ran purely as a side-channel; nothing wrote to live tables or influenced live routing. |
| 2 | Dual execution + dual-write shadow tables | `agents/series_agent.py`'s live routing path started running the Phase 1 shadow loop once per turn ("dry-run") and dual-writing its outputs to three new shadow tables (`AgenticSkeletonPreview`, `AgenticConfidenceDecision`, `AgenticGateDecision`) -- still never touching `SeriesSkeleton.skeleton_json`/`probes_json` or live confidence/gate. |
| 3 | Candidate promotion (feature-flagged) | `agentic/promotion_evaluator.py`'s `evaluate_promotion` -- the first module to decide, per book, whether an agentic decision is *eligible* to replace the live one (`"use_live"` / `"use_agentic"` / `"reject_agentic"`), persisted to a new `AgenticPromotionDecision` shadow table. Gated by `settings.AGENTIC_ROUTING_ENABLED`; still "record, don't apply". |
| 4 | Controlled activation | `settings.AGENTIC_SERIES_ACTIVATION` + `settings.is_agentic_activated(series_id)` -- a second, per-series allowlist gate on top of Phase 3's flag. Only for series in that allowlist does a `"use_agentic"` promotion outcome actually change what live routing resolves to. |
| 5 | Unified resolution layer | `agentic/resolution.py`'s `resolve_routing_decisions` -- extracted the inline per-book "which side wins" branch out of `agents/series_agent.py` into one pure(ish), independently-testable function. No behavior change versus Phase 4. |
| 6 | Stability & determinism | Deterministic ordering guarantees across every history fetch/resolution (sort by `book_number`, tie-break by `timestamp`/`id`), so two runs over identical stored data always iterate/resolve in the same order. |
| 7 | Safety & guardrails | `agentic/safety.py`'s `validate_agentic_decision`/`validate_promotion_outcome` -- an independent, self-contained veto layer checked both inside `evaluate_promotion` (before finalizing `"use_agentic"`) and inside `resolve_routing_decisions` (defense-in-depth, re-checked even for an already-approved decision). |
| 8 | Performance & efficiency | `agentic/cache.py`'s `AgenticTurnCache` (opt-in per-turn memoization for `evaluate_promotion`/`resolve_routing_decisions`/`build_activation_preview`), bulk-read plumbing for shadow-table history fetches, and `_run_agentic_turn_guarded` in `agents/series_agent.py` (ensures `run_agentic_turn` executes at most once per live turn). |
| 9 | Observability & telemetry | Process-wide, in-memory counters (`services/discovery_telemetry.py`'s `get_agentic_metrics`), per-series health (`agentic/health.py`'s `compute_agentic_health`), and three read-only admin endpoints (`/metrics`, `/health/{series_id}`, `/summary`). |
| 10 | Finalization & hardening | Per-series readiness reports (`agentic/readiness.py`), a global invariant-enforcement check run at startup and on demand (`agentic/invariants.py`), the module consolidation into the `agentic/` package described in this file, and this document. |

Every phase preserved the guarantee established in Phase 1: with
`settings.AGENTIC_ROUTING_ENABLED` off, none of this code runs at all,
and the live pipeline is byte-for-byte identical to before any of it
existed.

## 2. Routing flow diagram

```
run_series_check (agents/series_agent.py)
        |
        v
  live pipeline computes, per traced book:
    live_confidence, live_gate (unconditional, always happens)
        |
        v
  settings.AGENTIC_ROUTING_ENABLED?
        |
   no ---+--- yes
   |          |
   v          v
 resolved_* = live_*        agents/agentic_series_agent.run_agentic_turn
 (nothing below runs)       (guarded to run at most once per turn --
                              agents/series_agent._run_agentic_turn_guarded)
                                   |
                                   v
                              per traced book:
                                agentic_confidence, agentic_gate (shadow trace)
                                   |
                                   v
                agentic.promotion_evaluator.evaluate_promotion(
                    live_conf, agentic_conf, live_gate, agentic_gate,
                    series_id=..., book_number=..., cache=...)
                                   |
                    (rules 1-6, then agentic.safety.validate_agentic_decision
                     re-check before finalizing "use_agentic";
                     agentic.safety.validate_promotion_outcome asserted
                     on the final literal)
                                   |
                                   v
                       outcome in {"use_live", "use_agentic", "reject_agentic"}
                                   |
                                   v
                agentic.promotion_evaluator.store_promotion_decision(...)
                  (writes one AgenticPromotionDecision shadow-table row)
                                   |
                                   v
              agentic.resolution.resolve_routing_decisions(
                  series_id, live_confidence_snapshot, live_gate_snapshot,
                  promotion_decisions, cache=...)
                                   |
                    settings.AGENTIC_ROUTING_ENABLED off?  -> resolved_* = live_* (sorted)
                    settings.is_agentic_activated(series_id) False? -> resolved_* = live_* (sorted)
                    outcome == "use_agentic" AND validate_agentic_decision(...) (defense-in-depth re-check)?
                        yes -> resolved_* = agentic_*
                        no  -> resolved_* = live_*  (logs a safety violation if evaluate_promotion
                                                      had already approved "use_agentic" but this
                                                      re-check vetoed it anyway)
                                   |
                                   v
                 resolved_confidence, resolved_gate actually used by
                 live routing for this book, this turn
```

The Phase 2 "dry-run" path (unconditional, independent of `AGENTIC_
ROUTING_ENABLED`) runs the same `run_agentic_turn` call (sharing the
Phase 8 guard above) purely to dual-write `AgenticSkeletonPreview`/
`AgenticConfidenceDecision`/`AgenticGateDecision` rows -- it never
reaches the promotion/resolution steps above and never affects
`resolved_confidence`/`resolved_gate`.

## 3. Promotion evaluator rules (`agentic/promotion_evaluator.evaluate_promotion`)

Pure function: no DB, no I/O, no provider calls -- same inputs always
produce the same outcome.

1. **Deterministic-invariant check**: an agentic decision with no usable
   confidence grade and no usable gate opinion at all, while the live
   side has at least one, is rejected outright (never silently falls
   through to `"use_live"`).
2. **Required-fields check**: for every confidence dimension *both*
   sides report, the agentic grade must rank `>=` the live grade. Any
   dimension ranking lower is a violation (this is also "must not
   reduce provider agreement" -- not a separate rule).
3. **Gate-contradiction check**: if both sides express an opinion on
   `belongs_to_series` and disagree, that is a violation.
4. Any violation from 1-3 -> `"reject_agentic"`.
5. Otherwise, if the agentic side ranks strictly higher on at least one
   shared confidence dimension, it is a *candidate* for `"use_agentic"`
   -- subject to step 6 below.
6. Otherwise (no violation, no improvement) -> `"use_live"`.
7. **Safety re-check** (Phase 7): before finalizing a `"use_agentic"`
   candidate from step 5, `agentic.safety.validate_agentic_decision`
   independently re-checks the same pair. Failing it downgrades the
   outcome to `"reject_agentic"`.
8. **Outcome validation** (Phase 7): the final outcome is asserted
   against `agentic.safety.validate_promotion_outcome` before returning
   (can only ever pass in practice, given this function's own control
   flow, but asserted rather than assumed).

Optional `cache` (Phase 8, an `agentic.cache.AgenticTurnCache`) +
`book_number` memoizes this whole computation per book for that cache's
lifetime -- changes nothing about *what* is decided, only how many
times it is recomputed.

## 4. Safety rules (`agentic/safety.validate_agentic_decision`)

Independent, self-contained (no import from `agentic.promotion_
evaluator`), pure, deterministic, never raises (an internal error is
treated as `False` -- "unsafe" -- rather than propagating). Checks, in
order (unsafe on the first failure):

1. **Malformed structures**: each of the four inputs must be `None` or
   a `dict`.
2. **Missing required fields**: if the agentic side was given a `dict`
   at all, it must offer *some* usable opinion.
3. **Negative confidence values**: no numeric field in `agentic_conf`
   may be negative.
4. **Impossible `book_number` jumps**: an opaque, optional `book_number`
   field, if present, must be a real, finite, non-negative, non-absurd
   number.
5. **Unrecognized confidence grades**: every recognized dimension's
   value must be one of the known grade strings.
6. **Malformed gate opinion**: `belongs_to_series`, if present, must be
   a real `bool`.
7. **Determinism invariant**: a degenerate agentic opinion while live
   has one of its own is unsafe.
8. **Must not contradict / must not reduce agreement**: for every shared
   confidence dimension, agentic must rank `>=` live.
9. **Gate contradiction**: an explicit live/agentic disagreement on
   `belongs_to_series` is unsafe.

Called twice per candidate: once inside `evaluate_promotion` (before
finalizing `"use_agentic"`), and again, independently, inside `agentic.
resolution.resolve_routing_decisions` (defense-in-depth -- catches a
resolution-time bug, a stale/replayed decision, or a future caller that
bypasses `evaluate_promotion` entirely). Every rejection, from either
call site, is logged via `services.discovery_telemetry.record_agentic_
safety_violation`, which also increments the process-wide `agentic_
safety_violations` counter (Phase 9).

## 5. Determinism guarantees (Phase 6)

- `agentic.promotion_evaluator.get_promotion_history` sorts rows by
  `(book_number ASC, timestamp ASC, id ASC)` in Python (after an
  unordered DB fetch), not via `ORDER BY` -- so the same fail-soft
  coercion (a malformed `book_number` sorts last instead of raising)
  applies uniformly regardless of database backend.
- `agentic.promotion_evaluator.get_latest_promotion_decisions` picks,
  per book, the row with the greatest `(timestamp, promotion_outcome)`
  pair -- `timestamp` primary, `promotion_outcome` only breaking an
  exact tie.
- `agentic.confidence_gate_store.get_agentic_confidence_history`/
  `get_agentic_gate_history` follow the identical convention.
- `agentic.resolution.resolve_routing_decisions` resolves books in
  ascending-`book_number` order, and both returned dicts always have
  their keys inserted in that same ascending order -- regardless of
  what order the inputs were built in (a Python `set`, previously used
  to collect "every book_number seen", has no guaranteed iteration
  order; the *values* were never nondeterministic, only the order
  callers iterated over them).
- `agentic.invariants.enforce_agentic_invariants` includes a standing
  check (`resolution_layer_returns_sorted_keys`) that this guarantee
  still holds, re-verified at every startup and on-demand `/startup-
  check` call.

## 6. Activation model

Two independent gates, both required for agentic routing to actually
*apply* to live confidence/gate (as opposed to merely being computed
and recorded):

1. **`settings.AGENTIC_ROUTING_ENABLED`** (Phase 3): a global boolean.
   Off -> none of the agentic promotion/resolution machinery runs at
   all for any series (the dry-run shadow-write block is the one
   exception -- it runs unconditionally, independent of this flag, per
   Phase 2). On -> `evaluate_promotion` runs and every decision is
   recorded to `AgenticPromotionDecision`, but live routing still only
   ever resolves to the *live* confidence/gate ("record, don't apply")
   unless gate 2 also passes for that series.
2. **`settings.is_agentic_activated(series_id)`** (Phase 4): `False`
   whenever gate 1 is off; otherwise `True` only if `series_id` appears
   in the comma-separated `settings.AGENTIC_SERIES_ACTIVATION` env var.
   Both gates true for a series -> a `"use_agentic"` promotion outcome
   (that also survives `resolve_routing_decisions`' own defense-in-
   depth safety re-check) actually becomes what live routing uses for
   that book, that turn.

Reversible with no code change/deploy: flipping either env var and
restarting the process is enough to change either gate's state for any
series.

## 7. Observability endpoints (Phase 9, all under `/admin/agentic/*`, owner-only, read-only)

| Endpoint | Returns |
|---|---|
| `GET /metrics` | Every process-wide, in-memory agentic counter (`services.discovery_telemetry.get_agentic_metrics`). |
| `GET /health/{series_id}` | One series' promotion outcome counts, the process-wide safety-violation count, its real current activation state, and a determinism sanity flag (`agentic.health.compute_agentic_health`). |
| `GET /summary` | A global rollup: the current activation allowlist plus a handful of process-wide counters. |
| `GET /readiness/{series_id}` | One series' readiness report (see below). |
| `GET /startup-check` | Whether `agentic.invariants.enforce_agentic_invariants` currently holds. |

Plus everything Phases 1-8 already exposed: `/evaluate/{series_id}`
(+`/html`), `/batch`, `/promotion/{series_id}`, `/series`, `/history/
{series_id}`, `/promotion-plan/{series_id}`, `/dry-run/{series_id}`,
`/previews/{series_id}`, `/confidence/{series_id}`, `/gate/{series_id}`,
`/promotion-history/{series_id}`, `/activation-preview/{series_id}`,
`/activation-status`. None of these is a promotion/activation mechanism
itself -- every route in this router is a thin, read-only pass-through
to an already shadow-mode-only service function.

## 8. Readiness model (Phase 10, `agentic/readiness.compute_agentic_readiness`)

A per-series pre-flight/health snapshot answering "is it currently safe,
per everything this process can observe, for agentic routing to be live
for this series right now":

| Field | Meaning |
|---|---|
| `promotion_history_ok` | The promotion-history read path itself works without raising. |
| `safety_violations_recent` | The process-wide, lifetime `agentic_safety_violations` counter (no per-series/per-time-window store exists -- see caveat below). |
| `determinism_ok` | `agentic.health.compute_agentic_health`'s own determinism flag for this series. |
| `activation_state` | `settings.is_agentic_activated(series_id)` right now. |
| `metrics_ok` | The Phase 9 observability counters are present and well-formed. |
| `cache_ok` | A self-contained `agentic.cache.AgenticTurnCache` smoke test (throwaway probe data only) passed. |
| `ready` | `True` only if every field above is `True` (as applicable) *and* `safety_violations_recent == 0`. |

**Caveat**: because `safety_violations_recent` mirrors a process-wide,
lifetime counter rather than a per-series or per-time-window one, a
process that has ever logged a single safety violation for *any* series
will show `ready=False` for *every* series afterward, until the process
restarts. This is an intentional, conservative simplification for this
phase, not a bug -- a real production deployment would eventually want
a rolling/windowed counter instead. `compute_agentic_readiness` never
calls a live provider, never calls `run_agentic_turn`, and never writes
anything or flips either activation gate itself; it is strictly a read-
only report.
