"""Phase 10 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_
evaluation.md`, not re-litigated here): the stable, consolidated home for
this codebase's agentic decision-authority/safety/resolution/health
modules, moved here unchanged from `services/` where Phases 1-9 first
built them (see each module's own docstring for its own history --
nothing about *why* any of them exist changed in this move, only
*where* they live).

Deliberately a plain namespace package (no re-exports here) -- callers
import the specific submodule they need (`agentic.promotion_evaluator`,
`agentic.resolution`, `agentic.safety`, `agentic.confidence_gate_store`,
`agentic.cache`, `agentic.health`, `agentic.readiness`), exactly as they
previously imported `services.agentic_promotion_evaluator`, etc. Nothing
that still lives under `services/` (the shadow-write stores, the
evaluation harness, the admin UI stubs, `discovery_telemetry.py`, ...)
moved here -- only the six modules this package's own Phase 10 spec
named, plus the two new ones (`invariants.py`, `readiness.py`) Phase 10
itself adds.
"""

from __future__ import annotations
