# Agentic Replacement Architecture — Recommendation

**Companion to:** `discovery_agentic_migration_architecture_map.md` (canonical repo map, commit `fdbec65`) and `discovery_catchup_architecture_spec.md` (design history).

**Scope:** what to build on top of the existing repo to replace the deterministic multi-pass discovery pipeline with an agentic one, without losing the correctness the current pipeline encodes.

**Headline recommendation:** do **not** replace the pipeline with an agent. Replace the pipeline's *policy layer* with an agent and keep its *mechanism layer* permanently. The map's own replace-set (§4.1) is almost entirely policy — thresholds, query-width constants, round counts, "should I dig further" judgments — while its keep-set (§4.2, §6.2) is almost entirely mechanism — fusion, identity, filters, the `belongs_to_series` gate, persistence. That split is the architecture. Everything below is a consequence of it.

---

## 0. Two blocking defects in the existing scaffold

**SD-12 / RESOLVED (Phase 0, commit `ea22d7b`):** both defects below have
since been fixed and are covered by regression tests
(`tests/test_skeleton_store.py`, `tests/test_confidence_engine.py`). This
section is kept as historical design rationale -- *why* the merge-based
skeleton rebuild and the `unverified` confidence grade exist -- not as an
open item for Phase 1 planning.

These are not design opinions; both are verifiable in the current code and both will silently break an agentic migration if not fixed first. They belong in Phase 0 of the migration plan (§5), before any agent code is written.

### 0.1 The boot-time skeleton rebuild destroys agent memory

`main.py:30` calls `backfill_all_skeletons()` on every boot. `skeleton_store.backfill_skeleton_for_series` is a **full destructive rebuild from owned `Book` rows only**:

```73:81:services/skeleton_store.py
    if skeleton is None:
        skeleton = models.SeriesSkeleton(series_id=series_id)
        db.add(skeleton)

    skeleton.skeleton_json = entries
    skeleton.schema_version = SCHEMA_VERSION
    return skeleton
```

`skeleton_json` is *assigned*, not merged, and `entries` is derived exclusively from `active_books`. The moment an agent writes a durable finding the library does not own — "book 14 exists, unowned, confirmed, releasing 2026-03-02" — the next server restart erases it. The docstring's claim that a full rebuild "can't drift or accumulate stale entries on its own" is correct *today* precisely because owned `Book` rows are the only input; it stops being true the instant the skeleton becomes agent-writable.

The entry schema already carries the provenance needed to fix this — `sources[].provider == "library"` (`skeleton_store.py:38-44`). The fix is to make the rebuild a **merge keyed on `book_number`**, where library-sourced entries are rebuilt from ground truth and non-library entries are preserved with their `first_seen_at` / `last_confirmed_at` intact. A library entry appearing for a number the agent had predicted should *upgrade* that entry (status → `confirmed`, sources gain `library`), not replace it, so `first_seen_at` continues to record when the agent first found it.

### 0.2 `confidence_engine` cannot score a genuinely new book above "low"

This is the more serious one, because the obvious escalation policy ("act on `high`, escalate on `medium`, drop `low`") produces an agent that accepts nothing.

Trace a perfect new discovery — Hardcover-sourced, valid number 14, exact author match, series not yet containing book 14:

| Dimension | Value | Why |
|---|---|---|
| `provider_confidence` | `high` | `_PROVIDER_CONFIDENCE["hardcover"]` (`confidence_engine.py:45`) |
| `title_confidence` | **`low`** | `skeleton_by_number.get(14)` is `None` → early return `"low"` (`confidence_engine.py:111-116`) |
| `number_confidence` | `medium` | valid, but `14 not in skeleton_numbers` (`confidence_engine.py:141-143`) |
| `series_alignment_confidence` | `high` | exact author token match (`confidence_engine.py:172-173`) |
| **`overall`** | **`low`** | not all-high, and a `low` is present → falls through to `low` (`confidence_engine.py:215-221`) |

By construction, **`overall` can only reach `high` for a book already in the skeleton** — i.e. one the library already owns. `compute_confidence` as written is a *skeleton-corroboration* scorer, not a *candidate-acceptance* scorer. It answers "does this candidate agree with what I already know," which is exactly right for its shadow-mode diagnostic purpose and exactly wrong as an escalation signal for new-volume discovery, which is the entire point of the migration.

Two ways to fix it, and the choice matters:

