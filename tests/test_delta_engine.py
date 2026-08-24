"""TG-1: full test suite for delta_engine.py -- the single largest test gap
identified in the repo cleanup audit (zero tests existed for this module
before this file). Covers `compute_series_delta` end-to-end plus its
private helpers directly, since `_malformed_reason`'s branch order/behavior
is exactly what CR-8's confidence_engine wiring (tests/test_confidence_engine.py)
depends on staying correct.
"""
import copy
import unittest

import delta_engine


def _candidate(
    title: str = "A Real Book",
    series_number=3,
    isbn13=None,
    metadata_completeness_score: float = 1.0,
    source: str = "google_books",
) -> dict:
    return {
        "title": title,
        "series_number": series_number,
        "isbn13": isbn13,
        "metadata_completeness_score": metadata_completeness_score,
        "source_provenance": [{"source": source}],
    }


def _skeleton_entry(book_number) -> dict:
    return {"book_number": book_number, "status": "confirmed"}


class ToFloatOrNoneTest(unittest.TestCase):
    def test_none_stays_none(self):
        self.assertIsNone(delta_engine._to_float_or_none(None))

    def test_int_becomes_float(self):
        self.assertEqual(delta_engine._to_float_or_none(3), 3.0)

    def test_numeric_string_becomes_float(self):
        self.assertEqual(delta_engine._to_float_or_none("3.5"), 3.5)

    def test_non_numeric_string_becomes_none(self):
        self.assertIsNone(delta_engine._to_float_or_none("not a number"))

    def test_bad_type_becomes_none(self):
        self.assertIsNone(delta_engine._to_float_or_none(["not", "a", "number"]))


class CandidateProvidersTest(unittest.TestCase):
    def test_reads_source_key_not_provider_key(self):
        candidate = {"source_provenance": [{"source": "google_books"}, {"provider": "should_be_ignored"}]}
        self.assertEqual(delta_engine._candidate_providers(candidate), ["google_books"])

    def test_dedupes_and_sorts(self):
        candidate = {
            "source_provenance": [
                {"source": "web_search"},
                {"source": "google_books"},
                {"source": "google_books"},
            ]
        }
        self.assertEqual(delta_engine._candidate_providers(candidate), ["google_books", "web_search"])

    def test_missing_provenance_is_empty_list(self):
        self.assertEqual(delta_engine._candidate_providers({}), [])

    def test_entries_without_a_source_key_are_skipped(self):
        candidate = {"source_provenance": [{"other_key": "x"}]}
        self.assertEqual(delta_engine._candidate_providers(candidate), [])


