import unittest
from datetime import date
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import crud
import discovery_engine
from agents.series_agent import SeriesIntelligenceAgent
from database import Base
from models import Book, Series


class DiscoveryEngineHelperTest(unittest.TestCase):
    """Unit tests for the pure text-normalization/matching helpers that the
    live API-based discovery pipeline depends on to identify which API
    results are new books versus ones already owned.
    """

    def test_core_title_key_matches_across_differently_formatted_titles(self):
        owned_style = "1% Lifesteal (Volume 4): A LitRPG: (1% Lifesteal Book 4)"
        api_style = "1% Lifesteal (Volume 4): A LitRPG Adventure"
        self.assertEqual(discovery_engine.core_title_key(owned_style), discovery_engine.core_title_key(api_style))

    def test_core_title_key_distinguishes_volumes_with_shared_prefix(self):
        # Regression: volume number lives inside the "(...)" segment for this
        # series, so truncating there (without folding the number back in)
        # collapsed every volume to the same key.
        key_4 = discovery_engine.core_title_key("1% Lifesteal (Volume 4): A LitRPG Adventure")
        key_5 = discovery_engine.core_title_key("1% Lifesteal (Volume 5): A LitRPG Adventure")
        self.assertNotEqual(key_4, key_5)

    def test_core_title_key_matches_across_comma_and_bare_subtitle_formats(self):
        # Regression: some providers separate a short-story subtitle with a
        # comma rather than a colon/paren (e.g. Hardcover's "..., A Series
        # Short Story"), while others return just the bare title.
        comma_style = "Havoc in the Deathyards, A Completionist Chronicles Short Story"
        bare_style = "Havoc in the Deathyards"
        self.assertEqual(discovery_engine.core_title_key(comma_style), discovery_engine.core_title_key(bare_style))
        self.assertEqual(discovery_engine.bare_title_key(comma_style), discovery_engine.bare_title_key(bare_style))

    def test_core_title_key_comma_split_still_distinguishes_numbered_siblings(self):
        # Some owned titles use a comma as part of the title itself (not a
        # subtitle separator), e.g. this series' convention. Splitting on
        # the first comma over-truncates the "core", but the book number
        # (parsed from the full raw title) still keeps siblings distinct.
        key_9 = discovery_engine.core_title_key("Webs of Power, The Grand Game, Book 9: A Dark Fantasy LitRPG")
        key_10 = discovery_engine.core_title_key("The Mad God, The Grand Game, Book 10: A Dark Fantasy LitRPG")
        self.assertNotEqual(key_9, key_10)

    def test_infer_number_from_title_recognizes_common_patterns(self):
        self.assertEqual(discovery_engine.infer_number_from_title("Cherry Blossom Girls Book 7"), 7)
        self.assertEqual(discovery_engine.infer_number_from_title("Cherry Blossom Girls Volume 7"), 7)
        self.assertEqual(discovery_engine.infer_number_from_title("Cherry Blossom Girls #7"), 7)
        self.assertEqual(discovery_engine.infer_number_from_title("Cherry Blossom Girls Book Seven"), 7)

    def test_infer_number_from_title_recognizes_bare_trailing_number(self):
        # Many rapid-release indie/LitRPG series just number titles as
        # "<Series Name> <N>" with no "book"/"vol"/"#" keyword at all.
        self.assertEqual(discovery_engine.infer_number_from_title("All the Skills 5", "All The Skills"), 5)

    def test_infer_number_from_title_recognizes_series_name_and_number_mid_title(self):
        # Regression: a reprint listing titled "By Schism Rent Asunder
        # (Safehold 2) Publisher: Tor Science Fiction; Reprint edition"
        # embeds "<series name> <N>" as a parenthetical partway through the
        # title rather than as a prefix, so the bare-trailing-number check
        # (which only looks at the very start of the title) missed it,
        # leaving this reprint of an already-owned book with no resolvable
        # number at all.
        self.assertEqual(
            discovery_engine.infer_number_from_title(
                "By Schism Rent Asunder (Safehold 2) Publisher: Tor Science Fiction; Reprint edition",
                "Safehold",
            ),
            2,
        )

    def test_looks_like_non_new_release_filters_bundles_and_editions(self):
        self.assertTrue(discovery_engine.looks_like_non_new_release("Cherry Blossom Girls Books 1-3 Box Set"))
        self.assertTrue(discovery_engine.looks_like_non_new_release("Cherry Blossom Girls: French Edition"))
        self.assertFalse(discovery_engine.looks_like_non_new_release("Cherry Blossom Girls Book 7"))

    def test_parse_flexible_date_handles_partial_precision(self):
        self.assertEqual(discovery_engine.parse_flexible_date("2024-03-12"), date(2024, 3, 12))
        self.assertEqual(discovery_engine.parse_flexible_date("2024-03"), date(2024, 3, 1))
        self.assertEqual(discovery_engine.parse_flexible_date("2024"), date(2024, 1, 1))
        self.assertIsNone(discovery_engine.parse_flexible_date(""))


