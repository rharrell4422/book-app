# models.py
#
# Clean, modern SQLAlchemy models for the Goodreads-powered engine.
# No legacy fields. No unused columns. Lean and future-proof.

from sqlalchemy import Column, Integer, String, Boolean, Date, ForeignKey, JSON
from sqlalchemy.orm import relationship
from database import Base


# ---------------------------------------------------------------------------
# SERIES MODEL (CLEAN)
# ---------------------------------------------------------------------------

class Series(Base):
    __tablename__ = "series"

    id = Column(Integer, primary_key=True, index=True)

    # Core identity
    name = Column(String, index=True, nullable=False)

    # Relationship to books
    books = relationship("Book", back_populates="series", cascade="all, delete-orphan")

    # Intelligence fields (computed by intelligence.py)
    total_books = Column(Integer, nullable=True)
    read_books = Column(Integer, nullable=True)
    unread_books = Column(Integer, nullable=True)

    next_unread_book = Column(Integer, nullable=True)  # book_number
    upcoming_books = Column(Integer, nullable=True)

    # Missing book numbers (stored as JSON list)
    missing_books = Column(JSON, nullable=True)


# ---------------------------------------------------------------------------
# BOOK MODEL (CLEAN)
# ---------------------------------------------------------------------------

class Book(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)

    # Core identity
    title = Column(String, index=True, nullable=False)
    author = Column(String, index=True, nullable=True)

    # Optional genre (safe even if spreadsheet doesn't include it)
    genre = Column(String, nullable=True)

    # Series relationship
    series_id = Column(Integer, ForeignKey("series.id"), nullable=True)
    series = relationship("Series", back_populates="books")

    # Book metadata
    book_number = Column(Integer, nullable=True)
    year = Column(Integer, nullable=True)  # extracted from Goodreads
    release_date = Column(Date, nullable=True)

    # Reading status
    read_status = Column(Boolean, default=False)