- **Preferred — add a fifth grade `unverified` to the title dimension** for "no skeleton entry exists to compare against," distinct from `low` ("a skeleton entry exists and this title disagrees with it"). Then extend `_overall_confidence` so `unverified` on title alone, with everything else `high`/`medium`, yields `medium` rather than `low`. This preserves the "round down toward caution" principle in the docstring while distinguishing *unverified* from *contradicted* — the same distinction the codebase already treats as load-bearing elsewhere (`None` vs `CACHE_MISS` in `discovery_cache.py:42`, map §5.1).
- **Alternative — leave the engine alone and have the agent read the dimensions rather than `overall`.** Cheaper, but it means the escalation policy re-derives the combination rule, which reintroduces the exact "two places disagree about the same judgment" problem `confidence_engine`'s docstring says it exists to avoid.

Take the first. Note the module docstring already argues against adding a fifth grade (the OpenLibrary "medium-low" collapse) — that argument was about a fifth grade existing on *one input dimension* making `overall` ambiguous. This is the opposite case: an explicit fifth *state* with an explicit rule in `_overall_confidence`, which removes ambiguity rather than adding it.

---

## 1. Deterministic vs Agentic

### 1.1 Recommendation

Deprecate the deterministic **policy** layer. Keep the deterministic **mechanism** layer permanently, not as a transitional fallback.

**Deprecate (agent replaces):**
- `_should_trigger_author_fallback` / `_series_completeness_and_confidence` and their `0.5` / `0.35` thresholds
- `_needs_llm_reconciliation` and its `0.8` / `0.2` / `0.5` thresholds
- The blind lookahead: `WEB_SEARCH_LOOKAHEAD_BOOKS = 10` numbered queries fired on every targeted pass regardless of what's known
- `MAX_MISSING_VOLUME_LOOKAHEAD_QUERIES = 6` as a fixed cap, and `_reconstruct_series_skeleton`'s from-scratch recompute
- Eventually, `SERIES_CHECK_MAX_ROUNDS = 3` as the loop control (last, per map §6.3 point 5)

**Keep forever (agent must funnel through, never re-derive):**
- `_fuse_and_score_candidates` / `UnifiedCandidate` / `_filter_and_merge` / `_finalize_candidates`
- `services/identity.py` keys and the three identity-collapse passes
- `belongs_to_series` + `_is_known_candidate` in `agents/series_agent.py`
- `precheck_for_new_volumes`
- All persistence in `series_check_engine.py`

This is a stronger claim than "keep them as fallback." These are not a safety net the agent eventually outgrows — they are the validation surface that makes an agent safe to deploy at all. The agent proposes; deterministic code disposes.

### 1.2 The four axes

**Reliability.** The deterministic pipeline's failure mode is *systematic*: an entire class of series fails identically and reproducibly (under-indexed indie series, series where no provider carries numbering). That is bad coverage but excellent debuggability — you can reproduce it, fix it, and know the fix held. An agent's failure mode is *stochastic per-run*: better long-tail coverage, but "it worked yesterday" stops being evidence. The mitigation is structural, not prompt-engineering: because every agent finding still passes `belongs_to_series` and `_filter_and_merge`, the agent's non-determinism can only affect **recall** (which candidates get proposed), never **precision-critical invariants** (author match, profile isolation, identity collapse). That asymmetry is what makes the migration acceptable at all, and it is worth stating as a design rule: *the agent may only widen the funnel, never bypass a gate.*