class DiscoverCandidatesForSeriesTest(unittest.TestCase):
    """Tests discovery_engine.discover_candidates_for_series's merge/priority
    behavior across the three providers, with all three network calls
    mocked out so this runs offline and deterministically.
    """

    def test_hardcover_result_wins_over_google_on_same_book(self):
        # Hardcover tags each hit with its actual series position and
        # release status, which is more trustworthy than Google's free-text
        # match for indie/self-published titles -- so when both providers
        # return the same book, Hardcover's copy (with its number hint)
        # should be the one that survives the merge.
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {
                    "source": "hardcover",
                    "source_id": "hc-1",
                    "title": "Cherry Blossom Girls Book 7",
                    "authors": ["Harmon Cooper"],
                    "published_date": "2024-02-20",
                    "isbn13": None,
                    "source_url": None,
                    "language": "",
                    "series_number_hint": 7,
                    "upcoming_hint": False,
                }
            ],
        ), patch.object(
            discovery_engine,
            "_fetch_google_books",
            return_value=[
                {
                    "source": "google_books",
                    "source_id": "gb-1",
                    "title": "Cherry Blossom Girls Book 7",
                    "authors": ["Harmon Cooper"],
                    "published_date": "2024-02-20",
                    "isbn13": None,
                    "source_url": None,
                    "language": "",
                }
            ],
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            result = discovery_engine.discover_candidates_for_series("Cherry Blossom Girls", "Harmon Cooper")

        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["source"], "hardcover")
        self.assertEqual(result["candidates"][0]["series_number_hint"], 7)

    def test_excludes_titles_already_owned(self):
        with patch.object(
            discovery_engine,
            "_fetch_hardcover",
            return_value=[
                {
                    "source": "hardcover",
                    "source_id": "hc-1",
                    "title": "Cherry Blossom Girls Book 7",
                    "authors": ["Harmon Cooper"],
                    "published_date": "2024-02-20",
                    "isbn13": None,
                    "source_url": None,
                    "language": "",
                    "series_number_hint": 7,
                    "upcoming_hint": False,
                }
            ],
        ), patch.object(discovery_engine, "_fetch_google_books", return_value=[]), patch.object(
            discovery_engine, "_fetch_openlibrary", return_value=[]
        ):
            owned_key = discovery_engine.core_title_key("Cherry Blossom Girls Book 7")
            result = discovery_engine.discover_candidates_for_series(
                "Cherry Blossom Girls", "Harmon Cooper", exclude_title_keys={owned_key}
            )

        self.assertEqual(result["candidates"], [])

    def test_all_providers_failing_is_reported_distinctly_from_no_results(self):
        with patch.object(discovery_engine, "_fetch_hardcover", side_effect=RuntimeError("boom")), patch.object(
            discovery_engine, "_fetch_google_books", side_effect=RuntimeError("boom")
        ), patch.object(discovery_engine, "_fetch_openlibrary", side_effect=RuntimeError("boom")):
            result = discovery_engine.discover_candidates_for_series(
                "Cherry Blossom Girls", "Harmon Cooper", allow_author_fallback=False
            )

        self.assertTrue(result["all_providers_failed"])
        self.assertEqual(len(result["provider_failures"]), 3)

    def test_partial_provider_failure_is_not_all_providers_failed(self):
        with patch.object(discovery_engine, "_fetch_hardcover", return_value=[]), patch.object(
            discovery_engine, "_fetch_google_books", side_effect=RuntimeError("503")
        ), patch.object(discovery_engine, "_fetch_openlibrary", return_value=[]):
            result = discovery_engine.discover_candidates_for_series(
                "Cherry Blossom Girls", "Harmon Cooper", allow_author_fallback=False
            )

        self.assertFalse(result["all_providers_failed"])
        self.assertEqual(len(result["provider_failures"]), 1)


