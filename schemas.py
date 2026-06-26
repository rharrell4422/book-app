from datetime import date, datetime
from typing import Optional, List
from pydantic import BaseModel


# ---------------------------------------------------------
# SERIES SCHEMAS
# ---------------------------------------------------------

class SeriesBase(BaseModel):
    name: str
    total_books: Optional[int] = None
    is_finished: bool = False


class SeriesCreate(SeriesBase):
    pass


class SeriesUpdate(BaseModel):
    name: Optional[str] = None
    total_books: Optional[int] = None
    is_finished: Optional[bool] = None


# Forward declaration for nested relationship
class Book(BaseModel):
    id: int
    title: str
    author: Optional[str] = None
    isbn: Optional[str] = None
    format: Optional[str] = None
    publication_date: Optional[date] = None

    series_id: Optional[int] = None
    series_order: Optional[float] = None
    series_total_books: Optional[int] = None
    is_series_finished: Optional[bool] = None

    is_read: bool
    read_date: Optional[date] = None

    rating: Optional[float] = None
    notes: Optional[str] = None

    check_url: Optional[str] = None
    is_upcoming_auto: bool
    is_upcoming_final: bool

    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


class Series(SeriesBase):
    id: int
    books: List[Book] = []

    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True


# ---------------------------------------------------------
# BOOK SCHEMAS
# ---------------------------------------------------------

class BookBase(BaseModel):
    title: str
    author: Optional[str] = None
    isbn: Optional[str] = None
    format: Optional[str] = None
    publication_date: Optional[date] = None

    series_id: Optional[int] = None
    series_order: Optional[float] = None
    series_total_books: Optional[int] = None
    is_series_finished: Optional[bool] = None

    is_read: bool = False
    read_date: Optional[date] = None

    rating: Optional[float] = None
    notes: Optional[str] = None

    check_url: Optional[str] = None
    is_upcoming_auto: bool = False
    is_upcoming_final: bool = False


class BookCreate(BookBase):
    pass


class BookUpdate(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    isbn: Optional[str] = None
    format: Optional[str] = None
    publication_date: Optional[date] = None

    series_id: Optional[int] = None
    series_order: Optional[float] = None
    series_total_books: Optional[int] = None
    is_series_finished: Optional[bool] = None

    is_read: Optional[bool] = None
    read_date: Optional[date] = None

    rating: Optional[float] = None
    notes: Optional[str] = None

    check_url: Optional[str] = None
    is_upcoming_auto: Optional[bool] = None
    is_upcoming_final: Optional[bool] = None


class Book(BookBase):
    id: int

    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True
