"""Tests for the Series Fingerprint system (see
`discovery_agentic_fingerprint_recommendation.md` for the full
ten-round design chain this module implements): the two-tier activation
gate, the Builder's pure observation functions, the merge rules
(append-dedupe for lists, EMA for provider bias, full-recompute for
release cadence), and the single-writer-per-row upsert.
"""

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import settings
from database import Base
from models import Series, SeriesFingerprint
from services.fingerprint_store import (
    PROVIDER_BIAS_MAX,
    PROVIDER_BIAS_MIN,
    _compute_release_cadence,
    _merge_provider_bias,
    _merge_string_list,
    apply_fingerprint_updates,
    build_fingerprint_observations,
    get_effective_fingerprint,
    get_fingerprint_row,
)


class FingerprintStoreTestBase(unittest.TestCase):
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
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

    def tearDown(self):
        self.db.close()

    def _fingerprint_row(self):
        return (
            self.db.query(SeriesFingerprint)
            .filter(SeriesFingerprint.series_id == self.series.id)
            .first()
        )


class ActivationGateTest(FingerprintStoreTestBase):
    """Two-tier gate: global FINGERPRINT_INFLUENCE_ENABLED AND-ed with the
    per-series FINGERPRINT_SERIES_ACTIVATION allowlist -- see
    settings.is_fingerprint_activated.
    """

    def test_none_when_global_flag_off_even_with_no_row(self):
        with patch.object(settings, "FINGERPRINT_INFLUENCE_ENABLED", False):
            self.assertIsNone(get_effective_fingerprint(self.db, self.series.id))

    def test_none_when_global_on_but_series_not_in_allowlist(self):
        with patch.object(settings, "FINGERPRINT_INFLUENCE_ENABLED", True), patch.object(
            settings, "FINGERPRINT_SERIES_ACTIVATION", str(self.series.id + 999)
        ):
            self.assertIsNone(get_effective_fingerprint(self.db, self.series.id))

    def test_empty_fingerprint_when_activated_but_no_row_yet(self):
        with patch.object(settings, "FINGERPRINT_INFLUENCE_ENABLED", True), patch.object(
            settings, "FINGERPRINT_SERIES_ACTIVATION", str(self.series.id)
        ):
            fingerprint = get_effective_fingerprint(self.db, self.series.id)
            self.assertIsNotNone(fingerprint)
            self.assertEqual(fingerprint["author_aliases"], [])
            self.assertEqual(fingerprint["provider_bias"], {})

    def test_returns_persisted_row_once_activated(self):
        apply_fingerprint_updates(
            self.db,
            self.series.id,
            updates={"author_alias_observations": ["H. Cooper"]},
        )
        with patch.object(settings, "FINGERPRINT_INFLUENCE_ENABLED", True), patch.object(
            settings, "FINGERPRINT_SERIES_ACTIVATION", str(self.series.id)
        ):
            fingerprint = get_effective_fingerprint(self.db, self.series.id)
            self.assertEqual(fingerprint["author_aliases"], ["H. Cooper"])

    def test_building_never_requires_activation(self):
        # apply_fingerprint_updates (the Builder) must run regardless of
        # either gate -- "shadow-first": the row is always built, only
        # its *influence* on scoring is gated.
        with patch.object(settings, "FINGERPRINT_INFLUENCE_ENABLED", False):
            apply_fingerprint_updates(
                self.db,
                self.series.id,
                updates={"author_alias_observations": ["H. Cooper"]},
            )
        row = self._fingerprint_row()
        self.assertIsNotNone(row)
        self.assertEqual(row.fingerprint_json["author_aliases"], ["H. Cooper"])


