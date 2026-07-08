from __future__ import annotations

import html
import re
from datetime import datetime

from bs4 import BeautifulSoup

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def _clean_text(value: str | None) -> str:
    unescaped = html.unescape(str(value or "").strip())
    return re.sub(r"\s+", " ", unescaped)


def _is_valid_asin(value: str | None) -> bool:
    asin = _clean_text(value).upper()
    return bool(_ASIN_RE.fullmatch(asin))


def _extract_asin_from_url(url: str) -> str | None:
    raw = str(url or "")
    match = re.search(r"/dp/([A-Z0-9]{10})(?:[/?#]|$)", raw, flags=re.IGNORECASE)
    if match:
        asin = match.group(1).upper()
        if _is_valid_asin(asin):
            return asin

    match = re.search(r"/gp/product/([A-Z0-9]{10})(?:[/?#]|$)", raw, flags=re.IGNORECASE)
    if match:
        asin = match.group(1).upper()
        if _is_valid_asin(asin):
            return asin

    return None


def _extract_label_value_pairs(soup: BeautifulSoup) -> dict[str, str]:
    extracted: dict[str, str] = {}

    # detail bullets area
    for item in soup.select("#detailBullets_feature_div li"):
        label_node = item.select_one("span.a-text-bold")
        if not label_node:
            continue
        label = _clean_text(label_node.get_text(" ", strip=True)).rstrip(":").lower()
        full_text = _clean_text(item.get_text(" ", strip=True))
        label_text = _clean_text(label_node.get_text(" ", strip=True))
        value = _clean_text(full_text.replace(label_text, "", 1)).lstrip(": ").strip()
        if label and value and label not in extracted:
            extracted[label] = value

    # product details table area
    for row in soup.select("#productDetails_detailBullets_sections1 tr"):
        header = row.select_one("th")
        value_node = row.select_one("td")
        if not header or not value_node:
            continue
        label = _clean_text(header.get_text(" ", strip=True)).rstrip(":").lower()
        value = _clean_text(value_node.get_text(" ", strip=True))
        if label and value and label not in extracted:
            extracted[label] = value

    return extracted


def _extract_release_date(value: str | None) -> str | None:
    text = _clean_text(value)
    if not text:
        return None

    # Strip parenthetical details often appended by Amazon.
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()

    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            parsed = datetime.strptime(text, fmt).date()
            return parsed.isoformat()
        except ValueError:
            continue
    return None


def _extract_series_name_and_book_number(soup: BeautifulSoup, details: dict[str, str]) -> tuple[str | None, int | None]:
    series_name: str | None = None
    book_number: int | None = None

    series_candidates: list[str] = []
    series_candidates.extend(
        [
            _clean_text(node.get_text(" ", strip=True))
            for node in soup.select("#seriesBulletWidget_feature_div a")
        ]
    )
    series_candidates.extend(
        [
            _clean_text(node.get_text(" ", strip=True))
            for node in soup.select("#rpi-attribute-book_details-series_and_number .a-size-base")
        ]
    )

    detail_series_value = _clean_text(details.get("series") or "")
    if detail_series_value:
        series_candidates.append(detail_series_value)

    for candidate in series_candidates:
        if not candidate:
            continue

        # Handles strings like "Book 3 of 12: The Stormlight Archive".
        match = re.search(r"book\s*(\d+)(?:\s+of\s+\d+)?\s*[:\-]\s*(.+)$", candidate, flags=re.IGNORECASE)
        if match:
            try:
                parsed_number = int(match.group(1))
                if parsed_number > 0:
                    book_number = parsed_number
            except (TypeError, ValueError):
                pass
            possible_series = _clean_text(match.group(2))
            if possible_series:
                series_name = possible_series
            continue

        if candidate.lower().startswith("book "):
            number_match = re.search(r"book\s*(\d+)", candidate, flags=re.IGNORECASE)
            if number_match and book_number is None:
                try:
                    parsed_number = int(number_match.group(1))
                    if parsed_number > 0:
                        book_number = parsed_number
                except (TypeError, ValueError):
                    pass
            continue

        if series_name is None:
            series_name = candidate

    if book_number is None:
        inferred_number = details.get("book") or details.get("book number") or ""
        number_match = re.search(r"(\d+)", inferred_number)
        if number_match:
            try:
                parsed_number = int(number_match.group(1))
                if parsed_number > 0:
                    book_number = parsed_number
            except (TypeError, ValueError):
                pass

    return series_name, book_number


