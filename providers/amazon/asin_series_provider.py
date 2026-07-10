from __future__ import annotations

import re
from typing import Callable

from providers.amazon.product_page_extractor import extract_amazon_product_metadata_from_html

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_DP_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?#]|$)", flags=re.IGNORECASE)
_SERIES_HINTS = (
    "bookseries",
    "book series",
    "series",
    "more books in this series",
    "books in this series",
    "book ",
)


def _normalize_asin(value: str | None) -> str:
    asin = str(value or "").strip().upper()
    return asin if _ASIN_RE.fullmatch(asin) else ""


def _extract_related_asins_from_html(raw_html: str, current_asin: str) -> set[str]:
    html = str(raw_html or "")
    if not html:
        return set()

    strong_matches: set[str] = set()
    fallback_matches: set[str] = set()

    for match in _DP_RE.finditer(html):
        asin = _normalize_asin(match.group(1))
        if not asin or asin == current_asin:
            continue

        fallback_matches.add(asin)

        start = max(0, match.start() - 240)
        end = min(len(html), match.end() + 240)
        neighborhood = html[start:end].lower()
        if any(hint in neighborhood for hint in _SERIES_HINTS):
            strong_matches.add(asin)

    if strong_matches:
        return strong_matches
    return fallback_matches


def _canonical_candidate_from_metadata(metadata: dict, asin: str, url: str) -> dict | None:
    if not isinstance(metadata, dict):
        return None
    if metadata.get("failure_reason"):
        return None

    title = str(metadata.get("title") or "").strip()
    if not title:
        return None

    candidate_asin = _normalize_asin(str(metadata.get("asin_or_id") or asin))
    if not candidate_asin:
        candidate_asin = asin

    availability = str(metadata.get("availability") or "").strip().lower()
    if availability == "upcoming":
        status_hint = "upcoming"
    elif availability == "available":
        status_hint = "published"
    else:
        status_hint = "published"

    return {
        "title": title,
        "author": str(metadata.get("author") or "").strip(),
        "series_name": str(metadata.get("series_name") or "").strip(),
        "book_number": metadata.get("book_number"),
        "series_number": metadata.get("book_number"),
        "url": str(metadata.get("url") or url).strip() or url,
        "snippet": "amazon-asin-series",
        "publication_date": metadata.get("publish_date"),
        "expected_date": metadata.get("upcoming_date"),
        "release_date": metadata.get("publish_date") or metadata.get("upcoming_date"),
        "status_hint": status_hint,
        "availability": availability or "unknown",
        "asin_or_id": candidate_asin,
        "asin": candidate_asin,
        "provider": "amazon_asin_series",
        "source": "amazon_asin_series",
    }


def discover_series_candidates_from_seed_asins(
    *,
    seed_asins: list[str],
    series_name: str,
    author_name: str,
    fetch_product_html: Callable[[str], dict],
    max_seed_pages: int = 40,
) -> dict:
    del series_name, author_name

    normalized_seed_asins = []
    seen_seed_asins: set[str] = set()
    for raw_asin in seed_asins or []:
        asin = _normalize_asin(raw_asin)
        if not asin or asin in seen_seed_asins:
            continue
        seen_seed_asins.add(asin)
        normalized_seed_asins.append(asin)

    queue = list(normalized_seed_asins)
    seen: set[str] = set()
    candidates_by_asin: dict[str, dict] = {}

    seed_pages_fetched = 0
    seed_pages_failed = 0
    related_asins_discovered = 0

    while queue and len(seen) < max_seed_pages:
        asin = queue.pop(0)
        if asin in seen:
            continue
        seen.add(asin)

        product_url = f"https://m.amazon.com/dp/{asin}"
        fetch_result = fetch_product_html(product_url)
        if not fetch_result.get("ok"):
            seed_pages_failed += 1
            continue

        seed_pages_fetched += 1
        html = str(fetch_result.get("html") or fetch_result.get("raw_html") or "")
        metadata = extract_amazon_product_metadata_from_html(html, product_url, expected_asin=asin)
        candidate = _canonical_candidate_from_metadata(metadata, asin, product_url)
        if candidate is not None:
            candidates_by_asin[str(candidate.get("asin_or_id") or asin).strip().upper()] = candidate

        related_asins = _extract_related_asins_from_html(html, asin)
        related_asins_discovered += len(related_asins)
        for related_asin in sorted(related_asins):
            if related_asin in seen:
                continue
            if related_asin in queue:
                continue
            queue.append(related_asin)

    return {
        "candidates": list(candidates_by_asin.values()),
        "metrics": {
            "asin_seed_count": len(normalized_seed_asins),
            "asin_seed_pages_fetched": seed_pages_fetched,
            "asin_seed_pages_failed": seed_pages_failed,
            "asin_related_asins": related_asins_discovered,
            "asin_series_candidates": len(candidates_by_asin),
        },
    }
