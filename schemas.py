from datetime import date, datetime
from typing import List, Optional
from pydantic import BaseModel
from pydantic import BaseModel, ConfigDict, Field

# ------------------------------------------------------------
# Book Schemas
# ------------------------------------------------------------

class BookBase(BaseModel):
    title: str
    author: str
    subtitle: Optional[str] = None
    series_id: Optional[int] = None
    series_order: Optional[int] = None
    book_number: Optional[float] = None
    publication_date: Optional[date] = None
    publisher: Optional[str] = None
    edition: Optional[str] = None
    format: Optional[str] = None
    pages: Optional[int] = None
    language: Optional[str] = None
    release_date: Optional[date] = None
    read_date: Optional[date] = None
    isbn: Optional[str] = None
    isbn13: Optional[str] = None
    asin: Optional[str] = None
    google_books_id: Optional[str] = None
    goodreads_id: Optional[str] = None
    storygraph_id: Optional[str] = None
    auto_summary: Optional[str] = None
    date_added: Optional[date] = None
    date_started: Optional[date] = None
    date_finished: Optional[date] = None
    read_status: Optional[str] = None
    rating: Optional[int] = None
    is_read: Optional[bool] = None
    external_rating: Optional[float] = None
    external_rating_count: Optional[int] = None
    review: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[list] = None
    series_name: Optional[str] = None
    is_upcoming_auto: Optional[bool] = None
    is_upcoming_final: Optional[bool] = None
    is_missing: Optional[bool] = None
    record_status: Optional[str] = None


##

class BookResponse(BookBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BookUpdate(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    subtitle: Optional[str] = None
    series_id: Optional[int] = None
    series_order: Optional[int] = None
    book_number: Optional[float] = None
    publication_date: Optional[date] = None
    publisher: Optional[str] = None
    edition: Optional[str] = None
    format: Optional[str] = None
    pages: Optional[int] = None
    language: Optional[str] = None
    release_date: Optional[date] = None
    read_date: Optional[date] = None
    isbn: Optional[str] = None
    isbn13: Optional[str] = None
    asin: Optional[str] = None
    google_books_id: Optional[str] = None
    goodreads_id: Optional[str] = None
    storygraph_id: Optional[str] = None
    auto_summary: Optional[str] = None
    date_added: Optional[date] = None
    date_started: Optional[date] = None
    date_finished: Optional[date] = None
    read_status: Optional[str] = None
    rating: Optional[int] = None
    is_read: Optional[bool] = None
    external_rating: Optional[float] = None
    external_rating_count: Optional[int] = None
    review: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[list] = None
    series_name: Optional[str] = None
    is_upcoming_auto: Optional[bool] = None
    is_upcoming_final: Optional[bool] = None
    is_missing: Optional[bool] = None
    record_status: Optional[str] = None


# ------------------------------------------------------------
# Series Schemas
# ------------------------------------------------------------

class SeriesBase(BaseModel):
    name: str
    author: Optional[str] = None
    description: Optional[str] = None
    genre: Optional[str] = None
    tags: Optional[list] = None
    is_finished: Optional[bool] = None
    total_books: Optional[int] = None
    series_status: Optional[str] = None
    next_unread_book_number: Optional[float] = None
    next_upcoming_book_number: Optional[float] = None
    missing_books: Optional[list] = None


class SeriesResponse(SeriesBase):
    id: int
    created_at: datetime
    updated_at: datetime
    books: List[BookResponse] = []

    class Config:
        orm_mode = True

class SeriesDetailResponse(BaseModel):
    id: int
    name: str
    author: str | None = None
    description: str | None = None
    genre: str | None = None
    tags: list[str] | None = None

    # Intelligence fields
    is_finished: bool
    total_books: int
    series_status: str
    next_unread_book_number: float | None = None
    next_upcoming_book_number: float | None = None
    missing_books: list[str] | None = None

    created_at: datetime
    updated_at: datetime

    # List of books in the series
    books: list[BookResponse]

    class Config:
        from_attributes = True


# ------------------------------------------------------------
# Suggestion Schemas
# ------------------------------------------------------------

class SuggestionRecord(BaseModel):
    title: str
    author: str | None = None
    year: str | int | None = None
    description: str | None = None
    source_url: str | None = None
    series_name: str | list[str] | None = None
    series_position: int | str | float | None = None
    source: str | None = None


class SuggestionStageDiagnostic(BaseModel):
    stage: str
    provider: str
    query: str
    raw_count: int = 0
    accepted_count: int = 0


class SuggestionDiagnostics(BaseModel):
    selected_stage: str | None = None
    provider_counts: dict[str, int] = Field(default_factory=dict)
    stages: list[SuggestionStageDiagnostic] = Field(default_factory=list)
    accepted_total: int = 0


class SuggestionResponse(BaseModel):
    query: str
    results: list[SuggestionRecord]
    diagnostics: SuggestionDiagnostics | None = None
