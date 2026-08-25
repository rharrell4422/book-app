"""Phase 1 agentic discovery, tenth implementation block (extended in the
eleventh block with `/promotion`, `/series`, `/history`, in Phase 2's
kickoff block with `/promotion-plan`, in Phase 2's skeleton dual-write
block with `/previews`, in Phase 2's final scaffolding block with
`/confidence`, `/gate`, in Phase 3 with `/promotion-history`, and in
Phase 4 with `/activation-preview`, `/activation-status`): a read-only,
owner-only admin router exposing everything `services/agentic_
evaluation_harness.py`/`services/agentic_batch_orchestrator.py`/
`services/agentic_promotion_checklist.py`/`services/agentic_admin_ui_
stubs.py`/`services/agentic_promotion_plan.py`/`services/agentic_
skeleton_preview_store.py`/`services/agentic_confidence_gate_store.py`/
`services/agentic_promotion_evaluator.py`/`settings.py` already built,
for manual triggering during evaluation -- not for end users.

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
2 kickoff), `/promotion-history` (Phase 3), and `/activation-preview`/
`/activation-status` (Phase 4) are the same story: none of them is a
promotion/activation mechanism itself, just a read-only view of what
`agents/series_agent.py`'s feature-flagged live routing layer (gated by
`settings.AGENTIC_ROUTING_ENABLED`/`settings.is_agentic_activated`) has
decided/recorded/would-hypothetically-do so far.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends
from fastapi.responses import HTMLResponse

import settings
from routers.deps import require_owner
from services.agentic_admin_ui_stubs import get_agentic_history, list_agentic_series
from services.agentic_batch_orchestrator import run_batch_agentic_evaluations
from services.agentic_confidence_gate_store import get_agentic_confidence_history, get_agentic_gate_history
from services.agentic_evaluation_harness import generate_full_agentic_html, generate_full_agentic_report
from services.agentic_health import compute_agentic_health
from services.agentic_promotion_checklist import generate_promotion_readiness
from services.agentic_promotion_evaluator import build_activation_preview, get_promotion_history
from services.agentic_promotion_plan import build_phase2_promotion_plan
from services.agentic_skeleton_preview_store import get_agentic_skeleton_previews
from services.discovery_telemetry import get_agentic_metrics

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


@router.get("/confidence/{series_id}")
def admin_agentic_confidence(series_id: int) -> dict:
    """Returns every stored shadow confidence decision for one series
    (`services.agentic_confidence_gate_store.get_agentic_confidence_
    history`) -- the Phase 2 dual-write shadow table
    (`agentic_confidence_decisions`) pairing each traced book's live
    confidence against the shadow loop's confidence for the same book,
    on every live discovery turn. Entirely separate from live
    `confidence_engine.py`/`SeriesSkeleton.skeleton_json`. Read-only --
    no writes.
    """
    return {"series_id": series_id, "confidence_history": get_agentic_confidence_history(series_id)}


@router.get("/gate/{series_id}")
def admin_agentic_gate(series_id: int) -> dict:
    """Returns every stored shadow gate decision for one series
    (`services.agentic_confidence_gate_store.get_agentic_gate_history`)
    -- the Phase 2 dual-write shadow table (`agentic_gate_decisions`)
    pairing each traced book's live `belongs-to-series` gate outcome
    against the shadow loop's gate outcome for the same book, on every
    live discovery turn. Entirely separate from the live
    `evaluate_belongs_to_series_gate` logic. Read-only -- no writes.
    """
    return {"series_id": series_id, "gate_history": get_agentic_gate_history(series_id)}


@router.get("/promotion-history/{series_id}")
def admin_agentic_promotion(series_id: int) -> dict:
    """Returns every stored Phase 3 candidate-promotion decision for one
    series (`services.agentic_promotion_evaluator.get_promotion_
    history`) -- the shadow table (`agentic_promotion_decisions`)
    `agents/series_agent.py`'s live routing path writes to, one row per
    traced book per turn, only when `settings.AGENTIC_ROUTING_ENABLED`
    is on. Entirely separate from `SeriesSkeleton.skeleton_json`.
    Read-only -- no writes.

    Deliberately named `/promotion-history/{series_id}`, not
    `/promotion/{series_id}` -- that path is already taken by
    `admin_promotion_check` above (the Phase 1/2 promotion-*readiness*
    checklist, a different, older diagnostic with no relation to this
    endpoint's per-book promotion outcomes; both are read-only and
    "promotion" here means the Phase 3 feature this whole module is
    named after, not a conflict worth renaming the older route over).
    """
    return {"series_id": series_id, "promotion_history": get_promotion_history(series_id)}


@router.get("/activation-preview/{series_id}")
def admin_agentic_activation_preview(series_id: int) -> dict:
    """Shows what live routing's resolved confidence/gate would look
    like for this series *if* Phase 4 activation were on -- computed
    from the most recent stored promotion decision per book
    (`services.agentic_promotion_evaluator.build_activation_preview`),
    regardless of whether the series is actually activated right now
    (that real state is also returned, under `"activated"`, for
    comparison). Never calls a live provider, never calls `run_agentic_
    turn`, never writes anything. Read-only -- no writes.
    """
    preview = build_activation_preview(series_id)
    return {"series_id": series_id, "activated": preview["activated"], "preview": preview["preview"]}


@router.get("/activation-status")
def admin_agentic_activation_status() -> dict:
    """Shows every series_id currently allowlisted by `settings.
    AGENTIC_SERIES_ACTIVATION` -- the raw Phase 4 activation env var,
    parsed the same way `settings.is_agentic_activated` does. Does not
    also require `settings.AGENTIC_ROUTING_ENABLED` to be on to appear
    here (unlike `is_agentic_activated` itself) -- this endpoint answers
    "what does the allowlist say", not "is agentic routing actually
    live for this series right now" (see `/activation-preview` for the
    latter, per series). Read-only -- no writes.
    """
    activated_series = set()
    for raw in settings.AGENTIC_SERIES_ACTIVATION.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            activated_series.add(int(raw))
        except ValueError:
            continue
    return {"activated_series": sorted(activated_series)}


@router.get("/metrics")
def admin_agentic_metrics() -> dict:
    """Phase 9 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
    phase1_evaluation.md`, not re-litigated here): returns every
    process-wide, in-memory agentic counter (`services.discovery_
    telemetry.get_agentic_metrics`) -- promotion attempts/outcomes,
    safety violations, Phase 8 cache hits/misses, and `run_agentic_turn`
    invocations/failures. Purely observational: reading this never
    resets or otherwise mutates the counters. Read-only -- no writes.
    """
    return {"metrics": get_agentic_metrics()}


@router.get("/health/{series_id}")
def admin_agentic_health(series_id: int) -> dict:
    """Phase 9: returns one series' agentic health summary
    (`services.agentic_health.compute_agentic_health`) -- promotion
    outcome counts, this process's global safety-violation count, this
    series' real current activation state, and a determinism sanity
    flag. Never calls a live provider, never calls `run_agentic_turn`,
    never writes anything. Read-only -- no writes.
    """
    return {"series_id": series_id, "health": compute_agentic_health(series_id)}


@router.get("/summary")
def admin_agentic_summary() -> dict:
    """Phase 9: a global, at-a-glance rollup of agentic activity across
    every series -- the current activation allowlist (same parsing as
    `/activation-status` above) alongside a handful of process-wide
    counters (`services.discovery_telemetry.get_agentic_metrics`).
    Read-only -- no writes.
    """
    activated_series = set()
    for raw in settings.AGENTIC_SERIES_ACTIVATION.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            activated_series.add(int(raw))
        except ValueError:
            continue

    metrics = get_agentic_metrics()
    return {
        "activated_series": sorted(activated_series),
        "total_promotions": metrics.get("agentic_promotion_attempts", 0),
        "total_safety_violations": metrics.get("agentic_safety_violations", 0),
        "agentic_turn_invocations": metrics.get("agentic_turn_invocations", 0),
    }
