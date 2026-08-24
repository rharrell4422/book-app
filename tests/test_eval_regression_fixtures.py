"""PB-3a -- validates today's pipeline against the frozen eval fixture set.

This does not build the recorded-fixture ``WebSearchProvider`` (that is
PB-3b, Wave 2). It only proves the fixtures in ``fixtures/eval_regressions/``
are live and accurate right now, so later work (CR-8 in particular, which is
explicitly scoped to validate against "PB-3a frozen fixtures only") has a
trustworthy baseline to diff against.
"""

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import discovery_engine
from agents.series_agent import SeriesIntelligenceAgent
from database import Base
from fixtures.eval_regressions import RegressionFixture, load_all_regressions, load_regression
from models import Book, Series


class FixtureSetIntegrityTest(unittest.TestCase):
    """Sanity checks on the fixture set itself, independent of any regression
    it encodes -- catches a malformed/incomplete freeze early.
    """

    def test_all_five_named_regressions_are_present(self):
        regressions = load_all_regressions()
        self.assertEqual(len(regressions), 5)
        self.assertEqual(
            {r.id for r in regressions},
            {
                "jonathan_hunt",
                "safehold_omnibus",
                "starships_mage",
                "universe_tie_in_downgrade",
                "compilation_of_owned_titles_downgrade",
            },
        )

    def test_every_fixture_has_a_non_empty_description_and_known_kind(self):
        for regression in load_all_regressions():
            self.assertTrue(regression.description)
            self.assertIn(regression.kind, {"function", "pipeline"})


class FunctionKindRegressionTest(unittest.TestCase):
    """Replays 'function' kind fixtures directly against the pure function
    they target, bypassing the full discovery pipeline (mocking
    discover_candidates_for_series would skip the exact filter these
    regressions are about).
    """

    def _run_case(self, regression: RegressionFixture, case: dict) -> None:
        if regression.target == "discovery_engine._title_is_series_variant":
            actual = discovery_engine._title_is_series_variant(
                case["title"],
                case["series_name"],
                case["isbn13"],
                case["structured_number_hint"],
            )
            self.assertEqual(actual, case["expected_is_variant"], case["name"])
        elif regression.target == "discovery_engine.normalize_series_branding_name":
            key_a = discovery_engine.normalize_series_branding_name(case["title_a"])
            key_b = discovery_engine.normalize_series_branding_name(case["title_b"])
            if case["expect_equal"]:
                self.assertEqual(key_a, key_b, case["name"])
            else:
                self.assertNotEqual(key_a, key_b, case["name"])
        else:
            self.fail(f"No test runner wired up for fixture target {regression.target!r}")

    def test_jonathan_hunt(self):
        regression = load_regression("jonathan_hunt")
        for case in regression.cases:
            with self.subTest(case=case["name"]):
                self._run_case(regression, case)

    def test_starships_mage(self):
        regression = load_regression("starships_mage")
        for case in regression.cases:
            with self.subTest(case=case["name"]):
                self._run_case(regression, case)


class PipelineKindRegressionTest(unittest.TestCase):
    """Replays 'pipeline' kind fixtures through a real
    SeriesIntelligenceAgent.run_series_check pass, with discovery mocked to
    return exactly the fixture's frozen candidate list.
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

    def tearDown(self):
        self.db.close()

    def _build_series(self, regression: RegressionFixture) -> Series:
        series_config = regression.series
        series = Series(name=series_config["name"], author=series_config["author"])
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)

        for owned in regression.owned_books:
            self.db.add(
                Book(
                    title=owned["title"],
                    author=series_config["author"],
                    series_id=series.id,
                    series_order=owned["series_order"],
                    book_number=owned["book_number"],
                    record_status="active",
                    is_read=owned.get("is_read", False),
                )
            )
        for number in regression.owned_book_numbers:
            self.db.add(
                Book(
                    title=f"{series_config['name']} Book {number}",
                    author=series_config["author"],
                    series_id=series.id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=False,
                )
            )
        self.db.commit()
        return series

    def _mock_discovery(self, candidates):
        result = {
            "candidates": candidates,
            "provider_failures": [],
            "all_providers_failed": False,
            "used_author_fallback": False,
        }
        return patch("discovery_engine.discover_candidates_for_series", return_value=result)

    def _assert_expected(self, result: dict, expected: dict, context: str) -> None:
        if "available_missing_titles" in expected:
            self.assertEqual(
                [book["title"] for book in result["available_missing"]],
                expected["available_missing_titles"],
                context,
            )
        if "added_count" in expected:
            self.assertEqual(result["added_count"], expected["added_count"], context)

    def test_safehold_omnibus(self):
        regression = load_regression("safehold_omnibus")
        series = self._build_series(regression)
        with self._mock_discovery(regression.candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, series.id, emit_summary=False)
        self._assert_expected(result, regression.expected, regression.id)

    def test_compilation_of_owned_titles_downgrade(self):
        regression = load_regression("compilation_of_owned_titles_downgrade")
        series = self._build_series(regression)
        with self._mock_discovery(regression.candidates):
            agent = SeriesIntelligenceAgent()
            result = agent.run_series_check(self.db, series.id, emit_summary=False)
        self._assert_expected(result, regression.expected, regression.id)

    def test_universe_tie_in_downgrade(self):
        regression = load_regression("universe_tie_in_downgrade")
        for scenario in regression.scenarios:
            with self.subTest(scenario=scenario["name"]):
                # Each scenario gets its own series/books so scenarios can't
                # interfere with each other's owned-book state.
                series = self._build_series(regression)
                with self._mock_discovery(scenario["candidates"]):
                    agent = SeriesIntelligenceAgent()
                    result = agent.run_series_check(self.db, series.id, emit_summary=False)
                self._assert_expected(result, scenario["expected"], scenario["name"])


if __name__ == "__main__":
    unittest.main()
