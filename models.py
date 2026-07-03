from datetime import datetime, date
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    Date,
    DateTime,
    Float,
    Text,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.dialects.sqlite import JSON
from sqlalchemy.orm import relationship
from database import Base


class Series(Base):
    __tablename__ = "series"

    id = Column(Integer, primary_key=True, index=True)

    # Core
    name = Column(String, nullable=False)
    author = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    genre = Column(String, nullable=True)
    tags = Column(JSON, nullable=True)  # list of strings

    # Structure
    is_finished = Column(Boolean, default=False)
    total_books = Column(Integer, nullable=True)
    books_in_series = Column(JSON, nullable=True)  # list of book IDs
    series_status = Column(String, default="unknown")  # ongoing, completed, unknown

    # Intelligence
    next_unread_book_number = Column(Float, nullable=True)
    next_upcoming_book_number = Column(Float, nullable=True)
    missing_books = Column(JSON, nullable=True)  # list of book_numbers
    last_checked = Column(Date, nullable=True)
    has_new_books = Column(Boolean, default=False)
    has_unread_books = Column(Boolean, default=False)
    has_upcoming_books = Column(Boolean, default=False)
    is_caught_up = Column(Boolean, default=False)
    title_normalization_mode_override = Column(String, nullable=True)

    # External IDs
    goodreads_series_id = Column(String, nullable=True)
    storygraph_series_id = Column(String, nullable=True)

    # Importer metadata
    import_source = Column(String, nullable=True)
    import_raw_headers = Column(JSON, nullable=True)
    import_raw_row = Column(JSON, nullable=True)
    import_errors = Column(JSON, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    books = relationship("Book", back_populates="series")
    canonical_entries = relationship("SeriesCanonicalEntry", back_populates="series", cascade="all, delete-orphan")

    @property
    def series_state(self):
        return {
            "has_new_books": bool(self.has_new_books),
            "has_unread_books": bool(self.has_unread_books),
            "has_upcoming_books": bool(self.has_upcoming_books),
            "is_caught_up": bool(self.is_caught_up),
        }

    @property
    def read_count(self):
        active_books = [book for book in (self.books or []) if str(book.record_status or "active") != "deleted"]
        return sum(1 for book in active_books if bool(book.is_read))

    @property
    def unread_count(self):
        active_books = [book for book in (self.books or []) if str(book.record_status or "active") != "deleted"]
        return sum(1 for book in active_books if not bool(book.is_read))


class SeriesCanonicalEntry(Base):
    __tablename__ = "series_canonical_entries"
    __table_args__ = (
        UniqueConstraint("series_id", "book_number", name="uq_series_canonical_number"),
    )

    id = Column(Integer, primary_key=True, index=True)
    series_id = Column(Integer, ForeignKey("series.id"), nullable=False, index=True)
    book_number = Column(Float, nullable=False)
    canonical_title = Column(String, nullable=False)
    canonical_author = Column(String, nullable=True)
    publication_year = Column(Integer, nullable=True)
    entry_type = Column(String, default="novel")
    is_fractional = Column(Boolean, default=False)
    is_anthology = Column(Boolean, default=False)
    author_aliases = Column(JSON, nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    series = relationship("Series", back_populates="canonical_entries")


class Book(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)

    # Core identity
    title = Column(String, nullable=False)
    author = Column(String, nullable=False)
    subtitle = Column(String, nullable=True)
    series_id = Column(Integer, ForeignKey("series.id"), nullable=True)
    series_order = Column(Integer, nullable=True)
    series_total_books = Column(Integer, nullable=True)
    is_series_finished = Column(Boolean, default=False)
    book_number = Column(Float, nullable=True)  # supports 0.5, etc.

    # Publishing
    format = Column(String, nullable=True)
    publication_date = Column(Date, nullable=True)
    publisher = Column(String, nullable=True)
    edition = Column(String, nullable=True)
    pages = Column(Integer, nullable=True)
    language = Column(String, nullable=True)
    release_date = Column(Date, nullable=True)

    # Identifiers
    isbn = Column(String, nullable=True)
    isbn13 = Column(String, nullable=True)
    asin = Column(String, nullable=True)
    google_books_id = Column(String, nullable=True)
    goodreads_id = Column(String, nullable=True)
    storygraph_id = Column(String, nullable=True)

    # User reading data
    is_read = Column(Boolean, default=False)
    read_date = Column(Date, nullable=True)
    date_added = Column(Date, nullable=True)
    date_started = Column(Date, nullable=True)
    date_finished = Column(Date, nullable=True)
    read_status = Column(String, nullable=True)  # unread, reading, read, abandoned
    rating = Column(Integer, nullable=True)
    external_rating = Column(Float, nullable=True)
    external_rating_count = Column(Integer, nullable=True)
    review = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    tags = Column(JSON, nullable=True)  # list of strings

    # Intelligence
    is_upcoming_auto = Column(Boolean, default=False)
    is_upcoming_final = Column(Boolean, default=False)
    is_missing = Column(Boolean, default=False)
    record_status = Column(String, default="active")  # active, archived, deleted

    # Importer metadata
    import_source = Column(String, nullable=True)
    import_raw_headers = Column(JSON, nullable=True)
    import_raw_row = Column(JSON, nullable=True)
    import_errors = Column(JSON, nullable=True)

    # Auto summary (you already had)
    auto_summary = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    series = relationship("Series", back_populates="books")

    @property
    def series_name(self):
        return self.series.name if self.series else None
