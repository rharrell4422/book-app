"""PP-1/PP-4: a unified shape and calling contract for every outbound
discovery provider (Google Books, OpenLibrary, Hardcover, the web-search+LLM
pipeline, and Apify).

Why this exists: before this module, each provider had its own ad hoc return
shape (bare `list[dict]`, with the dict's exact keys varying per provider --
see `discovery_engine.py`'s `_fetch_google_books`/`_fetch_openlibrary`/
`_fetch_hardcover`/`_fetch_web_search` and `apify_provider.py`'s
`fetch_apify_candidates`) and its own failure contract (the four catalog/web
functions raise on HTTP/network/GraphQL errors; Apify never raises, always
returning `[]`/`None` -- see `apify_provider.fetch_apify_candidates`'s
docstring). `_fetch_all_providers_parallel` papered over the inconsistency
with a per-future `try/except` that could only detect failure via a raised
exception, which is indistinguishable at that layer from "the provider ran
fine and genuinely found nothing" for any provider that *doesn't* raise.

PP-3's resolution (explicit user decision, not a default): normalize every
provider to Apify's model, not the other way around -- Apify's "never
raises" contract is the one a discovery pass that must not abort on a single
provider's outage should have. `ProviderFetchResult.ok` is the *only*
failure signal now; `ok=True, items=[]` (a genuine empty search) is always
distinguishable from `ok=False` (the call itself failed).

Scope note: this module defines the canonical shape/contract and thin
provider *adapters* that wrap the existing `_fetch_*`/`fetch_apify_candidates`
functions in `discovery_engine.py`/`apify_provider.py` -- it does not change
those functions' own signatures or their many other direct call sites
(`intelligence.py`, `services/find_engine.py`,
`backfill_missing_publication_dates`, `verify_missing_volume_recovery_dates`,
etc.), which are one-off targeted lookups outside the multi-provider
orchestration path this protocol targets. Only `_fetch_all_providers_parallel`
(the actual "run every provider uniformly" call site) is wired to go through
these adapters -- see that function's docstring for the wiring.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Protocol

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class RawResult(BaseModel):
    """PP-4: canonical shape a provider hands back for one candidate book,
    merging every field actually produced across today's providers (Google
    Books/OpenLibrary/Hardcover's catalog dicts, `_fetch_web_search`'s
    structured web candidates, and `apify_provider._normalize_product_item`'s
    Apify dicts). No single provider populates every field -- the
    provider-specific hints below default to `None` precisely because they
    are hints, not a claim that a provider which doesn't set one is missing
    data it should have had.
    """

    source: str
    source_id: str | None = None
    title: str
    authors: list[str] = Field(default_factory=list)
    published_date: str | None = None
    description: Any | None = None
    isbn13: str | None = None
    source_url: str | None = None
    language: str = ""

    # Provider-specific hints -- see individual _fetch_* functions for which
    # provider(s) actually populate each one.
    series_number_hint: float | None = None
    upcoming_hint: bool | None = None
    series_name_hint: str | None = None
    series_total_hint: int | None = None
    confidence: str | None = None
    asin: str | None = None
    cover_image: str | None = None

    @classmethod
    def from_legacy_dict(cls, data: dict) -> "RawResult":
        """Tolerant of the exact per-provider dict shapes already in the
        wild -- extra keys are ignored (pydantic's default), missing keys
        fall back to this model's own defaults rather than raising, since a
        malformed/unexpected upstream dict should degrade gracefully here,
        not take down an entire provider's pass.
        """
        known_fields = set(cls.model_fields)
        return cls(**{k: v for k, v in data.items() if k in known_fields})

    def to_legacy_dict(self) -> dict:
        """Inverse of `from_legacy_dict`, for the one call site
        (`_fetch_all_providers_parallel`) that still hands its output to
        code expecting the old flat-dict shape (`_fuse_and_score_candidates`,
        `_filter_and_merge`, `UnifiedCandidate`). Keeps `None` values (not
        dropped) -- existing consumers already use `.get(key)`/`or` fallbacks
        that treat an explicit `None` the same as a missing key.
        """
        return self.model_dump()


class ProviderFetchResult(BaseModel):
    """What every provider adapter returns, always -- never an exception.

    `ok=False` is the only failure signal (see module docstring for why);
    `ok=True, items=[]` is a normal, successful "found nothing" outcome.
    `error` is a short human-readable string for logs/diagnostics, set only
    when `ok` is False.
    """

    items: list[RawResult] = Field(default_factory=list)
    ok: bool = True
    error: str | None = None


class WebSearchProvider(Protocol):
    """PP-1: the structural contract every provider adapter in this module
    satisfies. Deliberately loose on extra keyword arguments (`**kwargs`) --
    providers have genuinely different auxiliary inputs (Apify needs a call
    budget and optional Amazon URLs; the web-search provider needs a series
    name/author/cache; catalog providers need none of that) and forcing a
    single rigid signature across all of them would just move the
    inconsistency PP-2 is meant to remove into a pile of unused parameters.
    The one thing every implementation must honor is the return contract:
    always a `ProviderFetchResult`, never a raised exception.
    """

    name: str

    def fetch(
        self,
        query: str,
        *,
        max_results: int | None = None,
        telemetry: Any = None,
        **kwargs: Any,
    ) -> ProviderFetchResult:
        ...


def _safe_call(
    provider_name: str, fn, *args, _record_telemetry: Any = None, **kwargs
) -> ProviderFetchResult:
    """Shared "never raises" boundary (PP-3) used by every adapter below --
    one place that catches whatever a legacy `_fetch_*`/`fetch_apify_
    candidates` function raises, logs it, and turns it into
    `ProviderFetchResult(ok=False, error=...)` instead of letting it
    propagate.

    `_record_telemetry` (PB-9, a leading-underscore name deliberately kept
    out of `**kwargs` so it's never accidentally forwarded to `fn`) records
    one `record_provider_call` entry per call -- this is a coarser,
    every-provider counterpart to the web pass's existing per-query
    `record_web_search_call` bookkeeping, not a replacement for it.
    """
    started = time.monotonic()
    try:
        raw_items = fn(*args, **kwargs) or []
    except Exception as exc:  # noqa: BLE001 -- intentionally broad: this is the PP-3 boundary
        duration = time.monotonic() - started
        if _record_telemetry is not None:
            _record_telemetry.record_provider_call(provider_name, ok=False, duration_s=duration)
        logger.warning("Provider %r failed: %s", provider_name, exc)
        return ProviderFetchResult(items=[], ok=False, error=str(exc))

    duration = time.monotonic() - started
    if _record_telemetry is not None:
        _record_telemetry.record_provider_call(provider_name, ok=True, duration_s=duration)
    items = [RawResult.from_legacy_dict(item) for item in raw_items if isinstance(item, dict)]
    return ProviderFetchResult(items=items, ok=True, error=None)


class GoogleBooksProvider:
    name = "google"

    def fetch(
        self, query: str, *, max_results: int | None = None, telemetry: Any = None, **_kwargs: Any
    ) -> ProviderFetchResult:
        from discovery_engine import _fetch_google_books

        # `max_results=None` (the default) intentionally omits the argument
        # entirely rather than forwarding a hardcoded value, so a caller
        # that doesn't ask for an override gets exactly the same call
        # (and the same mockable call signature in existing tests) as
        # calling `_fetch_google_books` directly always had.
        args = (query,) if max_results is None else (query, max_results)
        return _safe_call(self.name, _fetch_google_books, *args, _record_telemetry=telemetry)


class OpenLibraryProvider:
    name = "openlibrary"

    def fetch(
        self, query: str, *, max_results: int | None = None, telemetry: Any = None, **_kwargs: Any
    ) -> ProviderFetchResult:
        from discovery_engine import _fetch_openlibrary

        args = (query,) if max_results is None else (query, max_results)
        return _safe_call(self.name, _fetch_openlibrary, *args, _record_telemetry=telemetry)


class HardcoverProvider:
    name = "hardcover"

    def fetch(
        self, query: str, *, max_results: int | None = None, telemetry: Any = None, **_kwargs: Any
    ) -> ProviderFetchResult:
        from discovery_engine import _fetch_hardcover

        args = (query,) if max_results is None else (query, max_results)
        return _safe_call(self.name, _fetch_hardcover, *args, _record_telemetry=telemetry)


class WebDiscoveryProvider:
    """Wraps `_fetch_web_search`, not `_fetch_serper_web_search` --
    `_fetch_web_search` is the function that actually gets called from
    `_fetch_all_providers_parallel`'s "web" task and already returns the
    canonical structured-candidate shape (post-LLM-structuring), whereas
    `_fetch_serper_web_search` returns raw, pre-structuring search hits
    (`{title, description, url}`) that are an internal implementation detail
    of `_fetch_web_search`, not something the orchestrator calls directly.

    Deliberately not a single-`query: str` signature like the catalog
    providers -- the web pass is inherently a batch of queries (the targeted
    query plus the lookahead "book N" queries), not one query per call, and
    forcing an artificial single-query shape here would just misrepresent
    what this provider actually does.
    """

    name = "web"

    def fetch(
        self,
        queries: list[str],
        series_name: str | None,
        author: str,
        *,
        max_results: int = 0,  # unused; kept for structural-shape symmetry only
        telemetry: Any = None,
        **kwargs: Any,
    ) -> ProviderFetchResult:
        from discovery_engine import _fetch_web_search

        # telemetry is passed twice deliberately: once via `_record_telemetry`
        # so _safe_call logs this whole call as one "web" provider entry,
        # and again via **kwargs so it still reaches _fetch_web_search
        # itself, which does its own finer-grained per-query
        # record_web_search_call bookkeeping internally.
        return _safe_call(
            self.name,
            _fetch_web_search,
            queries,
            series_name,
            author,
            _record_telemetry=telemetry,
            telemetry=telemetry,
            **kwargs,
        )


class ApifyProvider:
    """Apify already never raises (see `apify_provider.fetch_apify_candidates`'s
    own docstring) -- this adapter exists purely so Apify satisfies the same
    `WebSearchProvider` structural contract as every other provider, not
    because it needs the `_safe_call` boundary for correctness.
    """

    name = "apify"

    def fetch(
        self,
        query: str,
        *,
        max_results: int = 0,  # unused; Apify has no result-count knob
        telemetry: Any = None,
        amazon_urls: list[str] | None = None,
        budget: Any = None,
        **_kwargs: Any,
    ) -> ProviderFetchResult:
        from apify_provider import ApifyCallBudget, fetch_apify_candidates

        return _safe_call(
            self.name, fetch_apify_candidates, query, amazon_urls, budget or ApifyCallBudget(), _record_telemetry=telemetry
        )
