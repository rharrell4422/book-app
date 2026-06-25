from datetime import date
from pydantic import BaseModel
from typing import Optional, List


# -----------------------------
# Base Book Schema
# -----------------------------
class BookBase(BaseModel):
    title: str
    author: str
    year: Optional[int] = None
    genre: Optional[str] = None
    series_id: Optional[int] = None
    book_number: Optional[float] = None
    series_total_books: Optional[int] = None
    release_date: Optional[date] = None
    is_read: bool = False
    read_date: Optional[date] = None
    is_upcoming: bool = False
    is_upcoming_auto: bool = False


# -----------------------------
# Book Create Schema
# -----------------------------
class BookCreate(BookBase):
    pass


# -----------------------------
# Book Update Schema
# -----------------------------
class BookUpdate(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    year: Optional[int] = None
    genre: Optional[str] = None
    series_id: Optional[int] = None
    book_number: Optional[float] = None
    series_total_books: Optional[int] = None
    release_date: Optional[date] = None
    is_read: Optional[bool] = None
    read_date: Optional[date] = None
    is_upcoming: Optional[bool] = None
    is_upcoming_auto: Optional[bool] = None


# -----------------------------
# Series Schemas
# -----------------------------
class SeriesBase(BaseModel):
    name: str
    author: str
    check_url: Optional[str] = None
    series_finished: bool = False
    check_series: bool = True
    last_checked: Optional[date] = None
    series_total_books_manual: Optional[int] = None


class SeriesCreate(SeriesBase):
    pass


class SeriesUpdate(BaseModel):
    name: Optional[str] = None
    author: Optional[str] = None
    check_url: Optional[str] = None
    series_finished: Optional[bool] = None
    check_series: Optional[bool] = None
    last_checked: Optional[date] = None
    series_total_books_manual: Optional[int] = None


class SeriesIntelligenceResponse(BaseModel):
    series_id: int
    name: str
    author: str

    is_complete: bool
    is_ongoing: bool

    next_unread_book_number: Optional[float]
    next_unread_book_id: Optional[int]

    next_upcoming_book_number: Optional[float]
    next_upcoming_release_date: Optional[date]

    missing_books: Optional[str] = None

    series_total_books_manual: Optional[int]
    series_total_books_auto: Optional[int]
    series_total_books_final: Optional[int]

    class Config:
        orm_mode = True


class BookResponse(BaseModel):
    id: int
    title: str
    author: str
    year: Optional[int] = None
    genre: Optional[str] = None
    series_id: Optional[int] = None
    book_number: Optional[float] = None
    series_total_books: Optional[int] = None
    release_date: Optional[date] = None
    is_read: bool
    read_date: Optional[date] = None
    is_upcoming: bool
    is_upcoming_auto: bool
    is_upcoming_final: bool

    class Config:
        orm_mode = True


class SeriesResponse(BaseModel):
    id: int
    name: str
    author: str
    check_url: Optional[str] = None
    series_finished: Optional[bool] = None
    check_series: Optional[bool] = None
    last_checked: Optional[date] = None

    series_total_books_manual: Optional[int] = None
    series_total_books_auto: Optional[int] = None
    series_total_books_final: Optional[int] = None

    is_complete: Optional[bool] = None
    is_ongoing: Optional[bool] = None

    next_unread_book_number: Optional[float] = None
    next_unread_book_id: Optional[int] = None

    next_upcoming_book_number: Optional[float] = None
    next_upcoming_release_date: Optional[date] = None

    missing_books: Optional[str] = None

    class Config:
        orm_mode = True
