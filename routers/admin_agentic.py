"""Phase 1 agentic discovery, tenth implementation block (extended in the
eleventh block with `/promotion`, `/series`, `/history`, in Phase 2's
kickoff block with `/promotion-plan`, and in Phase 2's dual-write block
with `/previews`): a read-only, owner-only admin router exposing
everything `services/agentic_evaluation_harness.py`/`services/agentic_
batch_orchestrator.py`/`services/agentic_promotion_checklist.py`/
`services/agentic_admin_ui_stubs.py`/`services/agentic_promotion_plan.py`/
`services/agentic_skeleton_preview_store.py` already built, for manual
triggering during evaluation -- not for end users.

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
read-only diagnostics and returns their output. `/promotion-plan` (Phase
2 kickoff) is the same story: a diagnostic plan of what promotion would
require, never a promotion mechanism itself -- there is still no Phase 2
switch anywhere in this codebase.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from fastapi.responses import HTMLResponse

from routers.deps import require_owner
from services.agentic_admin_ui_stubs import get_agentic_history, list_agentic_series
from services.agentic_batch_orchestrator import run_batch_agentic_evaluations
from services.agentic_evaluation_harness import generate_full_agentic_html, generate_full_agentic_report
from services.agentic_promotion_checklist import generate_promotion_readiness
from services.agentic_promotion_plan import build_phase2_promotion_plan
from services.agentic_skeleton_preview_store import get_agentic_skeleton_previews

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


@router.get("/promotion/{series_id}")
def admin_promotion_check(series_id: int) -> dict:
    """Runs a full shadow-mode agentic evaluation for one series and
    returns its Phase 2 promotion-readiness checklist
    (`services.agentic_promotion_checklist.generate_promotion_
    readiness`). NOT a promotion mechanism -- purely diagnostic.
    Read-only -- no writes.
    """
    return generate_promotion_readiness(series_id)


@router.get("/series")
def admin_list_agentic_series() -> dict:
    """Lists every series with a `SeriesSkeleton` row -- i.e. every
    series that has discovery data an agentic evaluation could run
    against (`services.agentic_admin_ui_stubs.list_agentic_series`).
    Read-only -- no writes.
    """
    return list_agentic_series()


@router.get("/history/{series_id}")
def admin_agentic_history(series_id: int) -> dict:
    """Returns historical agentic evaluation logs for one series
    (`services.agentic_admin_ui_stubs.get_agentic_history`) -- currently
    always empty with an explanatory note, since no persisted evaluation-
    history store exists yet (see that function's docstring). Read-only
    -- no writes.
    """
    return get_agentic_history(series_id)


@router.get("/promotion-plan/{series_id}")
def admin_promotion_plan(series_id: int) -> dict:
    """Returns the Phase 2 promotion plan for one series
    (`services.agentic_promotion_plan.build_phase2_promotion_plan`) --
    what a future live promotion would require and how close this
    series currently is to that bar. NOT a promotion mechanism -- purely
    diagnostic. Read-only -- no writes.
    """
    return build_phase2_promotion_plan(series_id)


@router.get("/dry-run/{series_id}")
def admin_agentic_dry_run(series_id: int) -> dict:
    """Returns the most recent dry-run agentic execution snapshot for one
    series (`agents/series_agent.py`'s `run_series_check` now runs the
    Phase 1 shadow loop once more, in parallel, on every live discovery
    turn -- see that function's own comment for the Phase 2 dual
    execution mode this powers). History is limited because `services/
    discovery_telemetry.record_agentic_dry_run` is a log-only fallback,
    not a queryable store (see `services.agentic_admin_ui_stubs.
    get_agentic_history`'s identical gap) -- this endpoint returns
    whatever that stub can honestly reconstruct today (an empty history
    with an explanatory note), not a fabricated snapshot. Read-only --
    no writes.
    """
    return get_agentic_history(series_id)


@router.get("/previews/{series_id}")
def admin_agentic_previews(series_id: int) -> dict:
    """Returns every stored agentic skeleton preview for one series
    (`services.agentic_skeleton_preview_store.get_agentic_skeleton_
    previews`) -- the Phase 2 dual-write shadow table
    (`agentic_skeleton_previews`) that `agents/series_agent.py`'s
    dry-run block appends one row to on every live discovery turn.
    Entirely separate from the live `series_skeleton` table. Read-only
    -- no writes.
    """
    return {"series_id": series_id, "previews": get_agentic_skeleton_previews(series_id)}
