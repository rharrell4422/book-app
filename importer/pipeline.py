"""The import pipeline: spreadsheet parsing (headers, dates, rows),
series-link decisioning, and create_or_update_book/run_import -- the path
that actually writes rows to the database.

Split out of importer/importer.py (RT-6). preview.py's preview_import
reuses this module's header/row-parsing helpers (map_headers, import_row,
validate_book_row) to mirror this pipeline's parsing exactly without
writing anything; cli.py's CLI entry point calls run_import below.

importer/importer.py re-exports everything below, so existing external
callers (routers/imports.py, tests, etc.) are unaffected by this split.
"""
from __future__ import annotations

import os
import re
import csv
import traceback
from typing import List, Tuple, Dict, Any

try:
    import pandas as pd
    from sqlalchemy.orm import Session

    import crud
    from database import SessionLocal
    from models import Series, Book
    from intelligence import recalculate_intelligence
    from services.identity import is_placeholder_author
    from services.availability_bridge import derive_legacy_fields
    import discovery_engine
except Exception as e:
    print("\n\n🔥 IMPORTER MODULE FAILED DURING IMPORT 🔥")
    traceback.print_exc()
    raise e


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


# Explicit-token normalization for the imported Read/Status column,
# mapping onto the two-axis model (see services/availability_bridge.py and
# models.Book's docstring). Reading axis (is_read) and availability axis
# (availability_status/availability_locked) are derived independently from
# the same raw token rather than one being inferred from the other, so a
# spreadsheet's "Unread" row (owned, not yet read) is never confused with
# an "Available" row (exists, not yet owned/downloaded).
_IMPORT_READ_TOKENS = {"read", "completed", "finished"}
# Only these tokens are unambiguous enough to *lock* availability_status --
# an explicit spreadsheet cell is exactly as authoritative as a manual Edit
# Book choice. Anything else (blank, "reading", "dnf", free text, etc.) is
# ambiguous and is left unlocked at the column default so discovery/Check
# Now can still manage it going forward.
_IMPORT_AVAILABILITY_TOKEN_MAP = {
    "read": "owned",
    "completed": "owned",
    "finished": "owned",
    "unread": "owned",
    "available": "available",
    "upcoming": "upcoming",
    "tbr": "upcoming",
    "to be read": "upcoming",
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
# File Loading (Excel + CSV)
# ------------------------------------------------------------

def read_excel_file(file_path: str) -> Tuple[List[str], List[List[Any]]]:
    """Read the "Master" sheet if present (Robbie's personal template), or
    fall back to the workbook's first sheet -- a brand-new user's own
    export (or a Google Sheets download) has no reason to use that sheet
    name, and previously this would raise and block onboarding entirely."""
    try:
        df = pd.read_excel(file_path, sheet_name="Master")
    except ValueError:
        df = pd.read_excel(file_path, sheet_name=0)
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
    is_read = read_status_raw in _IMPORT_READ_TOKENS
    availability_token = _IMPORT_AVAILABILITY_TOKEN_MAP.get(read_status_raw)
    if availability_token is not None:
        availability_status = availability_token
        availability_locked = True
    else:
        # Ambiguous/blank cell: don't lock, so this row's availability can
        # still be corrected by discovery/Check Now going forward, same as
        # a brand-new manually-added book with no status chosen yet.
        availability_status = "available"
        availability_locked = False

    legacy = derive_legacy_fields(
        is_read=is_read, availability_status=availability_status, availability_locked=availability_locked
    )

    book_data["is_read"] = is_read
    book_data["availability_status"] = availability_status
    book_data["availability_locked"] = availability_locked
    book_data["read_status"] = legacy["read_status"]
    book_data["is_upcoming_auto"] = legacy["is_upcoming_auto"]
    book_data["is_upcoming_final"] = legacy["is_upcoming_final"]
    # Imported rows are never a discovery-provenance "ghost" -- is_missing
    # is a Check Now/discovery-only signal (see models.Book.is_missing),
    # never something a spreadsheet row can claim for itself.
    book_data["is_missing"] = False

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


def _find_existing_series_by_name(db: Session, series_name: str | None, profile_id: str) -> Series | None:
    """Return an existing canonical series record by name, scoped to one
    profile's library -- Robbie and Daughter can each track a same-named
    series independently.

    Import flow policy: do not auto-create new series from title-derived variations.
    A row links to series only when the provided series_name matches an existing
    canonical series name (exact, case-insensitive, after trim).

    NS-8: the case-insensitive fallback reuses crud.get_series_by_name
    directly rather than re-running the same func.lower(Series.name)==...
    query a second time under a different name. The exact-case-first query
    on top of it is a deliberate ordering guard (only relevant if two
    differently-cased rows for the same profile/name ever existed), kept
    here rather than folded into crud.get_series_by_name since every other
    caller of that function has no such ambiguity to resolve.
    """
    cleaned = str(series_name or "").strip()
    if not cleaned:
        return None

    existing = db.query(Series).filter(Series.name == cleaned, Series.profile_id == profile_id).first()
    if existing:
        return existing

    return crud.get_series_by_name(db, cleaned, profile_id)


_SERIES_NAME_LEADING_MARKER_PATTERN = re.compile(r"^[\s\-\u2010-\u2015_.:]+")
_NON_SERIES_PLACEHOLDER_VALUES = {"n a", "na", "none", "standalone", "tbd", "unknown", "n"}


def _is_meaningful_series_name(series_name: Any, author: Any = None) -> bool:
    """Reject spreadsheet "Series" values that are really just a personal
    tracker's placeholder for "not part of a series", not an actual series
    name -- e.g. a bare "-"/"--"/em-dash, or (the real-world case this was
    added for) a "\u2014 Author Name" marker some trackers put in the Series
    column for a standalone book by that author. Without this filter, an
    explicit-but-meaningless Series value is trusted exactly like a real
    one and creates a bogus series that swallows every standalone book by
    that author (and empties the actual "standalone books" view).
    """
    candidate = str(series_name or "").strip()
    if not candidate:
        return False

    normalized = normalize_header(candidate)
    if not normalized or normalized in _NON_SERIES_PLACEHOLDER_VALUES:
        return False

    author_text = str(author or "").strip()
    if author_text:
        after_marker = _SERIES_NAME_LEADING_MARKER_PATTERN.sub("", candidate).strip()
        # Only treat "matches the author" as disqualifying when a leading
        # dash/marker was actually present and stripped -- a series simply
        # named after its author with no marker (an eponymous series) is
        # still a real, explicit signal worth trusting.
        if after_marker and after_marker != candidate and after_marker.lower() == author_text.lower():
            return False

    return True


def _series_link_decision(db: Session, book_data: Dict[str, Any], profile_id: str) -> Dict[str, Any]:
    """Decide whether a row's series linkage is automatic.

    An explicit Series column in the spreadsheet is first-class,
    user-supplied evidence -- unlike inferring a series from patterns in
    the title text (which this deliberately never does), there's nothing
    ambiguous about a row that already says what series it belongs to. So
    any non-blank series_name is trusted outright: get-or-create the
    canonical series for this profile and link immediately. This matches
    how the ~2,300 books already in the app were treated and how a normal
    "Series" column export should behave.

    (DC-5: this used to also flag some rows as needing a manual per-row
    "does this book really belong to this series?" confirmation, backed by
    an import_raw_row metadata queue and a /import/series_confirmations
    API. That path always returned needs_confirmation=False in practice --
    an explicit Series column was unconditionally trusted below, and a
    blank one was always standalone -- so it was dead code; removed rather
    than kept as an always-False no-op.)

    The old version of this rule only ever *linked* to an already-existing
    canonical series and never created one -- which meant a profile's
    first-ever import (zero series on record yet) could never auto-link a
    single row. Rows with no Series value at all import as standalone --
    there's no name to link against; if a title looks numbered but the
    sheet left the series blank, it's still standalone. Any of these can be
    linked afterward via the normal "Edit book" flow, which is the "fix it
    only if it's wrong" model instead of a mandatory per-row validation
    pass.
    """
    series_name = str(book_data.get("series_name") or "").strip()

    if not series_name or not _is_meaningful_series_name(series_name, book_data.get("author")):
        return {
            "should_link": False,
            "series": None,
            "reason": "no_series_name_provided" if not series_name else "series_name_looks_like_placeholder",
        }

    return {
        "should_link": True,
        "series": None,  # resolved via get_or_create_series (may create or reuse)
        "series_name": series_name,
        "reason": "explicit_series_name",
    }

def get_or_create_series(
    db: Session, series_name: str, profile_id: str, total_books: Any = None, series_finished_flag: Any = None
) -> Series:
    if not series_name:
        return None

    series_name = str(series_name).strip()
    if not series_name:
        return None

    # Reuse the same case-insensitive lookup as everywhere else in this
    # file, so "Quest Academy" and "quest academy" in different rows of the
    # same import can't create two separate canonical series.
    existing = _find_existing_series_by_name(db, series_name, profile_id)
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
        profile_id=profile_id,
    )
    db.add(new_series)
    db.commit()
    db.refresh(new_series)
    return new_series


