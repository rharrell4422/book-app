from datetime import date, datetime
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict

# ------------------------------------------------------------
# Profile schemas
# ------------------------------------------------------------


class ProfileResponse(BaseModel):
    id: str
    display_name: str
    is_default: bool
    created_at: datetime
    book_count: int = 0
    has_data: bool = False

    model_config = ConfigDict(from_attributes=True)


class ProfileCreate(BaseModel):
    id: str
    display_name: str


class ProfileUpdate(BaseModel):
    display_name: str


# ------------------------------------------------------------
# Book Schemas
# ------------------------------------------------------------

class BookBase(BaseModel):
    title: str
    author: str
    subtitle: Optional[str] = None
    series_id: Optional[int] = None
    series_order: Optional[float] = None
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
    source_url: Optional[str] = None
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


# Slim projection of BookResponse for list/card views (mobile, or any client
# that doesn't need all ~40 fields just to render a title/author/status
# row). Deliberately excludes heavy text fields (auto_summary, review,
# notes) and the long tail of external-id columns. cover_url is not a real
# column yet -- reserved here for the Phase 1.5 cover-art follow-up so this
# schema doesn't need another breaking change when that field lands.
class BookListItem(BaseModel):
    id: int
    title: str
    author: str
    series_id: Optional[int] = None
    series_name: Optional[str] = None
    book_number: Optional[float] = None
    read_status: Optional[str] = None
    is_read: Optional[bool] = None
    is_upcoming_final: Optional[bool] = None
    rating: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class BookUpdate(BaseModel):
    title: Optional[str] = None
    author: Optional[str] = None
    subtitle: Optional[str] = None
    series_id: Optional[int] = None
    series_order: Optional[float] = None
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
    source_url: Optional[str] = None
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


class SeriesState(BaseModel):
    has_new_books: bool = False
    has_new_available_books: bool = False
    has_new_upcoming_books: bool = False
    has_unread_books: bool = False
    has_upcoming_books: bool = False
    is_caught_up: bool = False

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
    has_new_books: Optional[bool] = None
    has_unread_books: Optional[bool] = None
    has_upcoming_books: Optional[bool] = None
    is_caught_up: Optional[bool] = None
    read_count: Optional[int] = None
    unread_count: Optional[int] = None
    title_normalization_mode_override: Optional[str] = None
    series_state: Optional[SeriesState] = None


class SeriesResponse(SeriesBase):
    id: int
    created_at: datetime
    updated_at: datetime
    books: List[BookResponse] = []
    series_state: SeriesState | None = None

    model_config = ConfigDict(from_attributes=True)

# Slim projection of SeriesResponse for list/card views -- notably, it never
# nests the series' books[] the way SeriesResponse does, which is what
# duplicates every book's full payload once per series on GET /series/.
# Full per-book detail is still available via GET /series/{id}.
class SeriesListItem(BaseModel):
    id: int
    name: str
    author: Optional[str] = None
    total_books: Optional[int] = None
    read_count: Optional[int] = None
    unread_count: Optional[int] = None
    is_finished: Optional[bool] = None
    has_new_books: Optional[bool] = None
    has_unread_books: Optional[bool] = None
    has_upcoming_books: Optional[bool] = None
    is_caught_up: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True)


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
    missing_books: list[int] | None = None
    has_new_books: bool = False
    has_unread_books: bool = False
    has_upcoming_books: bool = False
    is_caught_up: bool = False
    read_count: int = 0
    unread_count: int = 0
    title_normalization_mode_override: str | None = None
    series_state: SeriesState | None = None

    created_at: datetime
    updated_at: datetime

    # List of books in the series
    books: list[BookResponse]

    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------
# Series action request Schemas
# ------------------------------------------------------------

class NormalizeTitlesRequest(BaseModel):
    normalization_mode: str
    custom_pattern: str | None = None
    exclude_upcoming: bool = True


# ------------------------------------------------------------
# Import confirmation request Schemas
# ------------------------------------------------------------

class SeriesImportConfirmationDecision(BaseModel):
    book_id: int
    decision: Literal["yes", "no", "dont_know"]
    series_name: str | None = None
    note: str | None = None


class SeriesImportConfirmationResolveRequest(BaseModel):
    decisions: list[SeriesImportConfirmationDecision]


# ------------------------------------------------------------
# "More by this author" series overview request schema
# ------------------------------------------------------------

class SeriesOverviewBookInput(BaseModel):
    title: str
    description: str | None = None


class SeriesOverviewRequest(BaseModel):
    series_name: str
    author: str
    # Descriptions the frontend already has in memory from the discovery
    # call that produced this series group -- deliberately passed in rather
    # than re-fetched, so this endpoint costs exactly one LLM call and no
    # extra catalog API requests.
    books: list[SeriesOverviewBookInput]
