import traceback
import argparse

try:
    import pandas as pd
    from sqlalchemy.orm import Session
    from models import Book, Series
    from intelligence import recompute_series_intelligence
except Exception as e:
    print("\n\n🔥 IMPORTER MODULE FAILED DURING IMPORT 🔥")
    traceback.print_exc()
    raise e

import os
import re
import csv
from datetime import datetime
from typing import List, Tuple, Dict, Any

import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import func

from database import SessionLocal
from models import Series, Book
from intelligence import recompute_series_intelligence


# ------------------------------------------------------------
# Header Normalization & Alias Map
# ------------------------------------------------------------

def normalize_header(header: str) -> str:
    h = header.strip().lower()
    h = re.sub(r"[^a-z0-9]+", " ", h)
    h = re.sub(r"\s+", " ", h).strip()
    return h


HEADER_MAP: Dict[str, List[str]] = {
    "title": ["title", "titles", "book title", "name", "bookname"],
    "subtitle": ["subtitle", "sub title", "sub-title"],
    "author": ["author", "authors", "writer", "book author"],
    "series_name": ["series", "series name", "series title", "series names"],
    "series_confirmed": ["series confirmed", "confirm series", "series confirmation", "is series confirmed"],
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
    "date_finished": ["date finished", "finished", "finish date", "completed date", "date read", "read date"],
    "release_date": ["release date", "next release date"],
    "read_status": ["read status", "record status", "status", "reading status"],
    "series_finished": ["series finished", "series complete", "series completed"],
    "rating": ["rating", "stars", "score"],
    "review": ["review", "review text", "comments"],
    "notes": ["notes", "note", "personal notes"],
    "tags": ["tags", "labels", "categories"],
}


def build_header_lookup() -> Dict[str, str]:
    lookup: Dict[str, str] = {}
    for field, aliases in HEADER_MAP.items():
        for alias in aliases:
            lookup[normalize_header(alias)] = field
    return lookup


HEADER_LOOKUP = build_header_lookup()


def map_headers(raw_headers: List[str]) -> Tuple[Dict[str, str], List[str]]:
    mapping: Dict[str, str] = {}
    unknown_headers: List[str] = []

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
# Date Parsing
# ------------------------------------------------------------

def parse_date(value: Any):
    if value is None or value == "":
        return None

    # If it's already a datetime/date from pandas, just normalize
    if isinstance(value, datetime):
        return value.date()

    # Try common string formats
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except Exception:
            pass

    # Excel serial date (numeric)
    try:
        iv = int(value)
        base = datetime(1899, 12, 30)
        return (base + pd.to_timedelta(iv, unit="D")).date()
    except Exception:
        return None


# ------------------------------------------------------------
# File Loading (Excel + CSV)
# ------------------------------------------------------------

def read_excel_file(file_path: str) -> Tuple[List[str], List[List[Any]]]:
    df = pd.read_excel(file_path, sheet_name="Master")
    headers = list(df.columns)
    rows = df.values.tolist()
    return headers, rows


