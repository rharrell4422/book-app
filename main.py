from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from intelligence import compute_series_intelligence_for_series, lookup_book_summary, suggest_book_by_series
from importer.importer import run_import
from database import SessionLocal, engine
from agents.book_agent import BookAgent
import models
import schemas
import crud
from crud import (
    create_book,
    get_all_books,
    get_book,
    update_book,
    delete_book,
    create_series,
    get_all_series,
    get_series,
    update_series,
    delete_series,
)

# Create database tables
models.Base.metadata.create_all(bind=engine)

app = FastAPI()


class AgentRunRequest(BaseModel):
    title: str
    author: str | None = None


class AgentApproveRequest(BaseModel):
    metadata: dict

# Allow frontend to talk to backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*", "Content-Type"],
    max_age=3600,
)


@app.post("/agent/run")
def run_agent(payload: AgentRunRequest):
    agent = BookAgent()
    metadata = agent.run(payload.title, payload.author)
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=500, detail="BookAgent.run must return a metadata dict")
    return metadata


@app.post("/agent/approve", response_model=schemas.BookResponse)
def approve_agent(payload: AgentApproveRequest, db: Session = Depends(get_db)):
    try:
        approved_book = schemas.BookBase.model_validate(payload.metadata)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Invalid approved metadata: {exc}")

    return crud.create_book(db=db, book=approved_book)

# ---------------------------------------------------------
# Dependency: get DB session
# ---------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------
# SERIES ENDPOINTS
# ---------------------------------------------------------

@app.post("/series/", response_model=schemas.SeriesResponse)
def create_series(series: schemas.SeriesBase, db: Session = Depends(get_db)):
    return crud.create_series(db=db, series=series)


@app.get("/series/", response_model=List[schemas.SeriesResponse])
def read_series(db: Session = Depends(get_db)):
    return crud.get_all_series(db)

