from __future__ import annotations

import html
import json
import re
from typing import Any

from provider_core.html_attribute_extractor import extract_html_attribute_metadata

_SCRIPT_TAG_RE = re.compile(r"(<script\b[^>]*>)(.*?)(</script>)", flags=re.IGNORECASE | re.DOTALL)

_TARGET_SCRIPT_IDS = {
    "search-data",
    "dp-data",
    "twister-js-init",
    "aplus-module-data",
    "p13n-state",
    "product-state",
    "product-details",
    "product-overview",
}

_ASSIGNMENT_PATTERNS = (
    "window.P =",
    "window.APOLLO_STATE =",
    "window.DYNAMIC_DATA =",
    "window.STATE =",
    "window.INITIAL_STATE =",
    "window.detailData =",
    "window.searchData =",
    "window.dpData =",
    "window.productData =",
    "window.twisterData =",
)

_ID_ATTR_RE = re.compile(r"\bid\s*=\s*['\"]([^'\"]+)['\"]", flags=re.IGNORECASE)
_TYPE_ATTR_RE = re.compile(r"\btype\s*=\s*['\"]([^'\"]+)['\"]", flags=re.IGNORECASE)
_DATA_A_STATE_RE = re.compile(r"\bdata-a-state\s*=\s*(['\"])(.*?)\1", flags=re.IGNORECASE | re.DOTALL)
_DATA_A_DYNAMIC_RE = re.compile(r"\bdata-a-dynamic\s*=\s*(['\"])(.*?)\1", flags=re.IGNORECASE | re.DOTALL)


def _find_matching_brace(text: str, start_index: int) -> int:
    depth = 0
    in_string = False
    escaped = False
    quote_char = '"'

    for idx in range(start_index, len(text)):
        char = text[idx]

        if in_string:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == quote_char:
                in_string = False
            continue

        if char in {'"', "'"}:
            in_string = True
            quote_char = char
            continue

        if char == "{":
            depth += 1
            continue

        if char == "}":
            depth -= 1
            if depth == 0:
                return idx

    return -1


def _extract_balanced_json_object(text: str, open_brace_index: int) -> str | None:
    close_brace_index = _find_matching_brace(text, open_brace_index)
    if close_brace_index == -1:
        return None
    return text[open_brace_index : close_brace_index + 1]


def _extract_json_strings_from_script(script_text: str) -> list[str]:
    fragments: list[str] = []

    for pattern in _ASSIGNMENT_PATTERNS:
        start = 0
        while True:
            assignment_index = script_text.find(pattern, start)
            if assignment_index == -1:
                break

            equals_index = script_text.find("=", assignment_index)
            if equals_index == -1:
                break

            brace_index = script_text.find("{", equals_index + 1)
            if brace_index == -1:
                start = assignment_index + len(pattern)
                continue

            candidate = _extract_balanced_json_object(script_text, brace_index)
            if candidate:
                fragments.append(candidate)

            start = assignment_index + len(pattern)

    return fragments


def extract_json_objects_from_html(raw_html: str) -> list[Any]:
    html_text = raw_html if isinstance(raw_html, str) else str(raw_html or "")
    if not html_text.strip():
        return []

    json_objects: list[Any] = []
    seen: set[str] = set()
    decode_failures = 0
    script_tags_scanned = 0
    product_json_candidates = 0
    product_json_decode_failures = 0

    def append_unique(obj: Any) -> None:
        try:
            signature = json.dumps(obj, sort_keys=True, default=str)
        except (TypeError, ValueError):
            signature = repr(obj)

        if signature in seen:
            return

        seen.add(signature)
        json_objects.append(obj)

    for opening, body, closing in _SCRIPT_TAG_RE.findall(html_text):
        del closing
        opening_lower = opening.lower()
        type_match = _TYPE_ATTR_RE.search(opening)
        script_type = type_match.group(1).strip().lower() if type_match else ""
        has_target_type = script_type in {"application/json", "application/ld+json"}
        has_data_attrs = ("data-a-state" in opening_lower) or ("data-a-dynamic" in opening_lower)
        id_match = _ID_ATTR_RE.search(opening)
        script_id = id_match.group(1).strip().lower() if id_match else ""
        has_target_id = script_id in _TARGET_SCRIPT_IDS

        body = str(body or "").strip()
        has_assignment_pattern = any(pattern in body for pattern in _ASSIGNMENT_PATTERNS)
        if not (has_target_type or has_data_attrs or has_target_id or has_assignment_pattern):
            continue

        script_tags_scanned += 1

        for attr_re in (_DATA_A_STATE_RE, _DATA_A_DYNAMIC_RE):
            attr_match = attr_re.search(opening)
            if not attr_match:
                continue

            product_json_candidates += 1
            attr_value = html.unescape(attr_match.group(2).strip())
            try:
                attr_obj = json.loads(attr_value)
            except (json.JSONDecodeError, TypeError, ValueError):
                product_json_decode_failures += 1
            else:
                append_unique(attr_obj)

        if has_target_type or has_target_id or has_data_attrs:
            if body:
                product_json_candidates += 1
                try:
                    product_obj = json.loads(body)
                except (json.JSONDecodeError, TypeError, ValueError):
                    product_json_decode_failures += 1
                else:
                    append_unique(product_obj)

        for fragment in _extract_json_strings_from_script(body):
            try:
                obj = json.loads(fragment)
            except (json.JSONDecodeError, TypeError, ValueError):
                decode_failures += 1
                continue

            append_unique(obj)

    for metadata_blob in extract_html_attribute_metadata(html_text):
        append_unique(metadata_blob)

    print(f"PRODUCT JSON EXTRACTOR: product-json candidates = {product_json_candidates}")
    print(f"PRODUCT JSON EXTRACTOR: product-json decode failures = {product_json_decode_failures}")
    print(f"JSON EXTRACTOR: script tags scanned = {script_tags_scanned}")
    print(f"JSON EXTRACTOR: JSON blobs extracted = {len(json_objects)}")
    print(f"JSON EXTRACTOR: decode failures = {decode_failures}")

    return json_objects
