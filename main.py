import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bootstrap import (
    backfill_series_state,
    clear_stale_ghost_flags_on_read_books,
    run_migrations,
)
from routers import admin, auth, books, imports, profiles, series

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
app.include_router(profiles.router)
app.include_router(series.router)
app.include_router(books.router)
app.include_router(imports.router)
