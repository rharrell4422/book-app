import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bootstrap import (
    backfill_series_state,
    clear_stale_ghost_flags_on_read_books,
    run_migrations,
)
from routers import admin, admin_agentic, auth, books, discovery, imports, notifications, profiles, series
from services.skeleton_store import backfill_all_skeletons

logger = logging.getLogger(__name__)

# Bring the DB schema up to date (see bootstrap.run_migrations) before
# anything else touches it.
run_migrations()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One-time data repairs on boot, not a recurring loop despite the name
    # this used to have -- kept here rather than in bootstrap.py since it
    # needs to run against a live DB session each time the app starts.
    await asyncio.to_thread(clear_stale_ghost_flags_on_read_books)
    await asyncio.to_thread(backfill_series_state)
    # Phase 0 of agentic discovery: keeps series_skeleton in sync with the
    # library on every boot. agents/series_agent.py already reads this
    # table for delta/confidence routing on every Check Now run, so this
    # backfill is what keeps that read path from seeing a stale skeleton
    # for a series whose books changed outside of a Check Now (e.g. a
    # manual edit, an import, or a fresh series that's never been checked).
    await asyncio.to_thread(backfill_all_skeletons)
    # Phase 10 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
    # phase1_evaluation.md`, not re-litigated here): a one-time, fail-
    # soft sanity check that the agentic package is wired together
    # correctly -- see `agentic/invariants.py`'s own docstring for what
    # it actually checks. A violation is logged loudly but never raised
    # here: this must never prevent the app from starting, and this
    # check is also re-runnable on demand via `GET /admin/agentic/
    # startup-check` for the same reason (an admin shouldn't have to
    # restart the process just to re-check this).
    try:
        from agentic.invariants import enforce_agentic_invariants

        if not await asyncio.to_thread(enforce_agentic_invariants):
            logger.error("startup: agentic invariants check failed -- see agentic.invariants logs above for detail")
    except Exception:
        logger.exception("startup: agentic invariants check itself raised unexpectedly; continuing startup anyway")
    yield


# redirect_slashes=False: with it on (the default), a request to a collection
# route missing its registered trailing slash (e.g. POST /series instead of
# POST /series/) gets a 307 redirect back to add the slash. That's normally
# invisible, but the Next.js API proxy in front of this app can lose the
# client's original trailing slash before forwarding, and Node's fetch does
# not reliably replay non-GET requests with a body across that redirect --
# it just fails the request outright. Routers register both the slash and
# no-slash forms of collection routes (see routers/books.py, routers/series.py)
# so disabling the implicit redirect just means both forms are handled
# directly instead of one bouncing through a redirect that may not survive
# the proxy hop.
app = FastAPI(lifespan=lifespan, redirect_slashes=False)

# Allow frontend to talk to backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_origin_regex=r"^https?://.*$",
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*", "Content-Type"],
    max_age=3600,
)

app.include_router(auth.router)
app.include_router(admin.router)
# Phase 1 agentic discovery: read-only, owner-only diagnostics only (see
# routers/admin_agentic.py's module docstring) -- not linked from any
# user-facing UI, and every route it exposes is a thin pass-through to an
# already shadow-mode-only service function.
app.include_router(admin_agentic.router)
app.include_router(profiles.router)
app.include_router(series.router)
app.include_router(books.router)
app.include_router(imports.router)
app.include_router(discovery.router)
app.include_router(notifications.router)