def read_csv_file(file_path: str) -> Tuple[List[str], List[List[Any]]]:
    rows: List[List[Any]] = []
    with open(file_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        for row in reader:
            rows.append([row.get(h) for h in headers])
    return headers, rows


def load_file(file_path: str) -> Tuple[List[str], List[List[Any]]]:
    ext = os.path.splitext(file_path)[1].lower()
    if ext in [".xlsx", ".xls"]:
        return read_excel_file(file_path)
    elif ext == ".csv":
        return read_csv_file(file_path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")


# ------------------------------------------------------------
# Row → Internal Data (PRD Naming)
# ------------------------------------------------------------
# ------------------------------------------------------------
# Row → Internal Data (PRD Naming)
# ------------------------------------------------------------

def import_row(raw_headers: List[str], row_values: List[Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    import pandas as pd
    from datetime import date, datetime

    mapping, unknown_headers = map_headers(raw_headers)

    book_data: Dict[str, Any] = {}
    unknown_data: Dict[str, Any] = {}

    def normalize_date(value):
        """Convert NaT, Timestamp, numpy datetime, or blank to None or Python date."""
        if value is None:
            return None
        if pd.isna(value):
            return None
        if isinstance(value, pd.Timestamp):
            return value.date()
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return pd.to_datetime(value).date()
        except:
            return None

    # ------------------------------------------------------------
    # MAIN LOOP
    # ------------------------------------------------------------
    for raw_h, value in zip(raw_headers, row_values):

        # Normalize NaT / NaN / Timestamp immediately
        if pd.isna(value):
            value = None
        if isinstance(value, pd.Timestamp):
            value = value.date()

        internal = mapping.get(raw_h)

        if internal:
            # Date-like fields
            if internal in ["publication_date", "date_added", "date_started", "date_finished", "release_date", "date_read"]:
                book_data[internal] = normalize_date(value)
            else:
                book_data[internal] = value
        else:
            # Clean unknown values too (they get JSON-encoded)
            if isinstance(value, pd.Timestamp):
                value = value.date()
            unknown_data[raw_h] = value

    # ------------------------------------------------------------
    # DERIVED FIELDS
    # ------------------------------------------------------------
    read_status_raw = (book_data.get("read_status") or "").strip().lower()
    is_read = read_status_raw in ["read", "completed", "finished"]
    is_upcoming = read_status_raw in ["upcoming", "tbr", "to be read"]

    book_data["is_read"] = is_read
    book_data["is_upcoming"] = is_upcoming
    book_data["read_status"] = read_status_raw

    # Use date_finished as date_read
    book_data["date_read"] = normalize_date(book_data.get("date_finished"))

    # ------------------------------------------------------------
    # FINAL CLEANUP — JSON-SAFE VALUES ONLY
    # ------------------------------------------------------------
    def json_safe(value):
        """Convert anything non-JSON-safe into JSON-safe values."""
        if value is None:
            return None
        if pd.isna(value):
            return None
        if isinstance(value, pd.Timestamp):
            return value.date().isoformat()
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return value

    # Keep true date values for database fields.
    # Only convert raw import metadata to JSON-safe values.
    for k, v in list(book_data.items()):
        if k in ["import_raw_headers", "import_raw_row"]:
            continue
        if isinstance(v, (datetime, date)):
            continue
        book_data[k] = v

    # Clean unknown_data (goes into import_raw_row)
    for k, v in list(unknown_data.items()):
        unknown_data[k] = json_safe(v)

    # ------------------------------------------------------------
    # RAW IMPORT CONTEXT (NOW JSON-SAFE)
    # ------------------------------------------------------------
    book_data["import_raw_headers"] = list(raw_headers)
    book_data["import_raw_row"] = unknown_data

    return book_data, unknown_data



# ------------------------------------------------------------
# Series & Book DB Helpers
# ------------------------------------------------------------

def parse_series_finished_flag(value: Any) -> bool:
    """Only an explicit 'no' means unfinished; yes/maybe/blank are finished."""
    normalized = "" if value is None else str(value).strip().lower()
    if normalized in ["no", "false", "n"]:
        return False
    return True


def _normalize_series_or_title_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.rstrip(":")
    text = re.sub(r"\s+", " ", text)
    return text


SERIES_NUMBER_MARKER_PATTERNS = [
    re.compile(r"\bbook\s*#?\s*\d+(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"#\s*\d+(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\bvol(?:ume)?\.?\s*\d+(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\bepisode\s*\d+(?:\.\d+)?\b", re.IGNORECASE),
    re.compile(r"\bpart\s*\d+(?:\.\d+)?\b", re.IGNORECASE),
]


def _title_has_clear_series_number(title: Any) -> bool:
    text = str(title or "").strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in SERIES_NUMBER_MARKER_PATTERNS)


def _parse_bool(value: Any) -> bool | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "t", "yes", "y", "confirmed"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "unconfirmed"}:
        return False
    return None


def _should_create_series_link(book_data: Dict[str, Any]) -> bool:
    """Require concrete evidence before attempting to link a series for a book row.

    This prevents standalone books (e.g., a single title with the same series name)
    from being auto-classified as a series.
    """
    series_name = str(book_data.get("series_name") or "").strip()
    if not series_name:
        return False

    title = str(book_data.get("title") or "").strip()
    normalized_series = _normalize_series_or_title_text(series_name)
    normalized_title = _normalize_series_or_title_text(title)

    # Evidence 1: explicit numbering for this row.
    raw_book_number = book_data.get("book_number")
    has_explicit_book_number = False
    try:
        has_explicit_book_number = raw_book_number is not None and str(raw_book_number).strip() != ""
    except Exception:
        has_explicit_book_number = False

    # Evidence 2: explicit series total greater than 1.
    raw_total = book_data.get("series_total_books") or book_data.get("series_total")
    has_explicit_series_total = False
    try:
        has_explicit_series_total = raw_total is not None and int(raw_total) > 1
    except Exception:
        has_explicit_series_total = False

    # Evidence 3: title contains common in-series marker.
    title_has_series_marker = bool(re.search(r"\bbook\s*\d+", title, flags=re.IGNORECASE))

    # Evidence 4: series name is clearly different from the title text.
    name_differs_from_title = bool(normalized_series and normalized_title and normalized_series != normalized_title)

    return bool(
        has_explicit_book_number
        or has_explicit_series_total
        or title_has_series_marker
        or name_differs_from_title
    )


def _find_existing_series_by_name(db: Session, series_name: str | None) -> Series | None:
    """Return an existing canonical series record by name.

    Import flow policy: do not auto-create new series from title-derived variations.
    A row links to series only when the provided series_name matches an existing
    canonical series name (exact, case-insensitive, after trim).
    """
    cleaned = str(series_name or "").strip()
    if not cleaned:
        return None

    existing = db.query(Series).filter(Series.name == cleaned).first()
    if existing:
        return existing

    return db.query(Series).filter(func.lower(Series.name) == cleaned.lower()).first()


def _series_link_decision(db: Session, book_data: Dict[str, Any]) -> Dict[str, Any]:
    """Decide whether series linkage is automatic or requires user confirmation.

    Rules:
    1) Numbered title markers => auto-series only when canonical series exists.
    2) Unnumbered title => require confirmation before series linkage.
    3) Never infer series name from title text.
    """
    series_name = str(book_data.get("series_name") or "").strip()
    title = str(book_data.get("title") or "").strip()
    explicit_confirmation = _parse_bool(book_data.get("series_confirmed"))
    has_number_marker = _title_has_clear_series_number(title)

    if not series_name:
        if not has_number_marker:
            return {
                "should_link": False,
                "series": None,
                "needs_confirmation": True,
                "reason": "unnumbered_title_no_series_name_requires_confirmation",
                "has_number_marker": False,
            }
        return {
            "should_link": False,
            "series": None,
            "needs_confirmation": False,
            "reason": "numbered_title_no_series_name",
            "has_number_marker": has_number_marker,
        }

    canonical_series = _find_existing_series_by_name(db, series_name)

    if has_number_marker:
        if canonical_series:
            return {
                "should_link": True,
                "series": canonical_series,
                "needs_confirmation": False,
                "reason": "numbered_title_auto_series",
                "has_number_marker": True,
            }
        return {
            "should_link": False,
            "series": None,
            "needs_confirmation": False,
            "reason": "numbered_title_canonical_missing",
            "has_number_marker": True,
        }

    # Unnumbered titles require explicit user confirmation.
    if explicit_confirmation is True and canonical_series:
        return {
            "should_link": True,
            "series": canonical_series,
            "needs_confirmation": False,
            "reason": "user_confirmed_unnumbered_title",
            "has_number_marker": False,
        }

    return {
        "should_link": False,
        "series": None,
        "needs_confirmation": True,
        "reason": "unnumbered_title_requires_confirmation",
        "has_number_marker": False,
    }

def get_or_create_series(db: Session, series_name: str, total_books: Any = None, series_finished_flag: Any = None) -> Series:
    if not series_name:
        return None

    series_name = str(series_name).strip()
    if not series_name:
        return None

    existing = db.query(Series).filter(Series.name == series_name).first()
    if existing:
        # Optionally update finished/total_books if provided
        if series_finished_flag is not None:
            existing.is_finished = bool(series_finished_flag)
        if total_books is not None:
            try:
                existing.total_books = int(total_books)
            except Exception:
                pass
        db.commit()
        db.refresh(existing)
        return existing

    is_finished = bool(series_finished_flag) if series_finished_flag is not None else False
    total = None
    if total_books is not None:
        try:
            total = int(total_books)
        except Exception:
            total = None

    new_series = Series(
        name=series_name,
        is_finished=is_finished,
        total_books=total,
    )
    db.add(new_series)
    db.commit()
    db.refresh(new_series)
    return new_series


def create_or_update_book(db: Session, book_data: Dict[str, Any]) -> tuple[Book, Dict[str, Any]]:
    series_name = book_data.get("series_name")
    series_total_books = book_data.get("series_total_books") or book_data.get("series_total") or None
    raw_series_finished_flag = book_data.get("series_finished")
    if raw_series_finished_flag is None and "is_series_finished" in book_data:
        raw_series_finished_flag = book_data.get("is_series_finished")
    series_finished_flag = parse_series_finished_flag(raw_series_finished_flag)

    decision = _series_link_decision(db, book_data)
    series = decision.get("series") if decision.get("should_link") else None

    if series:
        # Keep canonical series metadata fresh when import includes explicit values.
        if series_finished_flag is not None:
            series.is_finished = bool(series_finished_flag)
        if series_total_books is not None:
            try:
                series.total_books = int(series_total_books)
            except Exception:
                pass
        db.commit()
        db.refresh(series)

    if decision.get("needs_confirmation"):
        existing_row = book_data.get("import_raw_row") if isinstance(book_data.get("import_raw_row"), dict) else {}
        book_data["import_raw_row"] = {
            **existing_row,
            "series_confirmation_required": True,
            "series_confirmation_reason": decision.get("reason"),
            "series_candidate_name": series_name,
            "title_has_series_number": bool(decision.get("has_number_marker")),
        }

    # Map PRD fields → DB fields
    db_book = Book(
        title=book_data.get("title"),
        author=book_data.get("author"),
        subtitle=book_data.get("subtitle"),
        format=book_data.get("format"),
        publication_date=book_data.get("publication_date"),
        release_date=book_data.get("release_date"),
        series_id=series.id if series else None,
        series_order=book_data.get("book_number"),
        series_total_books=series_total_books,
        is_series_finished=series_finished_flag,
        book_number=book_data.get("book_number"),
        is_read=book_data.get("is_read"),
        read_date=book_data.get("date_read"),
        rating=book_data.get("rating"),
        notes=book_data.get("notes"),
        review=book_data.get("review"),
        tags=book_data.get("tags"),
        publisher=book_data.get("publisher"),
        edition=book_data.get("edition"),
        pages=book_data.get("pages"),
        language=book_data.get("language"),
        isbn=book_data.get("isbn"),
        isbn13=book_data.get("isbn13"),
        asin=book_data.get("asin"),
        google_books_id=book_data.get("google_books_id"),
        goodreads_id=book_data.get("goodreads_id"),
        storygraph_id=book_data.get("storygraph_id"),
        date_added=book_data.get("date_added"),
        date_started=book_data.get("date_started"),
        date_finished=book_data.get("date_finished"),
        read_status=book_data.get("read_status"),
        import_raw_headers=book_data.get("import_raw_headers"),
        import_raw_row=book_data.get("import_raw_row"),
    )

    db.add(db_book)
    db.commit()
    db.refresh(db_book)
    return db_book, decision


# ------------------------------------------------------------
# Main Import Function
# ------------------------------------------------------------

def _prompt_series_confirmation(book_data: Dict[str, Any], reason: str) -> bool:
    title = str(book_data.get("title") or "").strip() or "(untitled)"
    series_name = str(book_data.get("series_name") or "").strip() or "(no series name)"
    prompt = (
        f"\nSeries confirmation required [{reason}]\n"
        f"  Title: {title}\n"
        f"  Candidate series: {series_name}\n"
        "Link this book to the candidate series? [y/N]: "
    )
    answer = input(prompt).strip().lower()
    return answer in {"y", "yes"}


def run_import(file_path: str, *, interactive_confirm: bool = False):
    db: Session = SessionLocal()

    print(f"Loading file: {file_path}")
    headers, rows = load_file(file_path)

    mapping, unknown_headers = map_headers(headers)
    if unknown_headers:
        print("Unknown headers detected:")
        for h in unknown_headers:
            print(f"  - {h}")
    else:
        print("All headers mapped successfully.")

    imported_ids: List[int] = []
    confirmation_required: List[Dict[str, Any]] = []

    for row in rows:
        book_data, unknown_data = import_row(headers, row)

        if interactive_confirm:
            preview_decision = _series_link_decision(db, book_data)
            if preview_decision.get("needs_confirmation"):
                if _prompt_series_confirmation(book_data, str(preview_decision.get("reason") or "confirmation_required")):
                    book_data["series_confirmed"] = True

        book, decision = create_or_update_book(db, book_data)
        imported_ids.append(book.id)
        print(f"Imported book: {book.title} (ID: {book.id})")

        if decision.get("needs_confirmation"):
            confirmation_required.append(
                {
                    "book_id": book.id,
                    "title": book.title,
                    "author": book.author,
                    "series_name": str(book_data.get("series_name") or "").strip() or None,
                    "reason": decision.get("reason"),
                }
            )

    print(f"Import complete. {len(imported_ids)} books imported.")
    if confirmation_required:
        print(f"Series confirmation required for {len(confirmation_required)} row(s).")

    # Recompute intelligence after import
    try:
        recompute_series_intelligence(db)
        print("Series intelligence recomputed.")
    except Exception as e:
        print(f"Warning: failed to recompute series intelligence: {e}")

    db.close()
    return {
        "imported_count": len(imported_ids),
        "imported_ids": imported_ids,
        "confirmation_required_count": len(confirmation_required),
        "confirmation_required": confirmation_required,
    }


def reset_database(db: Session):
    # Delete books first to satisfy FK constraints, then series.
    deleted_books = db.query(Book).delete(synchronize_session=False)
    deleted_series = db.query(Series).delete(synchronize_session=False)
    db.commit()
    return deleted_books, deleted_series


def parse_args():
    parser = argparse.ArgumentParser(description="Import books from CSV/XLSX into Book App database")
    parser.add_argument("file", help="Path to import file (.csv/.xlsx/.xls)")
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Wipe books and series tables before import",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.reset_db:
        reset_session: Session = SessionLocal()
        try:
            deleted_books, deleted_series = reset_database(reset_session)
            print(f"Database reset complete. Deleted {deleted_books} books and {deleted_series} series.")
        finally:
            reset_session.close()

    run_import(args.file, interactive_confirm=True)