class ApplyFingerprintUpdatesTest(FingerprintStoreTestBase):
    def test_creates_row_on_first_call(self):
        self.assertIsNone(get_fingerprint_row(self.db, self.series.id))
        apply_fingerprint_updates(
            self.db,
            self.series.id,
            updates={"naming_pattern_observations": ["dash_series_marker"]},
        )
        row = get_fingerprint_row(self.db, self.series.id)
        self.assertIsNotNone(row)
        self.assertEqual(row.version, 0)
        self.assertEqual(row.fingerprint_json["naming_patterns"], ["dash_series_marker"])

    def test_second_call_merges_and_bumps_version(self):
        apply_fingerprint_updates(
            self.db, self.series.id, updates={"author_alias_observations": ["H. Cooper"]}
        )
        apply_fingerprint_updates(
            self.db, self.series.id, updates={"author_alias_observations": ["Harmon C."]}
        )
        row = get_fingerprint_row(self.db, self.series.id)
        self.assertEqual(row.version, 1)
        self.assertEqual(row.fingerprint_json["author_aliases"], ["H. Cooper", "Harmon C."])

    def test_none_updates_is_a_safe_no_op_merge(self):
        apply_fingerprint_updates(self.db, self.series.id, updates=None)
        row = get_fingerprint_row(self.db, self.series.id)
        self.assertIsNotNone(row)
        self.assertEqual(row.fingerprint_json["author_aliases"], [])

    def test_unknown_series_id_returns_none_without_raising(self):
        result = apply_fingerprint_updates(self.db, series_id=999999, updates={})
        self.assertIsNone(result)


class MergeStringListTest(unittest.TestCase):
    def test_append_dedupes_case_insensitively(self):
        merged = _merge_string_list(["H. Cooper"], ["h. cooper", "Harmon C."])
        self.assertEqual(merged, ["H. Cooper", "Harmon C."])

    def test_caps_at_max_stored_strings_keeping_most_recent(self):
        existing = [f"alias-{i}" for i in range(30)]
        merged = _merge_string_list(existing, ["alias-new"])
        self.assertEqual(len(merged), 25)
        self.assertEqual(merged[-1], "alias-new")
        self.assertNotIn("alias-0", merged)

    def test_blank_and_none_values_are_dropped(self):
        merged = _merge_string_list([], ["", None, "  ", "real"])
        self.assertEqual(merged, ["real"])


class MergeProviderBiasTest(unittest.TestCase):
    def test_new_provider_starts_from_neutral_1_0(self):
        merged = _merge_provider_bias({}, {"hardcover": 1.3})
        # EMA from a neutral prior of 1.0 with alpha=0.2:
        # 1.0*0.8 + 1.3*0.2 = 1.06
        self.assertAlmostEqual(merged["hardcover"], 1.06, places=4)

    def test_repeated_positive_signal_trends_upward_but_stays_bounded(self):
        bias = {}
        for _ in range(200):
            bias = _merge_provider_bias(bias, {"hardcover": 1.3})
        self.assertLessEqual(bias["hardcover"], PROVIDER_BIAS_MAX)

    def test_repeated_negative_signal_trends_downward_but_stays_bounded(self):
        bias = {"hardcover": 1.0}
        for _ in range(200):
            bias = _merge_provider_bias(bias, {"hardcover": 0.7})
        self.assertGreaterEqual(bias["hardcover"], PROVIDER_BIAS_MIN)

    def test_provider_absent_this_round_is_left_untouched_not_decayed(self):
        merged = _merge_provider_bias({"hardcover": 1.4, "openlibrary": 0.6}, {"hardcover": 1.3})
        self.assertEqual(merged["openlibrary"], 0.6)


