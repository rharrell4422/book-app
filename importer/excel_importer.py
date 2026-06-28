import re
from datetime import datetime
from sqlalchemy.orm import Session
from models import Book, Series
from database import SessionLocal

# ------------------------------------------------------------
# Header Normalization
# ------------------------------------------------------------

def normalize_header(header: str) -> str:
    h = header.strip().lower()
    h = re.sub(r"[^a-z0-9]+", " ", h)
    h = re.sub(r"\s+", " ", h).strip()
    return h


# ------------------------------------------------------------
# Header Alias Map (Option B)
# ------------------------------------------------------------

HEADER_MAP = {
    "title": ["title", "book title", "name", "bookname"],
    "subtitle": ["subtitle", "sub title", "sub-title"],
    "author": ["author", "writer", "book author"],
    "series_name": ["series", "series name", "series title"],
    "book_number": ["book number", "book #", "number", "order", "sequence", "seq"],
    "publication_date": ["publication date", "pub date", "published", "publish date"],
    "publisher": ["publisher", "publishing house", "imprint"],
    "edition": ["edition", "ed."],
    "format": ["format", "binding", "media"],
    "pages": ["pages", "page count", "num pages"],
    "language": ["language", "lang"],
    "isbn": ["isbn", "isbn10", "isbn-10"],
    "isbn13": ["isbn13", "isbn-13", "isbn 13"],
    "asin": ["asin", "amazon id"],
    "google_books_id": ["google books id", "google id"],
    "goodreads_id": ["goodreads id", "gr id"],
    "storygraph_id": ["storygraph id", "sg id"],
    "date_added": ["date added", "added"],
    "date_started": ["date started", "started", "start date"],
    "date_finished": ["date finished", "finished", "finish date", "completed date"],
    "read_status": ["read status", "status", "reading status"],
    "rating": ["rating", "stars", "score"],
    "review": ["review", "review text", "comments"],
    "notes": ["notes", "note", "personal notes"],
    "tags": ["tags", "labels", "categories"],
}


# ------------------------------------------------------------
# Build Lookup Table
# ------------------------------------------------------------

def build_header_lookup():
    lookup = {}
    for field, aliases in HEADER_MAP.items():
        for alias in aliases:
            lookup[normalize_header(alias)] = field
    return lookup

HEADER_LOOKUP = build_header_lookup()


# ------------------------------------------------------------
# Map Raw Headers → Internal Fields
# ------------------------------------------------------------

def map_headers(raw_headers):
    mapping = {}
    unknown_headers = []

    for h in raw_headers:
        norm = normalize_header(h)
        internal = HEADER_LOOKUP.get(norm)

        if internal:
            mapping[h] = internal
        else:
            mapping[h] = None
            unknown_headers.append(h)

    return mapping, unknown_headers


# ------------------------------------------------------------
# Parse Date Helper
# ------------------------------------------------------------

def parse_date(value):
    if not value:
        return None

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except:
            pass

    return None


# ------------------------------------------------------------
# Auto-Create or Fetch Series
# ------------------------------------------------------------

def get_or_create_series(db: Session, series_name: str):
    if not series_name:
        return None

    existing = db.query(Series).filter(Series.name == series_name).first()
    if existing:
        return existing

    new_series = Series(name=series_name)
    db.add(new_series)
    db.commit()
    db.refresh(new_series)
    return new_series


# ------------------------------------------------------------
# Import a Single Row (Option B)
# ------------------------------------------------------------

def import_row(raw_headers, row_values):
    mapping, unknown_headers = map_headers(raw_headers)

    book_data = {}
    unknown_data = {}

    for raw_h, value in zip(raw_headers, row_values):
        internal = mapping.get(raw_h)

        if internal:
            if internal in ["publication_date", "date_added", "date_started", "date_finished"]:
                book_data[internal] = parse_date(value)
            else:
                book_data[internal] = value
        else:
            unknown_data[raw_h] = value

    book_data["import_raw_headers"] = raw_headers
    book_data["import_raw_row"] = unknown_data

    return book_data


# ------------------------------------------------------------
# Save Book to Database
# ------------------------------------------------------------

def save_book(book_data):
    db = SessionLocal()

    series_name = book_data.pop("series_name", None)
    if series_name:
        series = get_or_create_series(db, series_name)
        book_data["series_id"] = series.id

    book = Book(**book_data)
    db.add(book)
    db.commit()
    db.refresh(book)
    db.close()

    return book


# ------------------------------------------------------------
# Main Import Function
# ------------------------------------------------------------

def import_excel_rows(headers, rows):
    imported = []

    for row in rows:
        book_data = import_row(headers, row)
        saved = save_book(book_data)
        imported.append(saved.id)

    return imported

