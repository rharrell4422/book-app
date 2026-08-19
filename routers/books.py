from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import crud
import schemas
from agents.series_agent import discover_more_by_author, discover_series_by_name
from crud.books import BookNumberRequiresSeriesError, InvalidSeriesForProfileError
from discovery_engine import generate_series_overview
from intelligence import lookup_book_summary
from routers.deps import enforce_access, get_current_profile_id, get_db

router = APIRouter(prefix="/books", tags=["books"], dependencies=[Depends(enforce_access)])


# Registered at both "/" and "" (see main.py's redirect_slashes=False) so a
# request that arrives without its trailing slash -- e.g. from a proxy that
# normalized it away -- is handled directly instead of needing a redirect.
@router.post("/", response_model=schemas.BookResponse)
@router.post("", response_model=schemas.BookResponse, include_in_schema=False)
def create_book(book: schemas.BookBase, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    try:
        return crud.create_book(db=db, book=book, profile_id=profile_id)
    except InvalidSeriesForProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except BookNumberRequiresSeriesError:
        raise HTTPException(status_code=400, detail="Book number requires a series.")


@router.get("/", response_model=List[schemas.BookResponse])
@router.get("", response_model=List[schemas.BookResponse], include_in_schema=False)
def read_books(db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    return crud.get_all_books(db, profile_id)


# Slim, paginated variant of read_books() for clients that only need
# list/card fields (mobile, future infinite-scroll views) -- avoids shipping
# every book's full ~40-field payload (auto_summary, review, notes, every
# external id) just to render a title/author/status row. Additive: existing
# callers of GET /books/ are completely unaffected.
@router.get("/light", response_model=List[schemas.BookListItem])
def read_books_light(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    profile_id: str = Depends(get_current_profile_id),
):
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")
    return crud.get_all_books(db, profile_id, limit=limit, offset=offset)


@router.get("/by_series/{series_id}", response_model=List[schemas.BookResponse])
def read_books_by_series(series_id: int, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    return crud.get_books_by_series(db, series_id, profile_id)


@router.get("/lookup")
def lookup_book(title: str, author: str | None = None):
    return lookup_book_summary(title, author)


# "More by this author" -- synchronous and lightweight by design (one query
# per catalog API plus at most one web search, no lookahead queries), so no
# background job/polling is needed the way series check needs one.
@router.get("/discover_by_author")
def discover_by_author(author: str, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    return discover_more_by_author(db, author, profile_id)


# Deeper, targeted follow-up to discover_by_author -- called on demand (a
# "Find the rest of this series" button) when that broad sweep's own
# maturity data suggests it only found part of a series (e.g. Hardcover
# says 6 books exist but the broad pass only turned up 1). Not run
# automatically for every "new series" group found, since the deeper
# per-series search costs more than the broad pass.
@router.get("/discover_series_by_name")
def discover_series_by_name_endpoint(
    series_name: str, author: str, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)
):
    return discover_series_by_name(db, series_name, author, profile_id)


# On-demand only -- called from a "Series Overview" button click in the
# "More by this author" dialog, never fetched automatically during
# discovery. Takes the descriptions the frontend already has in memory
# (from the same discover_by_author response) instead of re-querying any
# catalog API, so this costs exactly one LLM call per click.
@router.post("/series_overview")
def series_overview(request: schemas.SeriesOverviewRequest):
    overview = generate_series_overview(
        request.series_name, request.author, [book.model_dump() for book in request.books]
    )
    return {"overview": overview}


@router.get("/{book_id}", response_model=schemas.BookResponse)
def read_book_by_id(book_id: int, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    db_book = crud.get_book(db, book_id, profile_id)
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")
    return db_book


@router.put("/{book_id}", response_model=schemas.BookResponse)
def put_book(
    book_id: int, book: schemas.BookUpdate, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)
):
    try:
        updated = crud.update_book(db, book_id, book, profile_id)
    except InvalidSeriesForProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except BookNumberRequiresSeriesError:
        raise HTTPException(status_code=400, detail="Book number requires a series.")
    if not updated:
        raise HTTPException(status_code=404, detail="Book not found")
    return updated


@router.post("/{book_id}/summary")
def fetch_and_save_book_summary(book_id: int, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    db_book = crud.get_book(db, book_id, profile_id)
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")

    series_name = None
    if db_book.series_id is not None:
        db_series = crud.get_series(db, db_book.series_id, profile_id)
        series_name = db_series.name if db_series else None

    summary_result = lookup_book_summary(db_book.title, db_book.author, db_book.book_number, series_name)
    if summary_result.get("found") and summary_result.get("summary"):
        db_book.auto_summary = summary_result.get("summary")
        db.commit()
        db.refresh(db_book)

    return {
        "book": schemas.BookResponse.model_validate(db_book),
        "lookup": summary_result,
    }


@router.patch("/{book_id}", response_model=schemas.BookResponse)
def patch_book(
    book_id: int, book: schemas.BookUpdate, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)
):
    try:
        updated = crud.update_book(db, book_id, book, profile_id)
    except InvalidSeriesForProfileError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except BookNumberRequiresSeriesError:
        raise HTTPException(status_code=400, detail="Book number requires a series.")
    if not updated:
        raise HTTPException(status_code=404, detail="Book not found")
    return updated


@router.delete("/{book_id}")
def delete_book(book_id: int, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    deleted = crud.delete_book(db, book_id, profile_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Book not found")
    return {"message": "Book deleted"}
