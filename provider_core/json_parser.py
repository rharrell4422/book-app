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

            extracted_fields: list[str] = ["title", "asin_or_id", "url"]
            if author:
                extracted_fields.append("author")
            if release_date:
                extracted_fields.append("release_date")
            if price:
                extracted_fields.append("price")

            missing_fields = [
                field
                for field, value in (
                    ("author", author),
                    ("release_date", release_date),
                )
                if not value
            ]

            debug_lines.append("AMAZON JSON PARSER: attribute-metadata mode activated")
            debug_lines.append(f"AMAZON JSON PARSER: fields extracted = {extracted_fields}")
            debug_lines.append(f"AMAZON JSON PARSER: missing fields = {missing_fields}")

            candidates.append(
                {
                    "title": title,
                    "author": author,
                    "asin_or_id": asin_or_id,
                    "release_date": release_date,
                    "url": url,
                }
            )

            short_title = str(title)[:80]
            short_asin = str(asin_or_id)[:40]
            debug_lines.append(
                f"AMAZON JSON PARSER: candidate {len(candidates)} = "
                f"title='{short_title}', asin='{short_asin}'"
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

        extracted_fields: list[str] = ["title"]
        if author:
            extracted_fields.append("author")
        if asin_or_id:
            extracted_fields.append("asin_or_id")
        if release_date:
            extracted_fields.append("release_date")
        if url:
            extracted_fields.append("url")

        missing_fields = [
            field
            for field, value in (
                ("author", author),
                ("asin_or_id", asin_or_id),
                ("release_date", release_date),
                ("url", url),
            )
            if not value
        ]

        debug_lines.append(f"AMAZON JSON PARSER: fields extracted = {extracted_fields}")
        debug_lines.append(f"AMAZON JSON PARSER: missing fields = {missing_fields}")

        candidates.append(
            {
                "title": title,
                "author": author,
                "asin_or_id": asin_or_id,
                "release_date": release_date,
                "url": url,
            }
        )

        short_title = str(title)[:80]
        short_asin = str(asin_or_id or "")[:40]
        debug_lines.append(
            f"AMAZON JSON PARSER: candidate {len(candidates)} = "
            f"title='{short_title}', asin='{short_asin}'"
        )

    debug_lines.append(f"AMAZON JSON PARSER: candidates produced = {len(candidates)}")

    return candidates, debug_lines


def parse_json_objects_to_candidates_debug(json_objects: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    all_candidates: list[dict[str, Any]] = []
    all_debug_lines: list[str] = []

    for blob_index, json_object in enumerate(json_objects):
        candidates, debug_lines = _parse_json_object_to_candidates_with_debug(json_object)
        all_debug_lines.append(f"AMAZON JSON PARSER: parsing blob index = {blob_index}")
        all_debug_lines.extend(debug_lines)
        all_candidates.extend(candidates)

    all_debug_lines.append(f"AMAZON JSON PARSER: total candidates produced = {len(all_candidates)}")
    return all_candidates, all_debug_lines


def parse_json_object_to_candidates_debug(json_object: Any) -> tuple[list[dict[str, Any]], list[str]]:
    return _parse_json_object_to_candidates_with_debug(json_object)


def parse_json_object_to_candidates(json_object: Any) -> list[dict[str, Any]]:
    candidates, debug_lines = _parse_json_object_to_candidates_with_debug(json_object)
    for line in debug_lines:
        print(line)

    return candidates
