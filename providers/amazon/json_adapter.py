from __future__ import annotations

import re
from typing import Any

from provider_core.json_extractor import extract_json_objects_from_html
from provider_core.json_parser import parse_json_object_to_candidates_debug, parse_json_objects_to_candidates_debug

_AMAZON_KEYS = {
    "asin",
    "productdetails",
    "productoverview",
    "producttype",
    "brand",
    "title",
    "author",
    "store",
}

_REJECT_TITLE_TOKENS = (
    "kindle unlimited",
    "free trial",
    "deals",
    "best sellers",
    "results for",
    "shop",
    "coupon",
    "prime day",
    "sponsored",
    "filter",
    "storefront",
)

_BOOKISH_PRODUCTTYPE_TOKENS = (
    "book",
    "kindle",
    "paperback",
    "hardcover",
    "audiobook",
)

_DETAIL_URL_RE = re.compile(r"https?://(?:www\.)?amazon\.[^/]+/.*/dp/([A-Z0-9]{10})(?:[/?#]|$)", flags=re.IGNORECASE)


def _collect_keys(node: Any, sink: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            sink.add(str(key).lower())
            _collect_keys(value, sink)
    elif isinstance(node, list):
        for item in node:
            _collect_keys(item, sink)


def _amazon_blob_key_score(blob: Any) -> int:
    keys: set[str] = set()
    _collect_keys(blob, keys)
    return sum(1 for key in _AMAZON_KEYS if key in keys)


def _json_blob_score(blob: Any) -> int:
    score = 0

    def walk(node: Any) -> None:
        nonlocal score
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                if lowered in {"asin", "title", "name", "author", "authors", "product", "book", "release_date", "releasedate", "publicationdate"}:
                    score += 2
                if "book" in lowered or "product" in lowered:
                    score += 1
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(blob)
    return score


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


def _is_rejected_title(value: str) -> bool:
    title = str(value or "").strip().lower()
    if not title:
        return True
    if len(title) < 3:
        return True
    if re.fullmatch(r"[\W_\d]+", title):
        return True
    return any(token in title for token in _REJECT_TITLE_TOKENS)


def _collect_values_for_keys(node: Any, keys: set[str], sink: dict[str, list[str]]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if lowered in keys:
                if isinstance(value, (str, int, float)):
                    sink.setdefault(lowered, []).append(str(value).strip())
            _collect_values_for_keys(value, keys, sink)
    elif isinstance(node, list):
        for item in node:
            _collect_values_for_keys(item, keys, sink)


def _has_valid_book_metadata_fields(blob: Any) -> bool:
    watched_keys = {"producttype", "parentasin", "detailpageurl", "canonicalurl", "producturl"}
    values_by_key: dict[str, list[str]] = {}
    _collect_values_for_keys(blob, watched_keys, values_by_key)

    product_type_values = [v.lower() for v in values_by_key.get("producttype", []) if v]
    if product_type_values and not any(any(token in value for token in _BOOKISH_PRODUCTTYPE_TOKENS) for value in product_type_values):
        return False

    parent_asin_values = [v for v in values_by_key.get("parentasin", []) if v]
    if parent_asin_values and not any(_is_valid_asin(value) for value in parent_asin_values):
        return False

    detail_like_values = [v for key in ("detailpageurl", "canonicalurl", "producturl") for v in values_by_key.get(key, []) if v]
    if detail_like_values and not any(_is_valid_detail_url(value) for value in detail_like_values):
        return False

    return True


def _blob_has_valid_book_candidate(blob: Any) -> bool:
    candidates, _ = parse_json_object_to_candidates_debug(blob)
    for candidate in candidates:
        title = str(candidate.get("title") or "").strip()
        asin = str(candidate.get("asin_or_id") or "").strip().upper()
        url = str(candidate.get("url") or "").strip()

        if _is_rejected_title(title):
            continue
        if not _is_valid_asin(asin):
            continue
        if not _is_valid_detail_url(url):
            continue
        if f"/dp/{asin.lower()}" not in url.lower():
            continue
        return True

    return False


def _is_book_blob(blob: Any) -> bool:
    keys: set[str] = set()
    _collect_keys(blob, keys)

    has_id = any(key in keys for key in {"asin", "id", "isbn", "identifier", "productid", "sku"})
    has_title = any(key in keys for key in {"title", "name", "booktitle", "producttitle"})
    has_book_context = any(token in key for key in keys for token in {"book", "product", "author", "release", "publication", "detailpageurl", "parentasin", "producttype"})

    if not (has_id and has_title and has_book_context):
        return False

    if not _has_valid_book_metadata_fields(blob):
        return False

    return _blob_has_valid_book_candidate(blob)


def extract_amazon_candidates_from_json(raw_html: str) -> list[dict[str, Any]]:
    blobs = extract_json_objects_from_html(raw_html)
    adapter_debug_lines: list[str] = []
    adapter_debug_lines.append(f"AMAZON JSON ADAPTER: blobs received = {len(blobs)}")
    if not blobs:
        for line in adapter_debug_lines:
            print(line)
        return []

    scored_entries = []
    for index, blob in enumerate(blobs):
        amazon_score = _amazon_blob_key_score(blob)
        generic_score = _json_blob_score(blob)
        adapter_debug_lines.append(f"AMAZON JSON ADAPTER: blob index {index} score = {amazon_score}")
        scored_entries.append((index, blob, amazon_score, generic_score))

    book_blobs: list[Any] = []
    for _, blob, amazon_score, generic_score in scored_entries:
        if amazon_score <= 0 and generic_score <= 0:
            continue
        if not _is_book_blob(blob):
            continue
        book_blobs.append(blob)

    adapter_debug_lines.append(f"AMAZON JSON ADAPTER: book blobs selected = {len(book_blobs)}")

    if not book_blobs:
        for line in adapter_debug_lines:
            print(line)
        return []

    candidates, parser_debug_lines = parse_json_objects_to_candidates_debug(book_blobs)

    for line in adapter_debug_lines:
        print(line)
    for line in parser_debug_lines:
        print(line)

    return candidates
