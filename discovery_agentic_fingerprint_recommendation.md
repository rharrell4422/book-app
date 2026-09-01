# Series Fingerprint System — Recommendation

**Companion to:** `discovery_agentic_replacement_recommendation.md` (deterministic-vs-agentic split, `SeriesSkeleton` as durable memory) and `discovery_agentic_phase1_evaluation.md` (current-state ground truth for the shipped agentic scaffold this system extends). Consolidates a ten-round design conversation ("Series fingerprint system architecture") into the single converged spec; nothing below is a proposal still under discussion — this is what shipped.

**Scope:** a second, narrow, additive companion table to `SeriesSkeleton` that captures per-series *identity/pattern* memory — author aliases, catalog naming noise, per-series provider trustworthiness, and release cadence — and feeds it back into `confidence_engine.py` as an optional, always-safe scoring input.

**Headline recommendation:** do not extend `SeriesSkeleton` itself, and do not build a second confidence engine. `SeriesSkeleton` owns *what books exist* (titles, numbering, status, sources, gaps) — one row per known book. Fingerprinting owns *how this series tends to look* — one row per series, four fields, none of which have a natural per-book home. Keep them as two tables with one relationship (`series_id`), a Builder that is always on and free, and a Consumer that is gated behind a dedicated two-tier flag and touches exactly two existing grades inside `confidence_engine.py` — nothing else in the pipeline changes shape.

---

## 0. Why a second table, not a fifth `SeriesSkeleton` field

`SeriesSkeleton.skeleton_json` is a list of book-number-keyed entries; every field on it answers a per-book question ("is book 7 confirmed, and from where"). None of the four fingerprint signals are per-book:

- **Author aliases** — a property of the *series*, observed across many candidates, not tied to any one book number.
- **Naming patterns** — the branding noise this series' catalog listings tend to carry (`" - <Series> #<N>"`, `": A <X> Universe Novella"`) — again series-wide, not book-specific.
- **Provider bias** — how trustworthy each provider's hits have historically been *for this series* — a per-`(series, provider)` statistic, not a per-book one.
- **Release cadence** — the mean/stddev of interval-between-releases — a statistic *over* the book list, not an attribute *of* any book in it.

Forcing any of these onto a per-book entry would mean either duplicating the same value onto every entry (author_aliases repeated on all N rows) or inventing a book-number-shaped home for a series-shaped fact. A second table with `series_id` as its own primary key — the exact same shape `SeriesSkeleton` itself uses — is the natural fit, and it means `SeriesSkeleton`'s own read/write paths need zero changes to support this feature.

## 1. Table shape

One row per series, mirroring `SeriesSkeleton`'s primary-key/version/timestamp shape:

