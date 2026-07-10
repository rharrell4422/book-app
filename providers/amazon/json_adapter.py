from __future__ import annotations

import re
from typing import Any

from provider_core.json_extractor import extract_json_objects_from_html
_DETAIL_URL_RE = re.compile(r"https?://(?:www\.)?amazon\.[^/]+/.*/dp/([A-Z0-9]{10})(?:[/?#]|$)", flags=re.IGNORECASE)

_AMAZON_SCHEMA_CONTAINER_KEYS = {
    "searchresults",
    "items",
    "product",
    "productoverview",
    "productdetails",
    "metadata",
    "seriesinfo",
}

_AMAZON_SCHEMA_FIELD_KEYS = {
    "detailpageurl",
    "asin",
    "title",
    "author",
    "contributors",
    "publicationdate",
    "seriesinfo",
}


def _walk_nodes(node: Any):
    stack: list[Any] = [node]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, dict):
            for value in current.values():
                stack.append(value)
        elif isinstance(current, list):
            for item in current:
                stack.append(item)


def _iter_dict_nodes(node: Any):
    for current in _walk_nodes(node):
        if isinstance(current, dict):
            yield current


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value).strip()
    return ""


def _is_valid_asin(value: str) -> bool:
    asin = str(value or "").strip().upper()
    return bool(re.fullmatch(r"[A-Z0-9]{10}", asin))


def _is_valid_detail_url(value: str) -> bool:
    url = str(value or "").strip()
    if not url:
        return False
    if "/dp/" not in url:
        return False
    return bool(_DETAIL_URL_RE.search(url))


def _first_value(node: Any, keys: tuple[str, ...]) -> str:
    wanted = {key.lower() for key in keys}
    for current in _iter_dict_nodes(node):
        for key, value in current.items():
            if str(key).lower() not in wanted:
                continue
            text = _text(value)
            if text:
                return text
    return ""


def _extract_asin(node: Any) -> str:
    asin = _first_value(node, ("asin",))
    if _is_valid_asin(asin):
        return asin.upper()

    detail_url = _extract_detail_url(node)
    match = _DETAIL_URL_RE.search(detail_url)
    if match:
        candidate = str(match.group(1) or "").strip().upper()
        if _is_valid_asin(candidate):
            return candidate
    return ""


def _extract_title(node: Any) -> str:
    return _first_value(node, ("title", "name"))


def _extract_author(node: Any) -> str:
    direct_author = _first_value(node, ("author",))
    if direct_author:
        return direct_author

    for current in _iter_dict_nodes(node):
        for key, value in current.items():
            if str(key).lower() != "contributors":
                continue
            if isinstance(value, list):
                names: list[str] = []
                for contributor in value:
                    if isinstance(contributor, dict):
                        candidate_name = _text(contributor.get("name") or contributor.get("displayName") or contributor.get("author"))
                        if candidate_name:
                            names.append(candidate_name)
                    else:
                        candidate_name = _text(contributor)
                        if candidate_name:
                            names.append(candidate_name)
                if names:
                    return ", ".join(names)
            elif isinstance(value, dict):
                candidate_name = _text(value.get("name") or value.get("displayName") or value.get("author"))
                if candidate_name:
                    return candidate_name
            else:
                candidate_name = _text(value)
                if candidate_name:
                    return candidate_name
    return ""

def _extract_publication_date(node: Any) -> str:
    return _first_value(node, ("publicationDate", "publishDate", "releaseDate"))


def _extract_detail_url(node: Any) -> str:
    detail_url = _first_value(node, ("detailPageUrl", "canonicalUrl", "productUrl", "url"))
    if detail_url and _is_valid_detail_url(detail_url):
        return detail_url
    return ""

def _extract_series_info(node: Any) -> tuple[str, int | None]:
    series_name = ""
    series_number: int | None = None

    for current in _iter_dict_nodes(node):
        series_payload = None
        for key, value in current.items():
            if str(key).lower() == "seriesinfo":
                series_payload = value
                break

        if series_payload is None:
            continue

        if isinstance(series_payload, dict):
            if not series_name:
                series_name = _text(series_payload.get("name") or series_payload.get("seriesName") or series_payload.get("title"))
            if series_number is None:
                raw_number = _text(series_payload.get("number") or series_payload.get("seriesNumber") or series_payload.get("bookNumber"))
                if raw_number:
                    match = re.search(r"\d+", raw_number)
                    if match:
                        series_number = int(match.group(0))
        elif isinstance(series_payload, str):
            if not series_name:
                series_name = series_payload.strip()

        if series_name and series_number is not None:
            return series_name, series_number

    return series_name, series_number

def _node_has_amazon_schema_keys(node: dict[str, Any]) -> bool:
    lowered_keys = {str(key).lower() for key in node.keys()}
    return bool(lowered_keys & (_AMAZON_SCHEMA_CONTAINER_KEYS | _AMAZON_SCHEMA_FIELD_KEYS))

def _as_canonical_candidate(node: Any) -> dict[str, Any] | None:
    asin = _extract_asin(node)
    title = _extract_title(node)
    author = _extract_author(node)
    publication_date = _extract_publication_date(node)
    detail_page_url = _extract_detail_url(node)
    series_name, series_number = _extract_series_info(node)

    has_any_book_fields = bool(asin or title or author or publication_date or series_name or series_number is not None)
    if not has_any_book_fields:
        return None

    # Partial rows are skipped so downstream ranking only receives stable candidates.
    if not asin or not title:
        return None

    return {
        "asin": asin,
        "title": title,
        "author": author,
        "publication_date": publication_date,
        "series_name": series_name,
        "series_number": series_number,
        "provider": "amazon",
        "source": "json",
        "asin_or_id": asin,
        "publish_date": publication_date,
        "series": series_name,
        "book_number": series_number,
        "url": detail_page_url or f"https://www.amazon.com/dp/{asin}",
        "snippet": "amazon-json",
    }

def extract_amazon_candidates_from_json(raw_html: str) -> dict[str, Any]:
    blobs = extract_json_objects_from_html(raw_html)
    json_blobs_scanned = len(blobs)

    seen_keys: set[str] = set()
    book_candidates: list[dict[str, Any]] = []

    for blob in blobs:
        for node in _iter_dict_nodes(blob):
            if not _node_has_amazon_schema_keys(node):
                continue

            candidate = _as_canonical_candidate(node)
            if candidate is None:
                continue

            dedupe_key = "|".join(
                [
                    str(candidate.get("asin") or "").strip().upper(),
                    str(candidate.get("title") or "").strip().lower(),
                    str(candidate.get("series_number") if candidate.get("series_number") is not None else ""),
                ]
            )
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            book_candidates.append(candidate)

    json_blobs_valid = len(book_candidates)

    print(f"AMAZON JSON ADAPTER: blobs scanned = {json_blobs_scanned}")
    print(f"AMAZON JSON ADAPTER: book blobs extracted = {json_blobs_valid}")

    return {
        "book_candidates": book_candidates,
        "json_blobs_scanned": json_blobs_scanned,
        "json_blobs_valid": json_blobs_valid,
    }
