"""TG-6: direct unit tests against services/discovery_cache.py's
`DiscoveryCache` itself, without going through discovery_engine's
`_fetch_all_providers_parallel`/`_fetch_web_search` plumbing. Complements
(does not duplicate) the higher-level integration coverage already in
`tests/test_series_discovery.py::DiscoveryCacheTest`, which exercises this
cache through that plumbing (series-name scoping via `_fetch_web_search`,
`bypass_cached_rejection` end to end, etc).

TG-10 lives here too: a single test asserting all three `get_llm_verdict`
states -- `CACHE_MISS` (never checked), `None` (rejected), and a `dict`
(accepted) -- are simultaneously distinguishable, which the audit flagged
as only exercised indirectly before this file existed.
"""
import unittest

from services.discovery_cache import CACHE_MISS, DiscoveryCache, _normalize_query_text


class NormalizeQueryTextTest(unittest.TestCase):
    def test_lowercases_and_collapses_whitespace(self):
        self.assertEqual(_normalize_query_text("  Some   SERIES  Query  "), "some series query")

    def test_none_becomes_empty_string(self):
        self.assertEqual(_normalize_query_text(None), "")


class ProviderFetchCacheTest(unittest.TestCase):
    """Layer A."""

    def test_never_queried_is_cache_miss(self):
        cache = DiscoveryCache()
        self.assertIs(cache.get_provider_fetch("google", "some query"), CACHE_MISS)

    def test_cached_empty_list_is_a_real_hit_not_a_miss(self):
        # TG-6's headline gap: an empty list from a provider fetch (a
        # genuine "searched, found nothing" result) must be distinguishable
        # from CACHE_MISS ("never searched at all") by identity, not by
        # truthiness -- `[] == []` is True but `[] is CACHE_MISS` must stay
        # False.
        cache = DiscoveryCache()
        cache.set_provider_fetch("google", "some query", [])
        result = cache.get_provider_fetch("google", "some query")
        self.assertEqual(result, [])
        self.assertIsNot(result, CACHE_MISS)

    def test_cached_non_empty_list_is_returned_verbatim(self):
        cache = DiscoveryCache()
        items = [{"title": "Some Book"}]
        cache.set_provider_fetch("google", "some query", items)
        self.assertEqual(cache.get_provider_fetch("google", "some query"), items)

    def test_query_normalization_applies_to_the_cache_key(self):
        cache = DiscoveryCache()
        cache.set_provider_fetch("google", "Some Query", [{"title": "X"}])
        self.assertNotEqual(cache.get_provider_fetch("google", "  some   query "), CACHE_MISS)

    def test_cache_is_scoped_per_provider(self):
        cache = DiscoveryCache()
        cache.set_provider_fetch("hardcover", "some query", [{"title": "X"}])
        self.assertIs(cache.get_provider_fetch("google", "some query"), CACHE_MISS)

    def test_hit_counter_increments_only_on_real_hits(self):
        cache = DiscoveryCache()
        cache.get_provider_fetch("google", "some query")  # miss
        cache.set_provider_fetch("google", "some query", [])
        cache.get_provider_fetch("google", "some query")  # hit (even though value is [])
        cache.get_provider_fetch("google", "some query")  # hit again
        self.assertEqual(cache.summary()["provider_fetch_hits"], 2)


class LlmVerdictCacheTest(unittest.TestCase):
    """Layer B."""

    def test_all_three_states_are_simultaneously_distinguishable(self):
        # TG-10: CACHE_MISS (never checked), None (rejected), and a dict
        # (accepted) must never collapse into each other.
        cache = DiscoveryCache()
        cache.set_llm_verdict("series", "some series", "https://example.com/rejected", None)
        cache.set_llm_verdict(
            "series", "some series", "https://example.com/accepted", {"title": "Accepted Book"}
        )

        never_checked = cache.get_llm_verdict("series", "some series", "https://example.com/never-checked")
        rejected = cache.get_llm_verdict("series", "some series", "https://example.com/rejected")
        accepted = cache.get_llm_verdict("series", "some series", "https://example.com/accepted")

        self.assertIs(never_checked, CACHE_MISS)
        self.assertIsNone(rejected)
        self.assertEqual(accepted, {"title": "Accepted Book"})

        # Sanity: all three are pairwise distinct values/identities, not
        # just individually "correct" in isolation.
        self.assertIsNot(never_checked, rejected)
        self.assertNotEqual(rejected, accepted)

    def test_cache_key_is_scoped_by_scope_type_series_name_and_url_together(self):
        cache = DiscoveryCache()
        cache.set_llm_verdict("series", "some series", "https://example.com/a", {"title": "X"})

        self.assertIs(cache.get_llm_verdict("author", "some series", "https://example.com/a"), CACHE_MISS)
        self.assertIs(cache.get_llm_verdict("series", "other series", "https://example.com/a"), CACHE_MISS)
        self.assertIs(cache.get_llm_verdict("series", "some series", "https://example.com/b"), CACHE_MISS)

    def test_hit_counter_counts_both_accepted_and_rejected_hits(self):
        cache = DiscoveryCache()
        cache.set_llm_verdict("series", "some series", "https://example.com/rejected", None)
        cache.get_llm_verdict("series", "some series", "https://example.com/rejected")  # hit
        cache.get_llm_verdict("series", "some series", "https://example.com/never-checked")  # miss
        self.assertEqual(cache.summary()["llm_verdict_hits"], 1)


class SummaryTest(unittest.TestCase):
    def test_summary_reports_accepted_and_rejected_counts_separately(self):
        cache = DiscoveryCache()
        cache.set_llm_verdict("series", "s", "https://example.com/accepted-1", {"title": "A"})
        cache.set_llm_verdict("series", "s", "https://example.com/accepted-2", {"title": "B"})
        cache.set_llm_verdict("series", "s", "https://example.com/rejected-1", None)

        summary = cache.summary()
        self.assertEqual(summary["llm_verdict_entries"], 3)
        self.assertEqual(summary["llm_verdict_accepted"], 2)
        self.assertEqual(summary["llm_verdict_rejected"], 1)

    def test_fresh_cache_summary_is_all_zeroes(self):
        summary = DiscoveryCache().summary()
        self.assertEqual(
            summary,
            {
                "provider_fetch_entries": 0,
                "provider_fetch_hits": 0,
                "llm_verdict_entries": 0,
                "llm_verdict_accepted": 0,
                "llm_verdict_rejected": 0,
                "llm_verdict_hits": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