**Maintainability.** The eight tuned constants listed above are the real maintenance burden, and each was set in response to a specific incident. An agent replaces them with one budget and one eval suite. That is a genuine win — but only after the eval suite exists. Map §5.3 is exactly right that the regression-fixed heuristics (Jonathan Hunt, Safehold, Starship's Mage) are an unwritten eval suite; until they are written down as fixtures with expected accept/reject, "the agent is as good as the pipeline" is unfalsifiable and the migration is a guess.

**Cost.** The baseline is concrete (map §5.2): **39 Brave + 10 LLM calls / 73.8s** for a cold 18-book reconstruction; **0 / 0 / 1.19s** for an idle-confirmed series. A naive agent loop will lose on both — tool-call round-trips and per-turn reasoning tokens are strictly additive over a pipeline that fires 4 providers in parallel and batches its LLM structuring. The agent wins only by making *fewer, better-chosen* fetches, and the mechanism for that is durable skeleton memory: today's 39 comes largely from 10 blind lookahead queries plus 6 missing-volume queries, re-derived from scratch on each of up to 3 rounds, against a Layer-A cache keyed on literal query text that misses when two call sites build the author string differently (map §2.3, §7.1). An agent that knows which numbers are already confirmed never issues a blind lookahead. Target: **≤20 searches, ≤8 LLM turns cold; 0/0 idle** (the precheck is untouched, so the idle path stays free). Anything worse than the 39/10 baseline is a failed migration, and that comparison must be automatic from day one — which is why the agent must use the same `DiscoveryTelemetry` object (map §6.3 point 2).

**Correctness.** Deterministic code is strictly better at identity, dedup, and author matching — exact-match logic where an LLM has no comparative advantage and non-determinism is a pure regression (map §4.2). The agent is better exactly where the current hard filters over-reject: novellas and `.5` companion entries, omnibus/box-set editions, re-titled UK/US releases, anthologies containing one series entry. The existing drop diagnostics (`_record_drop_diagnostic`, threaded through every filter point, surfaced via `compute_drop_explanations`) are already the instrument for measuring this — they are the highest-value dataset in the repo for the migration, because they enumerate what the deterministic pipeline threw away and why. Phase 1 shadow mode should be evaluated primarily against them.

### 1.3 Invariants an agentic design must carry forward

Restating map §1.11 and §5.1 as constraints on the agent, since these are where an agentic rewrite most easily regresses:

1. **URL-keyed, never index-keyed, downstream of any LLM call.** An agent tool response returning positions into a list must be resolved against that exact list immediately, before any filtering or reordering. This is the single easiest invariant to break when a tool-calling loop replaces a batched structuring call.
2. **Original list order preserved on reassembly** — `_first_present_field` backfill is order-dependent; parallel tool calls must be reassembled in issue order, not completion order.
3. **A cached rejection is not final.** The `bypass_cached_rejection=True` escape hatch for the missing-volume pass exists because a rejection from a noisy batch must not poison a focused second look. The agent's memory needs the same escape hatch: a negative finding must record *what kind of look produced it*, so a cheap sweep's "not found" never blocks a targeted probe.
4. **"Rejected" ≠ "never checked."** Agent memory must represent both explicitly (§2.1 below).
5. **Confidence tags must survive re-merges** (`_filter_and_merge:1780`) — the agent's own provenance tags must merge the same way, preserving the first assignment.
6. **`all_providers_failed` means zero usable data, not zero surviving candidates.** An agent concluding "I found nothing relevant" is a successful run, not a provider failure.
7. **Profile isolation is absolute.** Every agent DB read stays `profile_id`-scoped.

---

## 2. Agentic Workflow Design

### 2.1 `skeleton_store` as durable memory

Graduate the skeleton from boot-backfilled to **read-write, agent-owned, merge-on-rebuild** (after fixing §0.1). Three changes to the entry shape, all additive to the existing schema:

- **`source_class`**: `"library"` (rebuildable from `Book` rows) vs `"discovered"` (durable, agent-written). Derivable from `sources[].provider` today, but making it explicit keeps the merge rule cheap and readable.
- **`probes`**: negative and inconclusive memory, per number — `{book_number, probed_at, probe_kind: "sweep"|"targeted"|"disambiguation", outcome: "absent"|"inconclusive", queries_used, ttl_days}`. This is the structural answer to invariant #3 and #4: a `sweep`-kind `absent` must not suppress a later `targeted` probe, and neither is the same as a number never probed. Without this the agent re-queries the same dead ends every run and the cost target is unreachable.
- **`expected_total`** at the skeleton level, with provenance (`hardcover_series_total_hint` vs LLM-inferred vs owned-max). Today this is recomputed per call inside `_reconstruct_series_skeleton` and thrown away.

The skeleton becomes the agent's opening context: it starts every run knowing what is confirmed, what is predicted-but-unowned, what has been probed and found absent, and how many volumes the world thinks exist. That is the difference between a 20-fetch run and a 39-fetch run.

### 2.2 `delta_engine` as the observation step

Call `compute_series_delta` at the **start of every agent iteration**, not once per run. It is pure, cheap, and its three outputs map cleanly onto three agent intents:

| Delta output | Meaning | Agent action |
|---|---|---|
| `numbering_gaps` | skeleton has it, this run's candidates didn't confirm it | targeted verification probe (or skip if `probes` says recently absent) |
| `missing_books` | discovery found it, skeleton lacks it | corroboration probe from a different provider class, then skeleton write |
| `malformed_books` | structurally unsound candidate | repair probe if `reason` is recoverable (`insufficient_metadata`), else drop with a negative finding |

One adjustment: today the caller feeds it `discovery["unified_candidates"]` (pre-`_filter_and_merge`, deliberately — see the docstring). In the agent loop, feed it the agent's **accumulated findings so far this run**, in the same `UnifiedCandidate.model_dump()` shape. That keeps `delta_engine` untouched and makes it a genuine loop-invariant "what remains unknown" function.

The `duplicate_number:N` reason is particularly valuable as an agent signal — it means fusion's identity chain (`isbn13 → title_key → normalized-title`) failed to collapse two records of the same book, which is precisely the ambiguous-metadata case where an LLM adds value and deterministic code doesn't.

### 2.3 `confidence_engine` as the escalation policy

After the §0.2 fix, express escalation directly against the grades — this is the replacement for `0.5` / `0.35` / `0.8` / `0.2`:

| `overall` | Policy | Fetch cost |
|---|---|---|
| `high` | Accept, stop researching this candidate | 0 |
| `medium` | Accept **if** corroborated by ≥2 provider classes; else one corroboration fetch | ≤1 |
| `low` | One targeted disambiguation fetch; if still `low`, drop and record `inconclusive` | ≤1 |
| `zero` | Drop immediately, record negative finding, never re-probe this job | 0 |

`zero` deserves emphasis: `_series_alignment_confidence` returns `"zero"` on a confirmed author mismatch (`confidence_engine.py:179`), which forces `overall` to `zero`. That aligns exactly with the hard invariant "never persist a candidate that fails `_author_matches`" (map §5.1) — the confidence engine and the acceptance gate agree by construction. Preserve that alignment; do not let the agent argue with a `zero`.

Series-level escalation (replacing `_should_trigger_author_fallback`) becomes: escalate to an author-scoped sweep when the count of `numbering_gaps` with no recent `absent` probe exceeds what targeted probing can close within remaining budget. That is a budget-aware decision, not a magic ratio, which is the actual argument for the migration.

### 2.4 Fetch budget — how many, and how chosen

Against the 39-search baseline, a five-phase budget for a cold 18-book reconstruction:

| Phase | Purpose | Searches | LLM turns |
|---|---|---|---|
| A. Catalog sweep | All 3 catalogs in parallel, existing `_fetch_all_providers_parallel`; harvest Hardcover `series_total_hint` | 3 (no web, no LLM) | 0 |
| B. Plan | Fuse → delta → confidence → decide the gap list | 0 | 1 |
| C. Gap probes | ≤1 web search per unprobed gap, `min(gaps, 12)`, issued in one batch | ≤12 | 1–2 |
| D. Disambiguation | `medium`/`low` candidates and `duplicate_number` conflicts | ≤4 | 1–2 |
| E. Frontier probe | One "is there anything after N" query | 1 | 1 |
| **Total** | | **≤20** | **≤7** |

Selection rules, in priority order: (1) interior gaps the skeleton knows about and hasn't recently probed; (2) numbers between `max(confirmed)` and `expected_total` when Hardcover supplied a total; (3) exactly one frontier probe beyond `max(confirmed)`; (4) never a blind numbered sweep — the current pipeline's 10 lookahead queries are the single largest recoverable cost, and they exist only because the pipeline has no memory of what it already knows.

Hard ceilings are non-negotiable (map §5.2): max searches, max LLM turns, max wall-clock, all per job, all enforced by the caller rather than trusted to the agent's judgment. The agent's stop decision is advisory; the budget is authoritative. The existing `SERIES_CHECK_HARD_TIMEOUT_SECONDS = 300` shared-across-rounds budget is the right model — a shared ceiling, not a per-call one, because the failure mode is many cheap-seeming calls, not one slow one.

### 2.5 Inferring numbers, titles, dates, membership

The governing rule, inherited from `delta_engine`'s own no-inference stance and from `belongs_to_series`'s `continues_numbering` requiring textual corroboration: **the agent may never assert a book number that no source text corroborates.** Position in a search-results list, arithmetic on neighbouring numbers, and "it's probably next" are all forbidden as sole evidence.

- **Book number** — structured provider field first (Hardcover `series_number_hint` is the only one that has it); then explicit text ("Book 14 of", "#14", "the fourteenth"); then nothing. A gap stays open rather than being filled by inference.
- **Title** — prefer the catalog title over a search snippet's rendering. Keep the existing display-suffix behaviour (`agents/series_agent.py:832-834`) rather than having the agent compose titles.
- **Release date** — the second-look date refinement (`_refine_undated_web_search_results_batch`) mostly disappears if the search provider returns page content rather than a snippet (§3). Dates stay unparsed strings until `parse_flexible_date` at the existing call site; do not move date parsing into the agent.
- **Series membership** — the agent proposes; `belongs_to_series` decides. The agent should read the gate's outcome mid-loop and treat a rejection as evidence to research differently, which is a capability the current pipeline structurally cannot have (its gate runs once, at the end, after all fetching is done). This is one of the clearest wins available and worth building explicitly rather than leaving implicit.

### 2.6 Duplicates and cross-series contamination

Unchanged mechanism, new trigger. Keep `_fuse_and_score_candidates`, `_filter_cross_series_contamination` / `_is_cross_series_contamination` / `_series_names_compatible`, and the three DB-level identity-collapse passes exactly as they are. The agent's role is to *notice* ambiguity and *ask a better question*, not to resolve identity — identity resolution is exact-match work where non-determinism is a regression.

Retrieval-layer contamination control is the genuine improvement available: domain allowlisting on the search provider (§3) prevents contaminated results from entering the funnel at all, which is strictly better than filtering them after they arrive and cheaper than an LLM reconciliation pass over a polluted set.

### 2.7 Ambiguous or missing metadata

Every gap resolves to exactly one of three states, and the third must be first-class:

- **`confirmed`** — corroborated, meets the acceptance band, written to skeleton, proposed for persistence.
- **`absent`** — probed and credibly not found. Recorded in `probes` with kind and timestamp so it isn't re-probed next run, with a TTL so an unreleased future volume gets re-checked later.
- **`unresolved`** — probed, evidence conflicting or insufficient. The gap stays open, nothing is persisted, and the reason is recorded. **The agent must never close an unresolved gap by fabricating a plausible entry.** For a discovery engine whose output feeds `Series.missing_books` and user-facing notifications, a confident wrong answer is worse than an open gap.

### 2.8 Return schema

Superset of the existing contract, never a redesign (map §5.3). The `added_books` entries must match `_build_added_book_entry`'s shape field-for-field, since `series_check_engine.py`'s persistence reads those exact keys (`candidate.get("title")`, `"author"`, `"canonical_metadata"`, `"asin_or_id"`, `"status_hint"`, `"is_missing"`, `"publication_date"`, `"expected_date"`, `"source_url"`).

```jsonc
{
  // ---- existing contract, unchanged, consumed by series_check_engine.py,
  // discovery_logging.py, and the frontend's job polling ----
  "series_id": 42,
  "series_name": "The Completionist Chronicles",
  "highest_owned_book_number": 7.0,
  "added_books": [
    {
      "title": "Unmapped: (The Completionist Chronicles Book 14)",
      "author": "Dakota Krout",
      "series_name": "The Completionist Chronicles",
      "book_number": 14.0,
      "source_url": "https://...",
      "provider": "hardcover",
      "publication_date": "2026-03-02",   // null when upcoming
      "expected_date": null,              // set instead when upcoming
      "status_hint": "available",         // "available" | "upcoming"
      "asin_or_id": "9781234567890",
      "is_missing": true,
      "status": "available",
      "canonical_metadata": {
        "title_normalized": "Unmapped: (The Completionist Chronicles Book 14)",
        "series_name_normalized": "The Completionist Chronicles",
        "book_number_normalized": 14.0,
        "publish_date_normalized": "2026-03-02",
        "upcoming_date_normalized": null,
        "availability": "available",
        "edition_type": "unknown",
        "title_selector": null
      }
    }
  ],
  "found_books": [ /* same list */ ],
  "missing_books": [ /* canonical dicts, pre-_build_added_book_entry */ ],
  "available_missing": [ /* ... */ ],
  "upcoming_books": [ /* ... */ ],
  "added_count": 1,
  "found": true,
  "status": "complete",
  "no_new_books": false,
  "reason": null,
  "provider_failures": [ { "provider": "tavily", "error": "429 after 3 retries" } ],
  "all_providers_failed": false,
  "telemetry": { /* DiscoveryTelemetry.summary() */ },
  "cache": { /* DiscoveryCache.summary() */ },
  "discovery_engine": "agent_v3",
  "agent_pipeline": true,

  // ---- agent-specific additions; no existing consumer reads these ----
  "agent": {
    "stop_reason": "confident_complete",
      // confident_complete | budget_exhausted | no_progress
      // | provider_failure | gate_rejected_all
    "turns": 6,
    "budget": {
      "searches_used": 17, "searches_max": 24,
      "llm_turns_used": 6, "llm_turns_max": 10,
      "wall_seconds_used": 41.2, "wall_seconds_max": 120
    },
    "expected_total": { "value": 18, "source": "hardcover_series_total_hint" },
    "open_gaps": [15.0, 16.0],
    "skeleton_updates": [
      { "book_number": 14.0, "op": "add", "status": "confirmed",
        "confidence": "medium", "source_class": "discovered" }
    ],
    "probes": [
      { "book_number": 15.0, "probe_kind": "targeted", "outcome": "absent",
        "queries_used": 1, "probed_at": "2026-08-21T23:04:11Z", "ttl_days": 30 }
    ],
    "candidate_confidence": [
      { "title": "Unmapped", "book_number": 14.0,
        "provider_confidence": "high", "title_confidence": "unverified",
        "number_confidence": "medium", "series_alignment_confidence": "high",
        "overall": "medium", "gate_outcome": "accepted" }
    ],
    "tool_calls": [
      { "seq": 1, "tool": "search_catalog", "args_digest": "…",
        "duration_ms": 812, "results": 23, "cache": "miss" }
    ]
  }
}
```

Two notes. `skeleton_updates` and `probes` are **returned, not written** by the agent — see §4.2. And `gate_outcome` per candidate is what makes the agent auditable: it records what the agent proposed *and* what the deterministic gate did with it, which is the core signal for shadow-mode evaluation.

---

## 3. Provider Strategy

### 3.1 Rule out Bing immediately

**The Bing Web Search API v7 was retired on 11 August 2025** and its endpoints return HTTP 410. Microsoft's replacement, *Grounding with Bing Search*, is a tool inside the Azure AI Foundry Agent Service rather than a SERP API — roughly **$14 per 1,000 transactions**, requiring full Azure AI Foundry provisioning, and returning model-grounded output rather than raw results you can feed to your own structuring step. It is both the most expensive option evaluated and a platform commitment, on a platform with its own migration churn. Remove it from consideration.

### 3.2 Prerequisite: build the provider interface that doesn't exist

Map §2 notes there is no `Provider` base class or registry — each fetcher is a bare function unified only by convention, and `_fetch_all_providers_parallel` returns a fixed four-key dict. Swapping Brave today means editing that dict's construction. Before choosing a vendor, introduce a minimal `WebSearchProvider` protocol (`search(query, *, count, include_domains, exclude_domains) -> list[RawResult]`) with Brave, the new provider, and a recorded-fixture provider behind it. The fixture implementation is what makes the eval suite (§5, Phase 0) possible at all, so this pays for itself immediately and is worth doing even if the vendor choice changes later.

### 3.3 Comparison (public list rates, August 2026 — verify before committing)

| | Tavily | Exa | SerpAPI | Brave (today) | Bing |
|---|---|---|---|---|---|
| Output | Cleaned, ranked LLM-ready content | Neural results + text/highlights for first 10 **included** | Real Google SERP JSON | title/description/url only | — |
| List price | $8/1k PAYG; $5/1k at Growth | $7/1k incl. contents | $25/1k Starter, $15/1k Developer | low | $14/1k |
| Free tier | 1,000 credits/mo, recurring | ~1,000/mo | 250/mo | — | — |
| Rate limits | 100 rpm; dynamic on low tiers | generous | **hourly caps → 429** (200/hr Starter) | ad-hoc | — |
| Domain allowlist | `include_domains` | `includeDomains` | via `site:` | limited | — |
| Cost @ 39 searches | ~$0.31 | ~$0.27 | ~$0.59 | — | ~$0.55 |
| Cost @ 20 (target) | ~$0.16 | ~$0.14 | ~$0.30 | — | ~$0.28 |
| Stability risk | vendor is young, but AI-agent-native | index shifts as it re-crawls | highest fidelity, strictest quotas | **the problem being solved** | retired |

### 3.4 Recommendation

**Primary: Tavily**, replacing Brave behind the new interface. Three reasons specific to this workload rather than generic benchmarks:

1. **`include_domains` attacks cross-series contamination at the retrieval layer.** Allowlisting fantasticfiction, the author's publisher, Goodreads, Wikipedia and the author's own site prevents contaminated hits from entering the funnel, rather than filtering them out after they arrive. Given the pipeline currently spends an LLM reconciliation pass and a dedicated `_filter_cross_series_contamination` step on that exact problem, this is the highest-leverage single change available.
2. **Cleaned content instead of a snippet directly improves `_structure_web_results_with_llm`'s accuracy** and largely removes the need for `_refine_undated_web_search_results_batch`, the Brave-only second-look date pass. That is a real cost reduction, not just a quality one.
3. **1,000 free credits per month, recurring**, is enough to run the entire Phase 1 shadow evaluation at zero marginal cost.

**Escalation: Exa**, as a second, *semantically different* tool the agent calls only when Tavily returns nothing for a specific gap. Neural retrieval surfaces fan wikis and publisher pages that keyword matching misses — which is precisely the under-indexed-indie-series case the deterministic pipeline fails on — and included page contents resolve dates without a second call. Two providers with genuinely different retrieval models is worth more here than one provider called twice.

**Eval-only: SerpAPI.** Too expensive per call and too quota-constrained for a loop that fires 20–40 searches per series, but it returns real Google results, which makes it the right tool for building the Phase 0 fixture set once. Build the eval set against SerpAPI; run production against Tavily.

**Keep Brave** behind the new interface as a degraded fallback. The key already exists and the code path is proven; there is no reason to delete it in the same change that introduces its replacement.

### 3.5 How the agent should call it

**Query patterns**, replacing the current `"<series>" <author> book <N>`:
- Gap probe: `"<series>" "<author>" "book <N>"` with the domain allowlist; on empty, retry once as `<series> <N> <author> release date` without the allowlist.
- Frontier probe: `"<series>" "<author>" next book` — one per run, never a numbered sweep.
- Disambiguation: `"<exact candidate title>" "<author>" <series>` to confirm membership, never to discover.
- Author escalation: `"<author>" "<series>" complete series in order`, allowlist on.

**Batching.** Preserve the existing two-tier structure: resolve cache hits synchronously first, then fire only genuine misses through a bounded pool (`WEB_SEARCH_BRAVE_MAX_PARALLEL_WORKERS = 5` is a reasonable starting bound for Tavily's 100 rpm) — and **reassemble in original query order**, per invariant #2. Do not let the agent issue searches one at a time across turns; a batch of 12 gap probes in one turn is both cheaper and faster than 12 sequential turns, and it preserves the existing single-LLM-structuring-call pattern.

**Retries.** Exponential backoff with jitter on 429/5xx, capped at 2 retries per query, counting against the search budget so a retry storm cannot exceed the ceiling. A per-provider circuit breaker after N consecutive failures records into `provider_failures` and falls through to the next provider. Critically: exhausting a provider is **not** `all_providers_failed` unless zero usable data came from any source, per invariant #6.

**Caching.** Both layers keep their per-job, in-memory-only lifetime (map §5.2). Worth taking the opportunity to close the known Layer-A gap: because the agent constructs queries from structured intent (`series`, `author`, `number`, `probe_kind`) rather than pre-formatted strings, key Layer A on that **semantic tuple** rather than literal query text — the fix map §4.4 identifies as falling out naturally from an agentic design. Layer B keeps `bypass_cached_rejection` semantics, now generalized: a verdict cached from a `sweep` probe never suppresses a `targeted` probe.

---

## 4. Integration with the Existing Job Engine

### 4.1 The insertion point

Exactly as map §6.3 specifies — `services/series_check_engine.py:312-314`:

```python
engine = select_discovery_engine(db_series)   # flag / per-series setting
future = executor.submit(
    engine.run_series_check, db, series_id, update_progress, False, telemetry, discovery_cache
)
```

Identical signature, identical return contract, identical `ThreadPoolExecutor`/timeout wrapping. Nothing else in the round loop changes on day one: the precheck short-circuit, `remaining_budget` computation, the stop condition, and all persistence stay untouched. The engine selector is the entire integration change.

Two properties this preserves that are easy to lose: `db_series` is re-read from the DB every round (line 356) and `highest_owned_book_number` is recomputed fresh inside `run_series_check` — so round N+1 sees round N's inserts. An agentic engine that caches series state across rounds in memory breaks this. The agent's memory lives in the skeleton and is re-read per round, not carried in a Python object across round boundaries.

### 4.2 Persistence semantics

The rule stands unchanged: **discovery is pure; the job engine owns DB writes.** The agentic engine may touch only `Series.has_new_books` and `Series.last_checked`, exactly as `series_agent.run_series_check` does today (`agents/series_agent.py:957-960`).

The skeleton needs an explicit ruling, since it is a new write target. `SeriesSkeleton` is not a `Book` row, so writing it does not literally violate the invariant — but writing it *from inside discovery* would break the single-writer discipline that makes the round loop correct. Recommendation: **the agent returns `skeleton_updates` and `probes` in its result; `series_check_engine.py` applies them after persistence**, in the same place it already calls `library_sync.update_from_series` and `intelligence.recalculate_intelligence`. This gives one writer, one commit point, and one ordering — the skeleton is updated only after the candidates it describes have actually been persisted, so it can never claim a book the DB doesn't have.

That ordering also fixes §0.1 cleanly: the post-persistence skeleton write is a merge, and the boot-time backfill becomes a merge too, so both paths converge on the same rule.

### 4.3 Telemetry

`DiscoveryTelemetry` needs no structural change — pass names are open strings, not an enum (map §1.10). Register agent-semantic passes: `agent_plan`, `agent_catalog_sweep`, `agent_gap_probe`, `agent_disambiguate`, `agent_frontier`, `agent_finalize`. Keep `precheck` untouched so the idle path stays directly comparable.

Add three counters, since per-pass duration alone stops being sufficient once the number of passes is dynamic:
- `record_tool_call(tool, duration, cache_hit)` — the agent's tool-call ledger, the analogue of `record_brave_call`
- `record_agent_turn(tokens_in, tokens_out, stop_reason)` — reasoning tokens are the new cost line and are invisible to `record_llm_call`'s current framing
- `record_gate_outcome(proposed, accepted, rejected_by)` — how many agent proposals survived `belongs_to_series`

The north-star metric that makes the two engines comparable is **cost and wall-time per newly-accepted book**, not per run. A run that makes 30 searches and finds 6 books beats one that makes 12 and finds 1. Because both engines share the same `DiscoveryTelemetry` instance and the same `CHECK NOW DEBUG SUMMARY` output path (`discovery_logging.log_discovery_summary`), this comparison is available from the first shadow run without new infrastructure — and the two reference points from map §5.2 (39/10/73.8s cold, 0/0/1.19s idle) are the acceptance bar.

---

## 5. Migration Plan

### Phase 0 — Prerequisites (no agent code)

1. **Freeze the eval set.** Convert the named regressions — Jonathan Hunt, Safehold, Starship's Mage, universe tie-in downgrade, compilation-of-owned-titles downgrade — into fixtures with expected accept/reject. Add the recorded-fixture provider (§3.2). Until this exists, no claim about the agent is falsifiable, and map §5.3 already identifies it as the blocking gap.
2. **Fix §0.1** — skeleton rebuild becomes a merge keyed on `book_number`.
3. **Fix §0.2** — add the `unverified` title grade and its `_overall_confidence` rule.
4. **Introduce the `WebSearchProvider` protocol** with Brave, Tavily, and fixture implementations.
5. **Add the three telemetry counters** (§4.3).

Steps 2 and 3 are independently shippable improvements to the shadow scaffold and can go in before any decision about the agent is final.

### Phase 1 — Shadow (weeks 1–3)

Agent runs **after** the deterministic pipeline within the same job, on the same `telemetry` and `discovery_cache`, with its result logged and diffed against the deterministic accept set. Zero behavioural change; the deterministic result is what gets persisted.

Gate to Phase 2: agreement rate on already-accepted books ≥ 95%; agent finds ≥ 1 book the pipeline missed on the known-failing series class; cost per accepted book ≤ deterministic baseline; zero eval-set regressions.

### Phase 2 — Canary (weeks 4–8)

Flip the engine selector for a deliberately chosen subset — **start with the hardest class**: series where the deterministic pipeline has left interior gaps across ≥ 2 consecutive checks. This is where the agent has the most upside and the least regression risk, because deterministic discovery has already demonstrably failed there. Do not start with easy series; a canary on cases the pipeline already handles measures nothing and risks regression for no information.

Deterministic fallback fires on agent exception, budget exhaustion with zero findings, or `all_providers_failed`. Both engines write through the identical gate and persistence path, so a fallback is a re-run, not a different code path.

Gate to Phase 3: no eval regressions over 4 weeks; cost per accepted book at or below baseline; fallback rate < 5%.

### Phase 3 — Primary (weeks 9+)

Agent primary for all series; deterministic retained as the fallback path. Now retire the policy modules — `_should_trigger_author_fallback`, `_needs_llm_reconciliation`, `_reconcile_candidates_with_llm`, the blind lookahead, and `_reconstruct_series_skeleton`'s search half (its gap arithmetic stays; it's pure and correct).

### Phase 4 — Loop ownership (last)

Only after Phase 3 is stable: replace `for _round_num in range(1, SERIES_CHECK_MAX_ROUNDS + 1)` with an agent-driven stop decision, still bounded by the shared 300-second budget and still calling the same persistence body after each iteration. Deliberately last, per map §6.3 point 5 — prove the research step before touching the orchestration around it.

Note the loop's existing single stop signal ("zero new books persisted") was arrived at empirically after a second signal was falsified live (map §1.6). An agent-driven stop condition must beat that signal on the eval set before replacing it, not merely seem more sophisticated.

### Never retired

`_fuse_and_score_candidates`, `_filter_and_merge`, `_finalize_candidates`, `finalize_discovery_output`, `services/identity.py` and the three collapse passes, `belongs_to_series`, `_is_known_candidate`, `precheck_for_new_volumes`, the entire persistence body, `library_sync`, `intelligence`, the auto-discovery eligibility gate, and the job API surface. These are the funnel every candidate passes through regardless of what proposed it.

---

## 6. Summary

| Question | Recommendation |
|---|---|
| Deprecate the deterministic pipeline? | Policy layer yes, mechanism layer never |
| Durable memory | Graduate `SeriesSkeleton` to read-write with merge semantics + negative-probe memory |
| Escalation signal | `confidence_engine` grades, after adding an `unverified` title state |
| Fetches per series | ≤20 searches / ≤7 LLM turns cold, vs 39/10 baseline; 0/0 idle unchanged |
| Search provider | Tavily primary, Exa escalation, SerpAPI for eval only, Brave as fallback, Bing ruled out (retired) |
| Insertion point | `series_check_engine.py:312-314`, engine selector, nothing else changes on day one |
| Persistence | Unchanged — agent returns skeleton updates, job engine applies them after persistence |
| Biggest risk | Shipping before the eval set exists |
