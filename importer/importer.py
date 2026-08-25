"""Book/series spreadsheet importer.

RT-6: this used to be one ~900-line module mixing the import pipeline,
CLI argument parsing, preview, and database-reset logic. Split into:

- pipeline.py: header/date/row parsing, series-link decisioning, and
  create_or_update_book/run_import -- the path that actually writes rows.
- preview.py: preview_import, the read-only "parse without writing" path
  the onboarding wizard's preview step uses.
- cli.py: parse_args/main(), the `python importer/importer.py <file>`
  command-line entry point.
- reset.py: reset_database/reset_profile_data.

DC-4: parse_date() and _should_create_series_link() (plus its sole helper
_normalize_series_or_title_text()) were removed during the RT-6 split --
both had zero callers (import_row has its own local date-normalization
closure; _should_create_series_link was already superseded by
_series_link_decision).

This module re-exports every public and private name the four submodules
define, so existing external callers (routers/imports.py, tests, etc.) are
unaffected by the split -- they can keep doing
`from importer.importer import run_import` exactly as before (note:
`SessionLocal` patches for tests now need to target `importer.pipeline`/
`importer.preview` directly, since that's where each session is actually
opened -- see tests/test_importer_onboarding.py). `python -m
importer.importer <file>` also keeps working, via cli.main() (this
package's absolute imports mean it must be run with `-m` from the repo
root, as `python importer/importer.py <file>` already required before
this split).
"""
from __future__ import annotations

from importer.pipeline import (
    DEFAULT_IMPORT_PROFILE_ID,
    HEADER_LOOKUP,
    HEADER_MAP,
    Book,
    Series,
    Session,
    SessionLocal,
    _find_existing_series_by_name,
    _is_meaningful_series_name,
    _NON_SERIES_PLACEHOLDER_VALUES,
    _series_link_decision,
    _SERIES_NAME_LEADING_MARKER_PATTERN,
    _to_float,
    _to_int,
    build_header_lookup,
    create_or_update_book,
    discovery_engine,
    get_or_create_series,
    import_row,
    is_placeholder_author,
    load_file,
    map_headers,
    normalize_header,
    parse_series_finished_flag,
    read_csv_file,
    read_excel_file,
    recalculate_intelligence,
    run_import,
    validate_book_row,
)
from importer.preview import preview_import
from importer.reset import reset_database, reset_profile_data
from importer.cli import main, parse_args

if __name__ == "__main__":
    main()
