from __future__ import annotations

from typing import Any

_TITLE_KEYS = ("title", "name", "bookTitle", "productTitle")
_AUTHOR_KEYS = ("author", "authors", "creator", "writer", "by")
_ID_KEYS = ("asin", "id", "sku", "productId", "identifier", "isbn")
_DATE_KEYS = ("release_date", "releaseDate", "publicationDate", "publishDate", "datePublished", "pub_date")
_URL_KEYS = ("url", "link", "detailPageURL", "canonicalUrl", "productUrl")


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _first_text(node: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key not in node:
            continue

        value = node.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    text = _first_text(item, ("name", "title", "id", "value"))
                else:
                    text = _to_text(item)
                if text:
                    return text
            continue

        if isinstance(value, dict):
            nested = _first_text(value, ("name", "title", "id", "value"))
            if nested:
                return nested
            continue

        text = _to_text(value)
        if text:
            return text

    return None


def _walk_json(node: Any) -> list[dict[str, Any]]:
    discovered: list[dict[str, Any]] = []

    if isinstance(node, dict):
        discovered.append(node)
        for value in node.values():
            discovered.extend(_walk_json(value))
    elif isinstance(node, list):
        for item in node:
            discovered.extend(_walk_json(item))

    return discovered


def _is_attribute_metadata_blob(node: Any) -> bool:
    return isinstance(node, dict) and "asin" in node and "title" in node


def _parse_json_object_to_candidates_with_debug(json_object: Any) -> tuple[list[dict[str, Any]], list[str]]:
    candidates: list[dict[str, Any]] = []
    debug_lines: list[str] = []
    attribute_mode_hits = 0
    generic_mode_hits = 0

    for node in _walk_json(json_object):
        if _is_attribute_metadata_blob(node):
            asin_or_id = _to_text(node.get("asin"))
            title = _to_text(node.get("title"))
            author = _to_text(node.get("author")) or ""
            release_date = _to_text(node.get("release_date")) or ""
            price = _to_text(node.get("price")) or ""

            if not asin_or_id or not title:
                continue

            url = f"https://www.amazon.com/dp/{asin_or_id}"

            attribute_mode_hits += 1

            candidates.append(
                {
                    "title": title,
                    "author": author,
                    "asin_or_id": asin_or_id,
                    "release_date": release_date,
                    "url": url,
                }
            )

            continue

        title = _first_text(node, _TITLE_KEYS)
        author = _first_text(node, _AUTHOR_KEYS)
        asin_or_id = _first_text(node, _ID_KEYS)
        release_date = _first_text(node, _DATE_KEYS)
        url = _first_text(node, _URL_KEYS)

        if not title:
            continue
        if not any((author, asin_or_id, release_date, url)):
            continue

        generic_mode_hits += 1

        candidates.append(
            {
                "title": title,
                "author": author,
                "asin_or_id": asin_or_id,
                "release_date": release_date,
                "url": url,
            }
        )

    debug_lines.append(f"AMAZON JSON PARSER: attribute-metadata hits = {attribute_mode_hits}")
    debug_lines.append(f"AMAZON JSON PARSER: generic-node hits = {generic_mode_hits}")
    debug_lines.append(f"AMAZON JSON PARSER: candidates produced = {len(candidates)}")

    return candidates, debug_lines


def parse_json_objects_to_candidates_debug(json_objects: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    all_candidates: list[dict[str, Any]] = []
    all_debug_lines: list[str] = []
    blobs_parsed = 0

    for json_object in json_objects:
        blobs_parsed += 1
        candidates, debug_lines = _parse_json_object_to_candidates_with_debug(json_object)
        all_debug_lines.extend(debug_lines)
        all_candidates.extend(candidates)

    all_debug_lines.append(f"AMAZON JSON PARSER: blobs parsed = {blobs_parsed}")
    all_debug_lines.append(f"AMAZON JSON PARSER: total candidates produced = {len(all_candidates)}")
    return all_candidates, all_debug_lines


def parse_json_object_to_candidates_debug(json_object: Any) -> tuple[list[dict[str, Any]], list[str]]:
    return _parse_json_object_to_candidates_with_debug(json_object)


def parse_json_object_to_candidates(json_object: Any) -> list[dict[str, Any]]:
    candidates, debug_lines = _parse_json_object_to_candidates_with_debug(json_object)
    for line in debug_lines:
        print(line)

    return candidates