def create_or_update_book(db: Session, book_data: Dict[str, Any], profile_id: str) -> tuple[Book, Dict[str, Any]]:
    series_name = book_data.get("series_name")
    book_number_value = _to_float(book_data.get("book_number"))
    # The spreadsheet's own Book# column occupies the "user" tier of the
    # priority chain (it's a value the user's own export/tool put there) --
    # when it's blank, fall back to the same title-text extractor Check Now
    # and Add Book use (see discovery_engine.infer_number_from_title /
    # crud.books._infer_series_numbers_from_title) rather than leaving an
    # inferable number unset just because this row happened to come through
    # the importer instead of the Add Book form.
    book_number_source = "user" if book_number_value is not None else None
    if book_number_value is None:
        inferred_book_number = discovery_engine.infer_number_from_title(book_data.get("title"), series_name)
        if inferred_book_number is not None:
            book_number_value = inferred_book_number
            book_number_source = "title_inferred"
    series_total_books = _to_int(book_data.get("series_total_books") or book_data.get("series_total"))
    raw_series_finished_flag = book_data.get("series_finished")
    if raw_series_finished_flag is None and "is_series_finished" in book_data:
        raw_series_finished_flag = book_data.get("is_series_finished")
    series_finished_flag = parse_series_finished_flag(raw_series_finished_flag)

    decision = _series_link_decision(db, book_data, profile_id)
    series = None
    if decision.get("should_link"):
        # get_or_create_series both finds-or-creates the canonical series
        # and freshens its is_finished/total_books from this row.
        series = get_or_create_series(
            db,
            decision.get("series_name") or str(series_name or "").strip(),
            profile_id,
            total_books=series_total_books,
            series_finished_flag=series_finished_flag,
        )
        if series:
            # Discovery searches by Series.author, so backfill it from the
            # imported row rather than leaving discovery permanently unable
            # to run for series that were created without one. Same
            # placeholder guard as crud.books._backfill_series_author_if_missing
            # -- a spreadsheet cell of "Unknown"/"N/A"/etc. is exactly as
            # dangerous to adopt here as the Add Book form's old fallback was.
            import_author = str(book_data.get("author") or "").strip()
            if import_author and not str(series.author or "").strip() and not is_placeholder_author(import_author):
                series.author = import_author
                db.commit()
                db.refresh(series)

    # Map PRD fields → DB fields
    db_book = Book(
        profile_id=profile_id,
        title=book_data.get("title"),
        # canonical_title stays unset here -- import is analogous to direct
        # user input (a bulk version of it), not a provider-verification
        # event, so it doesn't get to claim a "resolved" title the way a
        # FIND bind or Check Now match does. Bulk re-resolution is exactly
        # the mechanism meant to fill this in later for imported rows.
        metadata_source="import",
        author=book_data.get("author"),
        subtitle=book_data.get("subtitle"),
        format=book_data.get("format"),
        publication_date=book_data.get("publication_date"),
        release_date=book_data.get("release_date"),
        series_id=series.id if series else None,
        series_order=_to_int(book_number_value),
        series_total_books=series_total_books,
        is_series_finished=series_finished_flag,
        book_number=book_number_value,
        book_number_source=book_number_source,
        is_read=book_data.get("is_read"),
        read_date=book_data.get("date_read"),
        rating=_to_int(book_data.get("rating")),
        notes=book_data.get("notes"),
        review=book_data.get("review"),
        tags=book_data.get("tags"),
        publisher=book_data.get("publisher"),
        edition=book_data.get("edition"),
        pages=_to_int(book_data.get("pages")),
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
        availability_status=book_data.get("availability_status"),
        availability_locked=book_data.get("availability_locked"),
        is_upcoming_auto=book_data.get("is_upcoming_auto"),
        is_upcoming_final=book_data.get("is_upcoming_final"),
        is_missing=book_data.get("is_missing"),
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

def _to_float(value: Any) -> float | None:
    """Coerce a spreadsheet cell to a float, treating blanks as None rather
    than letting SQLAlchemy raise on `float('')` -- a blank "Book #" cell
    is the normal case for a standalone (non-series) book in real user
    spreadsheets, not a data error.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    parsed = _to_float(value)
    return int(parsed) if parsed is not None else None


def validate_book_row(book_data: Dict[str, Any]) -> List[str]:
    """Return a list of validation error codes for a parsed row. `title` and
    `author` are the only two DB-required fields (see `models.Book`); a row
    missing either would otherwise raise an IntegrityError mid-import and
    abort every row after it.
    """
    errors: List[str] = []
    if not str(book_data.get("title") or "").strip():
        errors.append("missing_title")
    if not str(book_data.get("author") or "").strip():
        errors.append("missing_author")
    return errors


DEFAULT_IMPORT_PROFILE_ID = "robbie"


def run_import(file_path: str, *, profile_id: str = DEFAULT_IMPORT_PROFILE_ID):
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
    failed_rows: List[Dict[str, Any]] = []

    for index, row in enumerate(rows):
        row_number = index + 2  # header is row 1 in the source spreadsheet
        book_data: Dict[str, Any] | None = None
        try:
            book_data, unknown_data = import_row(headers, row)

            validation_errors = validate_book_row(book_data)
            if validation_errors:
                raise ValueError(", ".join(validation_errors))

            book, decision = create_or_update_book(db, book_data, profile_id)
            imported_ids.append(book.id)
            print(f"Imported book: {book.title} (ID: {book.id})")
        except Exception as e:
            # A single malformed row (missing required fields, a DB
            # constraint violation, an unparseable value, etc.) must not
            # abort every row after it -- collect it and keep going so one
            # bad line in a 40-row onboarding spreadsheet doesn't leave the
            # profile half-imported with no way to tell which rows landed.
            db.rollback()
            failed_rows.append(
                {
                    "row_number": row_number,
                    "title": (book_data or {}).get("title"),
                    "error": str(e),
                }
            )
            print(f"Warning: skipped row {row_number}: {e}")
            continue

    print(f"Import complete. {len(imported_ids)} books imported.")
    if failed_rows:
        print(f"Skipped {len(failed_rows)} invalid row(s).")

    # Recompute intelligence after import, scoped to this profile's series
    # only -- the importer used to sweep every series across every profile,
    # which is correct but does needless work on unrelated libraries.
    try:
        profile_series_ids = [
            row[0] for row in db.query(Series.id).filter(Series.profile_id == profile_id).all()
        ]
        recomputed_count = 0
        for series_id in profile_series_ids:
            try:
                recalculate_intelligence(db, series_id)
                recomputed_count += 1
            except Exception as e:
                # Each series gets its own try/except -- one series with
                # unusual data (e.g. odd book numbers) throwing here must
                # not abort the loop and leave every *other* series in this
                # import with a stale/blank total_books. A single shared
                # try around the whole loop previously did exactly that.
                db.rollback()
                print(f"Warning: failed to recompute intelligence for series {series_id}: {e}")
        print(
            f"Series intelligence recomputed for {recomputed_count}/{len(profile_series_ids)} "
            f"series in profile '{profile_id}'."
        )
    except Exception as e:
        print(f"Warning: failed to recompute series intelligence: {e}")

    db.close()
    return {
        "imported_count": len(imported_ids),
        "imported_ids": imported_ids,
        "failed_count": len(failed_rows),
        "failed_rows": failed_rows,
    }
