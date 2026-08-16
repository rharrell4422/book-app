"""Regression coverage for the first-time profile import / onboarding
hardening added to importer/importer.py:
  1. A single malformed row (missing title/author) must not abort the rest
     of the import -- it gets collected in `failed_rows` instead.
  2. Post-import intelligence recompute is scoped to the importing
     profile's own series, not a global sweep.
  3. `read_excel_file` falls back to the workbook's first sheet when there
     is no sheet literally named "Master" (a brand-new user's own export
     won't have Robbie's personal sheet name).
  4. `preview_import` never writes to the database.
  5. `reset_profile_data` only clears the requested profile's rows.

These tests patch `importer.importer.SessionLocal` to point at a private
in-memory database instead of the real `books.db` -- `run_import` and
`preview_import` open their own sessions internally rather than accepting
an injected one, so patching the module-level session factory is the only
way to redirect them safely in a test.
"""

import csv
import os
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
from importer.importer import (
    preview_import,
    read_excel_file,
    reset_profile_data,
    run_import,
    validate_book_row,
)
from models import Book, Series


def _new_in_memory_session_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _write_csv(rows: list[list[str]]) -> str:
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return path


class ValidateBookRowTest(unittest.TestCase):
    def test_missing_title_is_flagged(self):
        self.assertEqual(validate_book_row({"title": "", "author": "Someone"}), ["missing_title"])

    def test_missing_author_is_flagged(self):
        self.assertEqual(validate_book_row({"title": "Something", "author": None}), ["missing_author"])

    def test_valid_row_has_no_errors(self):
        self.assertEqual(validate_book_row({"title": "Something", "author": "Someone"}), [])


class RunImportPartialFailureTest(unittest.TestCase):
    """A 30-40 row onboarding spreadsheet with one bad line shouldn't lose
    every row after it -- see importer/importer.py's run_import loop."""

    def setUp(self):
        self.engine, self.SessionLocal = _new_in_memory_session_factory()
        self.csv_path = _write_csv(
            [
                ["Title", "Author", "Series", "Book #"],
                ["Chronicles Book 3", "Author One", "", ""],
                ["Book Four", "", "", ""],  # missing author -> should be skipped, not fatal
                ["Book Two", "Author Two", "Some Series", "2"],
            ]
        )

    def tearDown(self):
        os.remove(self.csv_path)
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_bad_row_is_skipped_without_aborting_later_rows(self):
        with patch("importer.importer.SessionLocal", self.SessionLocal):
            result = run_import(self.csv_path, profile_id="daughter")

        self.assertEqual(result["imported_count"], 2)
        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(len(result["failed_rows"]), 1)
        self.assertIn("missing_author", result["failed_rows"][0]["error"])
        # Row 3 in the source file (1-indexed with header as row 1).
        self.assertEqual(result["failed_rows"][0]["row_number"], 3)

        db = self.SessionLocal()
        try:
            titles = {b.title for b in db.query(Book).filter(Book.profile_id == "daughter").all()}
            self.assertEqual(titles, {"Chronicles Book 3", "Book Two"})
        finally:
            db.close()

    def test_session_recovers_after_a_failed_row(self):
        # create_or_update_book commits internally -- a failure there
        # leaves the session in a state that needs an explicit rollback
        # before it can be reused for the next row.
        with patch("importer.importer.SessionLocal", self.SessionLocal):
            result = run_import(self.csv_path, profile_id="daughter")
        self.assertEqual(result["imported_count"] + result["failed_count"], 3)


class RunImportScopedIntelligenceTest(unittest.TestCase):
    """recompute_series_intelligence used to sweep every series across
    every profile after each import; it should now only touch the
    importing profile's own series."""

    def setUp(self):
        self.engine, self.SessionLocal = _new_in_memory_session_factory()
        db = self.SessionLocal()
        other_profile_series = Series(name="Robbie's Series", profile_id="robbie")
        db.add(other_profile_series)
        db.commit()
        db.close()

        self.csv_path = _write_csv(
            [
                ["Title", "Author", "Series", "Book #"],
                ["Some Book Book 1", "Author One", "New Series", ""],
            ]
        )

    def tearDown(self):
        os.remove(self.csv_path)
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_only_the_importing_profiles_series_are_recalculated(self):
        with patch("importer.importer.SessionLocal", self.SessionLocal), patch(
            "importer.importer.recalculate_intelligence"
        ) as mock_recalc:
            run_import(self.csv_path, profile_id="daughter")

        recalculated_series_ids = {call.args[1] for call in mock_recalc.call_args_list}

        db = self.SessionLocal()
        try:
            robbie_series_id = db.query(Series).filter(Series.profile_id == "robbie").first().id
        finally:
            db.close()

        self.assertNotIn(robbie_series_id, recalculated_series_ids)


