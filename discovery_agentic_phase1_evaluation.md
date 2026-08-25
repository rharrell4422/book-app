# Evaluation: `discovery_agentic_phase1_plan.md` vs. the actual repo (`main`, post-Wave-4)

**Purpose:** feasibility/correctness check of Copilot's Phase-1 plan against the repo *as it exists right now*, per the review workflow in `discovery_agentic_phase1_plan.md`. No code was changed to produce this document. Every claim below was checked against source (`confidence_engine.py`, `delta_engine.py`, `services/skeleton_store.py`, `provider_protocol.py`, `provider_io.py`, `services/discovery_telemetry.py`, `agents/series_agent.py`, `fixtures/eval_regressions/`, and the commit log) as it exists today, not as it existed when `discovery_agentic_replacement_recommendation.md`/`discovery_agentic_replacement_evaluation.md` were written.

**Bottom line up front:** the plan's *ticket taxonomy* (RT‑1b, PB‑5) is internally consistent with this repo's real numbering scheme (RT‑1a/RT‑2/RT‑4/RT‑5/RT‑6 and PB‑1/PB‑3a/PB‑3b/PB‑4/PB‑6/PB‑9 are all already shipped; RT‑1b and PB‑5 are genuinely unclaimed next-in-sequence numbers) — so whoever assembled this plan is tracking the same ticket ledger this codebase uses. But §2's "Phase‑1 Blockers (Resolved)" section is **stale**: it describes three things that have already shipped (in some cases with a materially better design than what's proposed here), one live gap that's correctly identified but under-specified, and a provider strategy (§3 Step 3, §8) that names three vendors (Tavily/Exa/SerpAPI) **none of which exist anywhere in this codebase** — the actual provider stack (Serper primary, Apify fallback, Brave fully retired) was a deliberate, cost-driven decision already made and shipped. Before Phase 1 work starts, this plan needs to be re-derived against current `main`, not the snapshot it appears to be working from.

---

## 1. Where I agree