```python
class SeriesFingerprint(Base):
    __tablename__ = "series_fingerprint"

    series_id = Column(Integer, ForeignKey("series.id"), primary_key=True)
    fingerprint_json = Column(JSON, nullable=False, default=dict)
    schema_version = Column(Integer, nullable=False, default=1)
    version = Column(Integer, nullable=False, default=0, server_default="0")  # optimistic concurrency
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

`fingerprint_json` is a single flat dict, not a list — there is no per-book axis on this table at all:

```jsonc
{
  "author_aliases": ["H. Cooper", "Harmon C."],
  "naming_patterns": ["dash_series_marker", "colon_universe_marker"],
  "provider_bias": { "hardcover": 1.32, "web_search": 0.71 },
  "release_cadence": {
    "mean_interval_days": 182.4,
    "stddev_interval_days": 21.7,
    "interval_count": 6
  }
}
```

Every field is plain strings/floats/counts — model-agnostic and provider-agnostic by construction (design chain item 4: "model-agnostic schema"), never an LLM-specific shape, so nothing about this table needs to change if the LLM or provider stack changes underneath it.

## 2. Builder / Consumer split (mirrors `SeriesSkeleton` exactly)

Two independent halves, same discipline as `services/skeleton_store.py`:

- **Builder** (`services/fingerprint_store.apply_fingerprint_updates`) — runs post-round, post-persistence, from the same `services/series_check_engine.py` call site that already calls `skeleton_store.apply_skeleton_updates`. Merges this round's observations into the durable row. **Always runs, unconditionally, regardless of either activation flag below** — "shadow-first": the fingerprint is always built at zero extra cost (it only reads this round's already-computed skeleton/delta/confidence output, no new fetches), and only its *influence* on live scoring is gated. This is the single most important convergence point of the whole design chain (item 9) — it means Fingerprinting can go live in production, accumulating real data, months before its first byte is ever allowed to change a score.
- **Consumer** (`services/fingerprint_store.get_effective_fingerprint`) — read exactly once per job, before confidence scoring, in `agents/series_agent.py`. Resolves the two-tier activation gate (§4) at this one call boundary and nowhere else. `confidence_engine.py` never imports `settings` or checks either flag itself — it only ever sees whatever `fingerprint` argument it was handed, and `None` means "no influence" (identical in effect to a real-but-empty fingerprint with no data yet).

Both halves live in one new module, `services/fingerprint_store.py`, deliberately **not** a generalization of `skeleton_store._upsert_skeleton_row` (design chain Round 4's explicit call): that helper is a stable, already-hardened production primitive `SeriesSkeleton` depends on; a second, additive `_upsert_fingerprint_row` carries zero regression risk to it, versus editing it to serve a second table for the sake of one shared implementation.

### 2.1 Concurrency

`_upsert_fingerprint_row` is the fingerprint analogue of `_upsert_skeleton_row`: read-with-`with_for_update`, recompute via a caller-supplied pure `merge_fn(existing) -> new`, conditional `UPDATE ... WHERE version = read_version`, retry with jittered backoff on a zero-rowcount conflict (up to 5 attempts), insert-if-absent on first write. Same bounds, same rationale, same "re-read fresh on every retry attempt so a concurrent writer's change is never silently clobbered" guarantee — just operating on a flat dict instead of a book-number-keyed list.

### 2.2 Shadow tracing

`agentic_hooks.shadow_fingerprint_merge_trace(context, before, after)` is the fingerprint analogue of `shadow_skeleton_merge_trace`, but with genuinely different diff semantics because the payload shape is different: list fields (`author_aliases`, `naming_patterns`) report which items were added; `provider_bias` reports which provider keys changed value and by how much; `release_cadence` reports its before/after dict wholesale (a small, fully-recomputed stat blob each round, not worth a sub-diff). Fired strictly after a successful commit, from inside `_upsert_fingerprint_row` — same non-feedback guarantee as the skeleton trace: it can never influence the merge it's reporting on.

## 3. Builder observations — what gets recorded, and from what

`build_fingerprint_observations(skeleton_entries, delta, confidence, *, series_author=None)` is a **pure function** — no DB access, no network, no LLM call — computed inside `agents/series_agent.py` from that round's already-computed skeleton/delta/confidence output alone (design chain item 9: "fingerprint building is free"). It returns one observation payload per round; `apply_fingerprint_updates` merges it into the durable row.

| Field | Source signal | Merge rule |
|---|---|---|
| `author_alias_observations` | This round's candidates with `series_alignment_confidence == "medium"` (the existing initials-variant branch in `_series_alignment_confidence`) — a plausible abbreviation/expansion of the series' own author string, not yet a confirmed exact match. | Append + case-insensitive dedupe, capped at 25 most-recent entries. |
| `naming_pattern_observations` | This round's *accepted* (`overall` high/medium) candidates' raw titles, tagged against the two catalog-branding-noise shapes `discovery_text.py` already recognizes globally (`_DASH_SERIES_MARKER_PATTERN`, `_TITLE_SERIES_MARKER_PATTERN`). | Append + dedupe (same cap). |
| `provider_bias_observations` | Every provider that contributed to at least one of this round's scored candidates: `overall` high/medium → positive signal (`1.3`); `overall` low/zero → negative signal (`0.7`). A provider absent from this round produces no observation at all. | Exponential moving average (§5). |
| `release_cadence` | Recomputed fresh, every round, from `SeriesSkeleton` entries' `release_date` (never discovery-bookkeeping timestamps), restricted to `source_class == "library"` — owned, trustworthy release history. | Full replacement each round (§6) — not a partial merge. |

`delta` and `series_author` are accepted for interface symmetry and reserved for future signals (weighting `provider_bias` by whether a hit was ever flagged `duplicate_number`; never recording an alias identical to the series' own author string) — no current behavior depends on either yet.

Two observations recorded here are deliberately **build-only** for this pass: naming-pattern and author-alias data is captured every round so it accumulates from day one, but *consuming* either signal to infer something (e.g. "this is a renamed volume, not a new one") is out of scope — see §9. Recording is unconditional; acting on what's recorded requires the three-way corroboration rule that section describes, and that consumer does not exist yet.

## 4. Activation gate

A dedicated, standalone two-tier gate — **not** a reuse of the pre-existing `AGENTIC_ROUTING_ENABLED` / `is_agentic_activated` pair, which gates an unrelated subsystem (the agentic promotion evaluator) and does not govern the always-live `confidence_engine.py` code path this feature touches:

```python
FINGERPRINT_INFLUENCE_ENABLED = bool(os.getenv("FINGERPRINT_INFLUENCE_ENABLED", "false").lower() == "true")
FINGERPRINT_SERIES_ACTIVATION = os.getenv("FINGERPRINT_SERIES_ACTIVATION", "")  # comma-separated series_ids