class ExcelMasterSheetFallbackTest(unittest.TestCase):
    """A brand-new user's own Excel export has no reason to have a sheet
    literally named 'Master' -- that's specific to Robbie's personal
    template. read_excel_file must fall back to the first sheet instead of
    raising and blocking onboarding entirely."""

    def setUp(self):
        import pandas as pd

        fd, self.xlsx_path = tempfile.mkstemp(suffix=".xlsx")
        os.close(fd)
        df = pd.DataFrame({"Title": ["My Book"], "Author": ["Me"]})
        df.to_excel(self.xlsx_path, index=False, sheet_name="Sheet1")

    def tearDown(self):
        os.remove(self.xlsx_path)

    def test_falls_back_to_first_sheet_when_master_is_absent(self):
        headers, rows = read_excel_file(self.xlsx_path)
        self.assertIn("Title", headers)
        self.assertIn("Author", headers)
        self.assertEqual(len(rows), 1)


class PreviewImportTest(unittest.TestCase):
    """POST /import/preview's underlying function -- must parse without
    ever writing a Book or Series row."""

    def setUp(self):
        self.engine, self.SessionLocal = _new_in_memory_session_factory()
        self.csv_path = _write_csv(
            [
                ["Title", "Author", "Series", "Book #"],
                ["Chronicles Book 3", "Author One", "", ""],
                ["Book Two", "Author Two", "Some Series", "2"],
                ["", "Author Three", "", ""],  # missing title
                ["Book Four", "", "", ""],  # missing author
            ]
        )

    def tearDown(self):
        os.remove(self.csv_path)
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_preview_reports_counts_without_writing_anything(self):
        with patch("importer.importer.SessionLocal", self.SessionLocal):
            result = preview_import(self.csv_path, profile_id="daughter")

        self.assertEqual(result["row_count"], 4)
        self.assertEqual(result["valid_row_count"], 2)
        self.assertEqual(len(result["validation_warnings"]), 2)
        error_codes = {tuple(w["errors"]) for w in result["validation_warnings"]}
        self.assertIn(("missing_title",), error_codes)
        self.assertIn(("missing_author",), error_codes)

        db = self.SessionLocal()
        try:
            self.assertEqual(db.query(Book).count(), 0)
            self.assertEqual(db.query(Series).count(), 0)
        finally:
            db.close()


class ResetProfileDataTest(unittest.TestCase):
    """Onboarding's 'start over' action -- must only ever clear the
    requesting profile's own rows."""

    def setUp(self):
        self.engine, self.SessionLocal = _new_in_memory_session_factory()
        self.db = self.SessionLocal()

        robbie_series = Series(name="Robbie Series", profile_id="robbie")
        daughter_series = Series(name="Daughter Series", profile_id="daughter")
        self.db.add_all([robbie_series, daughter_series])
        self.db.commit()

        self.db.add_all(
            [
                Book(title="Robbie Book", author="A", profile_id="robbie", series_id=robbie_series.id),
                Book(title="Daughter Book", author="B", profile_id="daughter", series_id=daughter_series.id),
            ]
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_only_the_target_profiles_rows_are_deleted(self):
        deleted_books, deleted_series = reset_profile_data(self.db, "daughter")

        self.assertEqual(deleted_books, 1)
        self.assertEqual(deleted_series, 1)
        self.assertEqual(self.db.query(Book).filter(Book.profile_id == "daughter").count(), 0)
        self.assertEqual(self.db.query(Series).filter(Series.profile_id == "daughter").count(), 0)
        self.assertEqual(self.db.query(Book).filter(Book.profile_id == "robbie").count(), 1)
        self.assertEqual(self.db.query(Series).filter(Series.profile_id == "robbie").count(), 1)


if __name__ == "__main__":
    unittest.main()
