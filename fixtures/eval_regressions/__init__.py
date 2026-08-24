"""PB-3a -- frozen eval fixture set for named discovery regressions.

This package freezes the five named regressions called out in
``discovery_agentic_replacement_recommendation.md`` (Phase 0, step 1) and in
the repo-cleanup plan's Wave 0 (``PB-3a``):

    - Jonathan Hunt (bare genre-tagline stub rejection)
    - Safehold (owned-omnibus range dedup)
    - Starship's Mage (branded-spinoff series-name collision)
    - universe tie-in downgrade
    - compilation-of-owned-titles downgrade

Each regression is stored as plain JSON data under this directory -- inputs
plus the expected accept/reject outcome -- deliberately decoupled from any
one test method so it survives Wave 3's structural refactors and can be
reused by:

    - CR-8 (Wave 1b), which must validate its confidence-grading change
      against exactly this frozen set and nothing broader;
    - PB-3b (Wave 2), which builds a recorded-fixture ``WebSearchProvider``
      that replays ``kind: "pipeline"`` fixtures' candidate lists as if they
      came from a live provider.

This module intentionally does NOT build that recorded-fixture provider --
that is out of scope for PB-3a and is tracked separately as PB-3b.

Two fixture shapes are used, distinguished by ``kind``:

``"function"``
    A table of direct-call cases against a single pure function
    (``target``), each with inputs and an expected return value. Used for
    regressions that live below the discovery pipeline's candidate-list
    boundary (e.g. a single title-normalization decision), where mocking
    ``discover_candidates_for_series`` would bypass the exact code path the
    regression is about.

``"pipeline"``
    A ``Series``/owned-books setup plus one or more ``scenarios``, each
    supplying a raw candidate list (in the same shape
    ``discovery_engine.discover_candidates_for_series`` returns) and the
    expected accept/reject outcome of a full
    ``SeriesIntelligenceAgent.run_series_check`` pass with discovery mocked
    at that boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FIXTURES_DIR = Path(__file__).resolve().parent

REGRESSION_IDS: tuple[str, ...] = (
    "jonathan_hunt",
    "safehold_omnibus",
    "starships_mage",
    "universe_tie_in_downgrade",
    "compilation_of_owned_titles_downgrade",
)


@dataclass(frozen=True)
class RegressionFixture:
    id: str
    kind: str
    description: str
    data: dict[str, Any]

    @property
    def target(self) -> str | None:
        return self.data.get("target")

    @property
    def cases(self) -> list[dict[str, Any]]:
        return self.data.get("cases", [])

    @property
    def series(self) -> dict[str, Any] | None:
        return self.data.get("series")

    @property
    def owned_books(self) -> list[dict[str, Any]]:
        return self.data.get("owned_books", [])

    @property
    def owned_book_numbers(self) -> list[int]:
        return self.data.get("owned_book_numbers", [])

    @property
    def candidates(self) -> list[dict[str, Any]]:
        return self.data.get("candidates", [])

    @property
    def expected(self) -> dict[str, Any]:
        return self.data.get("expected", {})

    @property
    def scenarios(self) -> list[dict[str, Any]]:
        return self.data.get("scenarios", [])


def load_regression(regression_id: str) -> RegressionFixture:
    """Load a single named regression fixture by id (its JSON filename minus
    the extension).
    """
    path = _FIXTURES_DIR / f"{regression_id}.json"
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return RegressionFixture(
        id=data["id"],
        kind=data["kind"],
        description=data["description"],
        data=data,
    )


def load_all_regressions() -> list[RegressionFixture]:
    """Load the full frozen PB-3a eval fixture set, in the order the
    recommendation doc names them.
    """
    return [load_regression(regression_id) for regression_id in REGRESSION_IDS]
