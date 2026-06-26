from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from importer import import_from_file
from fastapi.middleware.cors import CORSMiddleware

from database import SessionLocal, engine
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

# Allow frontend to talk to backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

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

@app.post("/series/", response_model=schemas.Series)
def create_series(series: schemas.SeriesCreate, db: Session = Depends(get_db)):
    return crud.create_series(db=db, series=series)


@app.get("/series/", response_model=List[schemas.Series])
def read_series(db: Session = Depends(get_db)):
    return crud.get_all_series(db)


@app.get("/series/{series_id}", response_model=schemas.Series)
def read_series_by_id(series_id: int, db: Session = Depends(get_db)):
    db_series = crud.get_series(db, series_id)
    if not db_series:
        raise HTTPException(status_code=404, detail="Series not found")
    return db_series


@app.put("/series/{series_id}", response_model=schemas.Series)
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

# --- existing routes above ---

@app.post("/series/{series_id}/check-now")
async def check_now(series_id: int):
    """
    Manually trigger a check for new books in the series.
    """
    try:
        # Placeholder logic — replace with your real scraping/checking logic
        result = await check_for_new_books(series_id)
        return {"status": "success", "details": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def check_for_new_books(series_id: int):
    # Temporary placeholder logic
    return f"Checked series {series_id} for new books."

# ---------------------------------------------------------
# BOOK ENDPOINTS
# ---------------------------------------------------------

@app.post("/books/", response_model=schemas.Book)
def create_book(book: schemas.BookCreate, db: Session = Depends(get_db)):
    return crud.create_book(db=db, book=book)


@app.get("/books/", response_model=List[schemas.Book])
def read_books(db: Session = Depends(get_db)):
    return crud.get_all_books(db)


@app.get("/books/{book_id}", response_model=schemas.Book)
def read_book_by_id(book_id: int, db: Session = Depends(get_db)):
    db_book = crud.get_book(db, book_id)
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")
    return db_book


@app.patch("/books/{book_id}", response_model=schemas.Book)
def patch_book(book_id: int, book: schemas.BookUpdate, db: Session = Depends(get_db)):
    db_book = crud.get_book(db, book_id)
    if not db_book:
        raise HTTPException(status_code=404, detail="Book not found")

    # Only update fields that were provided
    if book.book_number is not None:
        db_book.book_number = book.book_number

    if book.is_read is not None:
        db_book.is_read = book.is_read

    # Add more fields here later if needed

    db.commit()
    db.refresh(db_book)
    return db_book



@app.delete("/books/{book_id}")
def delete_book(book_id: int, db: Session = Depends(get_db)):
    deleted = crud.delete_book(db, book_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Book not found")
    return {"message": "Book deleted"}

@app.post("/import")
def import_library(filename: str, db: Session = Depends(get_db)):
    summary = import_from_file(db, filename)
    return summary