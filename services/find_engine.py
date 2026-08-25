"""FIND: single-call, multi-provider metadata lookup for the Add Book
workflow's Resolve/Select states (see project design chat, §4-5).

Given a title (plus optional author/book_number/series_name), fans out to
Google Books, OpenLibrary, and Hardcover in parallel, groups raw hits that
represent the same underlying book across providers into one ranked
candidate, and scores each candidate with a FIND-specific confidence tier.

FIND confidence tiers are intentionally independent from
confidence_engine.py's high/medium/low/zero vocabulary for Phase-3
discovery (Check Now) -- the two answer different questions and must never
be merged or have their thresholds shared:

  - confidence_engine (Check Now / discovery) scores *already-fused,
    already-persisted-or-about-to-be* candidates -- deciding "available"
    vs "upcoming" status and how much to trust a date/edition once a
    candidate has already survived series/author/title matching.
  - find_engine (Add Book / FIND) scores *raw, pre-persistence* search
    results the user is about to choose from -- deciding whether a match
    is a safe auto-suggestion or needs the user's own judgment, before
    anything is written to the database at all.

Confidence tiers (restored per the consolidated spec):
  - HIGH:   author agreement with the query AND an ISBN present AND a
            strong title match.
  - MEDIUM: any two of those three signals.
  - LOW:    everything else (zero or one signal).

"Strong title match" is defined as an exact match on
services/identity.py's _canonical_title_identity_key -- the same
edition-qualifier-stripping, article-stripping normalization already used
for owned-book identity matching -- rather than a similarity/fuzzy score.
This codebase has no existing fuzzy title matcher; introducing one is
tracked as an open decision (see the consolidated spec's Open Decisions
section) rather than invented ad hoc here.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import discovery_engine
from services.identity import _authors_match_exact, _canonical_title_identity_key

_PROVIDERS = ("hardcover", "google_books", "openlibrary")

# Priority order used when multiple providers supply the same field for a
# grouped candidate -- Hardcover is a dedicated book-catalog product with
# generally cleaner series/edition data than the other two general-purpose
# providers, so it's preferred when available.
_FIELD_PROVIDER_PRIORITY = ("hardcover", "google_books", "openlibrary")

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _normalize_author_list(authors) -> list[str]:
    if isinstance(authors, str):
        return [authors] if authors.strip() else []
    return [str(a).strip() for a in (authors or []) if str(a or "").strip()]


def _group_key_for_hit(hit: dict) -> str:
    """ISBN13 is a universal, cross-provider-safe identifier and is
    preferred whenever present. Falling back to the normalized title
    identity key (not title+author) avoids failing to merge two providers'
    hits for the same book purely because they format the author string
    differently (e.g. "Brandon Sanderson" vs "Sanderson, Brandon") --
    author agreement is instead checked per-candidate against the query,
    not used as a grouping key.
    """
    isbn13 = str(hit.get("isbn13") or "").strip()
    if isbn13:
        return f"isbn:{isbn13}"
    title_key = _canonical_title_identity_key(hit.get("title")) or ""
    return f"title:{title_key}"


def _query_author_matches_any(query_author: str | None, candidate_authors: list[str]) -> bool:
    if not query_author:
        return False
    return any(_authors_match_exact(query_author, candidate_author) for candidate_author in candidate_authors)


def _title_query_variants(raw_title: str) -> list[str]:
    """Users commonly paste in the full Amazon/KU listing title verbatim,
    e.g. "The Jericho Siege: A Jonathan Hunt Thriller Book 1 (Jonathan Hunt
    Thriller Series)" -- but catalog providers store just the core title
    ("The Jericho Siege"). Google Books' intitle: filter is an exact-phrase
    match (not relevance-ranked), so querying with the full raw string
    reliably returns zero hits for this extremely common title shape (see
    discovery_engine.core_title_key's docstring for the same pattern).
    Always try the raw title first (correct, and cheap, for the titles that
    really are just a short bare title already) plus this stripped-down
    core segment as a fallback variant, rather than only ever trying one or
    the other.
    """
    variants = [raw_title]
    core = discovery_engine._title_core_segment(raw_title).strip()  # noqa: SLF001
    if core and core.lower() != raw_title.strip().lower():
        variants.append(core)
    return variants


def _pick_field(hits_by_provider: dict[str, dict], field: str) -> tuple[object | None, str | None]:
    for provider in _FIELD_PROVIDER_PRIORITY:
        hit = hits_by_provider.get(provider)
        if hit and hit.get(field):
            return hit.get(field), provider
    return None, None


def _build_candidate(key: str, group: dict, *, query_title_variants: list[str], query_author: str | None) -> dict:
    hits_by_provider = group["hits_by_provider"]

    title_value, title_provider = _pick_field(hits_by_provider, "title")
    isbn_value, isbn_provider = _pick_field(hits_by_provider, "isbn13")
    description_value, description_provider = _pick_field(hits_by_provider, "description")
    source_url_value, source_url_provider = _pick_field(hits_by_provider, "source_url")
    published_date_value, published_date_provider = _pick_field(hits_by_provider, "published_date")

    all_authors: list[str] = []
    author_provider_by_name: dict[str, str] = {}
    for provider in _FIELD_PROVIDER_PRIORITY:
        hit = hits_by_provider.get(provider)
        if not hit:
            continue
        for candidate_author in _normalize_author_list(hit.get("authors")):
            if candidate_author not in author_provider_by_name:
                all_authors.append(candidate_author)
                author_provider_by_name[candidate_author] = provider
    primary_author = all_authors[0] if all_authors else None
    # The single "author" field applied to the Add Book form (and, from
    # there, straight onto Series.author) must carry every co-author, not
    # just the first -- joined with "; " to match this app's existing
    # multi-author convention (e.g. "J.N Chaney; Terry Maggert" already in
    # the library). Dropping co-authors here would leave Series.author
    # permanently incomplete for every co-authored series a user resolves
    # through FIND.
    display_author = "; ".join(all_authors) if all_authors else None

    author_match = _query_author_matches_any(query_author, all_authors)
    isbn_present = bool(isbn_value)
    # Matched against every title variant we queried with (raw string plus
    # the stripped-core fallback -- see _title_query_variants), not just the
    # raw one: a user-pasted KU/Amazon title's boilerplate suffix ("Book 1
    # (Series Name)") is exactly what the core variant already strips for
    # querying, so a candidate that matches on the core variant is just as
    # much a "strong" match as one that happens to equal the raw string.
    query_title_keys = {
        key for variant in query_title_variants if (key := _canonical_title_identity_key(variant))
    }
    candidate_title_key = _canonical_title_identity_key(title_value) if title_value else None
    strong_title_match = bool(candidate_title_key and candidate_title_key in query_title_keys)

    signal_count = sum([author_match, isbn_present, strong_title_match])
    if author_match and isbn_present and strong_title_match:
        confidence = "high"
    elif signal_count >= 2:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "candidate_id": key,
        "title": title_value,
        "author": display_author,
        "authors": all_authors,
        "isbn13": isbn_value,
        "description": description_value,
        "source_url": source_url_value,
        "published_date": published_date_value,
        "providers": list(group["providers"]),
        "field_provenance": {
            "title": title_provider,
            "author": author_provider_by_name.get(primary_author) if primary_author else None,
            "isbn13": isbn_provider,
            "description": description_provider,
            "source_url": source_url_provider,
            "published_date": published_date_provider,
        },
        "confidence": confidence,
        "signals": {
            "author_match": author_match,
            "isbn_present": isbn_present,
            "strong_title_match": strong_title_match,
        },
    }


def find_book_candidates(
    title: str,
    author: str | None = None,
    book_number: float | None = None,
    series_name: str | None = None,
    max_results: int = 6,
) -> dict:
    """The FIND call: fetches from every provider, groups hits into
    candidates, scores each with a confidence tier, and returns them ranked
    (highest confidence first, ties broken by provider agreement count).

    `book_number`/`series_name` are both accepted and echoed back in the
    response's `query` block for the caller's own display/context use, but
    only `series_name` is actually folded into the provider queries below
    (see `plain_query_for`) to narrow results -- `book_number` plays no
    role in query construction at all. Neither is part of the three-signal
    confidence formula either way (per the consolidated spec's restored
    confidence-tier definition), which is author/ISBN/title only.
    """
    clean_title = str(title or "").strip()
    clean_author = str(author or "").strip() or None
    if not clean_title:
        return {
            "query": {"title": clean_title, "author": clean_author, "book_number": book_number, "series_name": series_name},
            "candidates": [],
            "provider_failures": [],
        }

    title_variants = _title_query_variants(clean_title)

    def plain_query_for(title_variant: str) -> str:
        parts = [title_variant]
        if series_name and series_name.strip():
            parts.append(series_name.strip())
        if clean_author:
            parts.append(clean_author)
        return " ".join(parts)

    # One task per (provider, title variant) rather than one task per
    # provider -- Google's intitle: exact-phrase match in particular needs
    # both the raw and the stripped-core variant tried, since only one of
    # them will actually match a given listing (see _title_query_variants).
    # OpenLibrary/Hardcover's relevance-ranked search doesn't strictly need
    # the split, but trying both is harmless and keeps this uniform across
    # providers rather than special-casing Google alone.
    tasks: list[tuple[str, object, tuple]] = []
    for variant in title_variants:
        google_query = f'intitle:"{variant}"' + (f' inauthor:"{clean_author}"' if clean_author else "")
        tasks.append(("google_books", discovery_engine._fetch_google_books, (google_query,)))  # noqa: SLF001
        tasks.append(("openlibrary", discovery_engine._fetch_openlibrary, (plain_query_for(variant),)))  # noqa: SLF001
        tasks.append(("hardcover", discovery_engine._fetch_hardcover, (plain_query_for(variant),)))  # noqa: SLF001

    raw_hits: dict[str, list[dict]] = {provider: [] for provider in _PROVIDERS}
    failed_providers: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_to_provider = [(executor.submit(fn, *args), provider) for provider, fn, args in tasks]
        for future, provider in future_to_provider:
            try:
                raw_hits[provider].extend(future.result() or [])
            except Exception as exc:  # one provider/variant's failure shouldn't sink the others
                # Keyed by provider (last error wins) so a provider that fails
                # on both the raw and core title variants -- e.g. an auth
                # error that'll fail identically either way -- still surfaces
                # as one entry, not one per variant.
                failed_providers[provider] = str(exc)[:300]
    provider_failures = [{"provider": provider, "error": error} for provider, error in failed_providers.items()]

    groups: dict[str, dict] = {}
    group_order: list[str] = []
    for provider in _PROVIDERS:
        for hit in raw_hits.get(provider, [])[:max_results]:
            if not str(hit.get("title") or "").strip():
                continue
            key = _group_key_for_hit(hit)
            if key not in groups:
                groups[key] = {"providers": [], "hits_by_provider": {}}
                group_order.append(key)
            group = groups[key]
            if provider not in group["providers"]:
                group["providers"].append(provider)
            group["hits_by_provider"][provider] = hit

    candidates = [
        _build_candidate(key, groups[key], query_title_variants=title_variants, query_author=clean_author)
        for key in group_order
    ]
    candidates.sort(key=lambda c: (-_CONFIDENCE_RANK[c["confidence"]], -len(c["providers"])))

    return {
        "query": {"title": clean_title, "author": clean_author, "book_number": book_number, "series_name": series_name},
        "candidates": candidates[:max_results],
        "provider_failures": provider_failures,
    }
