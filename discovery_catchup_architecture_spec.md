# Discovery Catch-Up Architecture — Consolidated Spec

**Status:** §2.1 (lookahead), §2.2 (bounded loop), §2.5 (instrumentation), §7.1 (two-layer cache — literal-query-text-keyed, see gap noted in §7.1), §7.2 (catalog-only staleness-gated pre-check), and §7.3 (`idle_check` flag) are all implemented, tested, and live-validated against Jonathan Hunt (see §6 and §7.4).
**Purpose:** Let one "Check Now" click fully reconstruct a long, completed, or in-progress series (8–20+ volumes) from a single owned book, without uncontrolled Brave/LLM cost growth.
**Trigger case:** Jonathan Hunt Thriller Series — a long, under-indexed series that needed multiple Check Now runs to fully populate.

---

## 1. Current-State Behavior (as of this review, before any changes)

### 1.1 Job lifecycle

- `POST /{series_id}/check` (`routers/series.py`) schedules `run_series_check_job_full(series_id)` via FastAPI `BackgroundTasks`. A second click while `status == "running"` is a no-op (returns current progress); a completed job never blocks a fresh click from re-running.
- `run_series_check_job_full` (`services/series_check_engine.py`) opens one DB session for the whole job, submits **one** call to `series_agent.run_series_check` through a `ThreadPoolExecutor(max_workers=1)`, bounded by `SERIES_CHECK_HARD_TIMEOUT_SECONDS = 300`.
- On timeout: `executor.shutdown(wait=False, cancel_futures=True)`. This only cancels *pending* futures — an already-running discovery thread keeps executing to completion in the background; its result is simply discarded from the job's perspective. (Existing behavior, not introduced by this spec — but relevant to §3.3.)
- After discovery returns, **all** persistence (insert/update/dedupe against existing rows, three identity-collapse passes, series-intelligence rebuild, the durable discovery notification) happens once, synchronously, back in `run_series_check_job_full`. `series_agent.run_series_check` itself never writes to the DB — it's a pure discovery function.

### 1.2 Discovery pipeline (`agents/series_agent.py` → `discovery_engine.py`)

- `series_agent.run_series_check` computes `highest_owned_book_number` (max integer `book_number` among owned, non-`is_missing` books — **not** required to be contiguous) and calls `discovery_engine.discover_candidates_for_series` exactly once.
- **Targeted pass** (`discover_candidates_for_series` → `_fetch_all_providers_parallel`): Google Books + OpenLibrary + Hardcover + Brave web search, fetched concurrently.
  - Web-search query list = 1 targeted query (`"<series>" inauthor:"<author>"`-equivalent text) + `WEB_SEARCH_LOOKAHEAD_BOOKS` (currently **3**) lookahead queries of the form `"<series>" <author> book <N>"` for `N = highest_owned+1 .. highest_owned+WEB_SEARCH_LOOKAHEAD_BOOKS`.
  - All Brave calls for this pass are looped sequentially inside `_fetch_web_search`, then batched into **one** `_structure_web_results_with_llm` call.
  - Up to `WEB_SEARCH_DATE_REFINEMENT_MAX = 3` additional Brave+LLM call *pairs* (one each, not batched) for candidates `_structure_web_results_with_llm` couldn't date.
- **Author-fallback pass** (conditional, gated by `_should_trigger_author_fallback`): re-queries Google/OpenLibrary/Hardcover, scoped by series name (not a bare author sweep). Web search is **off by default** here (`enable_fallback_web_search=False`), so it normally adds zero Brave/LLM calls.
- **Conditional LLM reconciliation** (`_reconcile_candidates_with_llm`, gated by `_needs_llm_reconciliation`): one LLM call over the **entire fused candidate set together** (merges near-duplicates, flags cross-series contamination/bundles) — not a per-item verdict.
- **Missing-volume skeleton pass** (`discovery_engine._reconstruct_series_skeleton`, called from `series_agent.py`, separate from the targeted pass): targets *interior* gaps — numbers between 1 and the highest known number with neither an owned book nor a discovered candidate. Up to `MAX_MISSING_VOLUME_LOOKAHEAD_QUERIES = 6` Brave queries, batched into one LLM call, plus its own up-to-3 refinement pairs. **Recomputed from scratch on every call** — nothing carries over between calls today.
- `backfill_missing_publication_dates`: Hardcover-only (no Brave/LLM), up to `MAX_PUBLICATION_DATE_BACKFILL_LOOKUPS = 8`.