class SeriesCheckIntegrationTest(unittest.TestCase):
    """Integration tests for SeriesIntelligenceAgent.run_series_check against
    an in-memory database, with discovery_engine mocked so behavior is
    deterministic and doesn't depend on live third-party APIs.
    """

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=cls.engine)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setUp(self):
        self.db = self.SessionLocal()
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

        for number in [1, 2, 3, 4, 5, 6, 8, 9]:
            self.db.add(
                Book(
                    title=f"Cherry Blossom Girls Book {number}",
                    author="Harmon Cooper",
                    series_id=series.id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=False,
                )
            )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _mock_discovery(self, candidates, **overrides):
        result = {
            "candidates": candidates,
            "provider_failures": [],
            "all_providers_failed": False,
            "used_author_fallback": False,
        }
        result.update(overrides)
        return patch("discovery_engine.discover_candidates_for_series", return_value=result)

    def test_available_book_is_added_and_classified_available(self):
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-7",
                "title": "Cherry Blossom Girls Book 7",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": 7,
                "upcoming_hint": False,
            }
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertTrue(result["found"])
        self.assertEqual(len(result["available_missing"]), 1)
        self.assertEqual(result["available_missing"][0]["series_number"], 7)
        self.assertEqual(result["upcoming_books"], [])

    def test_future_dated_book_is_classified_upcoming(self):
        far_future_year = date.today().year + 5
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-10",
                "title": "Cherry Blossom Girls Book 10",
                "authors": ["Harmon Cooper"],
                "published_date": f"{far_future_year}-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": 10,
                "upcoming_hint": False,
            }
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertTrue(result["found"])
        self.assertEqual(result["available_missing"], [])
        self.assertEqual(len(result["upcoming_books"]), 1)
        self.assertEqual(result["upcoming_books"][0]["series_number"], 10)

    def test_unreleased_hint_marks_upcoming_even_without_a_parseable_date(self):
        # Hardcover can flag a book as not-yet-released without providing a
        # release date at all -- that hint alone should be enough.
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-10",
                "title": "Cherry Blossom Girls Book 10",
                "authors": ["Harmon Cooper"],
                "published_date": "",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": 10,
                "upcoming_hint": True,
            }
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(result["available_missing"], [])
        self.assertEqual(len(result["upcoming_books"]), 1)

    def test_already_owned_book_number_is_not_reported_as_new(self):
        candidates = [
            {
                "source": "google_books",
                "source_id": "gb-2",
                "title": "Cherry Blossom Girls Book 2 -- Special Reissue",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-02-20",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            }
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertFalse(result["found"])
        self.assertEqual(result["available_missing"], [])
        self.assertEqual(result["upcoming_books"], [])

    def test_bare_title_with_no_number_matches_owned_book_by_unique_stem(self):
        # Regression (live bug): "Unbound" book 9 is titled "Crown: A LitRPG:
        # (Unbound Book 9)" in the library, but Google Books surfaced it as
        # just the bare word "Crown" -- no "Book 9" suffix, no digits at
        # all, and no series_number_hint. core_title_key folds the "9" into
        # the *owned* book's key ("crown 9") but has nothing to fold into
        # the bare candidate's key ("crown"), so the two never matched and
        # it got re-added as a live "available" duplicate of an already-read
        # book. It should instead be recognized as already owned via the
        # unique bare-title fallback.
        self.db.add(
            Book(
                title="Crown: A LitRPG: (Cherry Blossom Girls Book 9)",
                author="Harmon Cooper",
                series_id=self.series.id,
                series_order=9,
                book_number=9.0,
                record_status="active",
                is_read=True,
            )
        )
        self.db.commit()

        candidates = [
            {
                "source": "google_books",
                "source_id": "gb-crown",
                "title": "Crown",
                "authors": ["Harmon Cooper"],
                "published_date": "2020-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            }
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertFalse(result["found"])
        self.assertEqual(result["available_missing"], [])
        self.assertEqual(result["upcoming_books"], [])

    def test_same_new_book_from_two_providers_with_different_title_formats_is_added_once(self):
        # Regression (live bug): checking a series surfaced a companion short
        # story that Hardcover titled "Havoc in the Deathyards, A Cherry
        # Blossom Girls Short Story" and OpenLibrary titled bare "Havoc in
        # the Deathyards" -- neither had an ISBN or a parseable book number,
        # so both slipped through as "new" and got added as two separate
        # duplicate library entries for the same real short story. (The
        # first candidate's title explicitly references the series by name,
        # which is what makes this different from an unrelated same-author
        # book that merely came back as a "targeted"-confidence hit --
        # confidence alone isn't trusted without either a number or some
        # textual tie to the series; see test_author_fallback_... below.)
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-havoc",
                "title": "Havoc in the Deathyards, A Cherry Blossom Girls Short Story",
                "authors": ["Harmon Cooper"],
                "published_date": "2022-06-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
            {
                "source": "openlibrary",
                "source_id": "ol-havoc",
                "title": "Havoc in the Deathyards",
                "authors": ["Harmon Cooper"],
                "published_date": "2022-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(result["added_count"], 1)
        self.assertEqual(len(result["available_missing"]), 1)

    def test_targeted_confidence_alone_does_not_pull_in_unrelated_same_author_books(self):
        # Regression (live bug): checking "Safehold" by David Weber (a
        # prolific author with many unrelated series) surfaced "Bolo!",
        # "Worlds Of Honor", and "At All Costs" -- real David Weber books,
        # but from entirely different series -- as "available" candidates.
        # They came back tagged confidence="targeted" (the API's own
        # relevance ranking against "Safehold David Weber"), had no
        # parseable book number, and had zero textual reference to
        # "Safehold" anywhere in the title. Trusting confidence=="targeted"
        # alone, with no number and no textual tie to the series, is too
        # weak a signal -- it should be rejected, while a same-batch hit
        # that actually continues the numbering (book 8) should still be
        # accepted.
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-bolo",
                "title": "Bolo!",
                "authors": ["Harmon Cooper"],
                "published_date": "2020-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
            {
                "source": "hardcover",
                "source_id": "hc-worlds-of-honor",
                "title": "Worlds Of Honor",
                "authors": ["Harmon Cooper"],
                "published_date": "2020-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
            {
                "source": "hardcover",
                "source_id": "hc-book10",
                "title": "Cherry Blossom Girls Book 10",
                "authors": ["Harmon Cooper"],
                "published_date": "2024-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(result["added_count"], 1)
        self.assertEqual(
            [book["title"] for book in result["available_missing"]],
            ["Cherry Blossom Girls Book 10"],
        )

    def test_owned_omnibus_range_prevents_individual_volume_reappearing_as_new(self):
        # Regression (live bug): "Safehold" is owned as a boxed set covering
        # books 1-3 in one row ("Safehold Boxed Set 1: (Safehold Books
        # 1-3)"), plus individually-owned books 4-7. Because that omnibus
        # row's own book_number is just 1 (its position in the shelf, not a
        # range), the discovery/dedupe identity sets didn't know books 2 and
        # 3 were already covered -- so a reprint single-volume edition like
        # "By Heresies Distressed (Safehold Book 3)" slipped through and got
        # added as a second, duplicate "available" copy of an already-owned
        # book.
        series = Series(name="Safehold", author="David Weber")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)

        self.db.add(
            Book(
                title="Safehold Boxed Set 1: (Safehold Books 1-3)",
                author="David Weber",
                series_id=series.id,
                series_order=1,
                book_number=1.0,
                record_status="active",
                is_read=True,
            )
        )
        for number in [4, 5, 6, 7]:
            self.db.add(
                Book(
                    title=f"Safehold Book {number}",
                    author="David Weber",
                    series_id=series.id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=True,
                )
            )
        self.db.commit()

        candidates = [
            {
                "source": "google_books",
                "source_id": "gb-heresies-reprint",
                "title": "By Heresies Distressed (Safehold Book 3)",
                "authors": ["David Weber"],
                "published_date": "2010-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
            {
                "source": "google_books",
                "source_id": "gb-book8",
                "title": "Safehold Book 8",
                "authors": ["David Weber"],
                "published_date": "2024-01-01",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": None,
                "upcoming_hint": False,
            },
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, series.id, emit_summary=False)

        self.assertEqual(
            [book["title"] for book in result["available_missing"]],
            ["Safehold Book 8"],
        )

    def test_bare_title_with_no_series_reference_gets_a_synthesized_suffix(self):
        # Regression: Hardcover tracks series position as structured data
        # rather than embedding it in the title text, so it can return a
        # clean bare title like "Unmapped" with no series name or book
        # number anywhere in it -- making it hard to tell at a glance that
        # the newly added book belongs to this series at all.
        candidates = [
            {
                "source": "hardcover",
                "source_id": "hc-13",
                "title": "Unmapped",
                "authors": ["Harmon Cooper"],
                "published_date": "2026-01-14",
                "isbn13": None,
                "source_url": None,
                "language": "",
                "confidence": "targeted",
                "series_number_hint": 13,
                "upcoming_hint": False,
            }
        ]
        with self._mock_discovery(candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, self.series.id, emit_summary=False)

        self.assertEqual(len(result["available_missing"]), 1)
        self.assertEqual(
            result["available_missing"][0]["title"],
            "Unmapped: (Cherry Blossom Girls Book 13)",
        )

    def test_no_author_on_file_returns_empty_result_without_calling_apis(self):
        series = Series(name="No Author Series")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)

        with patch("discovery_engine.discover_candidates_for_series") as mock_discover:
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, series.id, emit_summary=False)

        mock_discover.assert_not_called()
        self.assertEqual(result["reason"], "series-missing-author")
        self.assertFalse(result["found"])


class ManualDeleteRecalculationTest(unittest.TestCase):
    """total_books tracks the highest known book number in the series (not
    a plain count), so deleting a book that isn't the highest-numbered one
    should leave total_books unchanged while read/unread counts still
    reflect the smaller active set.
    """

    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=cls.engine)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setUp(self):
        self.db = self.SessionLocal()
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

        for number in [1, 2, 3, 4, 5, 6, 8, 9]:
            self.db.add(
                Book(
                    title=f"Book {number}",
                    author="Harmon Cooper",
                    series_id=series.id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=False,
                )
            )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_manual_delete_recounts_series_aggregates(self):
        keep_read = self.db.query(Book).filter(Book.series_id == self.series.id, Book.book_number == 2.0).first()
        delete_target = self.db.query(Book).filter(Book.series_id == self.series.id, Book.book_number == 1.0).first()
        self.assertIsNotNone(keep_read)
        self.assertIsNotNone(delete_target)
        keep_read.is_read = True
        self.db.commit()

        deleted = crud.delete_book(self.db, delete_target.id)
        self.assertTrue(deleted)

        refreshed = self.db.query(Series).filter(Series.id == self.series.id).first()
        self.assertIsNotNone(refreshed)
        self.assertEqual(refreshed.total_books, 9)
        self.assertEqual(refreshed.read_count, 1)
        self.assertEqual(refreshed.unread_count, 6)


if __name__ == "__main__":
    unittest.main()
