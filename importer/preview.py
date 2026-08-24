"""Read-only spreadsheet preview: parses a file exactly the way
pipeline.run_import would, without writing anything to the database.

Split out of importer/importer.py (RT-6). Reuses pipeline.py's own
header/row-parsing helpers (load_file, map_headers, import_row,
validate_book_row) so the preview can never drift from what a real import
would actually do to the same file.

importer/importer.py re-exports preview_import, so existing external
callers (routers/imports.py, tests, etc.) are unaffected by this split.
"""
from __future__ import annotations

from typing import Dict, List, Any

from sqlalchemy.orm import Session

from database import SessionLocal
from importer.pipeline import (
    DEFAULT_IMPORT_PROFILE_ID,
    import_row,
    load_file,
    map_headers,
    validate_book_row,
)


def preview_import(file_path: str, *, profile_id: str = DEFAULT_IMPORT_PROFILE_ID, sample_size: int = 20) -> Dict[str, Any]:
    """Parse a spreadsheet without writing anything to the database. Powers
    the onboarding wizard's "preview parsed rows" step. Series-link
    decisions are computed read-only (no series are created) purely so the
    sample rows can show what each will link to.
    """
    db: Session = SessionLocal()
    try:
        headers, rows = load_file(file_path)
        mapping, unknown_headers = map_headers(headers)

        sample_rows: List[Dict[str, Any]] = []
        validation_warnings: List[Dict[str, Any]] = []

        for index, row in enumerate(rows):
            row_number = index + 2  # header is row 1 in the source spreadsheet
            try:
                book_data, _ = import_row(headers, row)
            except Exception as e:
                validation_warnings.append({"row_number": row_number, "errors": [f"parse_error: {e}"]})
                continue

            errors = validate_book_row(book_data)
            if errors:
                validation_warnings.append(
                    {"row_number": row_number, "title": book_data.get("title"), "errors": errors}
                )

            if len(sample_rows) < sample_size:
                sample_rows.append(
                    {
                        "row_number": row_number,
                        "title": book_data.get("title"),
                        "author": book_data.get("author"),
                        "series_name": book_data.get("series_name"),
                        "book_number": book_data.get("book_number"),
                    }
                )

        return {
            "row_count": len(rows),
            "unknown_headers": unknown_headers,
            "sample_rows": sample_rows,
            "validation_warnings": validation_warnings,
            "valid_row_count": len(rows) - len(validation_warnings),
        }
    finally:
        db.close()
