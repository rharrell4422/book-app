"""Per-job, in-memory-only discovery cache (see discovery_catchup_
architecture_spec.md #2.4/#7.1): created fresh at the start of one Check
Now job, discarded when the job ends. Never persisted to disk, never
shared across jobs, series, or profiles -- the entire point of Check Now
is to catch what's changed since last time, so nothing here should outlive
one job. Always active for whatever job creates one; there is no
threshold/policy gate (see spec #7.1 for why that was rejected -- a
per-job in-memory cache has no meaningful cost to being unconditionally
on).

Two layers:

- Layer A (provider-fetch cache): raw Google Books/OpenLibrary/Hardcover/
  Brave results, keyed by (provider, normalized query text). Deliberately
  simpler than the originally-specified semantic tuple key
  (provider, series_name_normalized, primary_author_name, book_number) --
  literal-query-text keying already captures the dominant real-world case
  measured live (the generic targeted-pass query and per-round lookahead
  queries are byte-identical across rounds within one job, since they're
  built by the same code from the same series/author on every round). The
  known gap: it would NOT dedupe two *differently-formatted* queries for
  the same semantic (series, author, book_number) built by two different
  call sites (e.g. the exterior lookahead pass vs. the interior missing-
  volume gap pass using differently-shaped author strings) -- accepted for
  this iteration since that cross-pass case wasn't what the live
  measurement showed costing the most (see spec #6).
- Layer B (LLM-verdict cache): structured verdicts from
  _structure_web_results_with_llm, keyed by (scope_type,
  series_name_normalized, url) per spec #2.4. Caches both "accepted"
  verdicts (a dict) and "rejected" sentinels (None) so a junk URL that was
  sent to the LLM once and excluded isn't re-sent every round.
"""

from __future__ import annotations

import threading

# Distinguishes "never cached" from "cached, and the cached value is
# itself falsy" (an empty list from a provider fetch, or a rejected/None
# LLM verdict) -- both of the latter are meaningful, real cache hits that
# must short-circuit a re-fetch/re-verdict, not be mistaken for a miss.
CACHE_MISS = object()


def _normalize_query_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip().lower()


class DiscoveryCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._provider_fetch: dict[tuple[str, str], list[dict]] = {}
        self._llm_verdict: dict[tuple[str, str, str], dict | None] = {}
        # Counts avoided live calls -- a provider-fetch hit is one fewer
        # Google/OpenLibrary/Hardcover/Brave call, an LLM-verdict hit is
        # one fewer URL that needs sending to _structure_web_results_with_
        # llm. Reported in the debug summary as the cache's own measured
        # effect, rather than an estimate.
        self._provider_fetch_hits = 0
        self._llm_verdict_hits = 0

    def get_provider_fetch(self, provider: str, query: str):
        key = (provider, _normalize_query_text(query))
        with self._lock:
            value = self._provider_fetch.get(key, CACHE_MISS)
            if value is not CACHE_MISS:
                self._provider_fetch_hits += 1
            return value

    def set_provider_fetch(self, provider: str, query: str, results: list[dict]) -> None:
        key = (provider, _normalize_query_text(query))
        with self._lock:
            self._provider_fetch[key] = results

    def get_llm_verdict(self, scope_type: str, series_name_normalized: str, url: str):
        key = (scope_type, series_name_normalized, url)
        with self._lock:
            value = self._llm_verdict.get(key, CACHE_MISS)
            if value is not CACHE_MISS:
                self._llm_verdict_hits += 1
            return value

    def set_llm_verdict(self, scope_type: str, series_name_normalized: str, url: str, verdict: dict | None) -> None:
        key = (scope_type, series_name_normalized, url)
        with self._lock:
            self._llm_verdict[key] = verdict

    def summary(self) -> dict:
        with self._lock:
            return {
                "provider_fetch_entries": len(self._provider_fetch),
                "provider_fetch_hits": self._provider_fetch_hits,
                "llm_verdict_entries": len(self._llm_verdict),
                "llm_verdict_accepted": sum(1 for v in self._llm_verdict.values() if v is not None),
                "llm_verdict_rejected": sum(1 for v in self._llm_verdict.values() if v is None),
                "llm_verdict_hits": self._llm_verdict_hits,
            }