### 1.3 Cost profile today

Worst case for a single Check Now on a long/gappy series (author-fallback + reconciliation + missing-volume pass all triggered): **~16 Brave calls, ~9 LLM calls, in one click.**

**No caching layer exists anywhere in `discovery_engine.py`.** Every fetch function (`_fetch_google_books`, `_fetch_openlibrary`, `_fetch_hardcover`, `_fetch_brave_web_search`, and both Anthropic call sites) hits its live API fresh on every invocation — no memoization by query, URL, or series.

**No instrumentation exists today** to count Brave/LLM calls per job — `services/discovery_logging.py`'s `log_discovery_summary` tracks provider ledger/candidate counts but not call volume.

---

## 2. Agreed Design Changes

### 2.1 Lookahead width

- Increase `WEB_SEARCH_LOOKAHEAD_BOOKS` from 3 to ~10 (exact value still open — depends on Brave plan/cost tolerance, which hasn't been specified yet).
- Confirmed low-risk in isolation: only adds Brave calls, still batched into the **same single** LLM structuring call (`_fetch_web_search` loops queries, then does one `_structure_web_results_with_llm` call over the combined results) — no LLM call-count increase from this change alone, just a larger prompt (more input tokens on that one call).

### 2.2 Bounded multi-round loop

Loop lives in `run_series_check_job_full`, wrapping the existing discover-then-persist logic (does **not** require restructuring `series_agent.run_series_check` into something that persists mid-run):

```
repeat up to MAX_ROUNDS times:
  a. call series_agent.run_series_check   (one discovery round)
  b. persist new books                    (existing insert/update/dedupe logic, run per round)
  c. recompute highest_owned_book_number  (fresh DB read)
  d. check stop conditions -> break or continue
```

- `MAX_ROUNDS`: 3–4 (exact value open).
- **Stop condition — IMPLEMENTED, REVISED FROM ORIGINAL PROPOSAL**: the round persisted zero new books. Bounded regardless by `SERIES_CHECK_MAX_ROUNDS = 3`.
  - **The originally-proposed second stop condition was tried and empirically falsified**: "stop if the round's discovered candidates did not reach the top of that round's own lookahead window." Live test against Jonathan Hunt (reset to 1 owned book, `WEB_SEARCH_LOOKAHEAD_BOOKS = 10`): round 1 searched the book 2–11 window but Brave only surfaced usable results up through book 9 (search-result variance, not evidence the series ends there) — this stop condition fired and the loop halted at 9/18 books, in a single click. That is precisely the bug this whole effort exists to fix. Books 10–18 were confirmed real (found in a later round once the condition was dropped). **Decision: dropped entirely.** The cost of keeping only the zero-new-books condition is at most one extra "found nothing" round, already bounded by `SERIES_CHECK_MAX_ROUNDS` — a worthwhile tradeoff for actually guaranteeing one-click reconstruction.
- **Notification**: `create_series_discovery_notification` fires **once**, after the loop finishes, with the delta **summed across all rounds** — never once per round. (A per-round fire would spam the user with multiple "new books found" notifications from a single click.) **Implemented as specified.**

### 2.3 Timeout semantics

- The shared timeout (keep `SERIES_CHECK_HARD_TIMEOUT_SECONDS`, or a renamed/re-valued constant) bounds **scheduling of new rounds for the whole job**, not each round individually — prevents a naive worst case of `MAX_ROUNDS × 300s`.
- **Explicit, accepted limitation**: an already-in-flight round cannot be forcibly cancelled (`ThreadPoolExecutor` can't preempt a running thread — same constraint as §1.1's existing single-call timeout behavior). The last round may overrun the shared budget by up to one round's cost. This is documented, not treated as a bug.
- **Cooperative cancellation** (checking a shared abort flag mid-fetch, inside each provider-fetch loop) is **explicitly out of scope for the initial implementation.** Revisit only after §2.5's instrumentation shows measured overrun is large enough (multi-minute, not single-digit seconds) to justify the complexity and correctness risk (partial-state aborts mid-fusion/mid-persist).

### 2.4 Two-layer caching

**Scope for both layers: per-job, in-memory only.** Created at the start of a Check Now job, discarded when the job ends. Never persisted to disk, never shared across jobs, series, or users — the entire point of Check Now is to catch what's changed since last time, so nothing here should outlive one job.

#### Layer A — Provider-fetch cache

- Stores raw provider results (Brave, Google Books, OpenLibrary, Hardcover).
- **Key**: a normalized semantic tuple `(provider, series_name_normalized, primary_author_name, book_number)` — **not** the literal formatted query string.
- Built by **one shared `_cache_key(...)` helper** that every call site must go through — no call site rolls its own normalization inline. This is what actually closes the co-author mismatch: today the exterior-lookahead pass (`_fetch_all_providers_parallel`) builds its query with `primary_author_name(author)`, while the interior-gap pass (`_reconstruct_series_skeleton`, called from `series_agent.py`) passes the raw, unsplit `series_author` string. If both derive the cache key through the same helper, they normalize identically regardless of what each uses for its own human-readable query text.
- `series_name_normalized` should go through the same shared normalizer (lowercase/strip/collapse whitespace) for the same reason — not a live bug today (both call sites source it from the same `Series` row within one job), but cheap insurance against future drift.
- Shared across all sub-passes (targeted, missing-volume, refinement) and all loop rounds.

#### Layer B — LLM-verdict cache

- Stores structured verdicts from `_structure_web_results_with_llm`.
- **Key**: `(scope_type, series_name_normalized, url)`.
  - `scope_type` is an **open string/enum**. Exactly two values exist in the codebase today, confirmed by tracing every caller of `_structure_web_results_with_llm`: `"series"` (`discover_candidates_for_series`, which also covers the Add Book `discover_series_by_name` flow) and `"author"` (`discover_candidates_for_author`, "More by this author"). Kept open-ended so a future third scope is additive, not a breaking change.
  - Including `series_name_normalized` (not bare URL) is required so a series-scoped verdict can never leak into the author-wide path — that prompt asks a fundamentally different question about the same URL, and a bare-URL key would make that leak structurally possible.
- **Only objectively extractive fields are cached**: `title`, `series_name`, `book_number`, `author_names`, `published_date`, `is_upcoming`, `isbn13`. Any future probabilistic/confidence-style field must be treated as always-fresh, never cached — Anthropic calls aren't deterministic, so a stale cached confidence value could drift from what a fresh call would produce.
- **Must cache both directions**:
  - "Accepted" verdicts — the structured items the LLM actually returned.
  - An explicit **"rejected" sentinel** for a URL that *was* sent to the LLM but excluded from its output (a retailer category page, fan wiki summary, etc. — exactly what the prompt's own skip instructions target). Without this, a junk URL has no cache entry and looks indistinguishable from "never checked," so it would be re-sent to the LLM every single round, forever — quietly eroding most of the intended LLM-call reduction, since noise like this is a large fraction of Brave's raw output for under-indexed series.
- **Explicitly excluded from caching**: `_reconcile_candidates_with_llm`. It verdicts the entire fused candidate set together (merges near-duplicates, flags cross-series contamination and bundles) rather than one URL in isolation, and that set's composition changes every round as new candidates get persisted/excluded — there is no stable per-item unit to cache. It must always run in full whenever `_needs_llm_reconciliation` triggers it.

**Structural change required in `_fetch_web_search` to support Layer B:**

1. Split that round's `raw_results` into `cached_urls` (already have a verdict, positive or negative) vs. `uncached_urls`.
2. Call `_structure_web_results_with_llm` **only** on `uncached_urls` — skip the call entirely if it's empty.
3. Immediately after the call returns, resolve each returned `result_index` against `uncached_urls` — the exact subset actually sent — **not** against the original, longer `raw_results` list. From that point forward, everything downstream must use URL-keyed lookup (`dict[url] -> verdict`), never positional indices again.
   - *Why this matters*: naively reapplying `result_index` against the original full list would silently attribute one URL's structured data (title, date, source URL) to a *different* URL once any items had been filtered out before the call — no exception raised, just quietly wrong data feeding straight into the persistence pipeline.
4. Reassemble the final result list by iterating the **original** `raw_results` in their original order, looking up each URL's verdict (cached or freshly-resolved) as you go — do not reorder as "fresh items then cached items" or vice versa.
   - *Why this matters*: `_first_present_field` and similar downstream backfill logic pick "first non-empty value across members," which is order-dependent. Preserving original order keeps a cached run and an uncached run of the same underlying data behaviorally identical.

### 2.5 Instrumentation

- Add counters for Brave calls, LLM calls, and a per-pass breakdown (targeted / missing-volume / refinement / author-fallback / reconciliation).
- Surface these counters in the existing `CHECK NOW DEBUG SUMMARY` block (`services/discovery_logging.py`).
- **Must land before or alongside caching, not after.** Every reduction estimate discussed while designing this spec (e.g. "40–70% fewer Brave calls," "40–60% fewer LLM calls") is a reasoned estimate from reading the query-construction and short-circuit logic — not a measured fact. Real counters on a live run against a long series (Jonathan Hunt) settle this in one Check Now click, and are also the prerequisite for deciding whether §2.3's deferred cooperative-cancellation is ever worth building.

---

## 3. Non-Goals / Explicitly Deferred

- Cooperative cancellation of in-flight rounds (§2.3) — revisit only if instrumentation shows large measured overrun.
- Any cache that outlives a single job, or is shared across jobs/series/users.
- Any change to `_reconcile_candidates_with_llm`'s all-or-nothing, whole-set semantics — it stays non-cacheable by design.

---

## 4. Open Parameters (need a decision before implementation, not blocking further design review)

- Exact `WEB_SEARCH_LOOKAHEAD_BOOKS` value (10 proposed; real ceiling depends on Brave API plan tier/cost tolerance, which hasn't been specified).
- Exact `MAX_ROUNDS` (3 vs. 4).
- Exact shared timeout-budget value (keep 300s now that it covers a whole loop instead of one call, or raise it).

---

## 5. Recommended Implementation Sequencing

1. ~~**Instrumentation (§2.5) first**~~ — **done.** `DiscoveryTelemetry` (per-job, in-memory, shared across all rounds) tracks Brave calls, LLM calls, tokens, and per-pass duration; surfaced in `CHECK NOW DEBUG SUMMARY`.
2. ~~**Lookahead bump (§2.1)**~~ — **done.** `WEB_SEARCH_LOOKAHEAD_BOOKS` 3 → 10.
3. ~~**Loop (§2.2)**~~ — **done**, with the stop-condition revision documented in §2.2 above. **+ both cache layers (§2.4) together, in the same change** — **cache layers not yet implemented.** The original rationale for bundling them (don't ship the loop's redundant repeat calls without the caching that offsets them) still holds; §6 below is the real cost baseline for deciding how urgent that is.
4. **Re-measure** with instrumentation on real long/gappy series after rollout — **done, see §6.**
5. **Revisit cooperative-cancellation (§2.3) only if the measured data justifies it.** Not yet revisited — §6's worst-case wall time (~74s across 3 rounds) doesn't obviously justify the complexity yet, but this hasn't been discussed since the loop shipped.

---

## 6. Live Measurement Results (post-loop, pre-cache)

Test series: Jonathan Hunt (18 real volumes), reset to a single owned book (book 1) before each run, one `run_series_check_job_full` call (the loop is now entirely internal — no manual re-triggering).

**Result: full 18-book reconstruction in one click, 3 rounds, 73.8s wall time, one notification fired (`discovery_delta_count=8`, i.e. new-and-available count; total new inserts across rounds = 17).**

| Round | New books found | Brave calls | LLM calls | Duration | Notes |
|---|---|---|---|---|---|
| 1 | 8 (books 2–9) | 11 | 1 | 15.4s | targeted pass only |
| 2 | 9 (books 10–18) | 14 | 4 | ~26s | targeted + refinement (3 undated candidates) |
| 3 | 0 | 14 | 5 | ~32s | targeted + refinement + reconciliation (series briefly looked incomplete, correctly found nothing new, stopped) |
| **Total** | **17** | **39** | **10** | **73.8s** | tokens: 29,315 in / 7,669 out |

Provider failures observed (non-fatal, other providers covered): OpenLibrary connection resets/timeouts, Google Books 503, Hardcover 429 rate-limit — all pre-existing flakiness, unrelated to this change.

**Where caching would help most, based on this data:** round 3's targeted pass alone cost 11 Brave + 1–3 LLM calls to confirm "nothing new" — the generic `"<series>" inauthor:"<author>"` query is identical text on every round and currently re-fetched from scratch each time. The per-round lookahead-window queries overlap less than expected (windows were 2–11, 10–19, 19–28 — only 1–2 numbers of overlap each time, since `highest_owned_book_number` advances a lot per round now that reconstruction is faster), so Layer A's biggest win here is likely the repeated generic query and repeated missing-volume/author-fallback sweeps in round 3, not the numbered lookahead queries themselves.

---

## 7. Cost Optimizations Round (post-loop, Cursor + Copilot, 4 iterations) — AGREED

Triggered by §6's data point that an already-complete series still burns a full ~11-Brave-call round on every single Check Now click just to confirm "nothing new."

### 7.1 CachePolicy scope — final

No threshold-gating (an earlier proposal to gate caching behind `Brave_calls_per_job > 20`-style thresholds read from a *previous* run's telemetry was rejected: `SERIES_CHECK_MAX_ROUNDS`/`WEB_SEARCH_LOOKAHEAD_BOOKS` are global constants that can't discriminate per-job, and gating on prior-run telemetry misses the first heavy run on a newly added series entirely — exactly the case that matters). **Decision: the two-layer cache (§2.4) is always active per job, no policy layer.** `_reconcile_candidates_with_llm` remains explicitly excluded** — reaffirmed, not revisited: whole-fused-candidate-set verdict, composition changes every round, no stable per-item cache key, conditionally invoked only when completeness drops below threshold.

**Implementation note / deviation from §2.4:** Layer A (provider-fetch cache, `services/discovery_cache.py`) is keyed by literal, whitespace-normalized query text per `(provider, query)`, not the originally-specified semantic tuple `(provider, series_name_normalized, primary_author_name, book_number)`. This is simpler and was sufficient to capture the dominant cost measured live in §6 — Google Books/OpenLibrary/Hardcover's targeted-pass query text is round-invariant (doesn't depend on `highest_owned_book_number` at all), so it's byte-identical across every round of one job, and per-book-number Brave lookahead queries are also byte-identical across rounds when their windows overlap, since both are built by the exact same code path on every round. **Known accepted gap:** it does *not* dedupe two differently-*formatted* queries for the same semantic (series, author, book_number) built by two different call sites — e.g. the exterior lookahead pass (`_fetch_all_providers_parallel`, uses `primary_author_name(author)`) vs. the interior missing-volume gap pass (`_reconstruct_series_skeleton`, uses the full `resolved_author` string) both querying book number 12 would produce two different literal query strings and thus two separate cache entries, not one. Layer B (LLM-verdict cache) is implemented exactly as specified: `(scope_type, series_name_normalized, url)`-keyed, negative sentinel for rejected URLs, `result_index` resolved against the uncached subset then immediately converted to URL-keyed lookups, original `raw_results` order preserved on reassembly.

### 7.2 Pre-check for already-caught-up series — final

Before starting the full multi-round loop, run a cheap catalog-only check (Google Books + OpenLibrary + Hardcover via the existing `enable_web_search=False` path already used by the author-fallback pass — zero Brave calls, zero LLM calls) when:

- `Series.last_checked` is **not null** and is within **3 days** (rationale: sits safely below the existing `AUTO_DISCOVERY_COOLDOWN = timedelta(days=7)` in `services/auto_discovery.py`, so it never fires on the primary 7-day recurring sweep cadence — it only catches genuinely redundant close-together re-checks, e.g. a manual click shortly after a manual or automatic check already ran).
- **`last_checked IS NULL` always skips the pre-check and goes straight to the full loop** — a series with no check history (e.g. the very first Check Now click right after adding a new series) has no prior baseline to compare a "nothing new" result against.
- Ceiling for comparison is `max(highest_owned_book_number, highest_known_upcoming_or_missing_book_number)` — not owned-only. Comparing against owned-only would make the pre-check "discover" an already-tracked-but-unowned upcoming book every single time and needlessly escalate to the full loop every check, defeating the purpose.
- If the catalog-only fetch shows nothing above that ceiling: short-circuit, skip the full loop entirely. If it shows something above the ceiling: fall back to the full multi-round loop as normal.

**Known accepted gap**: Google Books/OpenLibrary's raw results carry no structured series-position field (`series_number_hint` is Hardcover- and web-search-only in the current provider code) — so in practice this pre-check's "is there something new" signal is only as strong as Hardcover's own catalog coverage for a given series/author. For a Brave-only-discoverable indie/self-published series (Hardcover's own coverage of Jonathan Hunt was previously found to be sparse — see `backfill_missing_publication_dates`'s docstring), a false "nothing new" from the pre-check just delays discovery until the next check that isn't gated by the 3-day window (which, per the cooldown math above, is never later than the next 7-day auto-discovery sweep) — not a permanent miss.

### 7.3 `idle_check` telemetry flag — final

Set when the pre-check's staleness+ceiling condition is met and the catalog-only fetch found nothing above the ceiling (i.e. the job short-circuited via §7.2, not via running a real round of the full loop that happened to persist zero books). Surfaced in `CHECK NOW DEBUG SUMMARY` to distinguish "confirmed nothing new via cheap pre-check" from "ran the full loop and it happened to find nothing." No independent definition beyond §7.2's condition.

### 7.4 Live re-measurement (Jonathan Hunt, post-§7.1/§7.2 implementation)

**Idle-check case** (series fully reconstructed, `last_checked` = today, re-ran Check Now immediately): pre-check short-circuited in **1.19s wall time, 0 Brave calls, 0 LLM calls** — down from round 3's previously-measured ~11 Brave + 1–3 LLM calls / ~30s+ just to confirm "nothing new" (see §6). This is the case §7's whole optimization round was aimed at, and it's now essentially free.

**Full reconstruction case** (series reset back to book 1 only, `last_checked` cleared so the pre-check doesn't fire, full 3-round loop runs): 14 new books persisted (1 → 9, 12, 14–18; volumes 10/11/13 remain undiscovered — at the time, believed to be a pre-existing Hardcover-coverage gap for this indie series; §8 below found the real cause), **73.19s wall, 38 Brave calls, 12 LLM calls total across all 3 rounds**. Cache effect for this run: `provider_fetch_hits=13` (13 Google/OpenLibrary/Hardcover/Brave calls served from cache instead of hitting the network) and `llm_verdict_hits=75` (75 URLs served a cached verdict instead of being re-sent to the LLM; `llm_verdict_entries=64` total, 11 accepted / 53 rejected-sentinel). Total wall time is comparable to the pre-cache §6 measurement (~74s) rather than dramatically lower — expected, since a 3-round *full advancing reconstruction* run's critical path is dominated by each round's genuinely new, never-before-seen book-number queries (the watermark advances a lot each round), not by repeats; the cache's measured payoff here is in avoided call *volume* (a real, non-trivial reduction — 75 avoided LLM-verdict sends alone), which matters most for cost, while the idle-check case above is where wall-clock time collapses dramatically too.

---

## 8. Recall-gap root cause investigation (books 10/11/13 never discovered) — RESOLVED

Per Copilot's priority order (correctness before further cost/latency work — items #1–#4 below deferred until this closed), diagnosed why the interior missing-volume pass's own dedicated per-number query consistently failed to recover books 10/11/13 even though `_reconstruct_series_skeleton` correctly identified them as gaps and fired targeted lookahead queries for them every round.

**Diagnostic method:**
1. Called `_fetch_brave_web_search` + `_structure_web_results_with_llm` directly, in isolation, for the exact 3 targeted queries (`"Jonathan Hunt" Georgia Wagner book 10/11/13`) — Brave reliably returned clean, unambiguous hits (e.g. "The Desert Reckoning: A Jonathan Hunt Thriller Book 10") and the LLM correctly structured `book_number=10/11/13` every time. Ruled out query-construction error and provider-indexing gap.
2. Suspected LLM non-determinism (all three structured-JSON call sites — `_structure_web_results_with_llm`, `_reconcile_candidates_with_llm` — had no `temperature` set, defaulting to Anthropic's `temperature=1.0`). Set `temperature=0` on both (kept `generate_series_overview`, a prose-generation call, untouched). Re-ran the full live job: **still failed on the exact same 3 numbers** — ruled out plain stochastic variance as the (sole) cause, since a real random failure wouldn't reproducibly hit the identical set twice.
3. Inspected the Layer B (LLM-verdict) cache directly after a real job run: every canonical URL for books 10/11/13 (all Amazon `us.amazon.com`/`www.amazon.com` mirror variants) was cached with `verdict=None` (the negative/rejected sentinel) under `scope_type="series"`. Root cause confirmed: the **targeted pass's own large batch** (its lookahead window already covers these same book numbers, e.g. round 1 with `highest_owned=1` and `WEB_SEARCH_LOOKAHEAD_BOOKS=10` queries books 2–11) sent these URLs to the LLM bundled with ~20-40 other candidates in one call, and the LLM — likely due to batch-size/attention effects, independent of temperature — wrongly rejected them. That wrong rejection got cached by URL. The interior missing-volume pass's *entire purpose* is to re-query these exact numbers with a small, focused batch to get a better answer — but its cache lookup (keyed only by `(scope_type, series_name, url)`, with no awareness of which pass is asking) hit the stale negative sentinel from the noisier pass and never gave the LLM a second look at all.

**Fix:** in `_fetch_web_search`, when `pass_label == "missing_volume"`, a cached **rejection** (`verdict is None`) is treated as a cache miss (forcing a fresh LLM call), while a cached **acceptance** is still trusted as before. This preserves the cache's cost benefit (an already-confirmed candidate is never redundantly re-verified) while letting the one pass whose entire job is "give this a second, cleaner look" actually do that instead of rubber-stamping an earlier pass's possible mistake.

**Verification (Jonathan Hunt, reset to book 1 only, fresh job):** single Check Now round now recovers **all 18 books (1–18) in one round**, including 10/11/13 (`missing_volumes=[10, 11, 13], recovered=[10, 11, 13]`). Loop's own round-2 re-check correctly finds nothing further and exits. Full 391-test suite passes unchanged. Call-count impact is minor (this run: 40 Brave / 14 LLM vs. the earlier 38 Brave / 12 LLM run that failed to recover 10/11/13 — 2 extra Brave + 2 extra LLM calls, the cost of `missing_volume`'s 3 URLs no longer short-circuiting on a bad cached rejection).

**Kept as-is:** `temperature=0` on the two extraction/reconciliation calls — not the fix for this specific bug, but still a reasonable determinism improvement with no observed downside (391/391 tests pass), so left in place.

**Not implemented:** the "safety-net retry" (re-verify if `recovered_numbers` doesn't match `targeted_missing`) discussed as a fallback if `temperature=0` didn't close the gap — unnecessary now that the actual cache-poisoning root cause is fixed directly.

---

## 9. Refinement-pass cache integration (post-§8) — DONE

`_refine_undated_web_search_result` (the "`<title> release date`" second-look query for undated candidates) previously bypassed the per-job cache entirely — it called `_fetch_brave_web_search`/`_structure_web_results_with_llm` directly. Fixed by extracting the Layer B cache-splicing logic shared with `_fetch_web_search` into `_structure_with_verdict_cache(...)` (also carries the §8 `bypass_cached_rejection` behavior as a parameter, scoped by the caller rather than hardcoded), and routing refinement's own Brave call through Layer A (`cache.get_provider_fetch`/`set_provider_fetch`) and its structuring call through that same shared helper. Refinement does NOT set `bypass_cached_rejection=True` — its job is date-enrichment on an already-accepted candidate, not a second look at whether to accept it at all, so the §8 semantics don't map cleanly there (revisit only if a future diagnostic shows it matters).

**Live re-verification (Jonathan Hunt, fresh reset to book 1 only):** all 18 books still recovered in one round (`recovered=[10, 11, 13]` unchanged). Cost/latency both improved vs. the §8 post-fix measurement: **10 LLM calls (was 14), 20848/5782 tokens in/out (was 31738/9257), 67.24s wall (was 80.04s)**. `llm_verdict_hits=116` (was 56) confirms refinement queries are now actually landing cache hits instead of bypassing the cache. Full 394-test suite passes (391 previous + 3 new: refinement cache reuse, missing_volume rejection-bypass, non-missing_volume pass still trusts cached rejection).

---

## 10. Batched date-refinement + guardrail (post-§9) — DONE

Replaced the per-candidate `_refine_undated_web_search_result` (one dedicated Brave query *and* one dedicated LLM structuring call per undated candidate, up to `WEB_SEARCH_DATE_REFINEMENT_MAX = 3`) with `_refine_undated_web_search_results_batch`: still one dedicated Brave query per candidate (query text has to stay per-title — merging query text would blur which hits belong to which book), but all of those queries' combined raw results now go through **one** `_structure_with_verdict_cache` call instead of up to 3 separate ones. Ceiling is 3× fewer LLM calls per refinement-pass invocation (2 refinement passes per round — `targeted_refinement` and `missing_volume_refinement` — so up to 6× fewer across a round with both maxed out).

**Guardrail against reintroducing the §8 failure mode:** batching multiple different candidates' results into one LLM call is exactly the shape of prompt that caused the missing-volume recall-gap bug — a structured item is only ever applied back to the one candidate whose *own* query's raw fetch actually returned that item's URL, **and** whose title matches via `core_title_key`. A candidate with no clean match is simply left unresolved (original "upcoming" default stands) rather than guessing. Covered by two new tests: multiple candidates batched into one LLM call, and a candidate never receiving another candidate's resolved date even when their raw fetches share an overlapping URL.

**Live re-verification (Jonathan Hunt, fresh reset to book 1 only):** all 18 books still recovered in one round. Further cost/latency improvement vs. §9's measurement: **6 LLM calls (was 10), 18466/5274 tokens in/out (was 20848/5782), 51.80s wall (was 67.24s)** — `targeted_refinement` dropped from 3 LLM calls to 1, `missing_volume_refinement` from 2 to 1. Brave call count unchanged (37, as expected — batching only removes redundant LLM calls, not the per-title Brave queries). Full 396-test suite passes (394 previous + 2 new).

---

## 11. Parallelized Brave calls (post-§10) — IMPLEMENTED, LIVE VERIFICATION PENDING

Both of `_fetch_web_search`'s main query loop (up to `WEB_SEARCH_LOOKAHEAD_BOOKS + 1` = 11 distinct queries in the targeted pass) and `_refine_undated_web_search_results_batch`'s per-candidate query loop (up to `WEB_SEARCH_DATE_REFINEMENT_MAX` = 3) now resolve cache hits synchronously first, then fire only genuine cache-miss queries concurrently through a `ThreadPoolExecutor` bounded by a new `WEB_SEARCH_BRAVE_MAX_PARALLEL_WORKERS = 5` constant -- a small fixed pool, not one thread per query, to avoid bursting Brave with ~11 simultaneous requests and risking rate-limiting that never happened when these were sequential. Results are reassembled in original query order (not completion order) so URL-dedup's "first query wins" behavior is unaffected by which concurrent fetch happens to finish first. `_fetch_brave_web_search` itself has no shared mutable state (a fresh `httpx.get` per call), and `DiscoveryCache`/`DiscoveryTelemetry` are already lock-protected, so no new thread-safety work was needed. Pure latency change: Brave call count, LLM call count, and which URLs get fetched are all unaffected.

Covered by a new test that measures actual peak concurrency via synthetic timing (`time.sleep` + a lock-guarded counter) and asserts it is both `> 1` (actually parallel, not accidentally still sequential) and `<= WEB_SEARCH_BRAVE_MAX_PARALLEL_WORKERS` (bounded). Full 397-test suite passes.

**Live re-verification: blocked, not yet done.** The Jonathan-Hunt re-measurement run hit `Client error '402 Payment Required'` directly from Brave's own API mid-run -- an account-level billing/quota issue (very plausibly from the volume of live measurement runs already done in this session), not a code defect from this change. Unit-level correctness (bounded concurrency, dedup ordering, cache interaction) is verified; the live wall-clock-improvement confirmation on Jonathan Hunt is still outstanding and should be re-run once Brave access is restored.