class ComputeReleaseCadenceTest(unittest.TestCase):
    def test_no_library_entries_yields_empty_cadence(self):
        cadence = _compute_release_cadence([])
        self.assertIsNone(cadence["mean_interval_days"])
        self.assertEqual(cadence["interval_count"], 0)

    def test_two_dated_library_entries_yield_one_interval(self):
        entries = [
            {"book_number": 1.0, "release_date": "2020-01-01", "source_class": "library"},
            {"book_number": 2.0, "release_date": "2020-07-01", "source_class": "library"},
        ]
        cadence = _compute_release_cadence(entries)
        self.assertEqual(cadence["interval_count"], 1)
        self.assertAlmostEqual(cadence["mean_interval_days"], 182)
        self.assertEqual(cadence["stddev_interval_days"], 0.0)

    def test_non_library_entries_are_excluded(self):
        entries = [
            {"book_number": 1.0, "release_date": "2020-01-01", "source_class": "library"},
            {"book_number": 2.0, "release_date": "2020-02-01", "source_class": "discovered"},
        ]
        cadence = _compute_release_cadence(entries)
        self.assertEqual(cadence["interval_count"], 0)

    def test_entries_missing_release_date_are_excluded_not_zero(self):
        entries = [
            {"book_number": 1.0, "release_date": "2020-01-01", "source_class": "library"},
            {"book_number": 2.0, "release_date": None, "source_class": "library"},
            {"book_number": 3.0, "release_date": "2020-07-01", "source_class": "library"},
        ]
        cadence = _compute_release_cadence(entries)
        self.assertEqual(cadence["interval_count"], 1)


class BuildFingerprintObservationsTest(unittest.TestCase):
    def test_accepted_candidate_contributes_provider_bias_and_naming_pattern(self):
        confidence = {
            "confidence": [
                {
                    "overall": "high",
                    "series_alignment_confidence": "high",
                    "candidate": {
                        "title": "Petals Falling - Cherry Blossom Girls #4",
                        "authors": ["Harmon Cooper"],
                        "source_provenance": [{"source": "hardcover"}],
                    },
                }
            ]
        }
        observations = build_fingerprint_observations(
            skeleton_entries=[], delta={"malformed_books": []}, confidence=confidence, series_author="Harmon Cooper"
        )
        self.assertIn("hardcover", observations["provider_bias_observations"])
        self.assertGreater(observations["provider_bias_observations"]["hardcover"], 1.0)
        self.assertIn("dash_series_marker", observations["naming_pattern_observations"])

    def test_rejected_candidate_contributes_negative_provider_bias_signal(self):
        confidence = {
            "confidence": [
                {
                    "overall": "low",
                    "series_alignment_confidence": "high",
                    "candidate": {
                        "title": "Unrelated",
                        "authors": ["Harmon Cooper"],
                        "source_provenance": [{"source": "google_books"}],
                    },
                }
            ]
        }
        observations = build_fingerprint_observations(
            skeleton_entries=[], delta={"malformed_books": []}, confidence=confidence, series_author="Harmon Cooper"
        )
        self.assertLess(observations["provider_bias_observations"]["google_books"], 1.0)

    def test_medium_alignment_confidence_contributes_author_alias_observation(self):
        confidence = {
            "confidence": [
                {
                    "overall": "medium",
                    "series_alignment_confidence": "medium",
                    "candidate": {
                        "title": "Cherry Blossom Girls Book 5",
                        "authors": ["H. Cooper"],
                        "source_provenance": [{"source": "hardcover"}],
                    },
                }
            ]
        }
        observations = build_fingerprint_observations(
            skeleton_entries=[], delta={"malformed_books": []}, confidence=confidence, series_author="Harmon Cooper"
        )
        self.assertIn("H. Cooper", observations["author_alias_observations"])

    def test_empty_round_yields_empty_observations_not_an_error(self):
        observations = build_fingerprint_observations(
            skeleton_entries=[], delta={"malformed_books": []}, confidence={"confidence": []}
        )
        self.assertEqual(observations["author_alias_observations"], [])
        self.assertEqual(observations["naming_pattern_observations"], [])
        self.assertEqual(observations["provider_bias_observations"], {})
        self.assertEqual(observations["release_cadence"]["interval_count"], 0)


if __name__ == "__main__":
    unittest.main()
