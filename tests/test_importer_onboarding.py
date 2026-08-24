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

These tests patch `importer.pipeline.SessionLocal`/`importer.preview.
SessionLocal` to point at a private in-memory database instead of the real
`books.db` -- `run_import` and `preview_import` open their own sessions
internally rather than accepting an injected one, so patching the
module-level session factory is the only way to redirect them safely in a
test.
"""

import csv
import os
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base
import importer.importer as importer_module
from importer.importer import (
    _is_meaningful_series_name,
    _series_link_decision,
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
        with patch("importer.pipeline.SessionLocal", self.SessionLocal):
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
        with patch("importer.pipeline.SessionLocal", self.SessionLocal):
            result = run_import(self.csv_path, profile_id="daughter")
        self.assertEqual(result["imported_count"] + result["failed_count"], 3)


class CreateOrUpdateBookProvenanceTest(unittest.TestCase):
    """Regression coverage for the Phase 3/4 provenance + number-inference
    wiring added to importer.create_or_update_book (see project design
    chat): every imported row is stamped metadata_source="import", and a
    blank Book# spreadsheet cell falls back to the same title-text
    extractor Check Now/Add Book use rather than staying unset just because
    the row came in through the importer.
    """

    def setUp(self):
        self.engine, self.SessionLocal = _new_in_memory_session_factory()
        self.db = self.SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_row_is_stamped_import_metadata_source(self):
        from importer.importer import create_or_update_book

        book, _decision = create_or_update_book(
            self.db,
            {"title": "Some Book", "author": "Some Author", "series_name": "Some Series", "book_number": 3},
            profile_id="robbie",
        )
        self.assertEqual(book.metadata_source, "import")
        self.assertIsNone(book.canonical_title)
        self.assertEqual(book.book_number_source, "user")

    def test_blank_book_number_falls_back_to_title_inference(self):
        from importer.importer import create_or_update_book

        book, _decision = create_or_update_book(
            self.db,
            {"title": "Cherry Blossom Girls Book 7", "author": "Some Author", "series_name": "Cherry Blossom Girls"},
            profile_id="robbie",
        )
        self.assertEqual(book.book_number, 7.0)
        self.assertEqual(book.book_number_source, "title_inferred")

    def test_explicit_blank_and_uninferable_title_leaves_number_source_unset(self):
        from importer.importer import create_or_update_book

        book, _decision = create_or_update_book(
            self.db,
            {"title": "An Unnumbered Standalone", "author": "Some Author"},
            profile_id="robbie",
        )
        self.assertIsNone(book.book_number)
        self.assertIsNone(book.book_number_source)


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
        with patch("importer.pipeline.SessionLocal", self.SessionLocal), patch(
            "importer.pipeline.recalculate_intelligence"
        ) as mock_recalc:
            run_import(self.csv_path, profile_id="daughter")

        recalculated_series_ids = {call.args[1] for call in mock_recalc.call_args_list}

        db = self.SessionLocal()
        try:
            robbie_series_id = db.query(Series).filter(Series.profile_id == "robbie").first().id
        finally:
            db.close()

        self.assertNotIn(robbie_series_id, recalculated_series_ids)


class ExplicitSeriesNameAutoLinksOnFirstImportTest(unittest.TestCase):
    """Regression test for the real-world "Mackenzie's first import" bug:
    a brand-new profile with zero series on record, importing rows whose
    spreadsheet already has a Series column filled in, must auto-create
    and link that series with no per-row confirmation. The old rule only
    ever linked to an *already-existing* canonical series -- which a
    profile's very first import can never have -- so every series-tagged
    row silently failed to link (or, for unnumbered titles, demanded a
    confirmation that could never actually resolve, since resolving also
    only linked to a pre-existing series)."""

    def setUp(self):
        self.engine, self.SessionLocal = _new_in_memory_session_factory()
        self.csv_path = _write_csv(
            [
                ["Title", "Author", "Series", "Book #"],
                # Unnumbered title -- used to require confirmation despite
                # an explicit Series column being present.
                ["Scavengers", "Author One", "Quest Academy", "2"],
                # Numbered title -- used to silently drop the series link
                # because no canonical "Quest Academy" series existed yet.
                ["Quest Academy Book 1", "Author One", "Quest Academy", "1"],
                # No series at all -- should stay standalone, no confirmation.
                ["A Standalone Novel", "Author Two", "", ""],
            ]
        )

    def tearDown(self):
        os.remove(self.csv_path)
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_series_tagged_rows_auto_link_without_confirmation(self):
        with patch("importer.pipeline.SessionLocal", self.SessionLocal):
            result = run_import(self.csv_path, profile_id="mackenzie")

        self.assertEqual(result["imported_count"], 3)

        db = self.SessionLocal()
        try:
            series_rows = db.query(Series).filter(Series.profile_id == "mackenzie").all()
            self.assertEqual(len(series_rows), 1)
            quest_academy = series_rows[0]
            self.assertEqual(quest_academy.name, "Quest Academy")

            books_by_title = {
                b.title: b for b in db.query(Book).filter(Book.profile_id == "mackenzie").all()
            }
            self.assertEqual(books_by_title["Scavengers"].series_id, quest_academy.id)
            self.assertEqual(books_by_title["Quest Academy Book 1"].series_id, quest_academy.id)
            self.assertIsNone(books_by_title["A Standalone Novel"].series_id)
        finally:
            db.close()

    def test_decision_helper_always_links_when_a_meaningful_series_name_is_given(self):
        db = self.SessionLocal()
        try:
            decision = _series_link_decision(
                db, {"title": "Scavengers", "series_name": "Quest Academy"}, "mackenzie"
            )
            self.assertTrue(decision["should_link"])

            standalone_decision = _series_link_decision(db, {"title": "Lone Book", "series_name": ""}, "mackenzie")
            self.assertFalse(standalone_decision["should_link"])
        finally:
            db.close()


class PlaceholderSeriesNameIsRejectedTest(unittest.TestCase):
    """Regression test for the real-world "Mackenzie's library" bug: her
    spreadsheet used a "\u2014 <Author Name>" marker in the Series column for
    standalone books (a common personal-tracker convention for "not part of
    a series"), which the auto-link fix above then trusted as a literal
    series name -- creating a bogus "\u2014 Rebecca Yarros" series that
    swallowed every standalone book by that author and emptied the actual
    "standalone books" view."""

    def test_dash_author_marker_is_not_a_meaningful_series_name(self):
        self.assertFalse(_is_meaningful_series_name("\u2014 Rebecca Yarros", "Rebecca Yarros"))
        self.assertFalse(_is_meaningful_series_name("- Rebecca Yarros", "Rebecca Yarros"))
        self.assertFalse(_is_meaningful_series_name("-- Rebecca Yarros", "Rebecca Yarros"))

    def test_bare_placeholder_values_are_not_meaningful_series_names(self):
        for placeholder in ("-", "--", "\u2014", "N/A", "n/a", "None", "TBD", "standalone", "   "):
            self.assertFalse(_is_meaningful_series_name(placeholder, "Some Author"), placeholder)

    def test_real_series_names_are_still_meaningful(self):
        self.assertTrue(_is_meaningful_series_name("Quest Academy", "Rebecca Yarros"))
        # A series literally named after its author (e.g. eponymous series)
        # is still meaningful as long as it isn't just a dash marker.
        self.assertTrue(_is_meaningful_series_name("Rebecca Yarros", "Rebecca Yarros"))

    def setUp(self):
        self.engine, self.SessionLocal = _new_in_memory_session_factory()
        self.csv_path = _write_csv(
            [
                ["Title", "Author", "Series", "Book #"],
                ["Fourth Wing", "Rebecca Yarros", "Empyrean", "1"],
                ["Iron Flame", "Rebecca Yarros", "Empyrean", "2"],
                # Standalone books tagged with the "-- Author" placeholder
                # convention instead of being left blank.
                ["Onyx Storm Prequel", "Rebecca Yarros", "\u2014 Rebecca Yarros", ""],
                ["A Different Standalone", "Rebecca Yarros", "-- Rebecca Yarros", ""],
            ]
        )

    def tearDown(self):
        os.remove(self.csv_path)
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_placeholder_tagged_rows_stay_standalone_not_a_bogus_series(self):
        with patch("importer.pipeline.SessionLocal", self.SessionLocal):
            result = run_import(self.csv_path, profile_id="mackenzie")

        self.assertEqual(result["imported_count"], 4)

        db = self.SessionLocal()
        try:
            series_names = {
                s.name for s in db.query(Series).filter(Series.profile_id == "mackenzie").all()
            }
            self.assertEqual(series_names, {"Empyrean"})

            books_by_title = {
                b.title: b for b in db.query(Book).filter(Book.profile_id == "mackenzie").all()
            }
            self.assertIsNone(books_by_title["Onyx Storm Prequel"].series_id)
            self.assertIsNone(books_by_title["A Different Standalone"].series_id)
        finally:
            db.close()


class RunImportPerSeriesIntelligenceIsolationTest(unittest.TestCase):
    """Regression test for the "finished series show no total" bug: the
    post-import intelligence recompute loop used to share a single
    try/except around the *entire* per-profile loop, so one series raising
    partway through silently skipped recomputing every series queued after
    it -- leaving their total_books/etc. stale or blank even though their
    books imported correctly."""

    def setUp(self):
        self.engine, self.SessionLocal = _new_in_memory_session_factory()
        self.csv_path = _write_csv(
            [
                ["Title", "Author", "Series", "Book #"],
                ["Broken Series Book", "Author One", "Broken Series", "1"],
                ["Finished Series Book 1", "Author Two", "Finished Series", "1"],
                ["Finished Series Book 2", "Author Two", "Finished Series", "2"],
            ]
        )

    def tearDown(self):
        os.remove(self.csv_path)
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    def test_one_series_raising_does_not_block_recompute_for_the_rest(self):
        real_recalculate_intelligence = importer_module.recalculate_intelligence

        def flaky_recalculate_intelligence(db, series_id, scan_result=None):
            series = db.query(Series).filter(Series.id == series_id).first()
            if series and series.name == "Broken Series":
                raise RuntimeError("simulated bad data for this one series")
            return real_recalculate_intelligence(db, series_id, scan_result=scan_result)

        with patch("importer.pipeline.SessionLocal", self.SessionLocal), patch(
            "importer.pipeline.recalculate_intelligence", side_effect=flaky_recalculate_intelligence
        ):
            run_import(self.csv_path, profile_id="mackenzie")

        db = self.SessionLocal()
        try:
            finished_series = (
                db.query(Series)
                .filter(Series.profile_id == "mackenzie", Series.name == "Finished Series")
                .first()
            )
            # Despite "Broken Series" raising, "Finished Series" (queued
            # after it) must still get its total_books computed.
            self.assertEqual(finished_series.total_books, 2)
        finally:
            db.close()


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
        with patch("importer.preview.SessionLocal", self.SessionLocal):
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