def is_fingerprint_activated(series_id: int) -> bool:
    ...  # False whenever the global flag is off; else True iff series_id is in the allowlist
```

Resolved exactly once per job, inside `get_effective_fingerprint(db, series_id)`, immediately before confidence scoring in `agents/series_agent.py`. Both flags are read fresh on every call (no caching), matching `is_agentic_activated`'s own test-ability rationale. This is the *only* place either flag is ever checked — every downstream consumer treats a `None` fingerprint as "no influence" and never asks why.

## 5. Provider bias — the EMA formula, pinned

Design chain items 5/6, Round 4's explicit demand to "name the exact formula, not a bare 'an EMA'":

```
new_bias[provider] = clamp(
    previous_bias[provider] * (1 - alpha) + signal * alpha,
    PROVIDER_BIAS_MIN, PROVIDER_BIAS_MAX,
)
```

with `alpha = 0.2`, domain `[0.5, 1.5]` (neutral = `1.0`), accept-signal `1.3`, reject-signal `0.7`. A provider with no observation this round keeps its existing bias untouched — it is never decayed toward neutral just for going quiet for a round. These four numbers are the one deliberately-open tuning surface the design chain left for implementation time (Round 11: "the *mechanism* — incremental EMA, full-history, no windowing — is the design decision; these numbers are the implementation-time tuning knob").

**Consumption** (`confidence_engine._apply_provider_bias_to_grade`), grade-mode only: a bias at or above `1.15` nudges that provider's base grade up one step on the shared `zero < low < medium < high` order; at or below `0.85` nudges it down one step; the `(0.85, 1.15)` band in between is a no-op. `bias is None` (no signal yet for this provider) is a no-op, identical to `fingerprint is None`. The nudge is a single shared primitive (`_nudge_grade`) also used by the cadence check below, rather than two independently-reinvented clamp-and-step implementations (Round 5 item 3).

A **float-weight half** of provider bias — re-biasing `deterministic_fusion.py`'s `_PROVIDER_CONFIDENCE_WEIGHT` / `_PROVIDER_SORT_RANK` so a per-series-trusted provider's *candidates* get fused/ranked ahead of a per-series-distrusted one's, not just graded differently after the fact — is explicitly deferred; see §9.

## 6. Release cadence — the "implausibly early" formula, pinned

The most heavily refined single piece of this design (Rounds 4–11). Final, converged formula, downward-only:

```
ref_entry = highest-numbered SeriesSkeleton entry with
    book_number < candidate_number, source_class == "library",
    release_date resolvable