- **General Phase‑1 framing** — "preserve all existing semantics; no new architecture or data model" (§1) — is the right governing constraint, and matches the standing consensus from `discovery_agentic_replacement_recommendation.md` §1.1 (keep the mechanism layer permanent; only the policy layer is a legitimate replacement target).
- **Ticket-taxonomy discipline** — RT‑1b/PB‑5 slot cleanly into the existing RT‑*/PB‑*/CR‑*/DC‑*/NS‑*/PP‑*/SD‑* scheme already used across ~30 files. No collision with a shipped ticket. Good continuity.
- **§4 Deterministic Invariants** — provider determinism, skeleton-merge determinism, confidence determinism, turn determinism, TTL determinism — all correctly restate principles this codebase already holds itself to (e.g. `confidence_engine.py`/`delta_engine.py` are explicitly pure functions today; `services/skeleton_store.py`'s `_upsert_skeleton_row` is already built for deterministic, idempotent, order-independent merge under retry).
- **§7 "Durable skeleton updated only after full agentic turn completes" / "never swallow failures, never drop fields, never reorder books, never lose confidence grades"** — this is exactly the existing single-writer/apply-after-persistence design already live in `services/series_check_engine.py`'s `apply_skeleton_updates` call (guarded by `FIX-PB-7`, which already covers "never swallow failures" — see §2.3 below). Correct as a principle; already implemented as a principle.
- **§9 completion-criteria shape** (loop exists, escalation deterministic, TTL deterministic, eval harness running, regressions passing) is a reasonable Phase‑1 exit bar in the abstract — my concerns below are about what's already true vs. still needed, not about the bar itself.

---

## 2. Where the plan is stale or factually wrong about current state

### 2.1 "Confidence Table Completion" (§2) — already shipped, more completely than described

`confidence_engine.py`'s `_overall_confidence` (lines 302–382) already has a **complete, explicitly-enumerated decision table** for the `unverified` title grade, finalized in commit `ea22d7b` ("Phase 0: asymmetric skeleton merge + full unverified confidence table") after `discovery_agentic_replacement_evaluation.md` §4/§6/§8 flagged the original one-worked-example version as underspecified. The docstring spells out every reachable combination (`unverified+zero→zero`, `unverified+low→low`, `unverified+medium→medium`, `unverified+high(all others)→medium`, title can never alone reach `high`) and states the general rule is dimension-count-agnostic, not a hardcoded 4-tuple lookup. This is not a blocker to resolve in Phase 1 — it's finished work. Re-doing it risks silently changing settled, carefully-reasoned behavior with no new information driving the change.

**Recommendation:** drop this item from Phase 1 entirely. If Copilot has a specific combination it believes is still wrong, name it against the existing table rather than re-scoping the whole thing as unresolved.

### 2.2 "Skeleton Single-Writer Rule" (§2) — the actual implementation is more robust than what's proposed, and the proposal is a regression

The plan says: *"Backfill becomes read-only once agent loop begins; agent is sole writer of skeleton rows."*

What's actually shipped in `services/skeleton_store.py` (`_upsert_skeleton_row`, `models.SeriesSkeleton.version` — added by migration `a1a17b22f53a`, ticket `CR‑4`) is **optimistic-concurrency upsert-with-retry**: both `backfill_skeleton_for_series` (boot-time + pre-round rebuild) and `apply_skeleton_updates` (post-persistence agent-finding merge) can run concurrently against the same row, each protected by a version-checked conditional UPDATE that retries on conflict rather than assuming one writer is disabled. This is explicitly *because* a blunt "backfill becomes read-only" rule was considered and rejected — the whole point of `backfill_skeleton_for_series` staying live is that a user editing/removing an owned book must still immediately rebuild the library-sourced half of the skeleton (see the asymmetric merge rule in the module docstring), which "read-only once the agent loop begins" would break.

**Recommendation:** don't implement a read-only toggle. The single-writer *property* (one row, one commit, no lost updates) is already satisfied by the version column + retry loop; what Phase 1 actually needs here is nothing — this ships as-is.

### 2.3 "Probes + TTL Schema" (§2) — partially real, partially wrong, and conflicts with the existing TTL

- **TTL exists today**, but at `DISCOVERED_ENTRY_TTL_DAYS = 90`, not 30, and it lives on the skeleton **entry** itself (`source_class: "discovered"`, aged out in `_is_expired_discovered_entry` when `last_confirmed_at` is stale) — not on a separate `probes` list. The plan's proposed `ttl_days: 30` on a `probe` object is a *second*, competing TTL concept with no stated relationship to the first. Before writing code, this needs to be resolved: is a "probe" the same thing as today's unconfirmed `discovered` entry (in which case the ask is "change 90→30," a one-line, low-risk config change), or something genuinely new (see §5 below — I think it is)?
- **`apply_skeleton_updates`'s `probes` parameter already exists as a typed no-op**, by design: `services/skeleton_store.py`'s docstring says outright *"`probes` is still always `[]`; no probe schema exists yet (Phase 1)"* — i.e. this specific gap was already identified and deliberately deferred to exactly this phase, with the call site already wired (`services/series_check_engine.py` already calls `apply_skeleton_updates(..., probes=result.get("probes"))` every round). So this part of the plan is legitimately in scope, just needs a concrete schema decision (see Q4 below) rather than being treated as a blocker that's already "resolved."

### 2.4 Provider escalation order (§3 Step 3, "Tavily → Exa → SerpAPI (eval-only) → Brave (fallback)") — does not match the shipped provider stack at all

This is the most significant mismatch in the plan. None of Tavily, Exa, or SerpAPI exist anywhere in this codebase. What's actually live, per `provider_io.py`'s own comment block:

> *"Brave Search is no longer a viable provider: its only sub-enterprise tier caps out at 1000 queries/month, which this app hit during personal-use testing alone. Serper is its replacement... Serper's coverage of the indie/LitRPG/web-serial sources Brave used to surface is unverified and may differ."*

The real stack is **Serper (primary web search) → Apify (fallback when Serper's HTTP layer fails or its LLM-structuring pass returns nothing usable)** — see `_fetch_web_search`'s fallback branch and `_web_search_empty_result_fallback`/`_fetch_apify_discovery` in `provider_io.py`. Brave is not "kept as a fallback" per the earlier recommendation doc's §3.4 — it's fully retired (no Brave code path exists at all in current `provider_io.py`). Adopting §3's list as-is would mean building three new, unpaid-for, unintegrated vendor relationships (Tavily, Exa, SerpAPI-for-production) while discarding a working, already-paid, already-tuned Serper+Apify pipeline, with no stated reason for the switch.

**Recommendation:** re-scope this to the real stack: **Serper (primary) → Apify (fallback)**, matching what `_fetch_web_search` already does. If Copilot has a specific, current (2026) cost/coverage argument for Tavily/Exa over Serper, that's a legitimate thing to bring back as a question — but it should be argued on its own merits against Serper, not asserted as if Serper were never chosen.

### 2.5 `provider_protocol.py` (§8 "Modify: … deterministic escalation") — already exists, already does most of this

`provider_protocol.py` already ships the exact seam the original recommendation doc's §3.2 called for: a `WebSearchProvider` `Protocol`, a canonical `RawResult`/`ProviderFetchResult` shape, and adapters (`GoogleBooksProvider`, `OpenLibraryProvider`, `HardcoverProvider`, `WebDiscoveryProvider`, `ApifyProvider`) wired into `_fetch_all_providers_parallel` — this is Wave 2's "unified provider protocol" work, ticket family `PP-1` through `PP-6`. "Modify" is fine as a verb, but the plan should say what's actually changing (e.g. "add an escalation-order helper that tries Serper then Apify through these adapters" — which is arguably already what `_fetch_web_search`'s fallback does, just not exposed as a standalone escalation function) rather than reading like this module needs to be built.

### 2.6 `skeleton.py` (§8 "Modify … skeleton.py") — doesn't exist

The durable skeleton lives in `services/skeleton_store.py` + `models.SeriesSkeleton`. There is a second, unrelated, ephemeral "skeleton" concept (`discovery_engine._reconstruct_series_skeleton`, ticket `PB-6`, explicitly documented as a different thing despite the shared name) that a plan author skimming for "skeleton" could plausibly confuse with the durable one. Whichever one Phase 1 means to touch needs to be named explicitly — they have different owners, different lifetimes, and conflating them was specifically called out as a risk in `services/skeleton_store.py`'s own module docstring.

### 2.7 Eval fixtures (§3 Step 5, §8 "Add fixtures: fixtures/agentic_eval/turn_*.json") — a new, disconnected fixture set is proposed where one already exists (though it's a different shape)

`fixtures/eval_regressions/` already exists (ticket `PB-3a`), with five frozen regressions (Jonathan Hunt, Safehold, Starship's Mage, universe-tie-in downgrade, compilation-of-owned-titles downgrade) plus a fixture-backed provider (`fixtures/provider_recordings/`, ticket `PB-3b`) for deterministic replay of recorded provider responses. These are genuinely a different *shape* than what a turn-based agentic loop would need (they're pure-function input/output pairs for specific guards like `_title_is_series_variant`, not full multi-turn world-model-delta replays), so building `fixtures/agentic_eval/` as a new, additive directory is legitimate — but it should explicitly build on `fixtures/provider_recordings/`'s recorded-fixture-provider pattern rather than reinventing provider replay from scratch, and the harness should be able to run the existing five regressions through whatever the new turn-based loop produces, not just its own new fixtures — otherwise Phase 1 could ship an agent that regresses Jonathan Hunt/Safehold/Starship's Mage while its own eval harness reports green.

---

## 3. What's actually still open (agree these belong in Phase 1)

- **Author-mismatch reconciliation (§2)** — this is real and correctly identified, and is *still unresolved* as of today. `confidence_engine._series_alignment_confidence` (token-set-equality + a bespoke initials-abbreviation carve-out) and `discovery_text._author_matches` (all-tokens-as-substrings-in-any-candidate-string) remain two independently-implemented algorithms with no shared code or test proving they agree — exactly the gap `discovery_agentic_replacement_evaluation.md` §6 flagged (a candidate with a co-author byline or middle name can pass `_author_matches`'s substring check but land on `_series_alignment_confidence`'s `zero`/`medium` branch). This is legitimately in scope for Phase 1, but per that evaluation's §8, it should ship as: either (a) make `_series_alignment_confidence` call `_author_matches` directly, or (b) add an eval fixture that exercises the disagreement case and prove `zero` is a strict subset of `_author_matches`'s reject set — not asserted as already-canonical.
- **Probe schema (§2, §5)** — see §2.3/§5 below; genuinely new, additive work, correctly scoped as Phase 1.
- **A real multi-turn evaluation harness with deterministic replay** — the existing `fixtures/eval_regressions/` covers pure-function regressions well but has no concept of "replay a full turn sequence and diff the resulting world-model." Building this is legitimate net-new Phase 1 work.

---

## 4. One sequencing concern not addressed by the plan

The prior consensus (`discovery_agentic_replacement_recommendation.md` §5) explicitly phased this as **Phase 0 (prereqs, no agent code) → Phase 1 (shadow: agent runs alongside the deterministic pipeline, zero behavioral change, gated by ≥95% agreement on already-accepted books before advancing) → Phase 2 (canary) → Phase 3 (primary) → Phase 4 (loop ownership, deliberately last)**. Since that consensus, Phase 0 has shipped (confidence table, skeleton merge, provider protocol, eval fixtures — see §2.1–§2.7 above) *and* something beyond shadow mode has already shipped too: `agents/series_agent.py`'s "manual-override routing" block already reads `confidence_engine`'s `overall` grade live to drive accept/needs_review/auto-drop decisions (not shadow-logged — see `delta_engine.py`'s own module docstring: *"No longer shadow-only... drives live accept/drop/needs-review routing"*), and already wires `needs_review` candidates into `skeleton_store.apply_skeleton_updates` via `_needs_review_to_skeleton_updates` (`PB-1`).

§3's Step 3 (a full 10-step turn-based loop with `begin_turn`/`select provider`/`issue call`/.../`end_turn`, replacing the current parallel-fan-out-then-confidence-routing design) is a substantially bigger structural change than what's live today, and the plan doesn't say whether it's meant to land in shadow mode (run alongside the current live routing, compared, not yet load-bearing) or replace the live routing directly on day one. Given the prior consensus put "loop ownership" deliberately last, and given the live routing already in `series_agent.py` is itself an untested-at-scale, fairly recent behavior change, I'd treat this as a question rather than assume either answer (see Q5 below).

---

## 5. Open questions (with my recommended answer for each)

**Q1. Is this plan working from a stale snapshot of the repo, or is §2's "Resolved" framing intentional (e.g. "confirm these are still resolved" rather than "these still need resolving")?**
*My recommendation:* Assume stale snapshot and re-derive Phase 1 scope against current `main` (specifically `confidence_engine.py`, `delta_engine.py`, `services/skeleton_store.py`, `provider_protocol.py`, `provider_io.py`, `agents/series_agent.py`'s manual-override routing block, and `fixtures/eval_regressions/`) before locking anything further. Otherwise Phase 1 effort gets spent re-solving §2.1/§2.2 (already done, and in §2.2's case, done better than proposed) while the two genuinely open items (author-matcher unification, probe schema) don't get the specificity they need.

**Q2. Given Serper+Apify are the live, paid, already-tuned providers (Brave fully retired for cost reasons), should §3's provider-escalation section be dropped, or re-scoped?**
*My recommendation:* Re-scope to **Serper (primary) → Apify (fallback)**, matching `_fetch_web_search`'s existing fallback behavior, and make the "escalation" work be about exposing that as an explicit, deterministic, testable function (it currently lives inline as a fallback branch) rather than introducing Tavily/Exa/SerpAPI with no stated cost/coverage justification over the current stack.

**Q3. For the skeleton single-writer rule — keep the existing optimistic-concurrency (version-column) design, or actually want a "backfill becomes read-only" switch for some reason not stated in the plan (e.g. a performance concern with retries under real agent-loop write volume)?**
*My recommendation:* Keep the existing design. It already gives single-writer-per-row correctness without disabling boot-time backfill (which real users depend on for "I fixed a book's number, the skeleton should reflect that immediately"). If there's a concrete concern about retry contention once the agent loop is writing far more frequently than today's post-round `apply_skeleton_updates` call, that's worth raising specifically, with the expected write frequency, rather than reaching for read-only mode as a default fix.

**Q4. Is a "probe" (§2, §5) meant to be conceptually different from today's `discovered`-source_class entry with its 90-day TTL? If so, what does a probe capture that an unconfirmed `discovered` entry doesn't?**
*My recommendation:* Yes, they should be different, and the difference matters: today's skeleton only has memory for numbers the agent found *something* for (however unconfirmed). There's currently no memory of "I searched for book 14 and found nothing" — a true negative. Recommend adding `probes` as genuinely new, additive structure (`{book_number, probed_at, probe_kind, outcome, ttl_days}`, roughly per `discovery_agentic_replacement_recommendation.md` §2.1, which scoped this well) rather than merging it into the existing 90-day discovered-entry TTL. A shorter TTL for probes (30 days, as proposed) is reasonable on its own terms — "confirmed nothing found" should be re-checked sooner than "found but unconfirmed" — as long as it's understood as a second, complementary mechanism, not a replacement for the first.

**Q5. Should the turn-based agent loop (§3 Step 3) ship as Phase 1's live behavior directly, or run in shadow mode first against the existing (already-live, already-tested-in-production) confidence-routing behavior in `agents/series_agent.py`?**
*My recommendation:* Shadow mode first, per the standing Phase 1→2→3→4 consensus. The current live routing is itself a fairly recent, real behavior change (not the original deterministic-only pipeline) with its own regression protections (`fixtures/eval_regressions/`) built specifically around named production incidents. Replacing it outright with a new loop structure in the same phase that also builds the loop for the first time removes the ability to A/B those regression fixtures against both designs before committing.

---

## 6. Suggested revision for §8 "Diff-Ready Instructions," pending answers above

Once Q1–Q5 are answered, I'd expect §8 to read closer to:

- **Create:** `agentic_hooks.py` (only the genuinely new pieces — see if `services/discovery_telemetry.py`'s existing `pass_scope`/`record_provider_call`/`record_gate_outcome`/`record_llm_call` cover `record_tool_call`/`record_reasoning_step` already, extend rather than duplicate), `agentic_series_agent.py` (shadow-mode initially, per Q5), `agentic_eval_harness.py` (built to also replay `fixtures/eval_regressions/` and `fixtures/provider_recordings/`, per §2.7).
- **Modify:** `services/skeleton_store.py` (add `probes` schema per Q4 — the call site already exists), `confidence_engine.py` (unify `_series_alignment_confidence` with `_author_matches`, per §3 above — not the decision table, which is done), `provider_protocol.py` / `provider_io.py` (expose Serper→Apify escalation as an explicit function, per Q2 — not add new vendors).
- **Do not touch:** `_overall_confidence`'s decision table (done), the skeleton single-writer mechanism (done, and better than proposed).

---

## Round 2 — Evaluation of Copilot's "Corrections Based on Evaluation (Diff Only)"

**Status:** Copilot's diff response accepted every correction from Round 1 above (items 1–5, 7, 8, 9 below are clean, no further comment needed). Two items (6 and 10) need one more precision pass before this is implementation-ready — not because the direction is wrong, but because "integrate probes with the existing 90-day TTL" is ambiguous between two designs, and one of the two designs will reintroduce a bug this codebase already fixed once.

### Clean agreement — no further comment

- **Item 1 (confidence table)** — agreed, matches Round 1.
- **Item 2 (skeleton single-writer)** — agreed. "Agentic writes use the existing optimistic concurrency mechanism / backfill remains allowed / no new locking model" is exactly `_upsert_skeleton_row`'s current behavior, restated correctly.
- **Item 3 (Serper → Apify)** — agreed, matches the live `_fetch_web_search` fallback behavior in `provider_io.py`.
- **Item 4 (file corrections)** — agreed. `services/skeleton_store.py` is correct; extending `fixtures/eval_regressions/` + `fixtures/provider_recordings/` instead of a disconnected new directory is correct.
- **Item 5 (author-mismatch reconciliation)** — agreed, correctly kept in scope, matches the still-open gap between `_series_alignment_confidence` and `_author_matches`.
- **Item 8 (RT‑1b/PB‑5)** — agreed, no ticket collision.
- **Item 9 (remove stale providers)** — agreed.

### Item 6 + 10 (Probes + TTL) — needs one more precision pass before consensus

Two separate questions were bundled into "integrate probes with the existing 90-day TTL rather than introducing a new TTL system," and they need to be answered separately because one answer is safe and the other is a concrete regression risk:

**Sub-question A — is "the existing 90-day TTL" a *duration to reuse*, or a *storage structure to reuse*?**

- Reusing the **duration** (i.e. probes also expire after `DISCOVERED_ENTRY_TTL_DAYS = 90`, just so there's only one TTL constant in the codebase to reason about) is safe and I'd recommend it — no objection, and it directly resolves my Round‑1 Q4 concern about two competing TTL numbers (30 vs. 90) with no stated relationship.
- Reusing the **storage structure** — i.e. representing a probe ("searched for book 14, found nothing") as an entry inside the same `skeleton_json` list that `discovered`/`library` entries live in, keyed by `book_number` the same way — is where I'd push back. Concretely:

  `confidence_engine._skeleton_by_number` (confidence_engine.py) builds `{book_number: entry}` from every entry in `skeleton_entries`, and `_title_confidence` (confidence_engine.py) does:

  ```
  skeleton_entry = skeleton_by_number.get(number)
  if skeleton_entry is None:
      return "unverified"   # nothing to compare against -- the case this grade exists for
  ...compare titles, worst case return "low"
  ```

  A "probed, absent, no title" entry for book 14 is **not** `None` — it's a real dict in the map. So the very next real candidate for book 14 (the book actually gets announced) would hit `skeleton_by_number.get(14)` → get the probe entry back → skip the `unverified` branch → compare the candidate's title against the probe entry's (empty) title → fail both the exact-match and `core_title_key` checks → fall through to `"low"`. Per `_overall_confidence`'s routing (documented in that module: `"low"` always auto-rejects, regardless of `belongs_to_series`), **a genuinely new, correctly-discovered book would be silently auto-dropped**, purely because it happened to have been probed-and-absent in an earlier run. That's mechanically the same failure mode `discovery_agentic_replacement_recommendation.md` §0.2 introduced the `unverified` grade to prevent (a new book scoring no better than a contradicted one) — just reintroduced through a different door.

  This is fixable (e.g. skip `source_class == "probed"` entries when building `skeleton_by_number`, or give a probe entry no `book_number` key that `_skeleton_by_number` recognizes), but it means "integrate probes with the entries list" is not schema-neutral with respect to `confidence_engine.py` — it requires a matching, deliberate change there, not just in `skeleton_store.py`.

  **My recommendation:** keep probes in a structurally separate place from `skeleton_json`'s entry list — either a sibling field on the same `SeriesSkeleton` row (a new `probes_json` column, one more small Alembic migration in the pattern of `a1a17b22f53a`'s `version` column) or a separate list nested under a top-level key rather than mixed into the book-number-keyed entries `confidence_engine.py` already iterates. Same 90-day TTL constant, same `_upsert_skeleton_row` single-writer mechanism, zero changes required to `confidence_engine.py`'s existing `None`-means-"unverified" check. This satisfies "integrate with the existing TTL system" (one constant, one retention policy) without satisfying the riskier reading (one list, one lookup function, silently reinterpreted).

**Sub-question B — does "activate probes" in Phase 1 conflict with "shadow mode first" (item 7)?**

`services/series_check_engine.py`'s call to `apply_skeleton_updates(..., probes=result.get("probes"))` already reads from the **live** `series_agent.run_series_check`'s result dict, not from any shadow agent — and `series_agent.py` never populates `probes` today (always absent/`None`). So there's no actual conflict *as long as* Phase 1's new `agentic_series_agent.py` (shadow mode, per item 7) stays a side-channel that logs its own would-be `skeleton_updates`/`probes` to the eval harness/diagnostics only, and does **not** get wired into that same `apply_skeleton_updates` call site until Phase 2 promotion. "Activating probes" in Phase 1 then means: (a) build the schema/plumbing in `skeleton_store.py` so it can accept and persist real probe data once something produces it, and (b) exercise that plumbing via the eval harness and via the shadow loop's own diagnostic replay — but the production call site keeps reading from `series_agent.py`, which won't produce probes until `series_agent.py` itself is taught to (a Phase 2+ question, not Phase 1). Worth stating explicitly in the plan so nobody wires the shadow loop's output into the live call site by convenience during implementation.

### Net assessment

Items 1–5, 7, 8, 9: **consensus reached.** Item 6/10: **consensus on intent** (reuse the 90-day duration, no second TTL system), **one open design decision** (separate storage for probes vs. inline entries) that should be answered before implementation starts, since the inline-entries reading has a concrete, specific regression path through `confidence_engine.py` that the separate-storage reading avoids entirely at no extra cost.

**Question to send back, with my recommended answer:** should probes live in their own field/column on `SeriesSkeleton` (separate from `skeleton_json`'s book-number-keyed entries), or as a new kind of entry inside that same list?
*My recommendation:* separate field (e.g. `probes_json`), same `DISCOVERED_ENTRY_TTL_DAYS` constant, zero changes needed to `confidence_engine.py`. If Copilot has a reason to prefer inline entries (e.g. wanting one unified "everything we know about book N" lookup), that's a legitimate design goal, but it needs to come with an explicit instruction to make `confidence_engine._skeleton_by_number` (and anything else that iterates `skeleton_json` expecting real book entries) skip/ignore probe-tagged entries — not left as an implementation detail to discover later.

---

## Round 3 — Evaluation of items 11–13 only (11 and 13 agreed; 12 conditionally agreed, pending 11's schema gap)

### Item 11 (separate `probes_json` column) — agree on structure, one real gap in the schema

The separate-column design and its stated rationale are exactly right, and the migration precedent it cites checks out: `alembic/versions/a1a17b22f53a_add_version_column_to_series_skeleton.py` (`CR-4`) added `version` to `series_skeleton` the same way — a plain nullable=False/server_default column addition via `batch_alter_table`, no data migration needed. A `probes_json` column follows the identical pattern. No objection to the mechanism.

**The proposed field list is missing the one thing a probe has to record to be useful: which book number it's about.** As written — `{"source": provider_name, "turn": n, "timestamp": iso8601}` — there is no `book_number` and no `outcome`/`probe_kind`. Every consumer this concept was designed for needs at least the first:

- The original motivating use case (`discovery_agentic_replacement_recommendation.md` §2.1, and this plan's own §5 "Re-query when... numbering incomplete / Stop querying when... TTL entries resolved") is "don't re-probe a specific gap number the agent already checked recently." That requires the record to say *which number* — without it, nothing downstream can answer "has book 14 been probed in the last N days?"
- It also needs to say *what happened* — a bare `{source, turn, timestamp}` can't distinguish "searched for book 14 and found credible evidence it doesn't exist yet" from "searched and got an ambiguous/inconclusive result" from "found it, just not corroborated yet" — three different things the plan's own §2/§5 treat as distinct follow-up signals (re-query on "inconclusive," don't re-query on a confident "absent").

**Recommendation:** extend the schema to at minimum `{"book_number": <float>, "source": provider_name, "turn": n, "outcome": "absent"|"inconclusive", "timestamp": iso8601}` — either as a flat list of these (book_number as a field) or as a dict keyed by `book_number` with `{source, turn, outcome, timestamp}` as the value (either shape works; a flat list is more consistent with `skeleton_json`'s own list-of-dicts convention, so I'd lean that way for consistency, but no strong preference). Whichever shape is chosen, it needs `book_number` and `outcome` before this is buildable — right now the three-field version can be written and read but can't actually answer the questions probes exist to answer.

**Second, secondary point on item 11 — the version-column mechanics need one more sentence.** `probes_json` and `skeleton_json` would live on the *same row*, sharing the *same* `version` counter (per `_upsert_skeleton_row`'s existing single-writer design). That means whichever function ends up writing `probes_json` must read-and-carry-forward the current `skeleton_json` value unchanged in the same conditional UPDATE (and vice versa for whatever writes `skeleton_json`) — two independent merge functions that each only set "their own" column while blindly re-writing the other to a stale value would silently clobber each other under the exact same lost-update pattern `CR-4` fixed for the single-column case. This isn't a reason to reject item 11 (a two-column, one-version-check row is a completely normal thing to build correctly) — it just means `_upsert_skeleton_row`'s `merge_fn` contract needs to be generalized to "read and return both columns" before there are two call sites writing to this row independently, rather than left as an implicit assumption.

### Item 12 (TTL applies only to probe entries, discovered-entry TTL unchanged) — agree in shape, blocked on item 11's gap

No objection to "one 90-day duration, two independent expiry code paths, zero shared logic between them" — that's the right shape and keeps `_is_expired_discovered_entry`/`_merge_discovered_entries` (the already-tested discovered-entry path) completely untouched, which was my Round 2 ask. But a probe expiry check is a function of `(book_number, timestamp)` — expire *this number's* probe once its `timestamp` is 90 days old, so *that number* becomes eligible for re-probing. Item 11's current 3-field schema has the `timestamp` half of that but not the `book_number` half, so item 12 can't actually be implemented against it as specified. This resolves automatically once item 11's schema gap above is closed — no separate design concern beyond that dependency.

### Item 13 (shadow-mode compatibility) — agree, no further comment

Matches my Round 2 recommendation on this exactly: the live `series_check_engine.py` call site keeps reading from `series_agent.py` (which produces no probes today) until Phase 2 promotion; `agentic_series_agent.py` populates `probes_json` only for the eval harness in the meantime. Nothing to add.

### Net for this round

Items 11 and 13: **consensus.** Item 12: **consensus on shape**, mechanically blocked until item 11 adds `book_number` (and, recommended, `outcome`) to the probe schema — once that's added, item 12 needs no further changes of its own.

---

## Round 4 — Evaluation of items 14–16 only

### Item 14 (add `book_number` + `outcome`) — agree on the fields, one type correction needed

Adding `book_number` and `outcome: "absent"|"inconclusive"` closes exactly the gap Round 3 flagged, and the `outcome` values match `discovery_agentic_replacement_recommendation.md` §2.1's original vocabulary (`outcome: "absent"|"inconclusive"`) precisely — good, no drift from the concept's origin.

**One concrete correction: `book_number` must be a float/number, not `int`.** Every other `book_number` field in this codebase is a float, deliberately: `models.Book.book_number` is a `Float` column (not `Integer`), `_to_float_or_none` is the shared coercion helper both `delta_engine.py` and `confidence_engine.py` import specifically so companion/novella entries at fractional positions (e.g. `3.5`) aren't mishandled, and `services/identity.py`'s `_normalized_book_number_value` was itself fixed once already for truncating `3.5` to `3`. `provider_io.py`'s own `_parse_web_search_structured_items` has a standing comment (`CR-3`) about exactly this: *"float, not int -- a fractional position... used to get silently truncated to an int."* A probe for a `.5`-numbered companion entry (a real, supported case in this app) would round-trip incorrectly if the schema's declared type is `int`. Same field name, same semantics as `skeleton_json`'s `book_number` — should be the same type.

**Second, unaddressed design point: does a book number get at most one live probe, or an accumulating history?** The schema as shown is a single flat object shape, presumably meaning `probes_json` is a flat list of these. If a number gets probed again in a later run (e.g. absent at turn 3, still absent at turn 9), does the list grow one entry per attempt, or does the new probe replace the old one for that `book_number`? For the concept's actual purpose — "has this number been probed within the last 90 days, and with what outcome" — only the *most recent* probe per number is ever actually consulted; an accumulating history adds unbounded row growth for a long-lived series with no corresponding benefit, and would require whoever reads `probes_json` to take a max-by-timestamp per `book_number` instead of doing a direct lookup. **Recommendation:** merge/replace by `book_number` — one live probe per number, most recent wins — mirroring the `by_number` dict pattern `apply_skeleton_updates` already uses for `discovered` skeleton entries (`services/skeleton_store.py`, the `by_number: dict = {}` loop in its `merge_fn`). Same shape, same precedent, no new pattern to invent.

(Minor, non-blocking note: `turn` is diagnostic only — it's only unique *within* one job/run, not globally, so it shouldn't be used for TTL math. `timestamp` is the field TTL expiry must key off of; worth stating explicitly so nobody reaches for `turn` there later.)

### Item 15 (generalize the upsert contract for two columns) — agree, three implementation-readiness notes for whoever writes this

The direction — one version check, one conditional UPDATE, both columns read-preserved-written together, no independent single-column writers — is exactly right and is the correct fix for the shared-row/shared-`version` mechanics Round 3 raised. Three things worth stating now so they aren't rediscovered mid-implementation:

1. **Both existing call sites need their `merge_fn` contract updated, not just `_upsert_skeleton_row` itself.** `backfill_skeleton_for_series`'s `merge_fn` (rebuilds library entries in `skeleton_json`) has no reason to touch `probes_json` at all — it should read-and-return it unchanged. `apply_skeleton_updates`'s `merge_fn` (currently `skeleton_json`-only) needs to grow to also handle `probes_json` once it starts accepting real probe data (per item 6/13, still gated to the eval harness in Phase 1, not the live call site). Neither of these is stated in item 15 but both fall directly out of it.
2. **Where does the 90-day probe sweep actually run?** Item 16 says probes get a 90-day expiry but doesn't say which function performs the sweep. The natural answer is *inside `backfill_skeleton_for_series`'s `merge_fn`*, since that function already runs on the matching cadence (boot, plus once per Check Now round before discovery) and already performs the equivalent sweep for `discovered` skeleton entries via `_merge_discovered_entries`/`_is_expired_discovered_entry`. Recommend saying so explicitly rather than leaving two sweeps (discovered-entry, probe) to possibly end up on two different cadences by accident.
3. **Existing regression coverage only exercises the single-column race.** `tests/test_skeleton_store.py`'s `CR-4` test (lost-update / optimistic-concurrency-conflict coverage) only proves two concurrent writers to `skeleton_json` can't clobber each other. Generalizing to two columns needs a companion test proving a concurrent `skeleton_json` write and a concurrent `probes_json` write each survive the other (i.e. neither writer's column silently reverts to a stale read when the other writer's conditional UPDATE wins the race) — not just a re-run of the existing single-column test.

None of these are objections to item 15's direction — they're the concrete follow-through it implies, written down now rather than left implicit.

### Item 16 (90-day TTL for probes, keyed on `book_number`) — agree, unblocked

Correct and unblocked now that item 14 supplies `book_number`. No further comment beyond what's already said under items 14–15 above (in particular: the sweep should key off `timestamp`, not `turn`, and should most naturally live in the same function/cadence as the existing discovered-entry sweep — see item 15 note 2).

### Net for this round

Items 15 and 16: **consensus** (15 with three follow-through notes for the implementer, not open questions; 16 fully unblocked). Item 14: **consensus on the two new fields**, plus one required correction (**`book_number` must be float, not int** — this one isn't a preference, it's a type that will silently corrupt `.5` companion-entry probes if shipped as `int`) and one open design choice (replace-by-`book_number` vs. accumulating history — recommended: replace, no objection expected but worth Copilot explicitly confirming before implementation).

---

## Round 5 — Evaluation of items 17–20 only

All four items are direct, faithful incorporations of Round 4's feedback, with no deviation and no new factual errors found on re-check against the actual code:

- **Item 17 (`book_number: float`)** — matches exactly. Confirmed correct against `models.Book.book_number` (a `Float` column) and every other `book_number` field in this codebase. No further comment.
- **Item 18 (replace-by-`book_number`, one live probe per number)** — matches exactly, correctly framed as mirroring the existing `by_number` dict pattern `apply_skeleton_updates` already uses for `discovered` entries. No further comment.
- **Item 19 (both `merge_fn` call sites read/preserve/pass-through both columns; companion cross-column lost-update test)** — matches exactly, covers both follow-through points from Round 4 (call-site contract update and test coverage extension). No further comment.
- **Item 20 (probe TTL sweep lives inside `backfill_skeleton_for_series`'s `merge_fn`, keyed on `timestamp` + the now-corrected `book_number` float)** — matches exactly. No further comment.

**One small clarification worth stating explicitly, not a concern:** since the sweep only runs inside `backfill_skeleton_for_series` (per item 20) while new probes are written inside `apply_skeleton_updates` (the function that actually receives the `probes` argument today, already plumbed from `series_check_engine.py`'s call site), `apply_skeleton_updates`'s own `merge_fn` writes/replaces probes but does **not** itself sweep expired ones — a probe written this round won't be checked against the 90-day TTL until the *next* `backfill_skeleton_for_series` call (next round or next boot). This is not a gap: it's exactly the same split the existing `discovered`-entry design already has today (`apply_skeleton_updates` writes/refreshes discovered entries; `backfill_skeleton_for_series`'s `merge_fn` is the only place that sweeps them). Stating it here just confirms the probe design is symmetric with the pattern it's mirroring, not a new asymmetry to watch for.

### Net for this round, and overall status

Items 17–20: **consensus, no changes requested.**

With this round, every item raised across the whole review (1–20) has reached consensus — the last open thread was items 6/10/11/12/14 (probe schema, storage, and TTL), and items 17–20 close it out cleanly. **The Phase‑1 plan as amended is, from this evaluation's perspective, ready for implementation planning** — no outstanding objections or open questions remain on this side. If Copilot has nothing further to add, the next step per the stated workflow is to move from "iterate until consensus" to implementation, starting with the sequencing already agreed in the plan (shadow-mode agentic loop first, per item 7/13; the probe schema/upsert-contract work from items 11–20 can land independently of the loop since it's additive, DB-level, and already isolated behind the not-yet-live `probes` plumbing).
