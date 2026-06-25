from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from database import SessionLocal, engine
from fastapi.middleware.cors import CORSMiddleware

import models
import crud
import schemas

from crud import check_series_for_new_books
from intelligence import check_book_for_series
from schemas import SeriesIntelligenceResponse

models.Base.metadata.create_all(bind=engine)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3000/",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------
# DB SESSION DEPENDENCY
# ---------------------------------------------------------

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------
# BOOK ENDPOINTS
# ---------------------------------------------------------

@app.post("/books/", response_model=schemas.BookResponse)
def create_book(book: schemas.BookCreate, db: Session = Depends(get_db)):
    return crud.create_book(db, book)


@app.get("/books/{book_id}", response_model=schemas.BookResponse)
def get_book(book_id: int, db: Session = Depends(get_db)):
    db_book = crud.get_book(db, book_id)
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")
    return db_book


@app.get("/books/", response_model=list[schemas.BookResponse])
def get_all_books(db: Session = Depends(get_db)):
    return crud.get_all_books(db)


@app.put("/books/{book_id}", response_model=schemas.BookResponse)
def update_book(book_id: int, book: schemas.BookUpdate, db: Session = Depends(get_db)):
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


# ⭐ NEW: BOOK-LEVEL CHECK ENDPOINT (D3 SMART DETECTION)
@app.post("/books/{book_id}/check", response_model=SeriesIntelligenceResponse)
def check_book_now(book_id: int, db: Session = Depends(get_db)):
    """
    Detects if a single book belongs to a series.
    If multiple book numbers are found online, auto-creates a new Series,
    links the book, adds detected books, and returns updated intelligence.
    """
    intelligence = check_book_for_series(db, book_id)
    if not intelligence:
        raise HTTPException(status_code=404, detail="Book not found or no series detected")
    return intelligence


# ---------------------------------------------------------
# SERIES ENDPOINTS
# ---------------------------------------------------------

@app.post("/series/", response_model=schemas.SeriesResponse)
def create_series(series: schemas.SeriesCreate, db: Session = Depends(get_db)):
    return crud.create_series(db, series)


@app.get("/series/{series_id}", response_model=schemas.SeriesResponse)
def get_series(series_id: int, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")
    return db_series


@app.get("/series/", response_model=list[schemas.SeriesResponse])
def get_all_series(db: Session = Depends(get_db)):
    return crud.get_all_series(db)


@app.put("/series/{series_id}", response_model=schemas.SeriesResponse)
def update_series(series_id: int, series: schemas.SeriesUpdate, db: Session = Depends(get_db)):
    updated = crud.update_series(db, series_id, series)
    if not updated:
        raise HTTPException(status_code=404, detail="Series not found")
    return updated


@app.delete("/series/{series_id}")
def delete_series(series_id: int, db: Session = Depends(get_db)):
    deleted = crud.delete_series(db, series_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Series not found")
    return {"message": "Series deleted"}


# ---------------------------------------------------------
# SERIES INTELLIGENCE
# ---------------------------------------------------------

@app.post("/series/{series_id}/recompute", response_model=schemas.SeriesIntelligenceResponse)
def recompute_series(series_id: int, db: Session = Depends(get_db)):
    result = crud.recompute_series_intelligence(db, series_id)
    if not result:
        raise HTTPException(status_code=404, detail="Series not found")
    return result


@app.post("/series/{series_id}/check", response_model=SeriesIntelligenceResponse)
def check_series_now(series_id: int, db: Session = Depends(get_db)):
    intelligence = check_series_for_new_books(db, series_id)
    if not intelligence:
        raise HTTPException(status_code=404, detail="Series not found")
    return intelligence


# ---------------------------------------------------------
# BOOKS BY SERIES
# ---------------------------------------------------------

@app.get("/books/by_series/{series_id}", response_model=list[schemas.BookResponse])
def get_books_by_series(series_id: int, db: Session = Depends(get_db)):
    books = db.query(models.Book).filter(models.Book.series_id == series_id).all()
    return books
