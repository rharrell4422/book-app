import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bootstrap import (
    backfill_series_state,
    clear_stale_ghost_flags_on_read_books,
    run_migrations,
)
from routers import admin, auth, books, imports, series

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


app = FastAPI(lifespan=lifespan)

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
app.include_router(series.router)
app.include_router(books.router)
app.include_router(imports.router)
