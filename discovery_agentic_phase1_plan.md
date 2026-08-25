# READERPRO — PHASE-1 AGENTIC AI IMPLEMENTATION PLAN (Authoritative Spec)

**Source:** pasted verbatim from Copilot, 2026-08-24. Stored here as the baseline
architecture reference for this phase's Cursor↔Copilot review loop (see
`discovery_agentic_phase1_evaluation.md` for Cursor's evaluation against the
actual current codebase, and iterate from there). Companion documents from the
prior phase: `discovery_agentic_migration_architecture_map.md`,
`discovery_catchup_architecture_spec.md`,
`discovery_agentic_replacement_recommendation.md`,
`discovery_agentic_replacement_evaluation.md`.

**Review workflow for this phase:**
1. Copilot proposes architecture (this document, and future revisions).
2. Cursor evaluates against current code/approach: agrees where warranted, flags
   concerns + recommendations, and raises open questions with a recommended
   answer.
3. Answers get carried back to Copilot.
4. Iterate until consensus.
5. **No implementation until consensus is reached.**

---

## 1. Phase‑1 Goals
- Implement RT‑1b agentic substrate (agentic_hooks.py).
- Implement PB‑5 shadow diagnostics hooks.
- Build deterministic multi-turn agentic discovery loop (agentic_series_agent.py).
- Integrate provider escalation, world-model updates, confidence/delta routing.
- Build deterministic agentic evaluation harness.
- Preserve all existing semantics; no new architecture or data model.

## 2. Phase‑1 Blockers (Resolved)
### Confidence Table Completion
All grade combinations defined; implement full decision table exactly as specified in confidence_engine.py + SD‑12 rules.

### Author-Mismatch Reconciliation
belongs_to_series gate must call into confidence-engine's author matcher; confidence-engine normalization is canonical.

### Skeleton Single-Writer Rule
Backfill becomes read-only once agent loop begins; agent is sole writer of skeleton rows.

### Probes + TTL Schema
Agent-discovered-but-unowned entries receive:
- probe: { source: provider_name, turn: n }
- ttl_days: 30
Expiry only when no owned evidence, no provider confirmation, and TTL expires.

## 3. Phase‑1 Sequencing
### Step 1 — RT‑1b (agentic substrate)
Create agentic_hooks.py:
- begin_turn(context)
- record_tool_call(provider, query, raw_result)
- record_reasoning_step(step_dict)
- record_world_model_update(update_dict)
- end_turn(context)

### Step 2 — PB‑5 (shadow diagnostics)
Add diagnostics:
- shadow_probe(provider, query)
- shadow_confidence_trace(book)
- shadow_skeleton_merge_trace(before, after)
- shadow_gate_trace(book, gate_outcome)

### Step 3 — Agentic Loop (agentic_series_agent.py)
Deterministic loop:
1. Start turn
2. Select provider
3. Issue provider call
4. Normalize raw result
5. Apply confidence-engine
6. Apply delta-engine
7. Merge into skeleton
8. Record diagnostics
9. Decide next action
10. End turn

Provider escalation order:
1. Tavily
2. Exa
3. SerpAPI (eval-only)
4. Brave (fallback)

### Step 4 — World-Model Integration
- Use existing skeleton merge rules.
- Never overwrite owned data.
- Never drop confidence grades.
- Never mutate publication dates.
- Never reorder series numbers unless confidence-engine approves.

### Step 5 — Evaluation Harness
Create agentic_eval_harness.py + fixtures/agentic_eval/.
Harness must:
- Replay agentic turns deterministically.
- Compare world-model deltas.
- Compare provider traces.
- Compare confidence/delta routing.
- Detect regressions.
- Produce diff reports.

### Step 6 — Regression Tests
Add tests for:
- Provider escalation
- Author mismatch reconciliation
- Confidence table completeness
- Skeleton merge invariants
- TTL expiry
- Multi-turn reasoning
- Deterministic replay

## 4. Deterministic Invariants
- Provider determinism (same query → same escalation → same normalized result).
- Skeleton merge determinism (idempotent, order-independent, conflict-resolving).
- Confidence determinism (identical inputs → identical outputs).
- Turn determinism (identical inputs → identical world-model deltas).
- TTL determinism (deterministic expiry).

## 5. Agentic Follow-Up Behavior
Re-query when:
- Confidence is unverified
- Provider returns partial data
- Series numbering incomplete
- Publication dates conflict
- Author mismatch detected
- Skeleton merge unresolved

Stop querying when:
- All books have stable confidence
- Stable numbering
- Stable publication dates
- No provider returns new data
- TTL entries resolved

Escalate when:
- Primary provider insufficient
- Confidence-engine requests escalation
- Skeleton merge detects unresolved conflicts

## 6. Canonical Series-Page Discovery Rule
If provider returns canonical series page:
1. Scrape canonical page
2. Extract all books
3. Normalize numbering
4. Normalize publication dates
5. Merge into skeleton
6. Mark entries as series-match
7. Re-run confidence-engine
8. Re-run delta-engine
9. Re-run TTL logic

## 7. World-Model Consolidation Rules
### Ephemeral vs Durable
Durable skeleton updated only after full agentic turn completes.

### Apply Lag Fix
Skeleton apply must:
- Never swallow failures
- Never drop fields
- Never reorder books
- Never lose confidence grades

## 8. Diff-Ready Instructions for Cursor
Cursor must:
1. Create:
   - agentic_hooks.py
   - agentic_series_agent.py
   - agentic_eval_harness.py

2. Modify:
   - series_agent.py (wrap provider calls with RT‑1b)
   - provider_protocol.py (deterministic escalation)
   - confidence_engine.py (complete decision table)
   - delta_engine.py (deterministic routing)
   - skeleton.py (single-writer rule + merge invariants)

3. Add tests:
   - test_agentic_loop.py
   - test_provider_escalation.py
   - test_confidence_table.py
   - test_skeleton_merge.py
   - test_ttl_expiry.py

4. Add fixtures:
   - fixtures/agentic_eval/turn_*.json

5. Preserve all existing behavior.

## 9. Phase‑1 Completion Criteria
Phase 1 is complete when:
- Agentic loop exists
- RT‑1b implemented
- PB‑5 implemented
- Provider escalation deterministic
- Confidence table complete
- Author mismatch unified
- Skeleton merge deterministic
- TTL deterministic
- Evaluation harness running
- All regression tests passing
- World-model stable across runs