gap_count = candidate_number - ref_entry.book_number
base_interval = max(0, mean_interval_days - k * stddev_interval_days)
margin = margin_days_by_precision[candidate_date_precision]
expected_earliest_plausible_date =
    ref_entry.release_date + max(0, gap_count * base_interval - margin)

candidate_date < expected_earliest_plausible_date
    -> nudge number_confidence "medium" -> "low"
```

Design decisions this pins down, in the order the chain converged on them:

1. **Reference-entry population must match the cadence-statistics population exactly** (Round 10's catch, Round 11's ruling): both draw from `source_class == "library"` entries with a resolvable `release_date` — never `"confirmed"` more broadly, never discovery-bookkeeping timestamps (`first_seen_at`/`last_confirmed_at`). Comparing a candidate against a reference point drawn from a different population than the mean/stddev were computed from would make the whole check statistically meaningless.
2. **Minimum-history floor**: never fires with fewer than 2 intervals (3+ dated library entries) — both in `_compute_release_cadence`'s own floor and re-checked at the consumption site. Below that floor, a mean/stddev pair is presumptively noise, not signal.
3. **Downward-only, deliberately** (Round 6/7): the upward direction (nudging a *late*-looking release more confident) is inert against `_overall_confidence`'s existing "unverified"-ceiling rule for every candidate this feature can ever see, so it was not implemented at all — not merely left unused, actually absent from the code, per the chain's "don't ship dead branches" stance.
4. **Precision-aware margin** (Round 9): a full ISO date candidate gets a zero margin; a year-month-only candidate gets 45 days; a year-only candidate gets 365 days. `SeriesSkeleton.release_date` is always stored at FULL precision, so only the *candidate's* date needs a margin — the reference side never does. This required `discovery_text.py`'s `parse_flexible_date` to be split into a private branch-returning helper plus two public wrappers (`parse_flexible_date` unchanged, `parse_flexible_date_with_precision` new) so this feature could get precision tags without touching any existing caller's return contract.
5. **`k` and the margin table are the one deliberately-open tuning surface** (Round 11): the *formula* is fully pinned; the *numbers* (`k = 2.0`, margins `{FULL: 0, YEAR_MONTH: 45, YEAR_ONLY: 365}`) are implementation-time tuning parameters, set conservatively (Round 7/8's explicit, accepted tradeoff): a false "implausibly early" verdict is `number_confidence`'s only unconditional, no-escalation auto-downgrade path this feature adds, so `k` is set high enough to keep a false positive rare even for well-dated comparisons where the margin has shrunk to zero.

**Consumption**: `confidence_engine._number_confidence` gains two optional parameters, `skeleton_entries` and `fingerprint`. When the cadence check fires, a candidate that would otherwise grade `"medium"` (a structurally valid number simply not yet in the skeleton) is downgraded to `"low"` instead. Nothing else about `_number_confidence` changes — a malformed number is still `"low"` regardless, and a number already `"high"`/present in the skeleton is untouched.

## 7. `compute_confidence` signature

One new optional keyword, threaded through the two dimensions above and nowhere else:

```python
def compute_confidence(
    series_id: int,
    skeleton_entries: list[dict],
    provider_candidates: list[dict],
    delta: dict,
    *,
    series_name: str | None = None,
    series_author: str | None = None,
    shadow_context: dict | None = None,
    fingerprint: dict | None = None,   # new
) -> dict:
```

`title_confidence` and `series_alignment_confidence` are **untouched** — the design chain never found a signal on this table that plausibly bears on either dimension without duplicating logic `_title_is_series_variant`/`belongs_to_series` already own. `fingerprint=None` (every call site before this feature existed, and every call site where the activation gate is closed) reproduces `compute_confidence`'s exact pre-fingerprint output — this module's purity guarantee (module docstring: "makes no LLM/network/DB call") is preserved; it is a pure function of its arguments including this one.

## 8. Wiring into the live pipeline

Exactly one insertion point on the read side, exactly one on the write side, both inside code paths that already exist for `SeriesSkeleton`:

**Read** (`agents/series_agent.py`, immediately before `compute_confidence`):
```python
effective_fingerprint = get_effective_fingerprint(db, series_id)
series_confidence = confidence_engine.compute_confidence(
    series_id, skeleton_entries, unified_candidate_dicts, series_delta,
    series_name=series.name, series_author=series_author,
    shadow_context=agentic_context, fingerprint=effective_fingerprint,
)
fingerprint_updates_this_round = build_fingerprint_observations(
    skeleton_entries, series_delta, series_confidence, series_author=series_author,
)
```
`fingerprint_updates_this_round` is threaded through `result["fingerprint_updates"]`, always present (never `None` unless the enclosing computation itself failed) — same "real, empty-safe payload" rule `result["skeleton_updates"]` already follows, so the write-side caller never has to guess whether the key exists.

**Write** (`services/series_check_engine.py`, same post-persistence block that already calls `skeleton_store.apply_skeleton_updates`):
```python
try:
    apply_fingerprint_updates(db, series_id, updates=result.get("fingerprint_updates"))
    telemetry.record_gate_outcome("fingerprint_update", "succeeded")
