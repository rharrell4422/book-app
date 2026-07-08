from __future__ import annotations

import html
import re
from typing import Any

_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9:_-]*)\b([^>]*)>", flags=re.IGNORECASE | re.DOTALL)
_ELEMENT_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9:_-]*)\b([^>]*)>(.*?)</\1>", flags=re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r"([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(['\"])(.*?)\2", flags=re.IGNORECASE | re.DOTALL)

_TRIGGER_ATTRS = {
    "data-asin",
    "data-index",
    "data-uuid",
    "data-csa-c-item-id",
    "data-csa-c-content-id",
    "data-csa-c-slot-id",
    "data-csa-c-type",
    "data-csa-c-content-type",
}

_METADATA_TOKENS = {
    "asin",
    "title",
    "author",
    "price",
    "isbn",
    "book",
    "product",
    "content-id",
    "item-id",
    "slot",
    "uuid",
    "index",
    "name",
}

_IGNORED_TOKENS = {
    "layout",
    "gating",
    "experiment",
    "variant",
    "treatment",
    "bucket",
    "feature",
    "flag",
    "impression",
    "tracking",
    "analytics",
    "abtest",
    "widget",
    "slot-name",
}

_ASSOCIATION_ATTRS = {
    "data-index",
    "data-uuid",
    "data-csa-c-item-id",
    "data-csa-c-content-id",
}

_OUTPUT_KEYS = (
    "asin",
    "title",
    "author",
    "price",
    "index",
    "uuid",
    "slot",
    "component",
)


def _parse_attributes(attr_text: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for key, _, value in _ATTR_RE.findall(attr_text or ""):
        normalized_key = key.strip().lower()
        normalized_value = html.unescape((value or "").strip())
        if not normalized_key or not normalized_value:
            continue
        parsed[normalized_key] = normalized_value
    return parsed


def _class_tokens(attrs: dict[str, str]) -> set[str]:
    return {token.strip().lower() for token in attrs.get("class", "").split() if token.strip()}


def _extract_visible_text(text: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _is_search_result(attrs: dict[str, str]) -> bool:
    component_type = attrs.get("data-component-type", "").strip().lower()
    cel_widget = attrs.get("data-cel-widget", "").strip().lower()
    return component_type == "s-search-result" or cel_widget.startswith("search_result_")


def _is_candidate_element(attrs: dict[str, str]) -> bool:
    if _is_search_result(attrs):
        return True
    return any(key in attrs for key in _TRIGGER_ATTRS)


def _is_metadata_attr(key: str) -> bool:
    lowered = key.lower()
    if any(token in lowered for token in _IGNORED_TOKENS):
        return False
    return any(token in lowered for token in _METADATA_TOKENS)


def _build_metadata(filtered: dict[str, str]) -> dict[str, Any]:
    asin = filtered.get("data-asin") or filtered.get("data-csa-c-item-id")
    title = (
        filtered.get("data-title")
        or filtered.get("data-csa-c-content-id")
        or filtered.get("title")
        or filtered.get("aria-label")
        or filtered.get("data-name")
    )
    author = filtered.get("data-author") or filtered.get("author")
    price = filtered.get("data-price") or filtered.get("price")
    index = filtered.get("data-index")
    uuid = filtered.get("data-uuid")
    component = filtered.get("data-component-type") or filtered.get("data-csa-c-type")
    slot = filtered.get("data-csa-c-slot-id") or filtered.get("data-cel-widget")

    return {
        "asin": asin,
        "title": title,
        "author": author,
        "price": price,
        "index": index,
        "uuid": uuid,
        "component": component,
        "slot": slot,
    }


def _is_title_container_element(tag_name: str, attrs: dict[str, str]) -> bool:
    classes = _class_tokens(attrs)
    lowered_tag_name = tag_name.strip().lower()
    return lowered_tag_name == "h2" and {"a-size-mini", "s-line-clamp-2"}.issubset(classes)


def _extract_title_text_from_container(body: str) -> str | None:
    for anchor_match in _ELEMENT_RE.finditer(body or ""):
        tag_name = anchor_match.group(1).strip().lower()
        if tag_name != "a":
            continue

        attrs = _parse_attributes(anchor_match.group(2))
        classes = _class_tokens(attrs)
        if not {"a-link-normal", "a-text-normal", "s-underline-text", "s-link-style"}.issubset(classes):
            continue

        title_text = _extract_visible_text(anchor_match.group(3))
        if title_text:
            return title_text

    return None


def _is_associable_element(attrs: dict[str, str]) -> bool:
    if _is_search_result(attrs):
        return True
    return any(key in attrs for key in _ASSOCIATION_ATTRS)


def _find_nearest_root_index(candidate_index: int, root_indexes: list[int]) -> int | None:
    if not root_indexes:
        return None

    best_index: int | None = None
    best_distance: int | None = None

    for root_index in root_indexes:
        distance = abs(root_index - candidate_index)
        if best_distance is None or distance < best_distance or (distance == best_distance and root_index < (best_index or root_index + 1)):
            best_index = root_index
            best_distance = distance

    return best_index


def _merge_group_metadata(group_nodes: list[dict[str, Any]]) -> dict[str, Any]:
    merged = {key: None for key in _OUTPUT_KEYS}

    for node in group_nodes:
        metadata = node["metadata"]
        for key in _OUTPUT_KEYS:
            if merged[key]:
                continue
            value = metadata.get(key)
            if value:
                merged[key] = value

    return merged


def extract_html_attribute_metadata(raw_html: str) -> list[dict[str, Any]]:
    html_text = raw_html if isinstance(raw_html, str) else str(raw_html or "")
    if not html_text.strip():
        return []

    candidate_nodes: list[dict[str, Any]] = []
    debug_lines: list[str] = []
    scanned = 0
    tag_index_by_start: dict[int, int] = {}

    for element_index, tag_match in enumerate(_TAG_RE.finditer(html_text)):
        tag_index_by_start[tag_match.start()] = element_index
        attr_text = tag_match.group(2)
        attrs = _parse_attributes(attr_text)
        if not attrs or not _is_candidate_element(attrs):
            continue

        scanned += 1
        filtered = {key: value for key, value in attrs.items() if _is_metadata_attr(key)}
        if not filtered:
            continue

        metadata = _build_metadata(filtered)
        if not any(metadata.values()):
            continue

        candidate_nodes.append(
            {
                "element_index": element_index,
                "attrs": attrs,
                "metadata": metadata,
            }
        )

    root_nodes = [node for node in candidate_nodes if node["metadata"].get("asin")]
    root_indexes = [node["element_index"] for node in root_nodes]
    root_asin_by_index = {
        node["element_index"]: str(node["metadata"].get("asin") or "").strip()
        for node in root_nodes
        if str(node["metadata"].get("asin") or "").strip()
    }
    grouped_nodes: dict[str, list[dict[str, Any]]] = {}
    title_nodes: list[dict[str, Any]] = []
    attached_titles_by_asin: dict[str, str] = {}

    for root_node in root_nodes:
        asin = str(root_node["metadata"].get("asin") or "").strip()
        if not asin:
            continue
        grouped_nodes.setdefault(asin, []).append(root_node)

    for element_match in _ELEMENT_RE.finditer(html_text):
        tag_name = element_match.group(1)
        attr_text = element_match.group(2)
        body = element_match.group(3)
        element_index = tag_index_by_start.get(element_match.start())
        if element_index is None:
            continue

        attrs = _parse_attributes(attr_text)
        if not attrs or not _is_title_container_element(tag_name, attrs):
            continue

        title_text = _extract_title_text_from_container(body)
        if not title_text:
            continue

        title_nodes.append({"title_text": title_text, "element_index": element_index})

    for title_node in title_nodes:
        nearest_root_index = _find_nearest_root_index(title_node["element_index"], root_indexes)
        if nearest_root_index is None:
            continue

        asin = root_asin_by_index.get(nearest_root_index)
        if not asin or attached_titles_by_asin.get(asin):
            continue

        attached_titles_by_asin[asin] = title_node["title_text"]
        debug_lines.append(f"HTML TITLE EXTRACTOR: title attached to asin={asin}")

    for node in candidate_nodes:
        metadata = node["metadata"]
        asin = str(metadata.get("asin") or "").strip()
        if asin:
            continue
        if not _is_associable_element(node["attrs"]):
            continue

        nearest_root_index = _find_nearest_root_index(node["element_index"], root_indexes)
        if nearest_root_index is None:
            continue

        for root_node in root_nodes:
            if root_node["element_index"] != nearest_root_index:
                continue
            root_asin = str(root_node["metadata"].get("asin") or "").strip()
            if root_asin:
                grouped_nodes.setdefault(root_asin, []).append(node)
            break

    merged_candidates: list[dict[str, Any]] = []
    for asin, group_nodes in grouped_nodes.items():
        root_first_nodes = sorted(
            group_nodes,
            key=lambda node: (
                0 if str(node["metadata"].get("asin") or "").strip() == asin else 1,
                abs(node["element_index"] - group_nodes[0]["element_index"]),
                node["element_index"],
            ),
        )
        merged_candidate = _merge_group_metadata(root_first_nodes)
        title_text = attached_titles_by_asin.get(asin)
        if title_text:
            merged_candidate["title"] = title_text
        merged_candidates.append(merged_candidate)

    debug_lines.append(f"HTML ATTRIBUTE EXTRACTOR: elements scanned = {scanned}")
    debug_lines.append(f"HTML ATTRIBUTE EXTRACTOR: product-metadata candidates = {len(candidate_nodes)}")
    debug_lines.append(f"HTML TITLE EXTRACTOR: title nodes found = {len(title_nodes)}")
    debug_lines.append(f"HTML ATTRIBUTE MERGER: asin groups = {len(merged_candidates)}")
    for index, candidate in enumerate(merged_candidates, start=1):
        keys = sorted([key for key, value in candidate.items() if value])
        debug_lines.append(f"HTML ATTRIBUTE MERGER: merged candidate {index} keys = {keys}")

    for line in debug_lines:
        print(line)

    return merged_candidates
