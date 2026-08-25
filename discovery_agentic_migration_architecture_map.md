# Series-Discovery Architecture Map — For Agentic Migration Design

**Purpose:** Repo-grounded reference for designing an agentic replacement of (or overlay on) the current deterministic multi-pass series-discovery pipeline. Every claim below is traceable to a specific file/function/line in this repo as of commit `fdbec65` (branch `main`, working tree clean). Companion document: `discovery_catchup_architecture_spec.md` (the deterministic pipeline's own design history — read that for *why* each piece exists; this document is organized for *where an agent would plug in*).

**Load-bearing fact for the whole design exercise:** this repo already has a **shadow-mode agentic scaffold half-built**, running today, in production, on every Check Now click. As of this map's commit it did nothing yet (nothing read its output); as of SD-12's update to §0 below, its delta/confidence phases now drive live routing. Either way, the seams it defines are almost certainly the seams a real migration should use. See §0.

---

## 0. Pre-existing agentic scaffold (read this first)

**SD-12 / UPDATED since this map was written (commit `fdbec65`):** phases 2
and 3 are no longer shadow-only -- delta now feeds `compute_confidence`,
and `confidence_engine`'s `overall` grade now drives live accept/drop/
needs-review routing in `agents/series_agent.py`'s manual-override
routing block (see that comment for the authoritative behavior). Only
phase 3.5/4 (external-reality diagnostics) remains pure shadow-mode
logging. The table below is kept as-written for traceability of the
scaffold's original design; treat the "Consumed by" column for phases 2-3
as historical, not current-state.

Four modules already exist, explicitly phased, all shadow-mode (log-only, zero effect on behavior) as of this map's commit:

| Phase | Module | Trigger | What it does | Consumed by |
|---|---|---|---|---|
| 1 | `services/skeleton_store.py` (`backfill_skeleton_for_series`) | On boot (`main.py:13,30`, `backfill_all_skeletons`), and callable per-series | Deterministic rebuild of `models.SeriesSkeleton` (one row per series, `skeleton_json`: list of `{book_number, title, status, confidence, release_date, edition_hints, sources, first_seen_at, last_confirmed_at}`) purely from current owned `Book` rows | Phase 2 (delta_engine) reads it as ground truth |
| 2 | `delta_engine.py` (`compute_series_delta`) | Every `run_series_check` call (`agents/series_agent.py:441-447`) | Pure function `(skeleton_entries, PRE-filter unified_candidates) -> {missing_books, malformed_books, numbering_gaps}` | *(as of this map)* Logged as `series_delta`; nothing reads it. **Now:** feeds `confidence_engine.compute_confidence` directly. |
| 3 | `confidence_engine.py` (`compute_confidence`) | Same call site (`agents/series_agent.py:449-457`) | Pure function scoring each candidate on 4 dimensions (provider/title/number/series-alignment confidence, each `zero\|low\|medium\|high`) → deterministic `overall` | *(as of this map)* Logged as `series_confidence`; nothing reads it. **Now:** drives live accept/drop/needs-review routing. |
| 3.5/4 | Inline in `agents/series_agent.py:598-954` | Same call | External-total-vs-owned gap analysis (Hardcover `series_total_hint`), per-drop-reason diagnostics (`discovery_engine._record_drop_diagnostic`, threaded through nearly every filter point), `new_volume_flags` | Logged as `series_external_reality`; nothing reads it -- still true today. |

**Why this matters for the migration design:** these four modules already define exactly the kind of structured, machine-readable "world model" (skeleton + delta + confidence + drop-reasons) an agent's tool-calling loop would want to consume. The migration doesn't need to invent this shape — it needs to decide when to stop treating it as shadow-only and start letting an agent read it, and eventually act on it (now already true for phases 2-3, per the update note above). `_malformed_reason` in `delta_engine.py:48-79` and the confidence dimensions in `confidence_engine.py` are effectively pre-written "agent judgment" heuristics, deterministic-only today.

---

## 1. Current Discovery Pipeline (deterministic)

Entry point: `discovery_engine.discover_candidates_for_series()` (`discovery_engine.py:3312`), called once per round by `agents/series_agent.py:SeriesIntelligenceAgent.run_series_check` (`agents/series_agent.py:325`), which is itself called 1–3 times per Check Now click by the round loop in `services/series_check_engine.py:run_series_check_job_full` (line 177).

### 1.1 Targeted pass
- `discover_candidates_for_series` → `_fetch_all_providers_parallel` (`discovery_engine.py:1788`), `pass_label="targeted"`.
- Queries built per-provider inside `_fetch_all_providers_parallel`: Google `"<series>" inauthor:"<author>"`, OpenLibrary/Hardcover both get bare `"<series> <author>"`, web search gets `["<series> <author>"] + WEB_SEARCH_LOOKAHEAD_BOOKS(10) lookahead queries` (`"<series>" <author> book <N>"` for `N = highest_owned+1 .. highest_owned+10`) — line 1836-1851.
- All four providers fired concurrently via `ThreadPoolExecutor` (line 1897), one thread per provider (not per query — web search internally parallelizes its own N queries separately, see §1.9).
- Results fused (`_fuse_and_score_candidates`, line 1990) into `UnifiedCandidate` objects, then conditionally reconciled (§1.4), then edition-collapsed (`_finalize_candidates`, line 2993), then flattened back to raw dicts and run through `_filter_and_merge` (line 1703) — the author/language/placeholder/bundle/series-index/dedup filter.

### 1.2 Author-fallback pass
- Gated by `_should_trigger_author_fallback` (line 3114) → `_series_completeness_and_confidence` (line 3083): triggers when `series_completeness < FALLBACK_SERIES_COMPLETENESS_THRESHOLD (0.5)` OR `avg_confidence < FALLBACK_CONFIDENCE_THRESHOLD (0.35)`.
- Same shape as targeted pass but series-scoped (not bare author-wide) — `discover_candidates_for_series:3498-3514`. Web search **off by default** (`enable_fallback_web_search=False`).
- Explicit cross-series contamination filter before fusion: `_filter_cross_series_contamination` (line 3168) drops any hit whose own `series_name_hint` is explicitly incompatible with the target series (`_is_cross_series_contamination`, line 3133; compatibility via `_series_names_compatible`).
- Additive to targeted-pass results (title-key exclusion against what targeted already found), tagged `confidence="author_fallback"` (weaker trust downstream — see `agents/series_agent.py:707-729`'s `belongs_to_series` gate).
- Hard kill switch: `allow_author_fallback` param (always `False` from `discover_series_by_name`, the "Add Book" targeted-fill flow).

### 1.3 Missing-volume (skeleton) pass
- `discovery_engine._reconstruct_series_skeleton` (line 2250), called separately from `series_agent.py:481`, **after** the targeted+fallback+reconciliation output is available.
- Computes `expected_total` = max integer number seen across owned books + all fused candidates; `missing_numbers` = interior gaps in `1..expected_total`.
- Up to `MAX_MISSING_VOLUME_LOOKAHEAD_QUERIES = 6` targeted `"<series> <author> book <N>"` Brave queries, one per missing number, batched into **one** LLM structuring call via `_fetch_web_search(..., pass_label="missing_volume")`.
- Recovered candidates re-fused with existing ones (existing given priority bucket — line 2366-2371) — **recomputed from scratch every call, nothing persisted between calls** (this is the exact gap `SeriesSkeleton`/Phase 1-4 exists to eventually fix).
- Carries a specific cache-correctness fix (§8 of the spec): `bypass_cached_rejection=True` only for `pass_label == "missing_volume"` (`_fetch_web_search:1628`, `_structure_with_verdict_cache:1481,1517`) — a cached LLM rejection from the noisier targeted-pass batch is *not* trusted here, forcing one fresh, focused LLM look.

### 1.4 Conditional LLM reconciliation
- Gated by `_needs_llm_reconciliation` (line 2450): completeness < `RECONCILIATION_SERIES_COMPLETENESS_THRESHOLD (0.8)`, OR provider disagreement ratio > `RECONCILIATION_DISAGREEMENT_RATIO_THRESHOLD (0.2)`, OR avg metadata completeness < `RECONCILIATION_METADATA_COMPLETENESS_THRESHOLD (0.5)`.
- `_reconcile_candidates_with_llm` (line 2683): **one** LLM call over the entire fused set together (not per-item) — merges duplicates, flags cross-series exclusions, suggests `missing_volume_suggestions` (currently **unused** — no call site reads this field). `RECONCILIATION_MAX_CANDIDATES = 40` cap.
- **Explicitly non-cacheable by design** (whole-set semantics, composition changes every round) — see `discovery_cache.py` docstring and spec §3.
- Runs on both targeted-pass and (if triggered) fallback-pass candidate sets independently.

### 1.5 Reconciliation → belongs_to_series gate → persistence handoff
- `discover_candidates_for_series` returns `{candidates, unified_candidates, provider_failures, all_providers_failed, used_author_fallback, drop_diagnostics}` (line 3585-3606). `candidates` is deterministically sorted/stripped via `finalize_discovery_output` (line 3228).
- `agents/series_agent.py:676-850` is the **real gate**: per-candidate `belongs_to_series` boolean built from `targeted_with_number`, `explicit_series_match`, `partial_match`, `continues_numbering_valid` (line 727-729), then downgraded for universe-tie-ins (line 738-740) and owned-title compilations (line 752-754). Only candidates clearing this AND `_is_known_candidate` (line 223) as *not* already known get persisted.

### 1.6 Multi-round loop
- Lives entirely in `services/series_check_engine.py:run_series_check_job_full` (line 177), **outside** `series_agent`/`discovery_engine` — `series_agent.run_series_check` has no awareness it's being called in a loop.
- `SERIES_CHECK_MAX_ROUNDS = 3` (line 50). Each round: call agent → persist (insert/update/dedupe, 2 identity-collapse passes) → recompute `highest_owned_book_number` implicitly via next round's fresh DB read inside `run_series_check` → stop if round persisted zero new books (line 769) or global timeout hit (line 306-309, `SERIES_CHECK_HARD_TIMEOUT_SECONDS = 300`, budget shared across the whole loop, not per-round).
- **Stop condition is deliberately single-signal** ("zero new books persisted") — a previously-tried second signal ("didn't reach top of lookahead window") was falsified live and dropped (spec §2.2) — important invariant for any agentic replacement's own stop logic to respect or consciously improve on.
- Notification (`create_series_discovery_notification`) and series-intelligence rebuild happen **once**, after the loop, with the delta **summed across all rounds** (line 774-824) — never per-round.

### 1.7 Caching layers
- **Layer A** — provider-fetch cache, `services/discovery_cache.py:DiscoveryCache._provider_fetch`, keyed `(provider, normalized_query_text)` (literal-text, not semantic tuple — known accepted gap, spec §7.1). Shared across every pass and round of one job; created fresh per job in `run_series_check_job_full:225`, discarded at job end.
- **Layer B** — LLM-verdict cache, `DiscoveryCache._llm_verdict`, keyed `(scope_type, series_name_normalized, url)`. Caches both accepted verdicts (dict) and rejected sentinels (`None`). `scope_type` is open-string, currently `"series"` | `"author"` | `"missing_volume"` implicitly folds into `"series"` with the bypass flag (not a separate scope_type — worth noting for an agent design that wants per-pass cache isolation). Shared cache-splicing logic: `_structure_with_verdict_cache` (line 1472), used by both `_fetch_web_search` and `_refine_undated_web_search_results_batch`.
- Both layers are **per-job, in-memory, never persisted, never shared across jobs/series/profiles** — a hard invariant (spec §3, "Non-Goals").
- `_reconcile_candidates_with_llm` is explicitly and permanently excluded from any caching.

### 1.8 Parallelization
- Provider-level: `_fetch_all_providers_parallel` (Google/OpenLibrary/Hardcover/web, one thread each, `ThreadPoolExecutor(max_workers=len(tasks))`, line 1897).
- Brave-query-level (inside the "web" task): `_fetch_web_search`'s own query loop and `_refine_undated_web_search_results_batch`'s per-candidate query loop each resolve cache hits synchronously first, then fire only genuine misses through a bounded `ThreadPoolExecutor(max_workers=min(n, WEB_SEARCH_BRAVE_MAX_PARALLEL_WORKERS=5))` (lines 1584-1599, 1414-1428). Results reassembled in **original query order**, not completion order — load-bearing for URL-dedup "first query wins" and `_first_present_field`'s order-dependent backfill.
- Round-level (the multi-round catch-up loop): **sequential**, one `ThreadPoolExecutor(max_workers=1)` submission per round in `series_check_engine.py:311-314`, purely so the hard timeout (`future.result(timeout=...)`) can be enforced — not for concurrency.

### 1.9 Temperature settings
- `_structure_web_results_with_llm` (extraction): `temperature=0` (line 1252) — set specifically to fix a recall-gap bug (spec §8); non-determinism was ruled out as the root cause but temperature=0 was kept as a reasonable determinism improvement anyway.
- `_reconcile_candidates_with_llm`: `temperature=0` (line 2743), same rationale.
- `generate_series_overview` (prose summary, on-demand only, never during discovery): **no explicit temperature** → Anthropic default (1.0) — deliberately untouched, it's generative writing, not extraction.
- Model: single constant `ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"` (line 66) for all three call sites — no per-task model selection exists today.

### 1.10 Telemetry
- `services/discovery_telemetry.py:DiscoveryTelemetry` — per-job, in-memory, thread-safe (`threading.Lock`), created in `series_check_engine.py:220`, shared across all rounds (cumulative, not per-round-reset).
- Tracks: `pass_scope` context manager (duration per named pass), `record_brave_call` (query + duration), `record_llm_call` (duration + tokens in/out). `summary()` produces a `by_pass` breakdown plus totals.
- Pass names in use today: `targeted`, `author_fallback`, `missing_volume`, `precheck`, plus each pass's own `<pass>_refinement` (date-refinement sub-pass) — string labels, not an enum; an agentic replacement introducing new pass names is free to do so without touching this module.
- Surfaced in `CHECK NOW DEBUG SUMMARY` via `services/discovery_logging.py:log_discovery_summary` (line 63) — console-only, not persisted, not exposed via any API endpoint today.
- `idle_check` flag (spec §7.3): distinguishes "confirmed nothing new via cheap catalog-only pre-check" from "ran the full loop and found nothing" — set in `series_check_engine.py:285`.

### 1.11 Invariants the pipeline depends on
1. **URL-keyed, not index-keyed, downstream of any LLM structuring call.** `result_index` from the LLM is only ever used to resolve against the *exact subset actually sent* to that call, then immediately converted to a `dict[url] -> verdict` (`_structure_with_verdict_cache:1528-1540`). Any agentic replacement that re-introduces positional indexing into a filtered/reordered list reintroduces the exact bug class this fixed.
2. **Original list order preserved on reassembly** (`_fetch_web_search:1601-1609,1631-1637`) — `_first_present_field`-style "first non-empty wins" backfill logic is order-dependent; a cached-vs-fresh run of the same data must be behaviorally identical.
3. **A cached LLM rejection is not universally final** — the missing-volume pass's `bypass_cached_rejection=True` is a deliberate, narrow exception (§1.3) proving that blanket verdict caching is unsafe for a pass whose entire purpose is "give this a second, cleaner look." Any agentic memory/cache design must preserve an equivalent escape hatch.
4. **`_reconcile_candidates_with_llm` has no stable per-item cache key** — whole-set semantics, composition changes every round. An agent design should not assume every LLM call in this domain is cacheable per-item.
5. **Confidence tags (`targeted` / `author_fallback` / `missing_volume_recovery`) must survive re-merges.** `_filter_and_merge` (line 1780) preserves a candidate's already-assigned confidence rather than overwriting it, specifically because an earlier version collapsed everything to `author_fallback` on any re-merge and broke `belongs_to_series`'s `targeted_with_number` gate for a real regression case (Jonathan Hunt).
6. **Per-job cache/telemetry must never persist or leak across jobs, series, or profiles** — this is explicit, not just current behavior (spec §3).
7. **`highest_owned_book_number` is recomputed from a fresh DB read every round**, not carried in memory across rounds — round N+1 must see round N's inserts (series_check_engine.py comment at line 236-240).

---

## 2. Provider Abstraction

There is **no formal provider interface/protocol** in this codebase — no `Provider` base class, no registry. Each provider is a bare function with its own bespoke signature and return shape, unified only by convention (every result dict uses the same flat field set) and by the two orchestration functions that call them.

### 2.1 Provider call sites
| Provider | Fetch fn | Line | Auth | Notes |
|---|---|---|---|---|
| Google Books | `_fetch_google_books(query, max_results=40)` | 862 | Optional `GOOGLE_BOOKS_API_KEY` | No series-position field ever |
| OpenLibrary | `_fetch_openlibrary(query, max_results=40)` | 897 | None | No series-position field ever |
| Hardcover | `_fetch_hardcover(query, max_results=25)` | 927 | Required `HARDCOVER_API_KEY` (returns `[]` silently if unset) | GraphQL POST; **only provider with a structured `series_number_hint`/`series_name_hint`/`series_total_hint`/`upcoming_hint`** |
| Brave (web search) | `_fetch_brave_web_search(query, count=8, telemetry=...)` | 1093 | Required `BRAVE_SEARCH_API_KEY` (returns `[]` silently if unset) | Returns raw title/description/url only — **no structured book fields at all**; everything else about a Brave hit is manufactured by the LLM structuring pass |

All four are called only from two orchestration points: `_fetch_all_providers_parallel` (catalog three) and `_fetch_web_search` (Brave, always paired with `_structure_web_results_with_llm`/`_structure_with_verdict_cache`).

### 2.2 Where provider results enter the pipeline
`_fetch_all_providers_parallel` returns `{"google": [...], "openlibrary": [...], "hardcover": [...], "web": [...], "_failures": {...}}` — a fixed 4-key dict, always, regardless of caller. This is the single seam where all provider output funnels into `_fuse_and_score_candidates`. An agent that wants to add a fifth provider (or replace Brave) would extend this dict shape and its `catalog_fetchers`/`tasks` construction (lines 1868-1894).

### 2.3 Query construction
- **Google**: `"<series>" inauthor:"<author>"` (or bare `inauthor:"<author>"` if no series name) — hardcoded in `_fetch_all_providers_parallel:1836`.
- **OpenLibrary/Hardcover**: bare `"<series> <author>"` text by default; overridable per-call via `openlibrary_query`/`hardcover_query` params (used by the author-fallback pass: `f'"{series_name}" "{query_author}"'`, line 3504, and `discover_candidates_for_author`'s plain author-wide sweep).
- **Web search**: a *list* of queries, not one — targeted query + up to `WEB_SEARCH_LOOKAHEAD_BOOKS(10)` numbered lookahead queries (line 1844-1851), or an explicit override list (`web_search_queries` param) for the fallback/author-wide passes.
- **Author name used in queries is `primary_author_name(author)`** (first co-author only, line 844) — but matching/filtering downstream (`_author_matches`, line 849) still checks against the *full* original author string. This asymmetry is the exact root cause of a known Layer-A cache gap (spec §7.1: two call sites build differently-shaped author strings — `primary_author_name(author)` in the exterior pass vs. the full `resolved_author` string in `_reconstruct_series_skeleton` — producing two literal-text cache keys for the same semantic query).

### 2.4 Normalization
- Every `_fetch_*` returns the same flat dict shape regardless of provider: `source`, `source_id`, `title`, `authors` (list), `published_date` (raw string, unparsed), `description`, `isbn13`, `source_url`, `language`, plus provider-specific optional fields (`series_number_hint`, `series_name_hint`, `series_total_hint`, `upcoming_hint`).
- Real normalization/identity work happens **after** fetch, in `_fuse_and_score_candidates` (line 1990) — groups raw hits into `UnifiedCandidate` by `isbn13 -> title_key -> normalized-title` identity chain, backfilling missing fields via `_first_present_field` (line 1969) in a fixed provider-priority order (Hardcover > Google > OpenLibrary > web search, both in `_PROVIDER_CONFIDENCE_WEIGHT:1930` and `_PROVIDER_SORT_RANK:3205`).
- Date parsing is deferred even further — `parse_flexible_date` (line 792) isn't called until `agents/series_agent.py:824`, i.e. raw `published_date` strings ride unparsed through the entire discovery_engine pipeline.

### 2.5 Brave-specific logic (concentrated, not scattered — a real seam)
Brave is structurally different from the other three: it never returns anything book-shaped on its own. Every place Brave's raw output becomes usable book data is LLM-mediated:
- `_fetch_brave_web_search` (line 1093) — raw fetch only, title/description/url.
- `_structure_web_results_with_llm` (line 1197) + its prompt `_WEB_SEARCH_STRUCTURING_PROMPT` (line 1127) — the only place free-text search snippets become `{title, series_name, book_number, author_names, published_date, is_upcoming, isbn13}`.
- `_structure_with_verdict_cache` (line 1472) — the Layer B cache wrapper, Brave-only (no other provider has an LLM-verdict step at all).
- `_fetch_web_search` (line 1553) — the only orchestration function that issues *multiple* queries per call and does its own dedup-by-URL before structuring.
- `_refine_undated_web_search_results_batch` (line 1339) — Brave-only second-look pass for undated candidates (`"<title> <series> <author> release date"` query).
- `precheck_for_new_volumes` (line 3262) explicitly **excludes** Brave (`enable_web_search=False`) — the one place in the pipeline that deliberately avoids Brave/LLM cost.

**Implication for migration:** Brave+LLM-structuring is the single most "agent-shaped" piece of the existing pipeline already — it's already doing free-text interpretation via an LLM call, batched, cached, with a correlation guardrail (`_refine_undated_web_search_results_batch`'s title-key matching, line 1449-1467) to prevent misattribution. An agentic replacement's "web research" tool could plausibly *be* a generalized version of this exact code path, rather than a from-scratch build.

---

## 3. Integration Points

### 3.1 Series-check job integration
- `POST /series/{id}/check` (`routers/series.py:196`) schedules `run_series_check_job_full(series_id)` via FastAPI `BackgroundTasks` (line 223). No request body — purely series-id-driven.
- In-memory job-status dict `series_check_jobs: dict[int, dict]` (`series_check_engine.py:41`) — polled via `GET /series/{id}/check` and `GET /series/{id}/check/status` (lines 244, 281). **Not persisted** — a server restart loses in-flight job status (acceptable today since jobs are short-lived and re-triggerable).
- A second click while `status == "running"` is a no-op returning current progress (`routers/series.py:213-221`); a *completed* job never blocks a fresh click (explicit design choice, comment at line 208-212).
- Auto Discovery sweep (`services/auto_discovery.py`) reuses the exact same `run_series_check_job_full` — it's a batch caller, not a separate discovery path. Its own eligibility filter (`is_series_eligible_for_auto_discovery`, line 34) is a **pre-job gate that manual Check Now never consults** — an important asymmetry for an agentic design to preserve (manual = full override, auto = conservative).

### 3.2 Persistence
- **`series_agent.run_series_check` never writes to the DB** except two fields at the very end of its own try block (`series.has_new_books`, `series.last_checked`, committed at `agents/series_agent.py:957-960`) — everything else is pure discovery, returned as a dict.
- All real persistence (insert/update/dedupe, `highest_owned_number` advancement via new rows, 3 identity-collapse passes) happens in `series_check_engine.py:run_series_check_job_full`, lines 401-748, **once per round**, synchronously, in the same DB session as the whole job.
- Identity/dedupe keys used for matching a discovered candidate against existing rows (in priority order): ASIN exact match → `_series_book_identity_key(series_id, title, author, book_number)` → `_canonical_title_identity_key(title)` (all from `services/identity.py`) — three separate collapse passes run after insert to catch cross-key duplicates the initial matching missed (lines 642-738). **Note (post-incident fix):** `_series_book_identity_key` is keyed on the immutable `series_id`, never `Series.name`/`series_name` (a display-only string that can and does change between runs — see the function's own docstring for the duplicate-insert incident this caused). The two *existing-row* dedupe-collapse passes (lines ~682, ~734) intentionally use a separate, more lenient `_series_number_slot_key(series_id, book_number)` instead — no title/author — since their job is to collapse rows that already share a series+number slot *despite* a title mismatch.
- `highest_owned_book_number` itself is **not stored anywhere** — recomputed fresh every call from `active_series_books` (`agents/series_agent.py:351-358`, `max(int(book_number) for owned, non-missing books)`), not necessarily contiguous.
- Downstream of persistence: `library_sync.update_from_series(series_id)` (line 745), `intelligence.recalculate_intelligence`/`recalculate_series_state_for_series` (lines 817, 986) — these own `Series.missing_books`, `next_unread_book_number`, `next_upcoming_book_number`, `is_caught_up`, `is_finished` etc.; discovery_engine/series_agent never touch these fields directly.

### 3.3 `idle_check` and stop conditions
- **Pre-check short-circuit** (`series_check_engine.py:260-294`): `Series.last_checked` within `SERIES_CHECK_PRECHECK_STALENESS_DAYS(3)` → one catalog-only, zero-Brave/zero-LLM fetch (`discovery_engine.precheck_for_new_volumes`) against `ceiling = max(known_numbers)`. If nothing above ceiling: `run_full_loop = False`, `idle_check = True`, loop body never executes (`rounds_run` stays 0).
- **Round loop stop condition** (`series_check_engine.py:769-772`): break when a round persists zero new books, or the shared job timeout is exhausted. No other stop signal exists (see §1.11 invariant #… the previously-tried "lookahead window" signal was removed).
- Both signals surface in the final `result`/`completion` dict (`idle_check`, `rounds_run`, `timed_out`) — consumed by `log_discovery_summary` and available to the frontend via the job's `completion` payload, but **no dedicated API field/endpoint documents these as a stable contract** — they're inline dict keys.

### 3.4 Reconciliation → final structured output
Three distinct "reconciliation" concepts exist at different layers — worth being precise about which one an agent would replace:
1. **Fusion** (`_fuse_and_score_candidates`) — deterministic, identity-based merge of raw provider hits into `UnifiedCandidate`s. Always runs.
2. **LLM reconciliation** (`_reconcile_candidates_with_llm`) — conditional, whole-set LLM pass for messy/incomplete/disagreeing fusion output. Runs 0–2 times per `discover_candidates_for_series` call (once for targeted set, once for fallback set if triggered).
3. **series_agent's belongs_to_series gate + already-known dedup** (`agents/series_agent.py:676-850`) — the final deterministic decision of "does this get persisted," independent of and downstream of both of the above.
4. **series_check_engine's identity-collapse passes** (§3.2) — DB-level reconciliation against rows that may have come from *different* discovery runs/rounds entirely, not just this run's candidate set.

---

## 4. Seams for Agentic Replacement

### 4.1 Modules that could be replaced entirely by an agent
- **`_fetch_web_search` + `_structure_web_results_with_llm` + `_refine_undated_web_search_results_batch`** (Brave orchestration + LLM structuring, discovery_engine.py lines 1093–1701): already the most "agentic" code in the pipeline (free-text search → LLM interpretation → structured output, with caching and a correlation guardrail). A tool-calling agent doing its own iterative "search, read, decide if this is a real book, search again if unclear" loop is a near-direct generalization of what this code already approximates with fixed, hand-tuned heuristics (lookahead width, refinement cap, batch-vs-per-item).
- **`_should_trigger_author_fallback` / `_needs_llm_reconciliation` / the whole "when do we escalate" threshold logic** (lines 3079-3130, 2391-2492): these are exactly the kind of judgment calls ("does this look complete enough, should I dig further") an agent's own reasoning loop is suited to replace — currently hardcoded magic-number thresholds (`0.5`, `0.35`, `0.8`, `0.2`) with no adaptivity per series shape (long-running vs. new, indie vs. major-publisher).
- **The missing-volume gap-filling loop** (`_reconstruct_series_skeleton`, line 2250) — "here's a numbered gap, go find it specifically" is a natural agent tool-call (`search_for_specific_volume(series, author, number)`), especially since it's currently stateless/recomputed from scratch every call — an agent with the durable `SeriesSkeleton` as memory would not need to.
- **The multi-round catch-up loop itself** (`series_check_engine.py:296-772`): currently a rigid `for round in range(3)` with one stop signal. An agent-driven loop ("keep researching until you're confident the series is either complete or you've hit a real wall") is a more natural fit than a fixed round count — though see §5 for why the *persistence* half of each round must stay deterministic regardless.

### 4.2 Modules that should remain deterministic fallback
- **`_fuse_and_score_candidates` / identity matching (`isbn13 -> title_key -> normalized-title`)** — this is exact-match, low-ambiguity logic; an LLM has no comparative advantage here and non-determinism would be a pure regression. Keep as the "given a pile of raw candidate dicts, collapse the ones that are obviously the same book" utility layer under any agent.
- **`services/identity.py`'s DB-matching keys** (ASIN → series_book_key → canonical_title_key) and the **three identity-collapse passes** in `series_check_engine.py` — these guard real persisted data against duplication; agent-driven discovery must still funnel through this exact gate, not bypass it.
- **`_filter_and_merge`'s hard filters** (author-match, language, placeholder-title, bundle-title, series-index-stub detection) — cheap, deterministic, well-tested guardrails against known bad patterns (regression comments throughout `discovery_engine.py` document real production bugs each one fixes). These are exactly the kind of narrow, battle-tested heuristics that should stay as post-hoc validation on *any* agent's output, not be re-derived by prompting.
- **`precheck_for_new_volumes`** — the zero-cost "is there anything new at all" gate. Keep deterministic; an agent invocation should never happen before this gate, for cost-control reasons alone (see §5).
- **The persistence/dedupe pipeline in `series_check_engine.py`** (§3.2) — DB writes must stay deterministic and auditable regardless of what decided *what* to write.

### 4.3 Abstractions to preserve
- **The `SeriesSkeleton` schema and Phase 1-4 shadow scaffold** (`models.SeriesSkeleton`, `delta_engine.py`, `confidence_engine.py`) — already exactly shaped for an agent's world-model/memory. Migration should graduate these from shadow-logged to actually-read, not replace them.
- **`DiscoveryTelemetry` / `DiscoveryCache` per-job pattern** — the per-job-scoped, in-memory, discard-at-end lifecycle is a clean seam an agent's own tool-call ledger can slot into (e.g. each agent tool call records into the same telemetry object; Layer A/B cache semantics — including the `bypass_cached_rejection` escape hatch — should be preserved even if the pass names above them change).
- **The `Series`/`Book` DB schema, `record_status`/`profile_id` scoping, `metadata_source` provenance field** — none of this is discovery-pipeline-specific; any agent must still write through the exact same models and respect existing profile isolation (`profile_id` scoping appears in nearly every query in this codebase — see `agents/series_agent.py:380`, `_owned_book_indexes:1036`).
- **`idle_check`/`rounds_run`/`timed_out`/`provider_failures`/`all_providers_failed` result-dict fields** — already-established API contract surface (frontend + logging consume these); an agentic engine's result shape should be a superset, not a replacement.
- **The `belongs_to_series` acceptance gate's specific regression-fixed rules** (universe-tie-in downgrade, compilation-of-owned-titles downgrade, `continues_numbering` requiring textual corroboration) — each one exists because of a specific, named production failure (Jonathan Hunt, Safehold, Starship's Mage — all cited in code comments). These are effectively a labeled eval set already; preserve them as guardrails/eval cases for the agent, don't silently drop them.

### 4.4 Abstractions to deprecate
- **The fixed multi-pass sequence itself** (targeted → author-fallback → reconciliation → missing-volume, each with its own hand-tuned trigger threshold) — this is precisely the "deterministic multi-pass pipeline" the user's framing wants replaced. The pass *labels* are useful for telemetry continuity; the rigid *sequencing and thresholds* are the target for replacement.
- **The literal-query-text Layer A cache key** (`services/discovery_cache.py`'s documented, accepted gap vs. the originally-specified semantic tuple key) — an agent-based design that reasons about queries semantically rather than issuing hand-built query strings could close this gap naturally instead of needing the originally-proposed `_cache_key(provider, series_name_normalized, primary_author_name, book_number)` helper.
- **`_reconstruct_series_skeleton`'s "recomputed from scratch every call"** — superseded once the durable `SeriesSkeleton` table is actually read/written incrementally instead of only Phase-1-backfilled from owned books.
- **Hardcoded lookahead width / refinement cap / round count as global constants** (`WEB_SEARCH_LOOKAHEAD_BOOKS=10`, `WEB_SEARCH_DATE_REFINEMENT_MAX=3`, `SERIES_CHECK_MAX_ROUNDS=3`, `MAX_MISSING_VOLUME_LOOKAHEAD_QUERIES=6`) — these exist because a deterministic pipeline can't adapt per-series; an agent with a real stopping-judgment (backed by cost/telemetry limits, not a magic number) is the entire point of the migration.

### 4.5 Where an agent plugs into the existing job engine
The cleanest seam is **inside `run_series_check_job_full`'s round loop, at the exact point it currently calls `series_agent.run_series_check`** (`series_check_engine.py:312-314`):

```
executor.submit(series_agent.run_series_check, db, series_id, update_progress, False, telemetry, discovery_cache)
```

An agentic discovery engine should be a **drop-in alternative to this one call** — same inputs available (`db` session read-only access, `series_id`, `telemetry`, `cache`), same required output contract (a dict with `candidates`/`added_books`-compatible shape, `provider_failures`, `all_providers_failed`). Everything *around* this call — the pre-check short-circuit, the round loop's stop condition, the timeout/executor wrapping, and 100% of the persistence/dedupe/notification logic after it returns — can stay untouched on day one. This makes an agent swap-in testable behind a feature flag (deterministic vs. agentic engine selectable per-job) without touching persistence at all, which is the highest-leverage, lowest-risk seam in the whole codebase for a first migration slice.

---

## 5. Constraints and Invariants

### 5.1 Correctness
- **Never persist a candidate that doesn't pass `_author_matches`** (`agents/series_agent.py:409`, `discovery_engine.py:849`) — a same-author-string mismatch is a hard reject everywhere, not a soft signal.
- **Never let a positionally-indexed LLM response outlive the exact list it was generated against** — see §1.11 invariant #1. Any agent tool-call response that returns indices/positions into a list must be resolved immediately, before any filtering/reordering happens to that list.
- **Profile isolation is absolute** — every DB query in the discovery path is scoped by `profile_id` (directly or via `series_id`, which itself is profile-scoped); an agent must never query or match across profiles (see `_owned_book_indexes`'s explicit `profile_id` docstring, `agents/series_agent.py:1036-1049`).
- **A "rejected" verdict must be distinguishable from "never checked"** — `None` sentinel vs. `CACHE_MISS` sentinel object distinction in `discovery_cache.py:42` is load-bearing; an agent's own memory of "I already looked at this and it wasn't a match" must be similarly explicit, not silently indistinguishable from "haven't looked yet."
- **`all_providers_failed` must mean literally zero usable data**, not "filtering left nothing" (`discovery_engine.py:3580` comment) — an agent's own error/failure semantics need the same distinction (a provider returning zero *relevant* results is a normal successful outcome, not a failure).

### 5.2 Cost control
- **The per-job cache (Layer A+B) must remain per-job, in-memory-only** — explicitly rejected design: any cache that outlives one job or is shared across jobs/series/profiles (spec §3). An agent's own memory/tool-call cache should follow the same scope discipline unless a deliberate, reviewed decision changes it (e.g. extending Layer A/B lifetime to span multiple Check Now clicks on the same series would be a real, intentional design change, not an accident).
- **Zero-cost gates must run before any paid call** — `precheck_for_new_volumes` (catalog-only, zero Brave, zero LLM) is checked *before* the full loop ever starts. Any agentic design must preserve an equivalently cheap "is this even worth researching" gate before invoking an LLM-driven research loop, given real per-call Anthropic/Brave costs.
- **Bounded call counts, not unbounded agent loops** — every existing pass has an explicit numeric ceiling (`WEB_SEARCH_LOOKAHEAD_BOOKS`, `WEB_SEARCH_DATE_REFINEMENT_MAX`, `MAX_MISSING_VOLUME_LOOKAHEAD_QUERIES`, `MAX_PUBLICATION_DATE_BACKFILL_LOOKUPS=8`, `MAX_SERIES_HINT_LOOKUPS=25`, `RECONCILIATION_MAX_CANDIDATES=40`). An agent's tool-calling loop needs an equivalent hard ceiling (max tool calls / max tokens / max wall time per job) — this is not optional, it's the load-bearing lesson of this entire codebase's cost-optimization history (§6-11 of the architecture spec).
- **Shared job-level timeout, not per-call timeout only** (`SERIES_CHECK_HARD_TIMEOUT_SECONDS=300`, budget shared across the whole multi-round loop) — an agent replacing the loop needs to budget against the same kind of shared ceiling, not just per-tool-call timeouts, or a "helpful" agent could keep researching indefinitely across many cheap-seeming small calls.
- **Live measured baseline exists for comparison**: 39 Brave + 10 LLM calls / 73.8s wall for a full 18-book cold reconstruction (spec §6/§10); 0 Brave + 0 LLM / 1.19s for an idle-confirmed series (spec §7.4). Any agentic replacement's cost profile should be evaluated against these two concrete reference points, not a fresh guess.

### 5.3 Integration with the rest of the repo
- **`series_agent.run_series_check` must remain side-effect-free w.r.t. Book rows** (it may only touch `Series.has_new_books`/`last_checked`) — persistence is `series_check_engine.py`'s job alone. An agentic engine occupying this seam must preserve this split, since the round loop's re-read-highest-owned-number-each-round logic depends on persistence happening exactly once, in one place, after discovery returns.
- **Result dict shape is a de facto contract** — `series_check_engine.py`, `discovery_logging.py`, and (implicitly) the frontend's job-polling code all read specific keys (`added_books`, `candidates`, `provider_failures`, `all_providers_failed`, `telemetry`, `cache`, `used_author_fallback`). A replacement engine must produce a compatible superset, not a redesigned shape, unless every consumer is updated in lockstep.
- **Auto Discovery's eligibility gate is a separate, pre-existing concern** (`services/auto_discovery.py`) that must keep working unmodified — it decides *whether* to call the job at all; it has no opinion on what happens inside the call.
- **Existing regression-fixed heuristics are effectively an unwritten eval suite** — every `belongs_to_series`/`_filter_and_merge` guard documents a real production bug with a real series name. A migration should convert these into an explicit eval set (fixture data + expected accept/reject) *before* replacing the logic that currently encodes them, so regressions are caught mechanically rather than rediscovered live again.

---

## 6. Migration Surface

### 6.1 Minimal set of modules an agent would need to replace
To ship the smallest meaningful agentic slice (research/decision-making only, not persistence):
- `discovery_engine._fetch_web_search`, `_structure_web_results_with_llm`, `_refine_undated_web_search_results_batch`, `_structure_with_verdict_cache` (the Brave+LLM research loop) — replace with an agent tool-calling loop that has `search_web`, `search_catalog(google|openlibrary|hardcover)`, and `record_finding` as tools.
- `discovery_engine._should_trigger_author_fallback`, `_needs_llm_reconciliation`, `_reconcile_candidates_with_llm` (the "is this good enough, should I dig further / reconcile" judgment layer) — replace with the agent's own reasoning about completeness, informed by the `SeriesSkeleton`/delta/confidence data already computed in shadow mode.
- `discovery_engine._reconstruct_series_skeleton`'s *search* half (not its gap-computation half, which is pure arithmetic and should stay) — replace the "go find this specific missing number" fetch with an agent tool-call, informed by the persistent skeleton instead of a from-scratch recompute.
- Optionally, `series_check_engine.py`'s round loop (lines 296-772's control flow, not its persistence body) — replace the fixed `for round in range(3)` + single stop-condition with an agent-driven "keep going until confident or budget exhausted" loop, still calling into the same persistence code after each discovery result.

### 6.2 Minimal set of modules that must remain (deterministic, untouched)
- `_fuse_and_score_candidates`, `UnifiedCandidate`, `_filter_and_merge`, `_finalize_candidates`, `finalize_discovery_output` — identity/dedup/normalization utility layer, provider-agnostic.
- `services/identity.py`'s DB-matching keys and `series_check_engine.py`'s three identity-collapse passes — persistence-side dedup, must stay exactly as-is regardless of what upstream logic decided the candidate.
- `agents/series_agent.py`'s `belongs_to_series` gate and `_is_known_candidate` — the final deterministic accept/reject/already-known checks, encoding a dozen real regression fixes; keep as a post-hoc validator over *any* upstream source (deterministic pipeline or agent) rather than something an agent's own judgment replaces.
- `services/discovery_cache.py`, `services/discovery_telemetry.py` — per-job cache/telemetry infra; extend, don't replace.
- `services/skeleton_store.py`, `delta_engine.py`, `confidence_engine.py`, `models.SeriesSkeleton` — the existing shadow-mode agentic scaffold; graduate to load-bearing rather than rebuilding equivalent functionality elsewhere.
- All of `services/series_check_engine.py`'s persistence body (insert/update/dedupe/notification/intelligence-rebuild), `library_sync.py`, `intelligence.py` — completely discovery-method-agnostic; nothing here should need to know whether candidates came from the deterministic pipeline or an agent.
- `routers/series.py`'s job endpoints, `services/auto_discovery.py`'s eligibility gate — API/scheduling surface, unaffected by what happens inside the job.

### 6.3 Recommended insertion point
**`services/series_check_engine.py:312-314`** — the single `executor.submit(series_agent.run_series_check, ...)` call inside the round loop. Concretely:

1. Introduce an engine-selection seam here (e.g. a feature flag or per-series setting choosing `series_agent.run_series_check` vs. a new `agentic_series_agent.run_series_check`), so both engines can be A/B-tested behind the identical persistence/round-loop/timeout/notification machinery that already exists and is already correct.
2. Have the agentic engine consume `telemetry`/`cache` exactly as the deterministic one does today, so cost comparisons against the §5.2 baselines are apples-to-apples from day one.
3. Have the agentic engine read `SeriesSkeleton` (already backfilled at boot) as its starting world-model instead of recomputing everything from `discover_candidates_for_series`'s pre-filter output — this is the one piece of "durable memory" infrastructure that's already sitting there unused, built for exactly this purpose.
4. Keep `discovery_engine._filter_and_merge`/`_fuse_and_score_candidates`/`agents/series_agent.py`'s `belongs_to_series` gate as a **mandatory post-processing step on the agent's proposed candidates**, not something the agent re-derives — this both preserves every regression fix documented in that code and gives the agent a deterministic, auditable "did my finding actually get accepted" signal to reason with mid-loop.
5. Do not touch the pre-check short-circuit (`precheck_for_new_volumes`) or the round loop's overall stop/timeout logic in the first slice — prove the agentic *research* step in isolation before touching the *orchestration* loop around it.
