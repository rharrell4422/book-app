from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

from models import Series


RETAIL_DOMAINS = {
    "amazon.com",
    "www.amazon.com",
    "fantasticfiction.com",
    "www.fantasticfiction.com",
}
PUBLISHER_DOMAINS = {
    "penguinrandomhouse.com",
    "www.penguinrandomhouse.com",
    "tor.com",
    "www.tor.com",
    "orbitbooks.net",
    "www.orbitbooks.net",
    "baen.com",
    "www.baen.com",
    "harpercollins.com",
    "www.harpercollins.com",
}
METADATA_WAREHOUSE_TOKENS = {
    "openlibrary",
    "goodreads",
    "isbn",
    "worldcat",
    "librarything",
    "bookfinder",
    "isfdb",
    "wikipedia",
}

PROVIDER_PRIORITY = {
    "amazon_series_page": 6,
    "publisher_site": 5,
    "author_site": 4,
    "fantasticfiction": 3,
    "amazon_books": 2,
    "google_html_search": 1,
}


def _to_int(value) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number.is_integer():
        return int(number)
    return None


def determine_next_book_number(series: Series) -> int:
    candidates: list[int] = []

    next_upcoming = _to_int(getattr(series, "next_upcoming_book_number", None))
    if next_upcoming:
        candidates.append(next_upcoming)

    next_unread = _to_int(getattr(series, "next_unread_book_number", None))
    if next_unread:
        candidates.append(next_unread)

    missing = getattr(series, "missing_books", None)
    if isinstance(missing, list):
        missing_numbers = sorted(number for number in (_to_int(item) for item in missing) if number)
        if missing_numbers:
            candidates.append(missing_numbers[0])

    highest_owned = _to_int(getattr(series, "highest_owned_book_number", None))
    if highest_owned:
        candidates.append(highest_owned + 1)

    total_books = _to_int(getattr(series, "total_books", None))
    if total_books:
        candidates.append(total_books + 1)

    if candidates:
        return min(candidates)
    return 1


def _strip_tags(text: str) -> str:
    collapsed = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", collapsed).strip()


