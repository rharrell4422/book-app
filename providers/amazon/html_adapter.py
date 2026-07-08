from __future__ import annotations

import re

from bs4 import BeautifulSoup

from provider_core.html_extractor import normalize_html_input
from provider_core.html_parser import parse_html_to_candidates


_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def _is_valid_asin(value: str | None) -> bool:
    asin = str(value or "").strip().upper()
    if not asin:
        return False
    if not _ASIN_RE.fullmatch(asin):
        return False
    return True


def extract_amazon_asins_from_search_html(raw_html: str, series_name: str = "") -> list[str]:
    del series_name
    normalized_html = normalize_html_input(raw_html)
    soup = BeautifulSoup(normalized_html, "html.parser")

    selectors = (
        "div.s-result-item[data-component-type='s-search-result'][data-asin]",
        "div[data-asin][data-component-type='sp-sponsored-result']",
        "div.sg-col-inner[data-asin]",
        "li[data-asin]",
        "div.a-section.a-spacing-base[data-asin]",
        "[data-asin]:not([data-asin=''])",
    )

    discovered: list[str] = []
    seen_asins: set[str] = set()

    for selector in selectors:
        for node in soup.select(selector):
            asin = str(node.get("data-asin") or "").strip().upper()
            if not _is_valid_asin(asin):
                continue
            if asin in seen_asins:
                continue
            seen_asins.add(asin)
            discovered.append(asin)

    return discovered


def extract_amazon_candidates_from_html(raw_html: str, series_name: str = "") -> list[dict]:
    normalized_html = normalize_html_input(raw_html)
    candidates = parse_html_to_candidates(normalized_html)

    clean_series = str(series_name or "").strip().lower()
    if not clean_series:
        return candidates

    filtered = [
        candidate
        for candidate in candidates
        if clean_series in str(candidate.get("title") or "").strip().lower()
    ]

    return filtered or candidates
