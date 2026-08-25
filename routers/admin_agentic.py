"""Phase 1 agentic discovery, tenth implementation block: a read-only,
owner-only admin router exposing everything `services/agentic_evaluation_
harness.py`/`services/agentic_batch_orchestrator.py` already built, for
manual triggering during evaluation -- not for end users.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(settled architecture, not re-litigated here): every route below is a thin
pass-through to an already shadow-mode-only, read-only service function
(see those modules' own docstrings for the no-write guarantees this
router adds no new surface on top of). This is the *first* Phase 1 block
wired into `main.py` at all -- deliberately scoped to `/admin/agentic/*`,
owner-auth-gated the same way every other `/admin/*` route already is
(`routers.deps.require_owner` -- see `routers/admin.py` for the existing
pattern this mirrors), and never linked from any user-facing UI. Nothing
here writes to the database, calls a live provider, or changes routing/
confidence/gate/skeleton behavior -- it only ever triggers the existing
read-only diagnostics and returns their output.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from fastapi.responses import HTMLResponse

from routers.deps import require_owner
from services.agentic_batch_orchestrator import run_batch_agentic_evaluations
from services.agentic_evaluation_harness import generate_full_agentic_html, generate_full_agentic_report

# Router-level dependency (not per-route): every route here is read-only
# diagnostics, so unlike routers/admin.py (which has one route needing a
# different, more restricted auth path -- see that module's own comment),
# there's no route in this router that should ever be reachable by
# anything less than full owner auth.
router = APIRouter(prefix="/admin/agentic", tags=["admin-agentic"], dependencies=[Depends(require_owner)])


@router.get("/evaluate/{series_id}")
def admin_evaluate_series(series_id: int) -> dict:
    """Runs a full shadow-mode agentic evaluation for one series and
    returns the consolidated JSON report (`services.agentic_evaluation_
    harness.generate_full_agentic_report`). Read-only -- no writes.
    """
    return generate_full_agentic_report(series_id)


@router.get("/evaluate/{series_id}/html")
def admin_evaluate_series_html(series_id: int) -> HTMLResponse:
    """Same evaluation as `admin_evaluate_series` above, but returns the
    HTML-style string rendering (`services.agentic_evaluation_harness.
    generate_full_agentic_html`) instead of JSON. Served as `text/html`
    (rather than a JSON-quoted string) so it's directly viewable in a
    browser -- the whole point of an admin-only "html" variant -- while
    the underlying content is unchanged and just as safe to embed
    elsewhere (see `services/agentic_report_generator.py`'s escaping).
    Read-only -- no writes.
    """
    return HTMLResponse(content=generate_full_agentic_html(series_id))


@router.post("/batch")
def admin_batch_evaluation(series_ids: list[int] = Body(...)) -> dict:
    """Runs a full shadow-mode agentic evaluation for each series in
    `series_ids` (`services.agentic_batch_orchestrator.
    run_batch_agentic_evaluations`) and returns the aggregated batch
    report. Read-only -- no writes.
    """
    return run_batch_agentic_evaluations(series_ids)