# NEW SERIES ID
@app.get("/series/{series_id}", response_model=schemas.SeriesDetailResponse)
def read_series_by_id(series_id: int, db: Session = Depends(get_db)):
    # 1. Load the series
    db_series = crud.get_series(db, series_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    # 2. Load all books for this series
    books = crud.get_books_by_series(db, series_id)

    # 3. Sort books by book_number
    sorted_books = sorted(books, key=lambda b: (b.book_number or 0))
    series_author = db_series.author or next((book.author for book in sorted_books if book.author), None)

    # 4. Run intelligence engine
    intelligence = compute_series_intelligence_for_series(db, series_id)


        # 5. Return enriched response
    is_finished = bool(intelligence.get("is_series_finished"))

    return schemas.SeriesDetailResponse(
        id=db_series.id,
        name=db_series.name,
        author=series_author,
        description=db_series.description,
        genre=db_series.genre,
        tags=db_series.tags,
        is_finished=is_finished,
        total_books=intelligence["total_books"],
        series_status="finished" if is_finished else "ongoing",
        next_unread_book_number=intelligence.get("next_unread_book_number"),
        next_upcoming_book_number=intelligence.get("next_upcoming_book_number"),
        missing_books=intelligence["missing_orders"],
        created_at=db_series.created_at,
        updated_at=db_series.updated_at,
        books=[schemas.BookResponse.model_validate(book) for book in sorted_books]
    )


#BUFFER
@app.put("/series/{series_id}", response_model=schemas.SeriesResponse)
def update_series(series_id: int, series: schemas.SeriesBase, db: Session = Depends(get_db)):
    updated = crud.update_series(db, series_id, series)
    if not updated:
        raise HTTPException(status_code=404, detail="Series not found")
    return updated


@app.post("/series/{series_id}/mark_unfinished")
def mark_series_unfinished(series_id: int, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    books = crud.get_books_by_series(db, series_id)
    for book in books:
        book.is_series_finished = False

    db_series.is_finished = False
    db_series.series_status = "ongoing"

    db.commit()

    intelligence = compute_series_intelligence_for_series(db, series_id)
    is_finished = bool(intelligence.get("is_series_finished")) if intelligence else False

    return {
        "series_id": series_id,
        "updated_books": len(books),
        "is_finished": is_finished,
        "series_status": "finished" if is_finished else "ongoing",
    }


@app.post("/series/{series_id}/mark_finished")
def mark_series_finished(series_id: int, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")

    books = crud.get_books_by_series(db, series_id)
    for book in books:
        book.is_series_finished = True

    db_series.is_finished = True
    db_series.series_status = "finished"

    db.commit()

    intelligence = compute_series_intelligence_for_series(db, series_id)
    is_finished = bool(intelligence.get("is_series_finished")) if intelligence else False

    return {
        "series_id": series_id,
        "updated_books": len(books),
        "is_finished": is_finished,
        "series_status": "finished" if is_finished else "ongoing",
    }


@app.delete("/series/{series_id}")
def delete_series(series_id: int, db: Session = Depends(get_db)):
    deleted = crud.delete_series(db, series_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Series not found")
    return {"message": "Series deleted"}

# ---------------------------------------------------------
# BOOK ENDPOINTS
# ---------------------------------------------------------


@app.post("/books/", response_model=schemas.BookResponse)
def create_book(book: schemas.BookBase, db: Session = Depends(get_db)):
    return crud.create_book(db=db, book=book)


@app.get("/books/", response_model=List[schemas.BookResponse])
def read_books(db: Session = Depends(get_db)):
    return crud.get_all_books(db)


@app.get("/books/by_series/{series_id}", response_model=List[schemas.BookResponse])
def read_books_by_series(series_id: int, db: Session = Depends(get_db)):
    return crud.get_books_by_series(db, series_id)


@app.get("/books/lookup")
def lookup_book(title: str, author: str | None = None):
    return lookup_book_summary(title, author)


@app.get("/books/suggest", response_model=schemas.SuggestionResponse)
def suggest_book(series_name: str, book_number: int | None = None, author: str | None = None):
    return suggest_book_by_series(series_name, book_number, author)


@app.get("/books/{book_id}", response_model=schemas.BookResponse)
def read_book_by_id(book_id: int, db: Session = Depends(get_db)):
    db_book = crud.get_book(db, book_id)
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")
    return db_book


@app.put("/books/{book_id}", response_model=schemas.BookResponse)
def put_book(book_id: int, book: schemas.BookUpdate, db: Session = Depends(get_db)):
    updated = crud.update_book(db, book_id, book)
    if not updated:
        raise HTTPException(status_code=404, detail="Book not found")
    return updated


@app.post("/books/{book_id}/summary")
def fetch_and_save_book_summary(book_id: int, db: Session = Depends(get_db)):
    db_book = crud.get_book(db, book_id)
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")

    summary_result = lookup_book_summary(db_book.title, db_book.author)
    if summary_result.get("found") and summary_result.get("summary"):
        db_book.auto_summary = summary_result.get("summary")
        db.commit()
        db.refresh(db_book)

    return {
        "book": schemas.BookResponse.model_validate(db_book),
        "lookup": summary_result,
    }


@app.patch("/books/{book_id}", response_model=schemas.BookResponse)
def patch_book(book_id: int, book: schemas.BookUpdate, db: Session = Depends(get_db)):
    updated = crud.update_book(db, book_id, book)
    if not updated:
        raise HTTPException(status_code=404, detail="Book not found")
    return updated


@app.delete("/books/{book_id}")
def delete_book(book_id: int, db: Session = Depends(get_db)):
    deleted = crud.delete_book(db, book_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Book not found")
    return {"message": "Book deleted"}

# ---------------------------------------------------------
# IMPORT
# ---------------------------------------------------------
@app.post("/import")
def trigger_import():
    file_path = "Test_LibraryImport_new_fields_28Jun2026.xlsx"

    try:
        run_import(file_path)
        return {"status": "success"}
    except Exception as e:
        import traceback
        traceback.print_exc()   # <-- forces full traceback to terminal
        raise e
