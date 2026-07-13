import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import models
from database import engine
from bootstrap import (
    backfill_series_state,
    clear_stale_ghost_flags_on_read_books,
    ensure_series_state_columns,
)
from routers import admin, auth, books, imports, series

# Create database tables
models.Base.metadata.create_all(bind=engine)
ensure_series_state_columns()

app = FastAPI()

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


@app.on_event("startup")
async def start_series_scan_loop() -> None:
    await asyncio.to_thread(clear_stale_ghost_flags_on_read_books)
    await asyncio.to_thread(backfill_series_state)