def _extract_book_number(text: str) -> int | None:
    match = re.search(r"\b(?:book|volume|vol\.?|#)\s*(\d+)\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _normalize_domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().strip()


def _extract_author_from_text(text: str) -> str | None:
    match = re.search(r"\bby\s+([A-Z][A-Za-z\-\'\s\.]{2,80})", text)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip(" .,-")


def _extract_series_number_pattern(text: str, series_name: str) -> int | None:
    escaped_series = re.escape(series_name)
    patterns = [
        rf"book\s*#?\s*(\d+)\s+in\s+the\s+{escaped_series}\s+series",
        rf"{escaped_series}\s+series\s*[\-|:]?\s*book\s*#?\s*(\d+)",
        rf"{escaped_series}\s*[\-|:]?\s*#\s*(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                continue
    return _extract_book_number(text)


def _extract_publication_date_from_text(text: str) -> str | None:
    if not text:
        return None
    patterns = [
        r"\b(?:on|published\s+on|publication\s+date\s*[:\-]?)\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})\b",
        r"\b([A-Za-z]+\s+\d{1,2},\s+\d{4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = str(match.group(1) or "").strip()
        try:
            parsed = datetime.strptime(candidate, "%B %d, %Y").date()
            return parsed.isoformat()
        except ValueError:
            continue
    return None


def _status_hint_for_amazon(text: str, publication_date_iso: str | None) -> str:
    lowered = str(text or "").lower()
    if "pre-order" in lowered or "preorder" in lowered or "upcoming" in lowered:
        return "upcoming"

    if publication_date_iso:
        try:
            parsed = date.fromisoformat(publication_date_iso)
            if parsed > date.today():
                return "upcoming"
            return "available"
        except ValueError:
            pass
    return "unknown"


def _normalize_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _tokenize(value: str) -> set[str]:
    return {token for token in _normalize_match_text(value).split() if token}


def _series_names_match(target_series: str, observed_series: str) -> bool:
    target_norm = _normalize_match_text(target_series)
    observed_norm = _normalize_match_text(observed_series)
    if not target_norm or not observed_norm:
        return False
    if target_norm == observed_norm:
        return True
    if target_norm in observed_norm or observed_norm in target_norm:
        return True

    target_tokens = _tokenize(target_series)
    observed_tokens = _tokenize(observed_series)
    if not target_tokens or not observed_tokens:
        return False

    overlap = len(target_tokens & observed_tokens)
    return overlap >= max(2, int(len(target_tokens) * 0.75))


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_series_name(value: str) -> str:
    cleaned = _normalize_whitespace(value)
    cleaned = re.sub(r"\b(series|book series)\b", "", cleaned, flags=re.IGNORECASE)
    return _normalize_whitespace(cleaned)


def _normalize_author_name(value: str) -> str:
    cleaned = _normalize_whitespace(value)
    cleaned = re.sub(r"\band\s+\d+\s+more\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(author|narrator|editor)\b", "", cleaned, flags=re.IGNORECASE)
    return _normalize_whitespace(cleaned).strip(",-")


def _author_matches(target_author: str, observed_author: str) -> bool:
    target_norm = _normalize_match_text(_normalize_author_name(target_author))
    observed_norm = _normalize_match_text(_normalize_author_name(observed_author))
    if not target_norm or not observed_norm:
        return False
    return target_norm == observed_norm


def _passes_early_author_gate(target_author: str, candidate_author: str) -> bool:
    return _author_matches(target_author, candidate_author)


def _passes_title_series_fallback(
    *,
    target_series_name: str,
    candidate_title: str,
    candidate_series_name: str,
) -> bool:
    target_series_norm = _normalize_match_text(target_series_name)
    title_norm = _normalize_match_text(candidate_title)
    candidate_series_norm = _normalize_match_text(candidate_series_name)
    if not target_series_norm:
        return False
    return bool(
        (title_norm and target_series_norm in title_norm)
        or (candidate_series_norm and target_series_norm in candidate_series_norm)
    )


def _evaluate_hybrid_author_gate(
    *,
    target_author: str,
    candidate_author: str,
    target_series_name: str,
    candidate_title: str,
    candidate_series_name: str,
    provider_name: str = "",
) -> tuple[bool, bool, str]:
    if str(provider_name or "").strip().lower().startswith("amazon"):
        return True, False, "amazon-relaxed-gate"

    if _passes_early_author_gate(target_author, candidate_author):
        return True, False, "strict-author-match"

    target_author_norm = _normalize_match_text(_normalize_author_name(target_author))
    candidate_author_norm = _normalize_match_text(_normalize_author_name(candidate_author))
    strict_inoperable = not target_author_norm or not candidate_author_norm
    if not strict_inoperable:
        return False, False, "author-mismatch"

    if _passes_title_series_fallback(
        target_series_name=target_series_name,
        candidate_title=candidate_title,
        candidate_series_name=candidate_series_name,
    ):
        return True, True, "fallback-title-series-match"

    return False, False, "fallback-title-series-mismatch"


def _candidate_text_value(node: Any, keys: set[str]) -> str:
    if isinstance(node, dict):
        for key, value in node.items():
            if str(key).lower() not in keys:
                continue
            text = _candidate_text_value(value, keys)
            if text:
                return text
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                text = _candidate_text_value(value, keys)
                if text:
                    return text
    elif isinstance(node, list):
        for item in node:
            text = _candidate_text_value(item, keys)
            if text:
                return text
    else:
        if isinstance(node, (str, int, float)):
            text = str(node).strip()
            if text:
                return text
    return ""


def _candidate_field_text(candidate: dict, keys: tuple[str, ...]) -> str:
    return _candidate_text_value(candidate, {str(key).lower() for key in keys})


def _is_clearly_non_book_candidate(candidate: dict) -> bool:
    non_book_tokens = (
        "apparel",
        "clothing",
        "toys",
        "toy",
        "electronics",
        "electronic",
        "games",
        "game",
        "accessory",
        "accessories",
        "gadget",
        "device",
        "luggage",
        "furniture",
        "kitchen",
        "home",
        "garden",
        "sports",
        "automotive",
        "beauty",
        "music",
        "video",
        "software",
        "shoes",
        "jewelry",
        "watch",
        "collectible",
        "merchandise",
        "office product",
        "pet supplies",
    )
    category_keys = (
        "category",
        "categories",
        "product_category",
        "productcategory",
        "product_type",
        "producttype",
        "item_type",
        "itemtype",
        "department",
        "browse_node",
        "browse_nodes",
        "classification",
        "type",
        "product_group",
        "productgroup",
    )
    category_text = _candidate_field_text(candidate, category_keys).lower()
    if not category_text:
        return False
    return any(token in category_text for token in non_book_tokens)


def _passes_micro_filters(candidate: dict, series_name: str, source_type: str) -> bool:
    url = str(candidate.get("url") or "").strip()
    title = str(candidate.get("title") or "").strip()
    domain = _normalize_domain(url)

    if source_type not in {"retail", "publisher", "author"}:
        if any(token in domain for token in METADATA_WAREHOUSE_TOKENS):
            return False
        if not (domain in RETAIL_DOMAINS or domain in PUBLISHER_DOMAINS):
            return False

    if candidate.get("book_number") is None:
        return False

    candidate_series_name = str(candidate.get("series_name") or "").strip()
    if series_name.lower() not in title.lower() and series_name.lower() not in candidate_series_name.lower():
        return False

    return True


def _micro_filter_reasons(candidate: dict, series_name: str, source_type: str) -> list[str]:
    reasons: list[str] = []
    url = str(candidate.get("url") or "").strip()
    title = str(candidate.get("title") or "").strip()
    domain = _normalize_domain(url)
    provider_name = str(candidate.get("provider") or candidate.get("provider_name") or "").strip().lower()

    if provider_name.startswith("amazon"):
        if _is_clearly_non_book_candidate(candidate):
            reasons.append("non-book-category")
        return reasons

    if source_type not in {"retail", "publisher", "author"}:
        if any(token in domain for token in METADATA_WAREHOUSE_TOKENS):
            reasons.append("metadata-domain")
        if not (domain in RETAIL_DOMAINS or domain in PUBLISHER_DOMAINS):
            reasons.append("unsupported-domain")

    if candidate.get("book_number") is None:
        reasons.append("missing-book-number")

    candidate_series_name = str(candidate.get("series_name") or "").strip()
    if series_name.lower() not in title.lower() and series_name.lower() not in candidate_series_name.lower():
        reasons.append("series-not-in-title")

    return reasons


def _classify_candidate_signal(candidate: dict, series_name: str, author_name: str) -> str:
    provider_name = str(candidate.get("provider") or candidate.get("provider_name") or "").strip().lower()
    title = _candidate_field_text(candidate, ("title", "name", "bookTitle", "productTitle"))
    candidate_series = _candidate_field_text(candidate, ("series", "series_name", "seriesName"))
    candidate_author = _candidate_field_text(candidate, ("author", "authors", "contributors", "creator", "writer"))
    asin = _candidate_field_text(candidate, ("asin", "asin_or_id", "productId", "id")).upper()
    isbn = _candidate_field_text(candidate, ("isbn",)).upper()
    publication_date = _candidate_field_text(candidate, ("publication_date", "publicationDate", "publish_date", "release_date", "releaseDate"))
    availability = str(candidate.get("availability") or candidate.get("status_hint") or candidate.get("status") or "").strip().lower()
    snippet = str(candidate.get("snippet") or "").strip().lower()
    url = str(candidate.get("url") or "").strip().lower()

    if provider_name.startswith("amazon"):
        if not asin or not title:
            return "invalid"
        if _is_clearly_non_book_candidate(candidate):
            return "invalid"

        # Some amazon-sourced candidates (e.g. amazon_asin_series, which
        # crawls "related product" ASINs off a seed book's own page) carry
        # the *candidate's own* real series metadata straight from its
        # product page -- not a value defaulted to the target series. When
        # that field is present and clearly names a different series (no
        # meaningful token overlap with the target), it is strong, reliable
        # evidence the book belongs to a different series -- e.g. another
        # series by the same author that Amazon surfaced as "related". This
        # must reject regardless of author/number/asin match, since those
        # signals can't distinguish "same author, different series."
        if candidate_series and series_name and not _series_names_match(series_name, candidate_series):
            return "invalid"

        if any(token in availability for token in ("coming soon", "pre-order", "preorder", "releases on")) or any(
            token in f"{title.lower()} {snippet} {availability}"
            for token in ("coming soon", "pre-order", "preorder", "releases on")
        ):
            return "upcoming"
        if publication_date and any(token in availability for token in ("upcoming",)):
            return "upcoming"
        return "published"

    has_series_signal = bool(series_name and (series_name.lower() in title.lower() or series_name.lower() in candidate_series.lower()))
    if not has_series_signal:
        return "invalid"
    if not _author_matches(author_name, candidate_author):
        return "invalid"
    if any(token in f"{snippet} {url}" for token in METADATA_WAREHOUSE_TOKENS):
        return "invalid"

    if asin or isbn or any(token in availability for token in ("available", "in stock", "published", "released")):
        return "published"

    has_upcoming_signal = any(
        token in f"{title.lower()} {snippet} {availability}"
        for token in ("coming soon", "pre-order", "preorder", "releases on")
    )
    if has_upcoming_signal:
        return "upcoming"

    if title and candidate.get("book_number") is not None and not asin and not isbn:
        return "upcoming"

    return "invalid"


def _to_canonical_validated_candidate(candidate: dict, provider_name: str, series_name: str) -> dict:
    return {
        "title": str(candidate.get("title") or "").strip(),
        "author": str(candidate.get("author") or "").strip(),
        "asin": str(candidate.get("asin") or candidate.get("asin_or_id") or "").strip().upper(),
        "asin_or_id": str(candidate.get("asin") or candidate.get("asin_or_id") or "").strip().upper(),
        "series_name": str(candidate.get("series_name") or candidate.get("series") or series_name).strip() or series_name,
        "series_number": candidate.get("series_number") if candidate.get("series_number") is not None else candidate.get("book_number"),
        "book_number": candidate.get("book_number") if candidate.get("book_number") is not None else candidate.get("series_number"),
        "publication_date": candidate.get("publication_date") or candidate.get("release_date"),
        "status": str(candidate.get("status") or candidate.get("status_hint") or "published").strip().lower(),
        "provider": str(candidate.get("provider") or provider_name).strip() or provider_name,
        "url": str(candidate.get("url") or "").strip(),
    }


def _passes_minimal_scoring(candidate: dict, series_name: str, author_name: str, expected_number: int) -> bool:
    provider_name = str(candidate.get("provider") or candidate.get("provider_name") or "").strip().lower()
    title = str(candidate.get("title") or "")
    candidate_series_name = str(candidate.get("series_name") or "")
    author = str(candidate.get("author") or "")
    number = candidate.get("book_number")

    title_ok = series_name.lower() in title.lower() or series_name.lower() in candidate_series_name.lower()
    author_ok = _author_matches(author_name, author)
    number_ok = number == expected_number

    if provider_name.startswith("amazon"):
        return bool(title) and bool(str(candidate.get("asin") or candidate.get("asin_or_id") or "").strip())

    return title_ok and author_ok and number_ok


def _rank_candidate(candidate: dict, series_name: str, author_name: str, expected_number: int, provider_name: str) -> int:
    title = str(candidate.get("title") or "")
    candidate_series_name = str(candidate.get("series_name") or "")
    author = str(candidate.get("author") or "")
    number = candidate.get("book_number")

    title_score = 1 if (series_name.lower() in title.lower() or series_name.lower() in candidate_series_name.lower()) else 0
    author_score = 1 if _author_matches(author_name, author) else 0
    number_score = 1 if number == expected_number else 0
    provider_score = PROVIDER_PRIORITY.get(provider_name, 0)
    if provider_name.startswith("amazon"):
        return provider_score + (1 if title else 0) + (1 if str(candidate.get("asin") or candidate.get("asin_or_id") or "").strip() else 0)
    return title_score + author_score + number_score + provider_score


def _normalize_title_text(value: str) -> str:
    cleaned = _normalize_whitespace(value)
    cleaned = re.sub(
        r"\((?:audible|audible audio|audio cd|kindle|kindle edition|paperback|hardcover|mass market paperback)[^)]*\)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s*[:\-]\s*(audible|kindle|paperback|hardcover)\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+,\s+book\s+\d+\b", "", cleaned, flags=re.IGNORECASE)
    return _normalize_whitespace(cleaned)


def _extract_book_number_from_text(value: str) -> int | None:
    text = _normalize_whitespace(value)
    patterns = (
        r"\bbook\s*(\d+)\b",
        r"\b#\s*(\d+)\b",
        r"\((?:[^)]*?)book\s*(\d+)(?:[^)]*?)\)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            number = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _parse_date_flexible(value: str | None) -> str | None:
    raw = _normalize_whitespace(str(value or ""))
    if not raw:
        return None

    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            return None

    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _derive_edition_type(value: str) -> str:
    lowered = _normalize_whitespace(value).lower()
    if "audible" in lowered or "audio" in lowered:
        return "audio"
    if "hardcover" in lowered:
        return "hardcover"
    if "paperback" in lowered:
        return "paperback"
    if "kindle" in lowered or "ebook" in lowered:
        return "ebook"
    return "unknown"


def _edition_priority(edition_type: str) -> int:
    priorities = {
        "hardcover": 5,
        "paperback": 4,
        "ebook": 3,
        "audio": 2,
        "unknown": 1,
    }
    return priorities.get(edition_type, 0)


def _build_canonical_amazon_metadata(
    *,
    target_series_name: str,
    metadata_candidate: dict,
) -> tuple[dict | None, list[str]]:
    reasons: list[str] = []

    raw_title = str(metadata_candidate.get("title") or "").strip()
    if not raw_title:
        return None, ["missing-title"]

    title = _normalize_title_text(raw_title)
    if not title:
        return None, ["empty-normalized-title"]

    extracted_series_name = _normalize_series_name(str(metadata_candidate.get("series_name") or ""))
    target_series_normalized = _normalize_series_name(target_series_name)

    if not extracted_series_name and target_series_normalized and target_series_normalized.lower() in raw_title.lower():
        extracted_series_name = target_series_normalized

    raw_author = str(metadata_candidate.get("author") or "").strip()
    author = _normalize_author_name(raw_author)

    raw_book_number = metadata_candidate.get("book_number")
    book_number: int | None = None
    try:
        if raw_book_number is not None and str(raw_book_number).strip() != "":
            parsed = int(float(raw_book_number))
            if parsed > 0:
                book_number = parsed
    except (TypeError, ValueError):
        book_number = None
    if book_number is None:
        book_number = _extract_book_number_from_text(raw_title)

    publish_date = (
        _parse_date_flexible(metadata_candidate.get("publish_date"))
        or _parse_date_flexible(metadata_candidate.get("release_date"))
        or _parse_date_flexible(metadata_candidate.get("publication_date"))
    )
    upcoming_date = _parse_date_flexible(metadata_candidate.get("upcoming_date"))

    availability = str(metadata_candidate.get("availability") or "").strip().lower()
    if availability not in {"available", "upcoming", "unknown"}:
        availability = "unknown"
    if upcoming_date:
        availability = "upcoming"
    elif publish_date and availability == "unknown":
        availability = "available"

    if not extracted_series_name:
        reasons.append("ambiguous-missing-series")
    elif target_series_normalized and not _series_names_match(target_series_normalized, extracted_series_name):
        reasons.append("ambiguous-series-mismatch")

    canonical = {
        "title": title,
        "title_raw": raw_title,
        "author": author,
        "author_raw": raw_author,
        "asin_or_id": str(metadata_candidate.get("asin_or_id") or "").strip().upper(),
        "series_name": extracted_series_name,
        "book_number": book_number,
        "publish_date": publish_date,
        "upcoming_date": upcoming_date,
        "availability": availability,
        "release_date": publish_date,
        "url": str(metadata_candidate.get("url") or "").strip(),
        "title_selector": metadata_candidate.get("title_selector"),
        "edition_type": _derive_edition_type(raw_title),
        "canonical_key": f"{_normalize_match_text(extracted_series_name)}|{book_number if book_number is not None else _normalize_match_text(title)}",
    }
    return canonical, reasons


def _extract_asin_from_value(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    direct = raw.upper()
    if re.fullmatch(r"[A-Z0-9]{10}", direct):
        return direct
    match = re.search(r"/dp/([A-Z0-9]{10})", raw, flags=re.IGNORECASE)
    if match:
        return str(match.group(1) or "").strip().upper()
    match = re.search(r"/gp/product/([A-Z0-9]{10})", raw, flags=re.IGNORECASE)
    if match:
        return str(match.group(1) or "").strip().upper()
    return ""


def _passes_amazon_membership_reconciliation(
    *,
    target_series_name: str,
    target_author_name: str,
    expected_next_number: int,
    title: str,
    extracted_series_name: str,
    extracted_author: str,
    extracted_book_number: int | None,
) -> tuple[bool, list[str]]:
    def _reject(reason: str) -> tuple[bool, list[str]]:
        print(f"ASIN continuation rejected due to strict mismatch: {reason}", flush=True)
        return False, [reason]

    target_author = _normalize_whitespace(target_author_name).casefold()
    observed_author = _normalize_whitespace(extracted_author).casefold()
    if not target_author or not observed_author:
        return _reject("author-missing")
    if target_author != observed_author:
        return _reject("author-mismatch")

    target_series = _normalize_whitespace(target_series_name).casefold()
    observed_series = _normalize_whitespace(extracted_series_name).casefold()
    if not target_series or not observed_series:
        return _reject("series-missing")
    if target_series != observed_series:
        return _reject("series-mismatch")

    if extracted_book_number is None:
        return _reject("book-number-missing")
    if extracted_book_number != expected_next_number:
        return _reject("book-number-mismatch")

    title_text = _normalize_whitespace(title)
    if not title_text:
        return _reject("title-missing")

    expected_title_with_series_number = f"{_normalize_whitespace(target_series_name)} Book {expected_next_number}".casefold()
    if title_text.casefold() != expected_title_with_series_number:
        return _reject("title-mismatch")

    series_metadata_match = re.search(r"\bbook\s*(\d+)\b", title_text, flags=re.IGNORECASE)
    if series_metadata_match:
        try:
            metadata_number = int(series_metadata_match.group(1))
        except (TypeError, ValueError):
            return _reject("series-metadata-invalid")
        if metadata_number != expected_next_number:
            return _reject("series-metadata-mismatch")

    return True, []


def _candidate_summary(candidate: dict, score: int) -> str:
    return (
        f"title={str(candidate.get('title') or '').strip()!r} "
        f"number={candidate.get('book_number')} "
        f"author={str(candidate.get('author') or '').strip()!r} "
        f"url={str(candidate.get('url') or '').strip()!r} "
        f"score={score}"
    )