except Exception as exc:
    telemetry.record_gate_outcome("fingerprint_update", "failed")
    fingerprint_update_failures.append(f"round {rounds_run}: {exc}")
```
Never allowed to fail the round — mirrors `skeleton_update_failures`'s exact fail-soft discipline (a stale/un-merged fingerprint self-heals on the next round's Builder pass) and is surfaced in the job result the same way (`result["fingerprint_update_failures"]`).

Nothing else in the round loop, the persistence body, `delta_engine.py`, or `deterministic_fusion.py` changes shape. The engine selector, the stop condition, `belongs_to_series`, and every identity-collapse pass are all untouched.

## 9. Deferred to follow-up (explicitly out of scope for this pass)

Named here so they are not silently forgotten, per the design chain's own "list what's deferred and why" convention:

- **Provider-bias float-weight integration into `deterministic_fusion.py`** (`_PROVIDER_CONFIDENCE_WEIGHT`, `_PROVIDER_SORT_RANK`) — today provider bias only re-grades a candidate *after* fusion has already picked a canonical record; biasing which record fusion prefers in the first place is a real, larger change to a module this design chain deliberately left untouched for this pass.
- **Naming-pattern / author-alias consumption** — both fields are built and accumulating today, but nothing reads them back to make a decision yet. The design chain's stated bar for a consumer here is three-way corroboration (numbering + author + provider) before ever inferring "this is a renamed volume," which is a meaningfully bigger feature than the Builder work in this document.
- **`delta`-weighted provider-bias signal** — e.g. discounting a provider's positive signal if its hit was ever separately flagged `duplicate_number` by `delta_engine`. `build_fingerprint_observations` already accepts `delta` for exactly this reason; no behavior depends on it yet.

## 10. Summary

| Question | Recommendation |
|---|---|
| New table or extend `SeriesSkeleton`? | New table, `series_id`-keyed, one row per series — none of the four signals are per-book |
| Concurrency | Optimistic-concurrency upsert-with-retry, parallel implementation to `_upsert_skeleton_row`, not a shared generalization of it |
| Building vs. consuming | Builder always on and free (shadow-first); Consumer gated behind a dedicated `FINGERPRINT_INFLUENCE_ENABLED` + per-series allowlist pair |
| Confidence-engine surface touched | Exactly two dimensions — `provider_confidence` (bias grade nudge) and `number_confidence` (cadence-based downward-only nudge) — via one new optional `fingerprint` argument |
| Provider bias mechanism | EMA, `alpha=0.2`, domain `[0.5, 1.5]`, accept/reject signals `1.3`/`0.7` — tuning numbers, not the pinned mechanism |
| Cadence mechanism | `k`-sigma-below-mean interval floor + precision-aware date margin, reference entry drawn from the same `source_class == "library"` population the statistics themselves use, downward-only |
| Biggest risk avoided | Consuming naming/author signals to infer renames without corroboration — explicitly deferred, not implemented half-way |