class MalformedReasonTest(unittest.TestCase):
    def test_real_distinctly_titled_book_is_not_malformed(self):
        self.assertIsNone(delta_engine._malformed_reason("Some Series", _candidate(title="The Jericho Siege")))

    def test_missing_title_is_malformed(self):
        self.assertEqual(delta_engine._malformed_reason("Some Series", _candidate(title="")), "missing_title")

    def test_whitespace_only_title_is_malformed(self):
        self.assertEqual(delta_engine._malformed_reason("Some Series", _candidate(title="   ")), "missing_title")

    def test_placeholder_title_is_malformed(self):
        self.assertEqual(
            delta_engine._malformed_reason("Some Series", _candidate(title="Untitled")), "placeholder_title"
        )

    def test_bare_series_variant_title_is_malformed(self):
        # series_number=None here matters: _title_is_series_variant treats
        # any truthy structured_number_hint as corroboration once the title
        # isn't an exact match to the series name (see its own docstring),
        # so a bare genre-tagline title is only caught when no such hint is
        # present -- exactly the live regression this mirrors (Jonathan Hunt,
        # fixtures/eval_regressions/jonathan_hunt.json).
        self.assertEqual(
            delta_engine._malformed_reason(
                "Jonathan Hunt", _candidate(title="A Jonathan Hunt Thriller", series_number=None)
            ),
            "title_is_series_variant",
        )

    def test_series_variant_with_isbn_is_not_malformed(self):
        # _title_is_series_variant itself short-circuits when isbn13 is
        # present -- delta_engine doesn't re-implement that judgment.
        self.assertIsNone(
            delta_engine._malformed_reason(
                "Jonathan Hunt",
                _candidate(title="A Jonathan Hunt Thriller", series_number=None, isbn13="9781234567897"),
            )
        )

    def test_non_numeric_series_number_is_invalid_number(self):
        self.assertEqual(
            delta_engine._malformed_reason("Some Series", _candidate(series_number="not-a-number")),
            "invalid_number",
        )

    def test_zero_series_number_is_negative_number(self):
        self.assertEqual(delta_engine._malformed_reason("Some Series", _candidate(series_number=0)), "negative_number")

    def test_negative_series_number_is_negative_number(self):
        self.assertEqual(
            delta_engine._malformed_reason("Some Series", _candidate(series_number=-1)), "negative_number"
        )

    def test_missing_series_number_is_not_malformed_on_number_grounds(self):
        self.assertIsNone(delta_engine._malformed_reason("Some Series", _candidate(series_number=None)))

    def test_insufficient_metadata_below_threshold_is_malformed(self):
        candidate = _candidate(metadata_completeness_score=0.1)
        self.assertEqual(delta_engine._malformed_reason("Some Series", candidate), "insufficient_metadata")

    def test_metadata_completeness_at_threshold_is_not_malformed(self):
        candidate = _candidate(
            metadata_completeness_score=delta_engine.discovery_engine.RECONCILIATION_METADATA_COMPLETENESS_THRESHOLD
        )
        self.assertIsNone(delta_engine._malformed_reason("Some Series", candidate))

    def test_missing_metadata_completeness_score_is_not_malformed_on_that_ground(self):
        candidate = _candidate(metadata_completeness_score=None)
        self.assertIsNone(delta_engine._malformed_reason("Some Series", candidate))

    def test_first_matching_reason_wins_when_several_apply(self):
        # Missing title is checked before the number checks -- an empty
        # title AND a bad number should still report missing_title.
        candidate = _candidate(title="", series_number="garbage")
        self.assertEqual(delta_engine._malformed_reason("Some Series", candidate), "missing_title")


