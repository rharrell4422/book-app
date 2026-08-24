"""PB-3b: a `WebSearchProvider`-conformant provider backed by recorded,
version-controlled fixtures instead of live network calls.

Why: PB-3a (`fixtures/eval_regressions/`) froze pure function-level
input/output pairs (e.g. `discovery_engine._title_is_series_variant`), which
is the right shape for testing a single classification rule but can't stand
in for a whole provider -- there's no recorded *provider response* to plug
into `discover_candidates_for_series`'s orchestration for an end-to-end,
network-free eval run. This module is that missing piece: a provider that
satisfies the same `provider_protocol.WebSearchProvider` contract as
`GoogleBooksProvider`/`HardcoverProvider`/etc, but reads its "results" from
`fixtures/provider_recordings/*.json` recordings instead of an HTTP call.

An unrecognized query returns `ProviderFetchResult(ok=True, items=[])` --
a deterministic empty hit, not a failure -- so an eval run stays
reproducible (same query always gets the same answer) instead of silently
reporting a spurious provider outage for anything not yet recorded.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from provider_protocol import ProviderFetchResult, RawResult
from services.discovery_cache import _normalize_query_text

logger = logging.getLogger(__name__)

DEFAULT_RECORDINGS_DIR = Path(__file__).resolve().parent / "fixtures" / "provider_recordings"


def _load_recordings(recordings_dir: Path) -> dict[str, list[dict]]:
    """Merges every `*.json` file in `recordings_dir` into one
    `normalized_query -> [legacy_dict, ...]` mapping. Uses the same query
    normalization (`_normalize_query_text`) Layer A provider-fetch caching
    already uses, so a recording keyed by "Jonathan Hunt Georgia Wagner"
    matches a lookup for " jonathan hunt   georgia wagner " identically.
    """
    merged: dict[str, list[dict]] = {}
    if not recordings_dir.is_dir():
        return merged
    for path in sorted(recordings_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping unreadable fixture recording %s: %s", path, exc)
            continue
        for recording in data.get("recordings", []):
            query = recording.get("query")
            items = recording.get("items")
            if not isinstance(query, str) or not isinstance(items, list):
                continue
            merged[_normalize_query_text(query)] = items
    return merged


class FixtureBackedProvider:
    """PB-3b's recorded-fixture provider. `recordings_dir` defaults to
    `fixtures/provider_recordings/` but is overridable so tests can point at
    a small, self-contained temp directory instead of the shared fixture
    set.
    """

    name = "fixture"

    def __init__(self, recordings_dir: Path | str | None = None) -> None:
        self._recordings_dir = Path(recordings_dir) if recordings_dir is not None else DEFAULT_RECORDINGS_DIR
        self._recordings = _load_recordings(self._recordings_dir)

    def fetch(
        self,
        query: str,
        *,
        max_results: int | None = None,
        telemetry: Any = None,
        **_kwargs: Any,
    ) -> ProviderFetchResult:
        items = self._recordings.get(_normalize_query_text(query), [])
        if max_results is not None:
            items = items[:max_results]
        raw_results = [RawResult.from_legacy_dict(item) for item in items]
        return ProviderFetchResult(items=raw_results, ok=True, error=None)