def _derive_availability(publish_date: str | None, upcoming_date: str | None) -> str:
    if upcoming_date:
        return "upcoming"
    if publish_date:
        return "available"
    return "unknown"


def _has_alphabetic_character(value: str | None) -> bool:
    return bool(re.search(r"[A-Za-z]", str(value or "")))


def _normalize_editorial_title(value: str | None) -> str:
    text = _clean_text(value)
    if not text:
        return ""

    text = re.sub(r"^Amazon\.com:\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(
        r"\s*\([^)]*(?:Audible Audio Edition|Kindle Edition|Hardcover|Paperback)[^)]*\)\s*:",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    text = re.sub(r"\s*:\s*[^:]+$", "", text).strip()
    return text


def _extract_title_with_selector(soup: BeautifulSoup, details: dict[str, str]) -> tuple[str, str | None]:
    selector_checks = [
        ("#productTitle", "text"),
        ("#title", "text"),
        ("#title_feature_div", "text"),
        ("#rpi-attribute-book_details-title .a-size-base", "text"),
        ("#rpi-attribute-book_details-title", "text"),
        ("#productDetails_detailBullets_sections1", "details-title"),
        ("#detailBullets_feature_div", "details-title"),
        ('meta[name="title"]', "meta"),
        ('meta[property="og:title"]', "meta"),
    ]

    for selector, mode in selector_checks:
        if mode == "details-title":
            candidate = _clean_text(details.get("title") or details.get("book title") or "")
        else:
            node = soup.select_one(selector)
            if not node:
                continue
            if mode == "meta":
                candidate = _normalize_editorial_title(node.get("content"))
            else:
                candidate = _clean_text(node.get_text(" ", strip=True))

        if not candidate or not _has_alphabetic_character(candidate):
            continue

        print(f"AMAZON PRODUCT TITLE EXTRACTOR: matched selector = {selector}")
        return candidate, selector

    return "", None


def extract_amazon_product_metadata_from_html(
    raw_html: str,
    product_url: str,
    expected_asin: str | None = None,
) -> dict:
    html_text = str(raw_html or "")
    if not html_text.strip():
        return {
            "failure_reason": "empty_html",
            "url": str(product_url or "").strip(),
        }

    soup = BeautifulSoup(html_text, "html.parser")
    details = _extract_label_value_pairs(soup)
    title, title_selector = _extract_title_with_selector(soup, details)
    if not title:
        return {
            "failure_reason": "missing_title",
            "title_selector": None,
            "url": str(product_url or "").strip(),
        }

    author_node = soup.select_one("#bylineInfo")
    author_text = _clean_text(author_node.get_text(" ", strip=True) if author_node else "")
    author_text = re.sub(r"^by\s+", "", author_text, flags=re.IGNORECASE).strip()

    asin = ""
    for key in ("asin",):
        if key in details and _is_valid_asin(details[key]):
            asin = details[key].upper()
            break

    if not asin:
        asin = _extract_asin_from_url(product_url) or ""

    if expected_asin and _is_valid_asin(expected_asin):
        expected = expected_asin.upper()
        if asin and asin != expected:
            return {
                "failure_reason": "asin_mismatch",
                "asin_or_id": asin,
                "expected_asin": expected,
                "title_selector": title_selector,
                "url": str(product_url or "").strip(),
            }
        asin = expected

    if not _is_valid_asin(asin):
        return {
            "failure_reason": "missing_asin",
            "expected_asin": str(expected_asin or "").strip().upper() or None,
            "title_selector": title_selector,
            "url": str(product_url or "").strip(),
        }

    publish_date = _extract_release_date(
        details.get("publication date")
        or details.get("publisher")
    )
    upcoming_date = None
    if publish_date:
        try:
            if datetime.strptime(publish_date, "%Y-%m-%d").date() > datetime.utcnow().date():
                upcoming_date = publish_date
        except ValueError:
            upcoming_date = None

    series_name, book_number = _extract_series_name_and_book_number(soup, details)
    availability = _derive_availability(publish_date, upcoming_date)

    return {
        "title": title,
        "author": author_text,
        "asin_or_id": asin,
        "release_date": publish_date,
        "series_name": series_name,
        "book_number": book_number,
        "publish_date": publish_date,
        "upcoming_date": upcoming_date,
        "availability": availability,
        "failure_reason": None,
        "title_selector": title_selector,
        "url": str(product_url or "").strip() or f"https://www.amazon.com/dp/{asin}",
    }