class ComputeSeriesDeltaTest(unittest.TestCase):
    def test_new_number_not_in_skeleton_is_a_missing_book(self):
        skeleton = [_skeleton_entry(1), _skeleton_entry(2)]
        candidates = [_candidate(title="Book Three", series_number=3)]
        delta = delta_engine.compute_series_delta(42, skeleton, candidates, series_name="Some Series")
        self.assertEqual(len(delta["missing_books"]), 1)
        self.assertEqual(delta["missing_books"][0]["book_number"], 3.0)
        self.assertEqual(delta["missing_books"][0]["title"], "Book Three")
        self.assertEqual(delta["missing_books"][0]["providers"], ["google_books"])
        self.assertEqual(delta["missing_books"][0]["series_id"], 42)

    def test_number_already_in_skeleton_is_not_a_missing_book(self):
        skeleton = [_skeleton_entry(1), _skeleton_entry(2)]
        candidates = [_candidate(title="Book Two", series_number=2)]
        delta = delta_engine.compute_series_delta(42, skeleton, candidates, series_name="Some Series")
        self.assertEqual(delta["missing_books"], [])

    def test_candidate_with_no_number_at_all_is_a_missing_book(self):
        skeleton = [_skeleton_entry(1)]
        candidates = [_candidate(title="Some Unnumbered Book", series_number=None)]
        delta = delta_engine.compute_series_delta(42, skeleton, candidates, series_name="Some Series")
        self.assertEqual(len(delta["missing_books"]), 1)
        self.assertIsNone(delta["missing_books"][0]["book_number"])

    def test_malformed_candidate_is_never_also_a_missing_book(self):
        skeleton: list[dict] = []
        candidates = [_candidate(title="Untitled")]
        delta = delta_engine.compute_series_delta(42, skeleton, candidates, series_name="Some Series")
        self.assertEqual(delta["missing_books"], [])
        self.assertEqual(len(delta["malformed_books"]), 1)
        self.assertEqual(delta["malformed_books"][0]["reason"], "placeholder_title")
        self.assertEqual(delta["malformed_books"][0]["series_id"], 42)
        self.assertIs(delta["malformed_books"][0]["candidate"], candidates[0])

    def test_two_non_malformed_candidates_sharing_a_number_are_both_flagged_duplicate(self):
        skeleton: list[dict] = []
        candidates = [
            _candidate(title="Book Three (Google)", series_number=3, source="google_books"),
            _candidate(title="Book Three (OpenLibrary)", series_number=3, source="openlibrary"),
        ]
        delta = delta_engine.compute_series_delta(42, skeleton, candidates, series_name="Some Series")
        duplicate_reasons = [m["reason"] for m in delta["malformed_books"]]
        self.assertEqual(duplicate_reasons, ["duplicate_number:3.0", "duplicate_number:3.0"])
        # Both duplicates still surface as missing_books too -- duplicate
        # detection doesn't suppress the missing-book signal, since we
        # don't know yet which (if either) copy is the "real" one.
        self.assertEqual(len(delta["missing_books"]), 2)

    def test_numbering_gaps_are_skeleton_numbers_not_resurfaced_this_round(self):
        skeleton = [_skeleton_entry(1), _skeleton_entry(2), _skeleton_entry(3)]
        candidates = [_candidate(title="Book One", series_number=1)]
        delta = delta_engine.compute_series_delta(42, skeleton, candidates, series_name="Some Series")
        self.assertEqual(delta["numbering_gaps"], [2.0, 3.0])

    def test_numbering_gaps_sorted_ascending(self):
        skeleton = [_skeleton_entry(5), _skeleton_entry(1), _skeleton_entry(3)]
        delta = delta_engine.compute_series_delta(42, skeleton, [], series_name="Some Series")
        self.assertEqual(delta["numbering_gaps"], [1.0, 3.0, 5.0])

    def test_a_malformed_candidates_number_does_not_close_a_numbering_gap(self):
        # A malformed candidate never reaches the found_numbers bookkeeping
        # -- its number staying "missing" even if it happens to match a
        # skeleton number is deliberate: an untitled/placeholder hit isn't
        # real confirmation that book N still exists.
        skeleton = [_skeleton_entry(1)]
        candidates = [_candidate(title="Untitled", series_number=1)]
        delta = delta_engine.compute_series_delta(42, skeleton, candidates, series_name="Some Series")
        self.assertEqual(delta["numbering_gaps"], [1.0])

    def test_empty_inputs_produce_empty_outputs(self):
        delta = delta_engine.compute_series_delta(42, [], [], series_name=None)
        self.assertEqual(delta["missing_books"], [])
        self.assertEqual(delta["malformed_books"], [])
        self.assertEqual(delta["numbering_gaps"], [])
        self.assertEqual(delta["series_id"], 42)
        self.assertIn("timestamp", delta)

    def test_does_not_mutate_its_inputs(self):
        skeleton = [_skeleton_entry(1), _skeleton_entry(2)]
        candidates = [_candidate(title="Book Three", series_number=3), _candidate(title="Untitled")]
        skeleton_before = copy.deepcopy(skeleton)
        candidates_before = copy.deepcopy(candidates)
        delta_engine.compute_series_delta(42, skeleton, candidates, series_name="Some Series")
        self.assertEqual(skeleton, skeleton_before)
        self.assertEqual(candidates, candidates_before)

    def test_series_name_none_still_runs_the_series_variant_check_safely(self):
        # _title_is_series_variant must tolerate series_name=None without
        # raising, since compute_series_delta's own series_name parameter
        # is optional.
        delta = delta_engine.compute_series_delta(
            42, [], [_candidate(title="Some Book", series_number=1)], series_name=None
        )
        self.assertEqual(delta["malformed_books"], [])


if __name__ == "__main__":
    unittest.main()
