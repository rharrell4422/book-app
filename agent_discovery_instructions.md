# Agent Discovery Instructions

## 🚫 DO NOT RUN DISCOVERY IN THE VSCC SANDBOX

Do not execute any provider discovery, multi-pass discovery, external API calls, or long-running background tasks inside the VSCC sandbox.

This includes:

- industry provider queries
- fallback provider queries (Goodreads, OpenLibrary, GoogleBooks)
- multi-pass discovery (exact, canonical, fuzzy, fallback)
- strict cleanup passes
- any task that touches external metadata sources

These operations must run only from my backend environment, where I have my own provider keys, quotas, and compute resources.

Inside VSCC:

- Write code
- Validate structure
- Validate imports
- Validate function signatures
- Validate integration points
- Run only lightweight, local, non-provider logic

I will run all discovery tests from my backend.

## 🧠 AGENT PIPELINE MUST PERFORM FULL DISCOVERY

The current agent pipeline (run_series_check) performs cleanup and normalization only.

It does not perform discovery.

We need the agent pipeline to:

1. Call the primary industry provider discovery functions that were built for the agent.
2. Use Goodreads/OpenLibrary/GoogleBooks only as fallback providers, as already implemented.
3. Run the full multi-pass discovery logic.
4. Detect missing books and added books.
5. Update series_check_jobs with progress and status for the full discovery workflow.
6. Return FE-compatible discovery results identical to the legacy pipeline.

This allows the agent pipeline to fully replace the legacy discovery engine.

## 📌 PERSISTENCE REQUIREMENT

Please store these instructions in a persistent location such as:

- @workspace
- or a file named agent_discovery_instructions.md

This ensures the rules are always visible and enforced every time I sign into a chat.
