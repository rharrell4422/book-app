from datetime import datetime
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
)
from sqlalchemy.orm import relationship
from database import Base


class Series(Base):
    __tablename__ = "series"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    is_finished = Column(Boolean, default=False)
    total_books = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    books = relationship("Book", back_populates="series")


class Book(Base):
    __tablename__ = "books"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)
    author = Column(String, nullable=False)
    format = Column(String, nullable=True)
    publication_date = Column(Date, nullable=True)

    series_id = Column(Integer, ForeignKey("series.id"), nullable=True)
    series_order = Column(Integer, nullable=True)
    series_total_books = Column(Integer, nullable=True)
    is_series_finished = Column(Boolean, default=False)

    # ⭐ MISSING FIELD — now added
    book_number = Column(Integer, nullable=True)

    is_read = Column(Boolean, default=False)
    read_date = Column(Date, nullable=True)

    rating = Column(Integer, nullable=True)
    notes = Column(Text, nullable=True)

    # NEW FIELDS
    auto_summary = Column(Text, nullable=True)
    external_rating = Column(Float, nullable=True)
    external_rating_count = Column(Integer, nullable=True)

    # UPCOMING FLAGS
    is_upcoming_auto = Column(Boolean, default=False)
    is_upcoming_final = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    series = relationship("Series", back_populates="books")
