from __future__ import annotations

import re
from datetime import date, datetime

from bs4 import BeautifulSoup


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


def _extract_author_from_text(text: str) -> str | None:
    match = re.search(r"\bby\s+([A-Z][A-Za-z\-\'\s\.]{2,80})", text)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip(" .,-")


def _extract_publication_date_from_text(text: str) -> str | None:
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


def _status_hint_from_text(text: str, publication_date_iso: str | None) -> str:
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


def _normalize_amazon_url(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if raw.startswith("/"):
        return f"https://www.amazon.com{raw}"
    return f"https://www.amazon.com/{raw.lstrip('/')}"


def parse_html_to_candidates(normalized_html: str) -> list[dict]:
    candidates: list[dict] = []
    html_text = normalized_html if isinstance(normalized_html, str) else str(normalized_html or "")
    soup = BeautifulSoup(html_text, "html.parser")
    cards = soup.select("div.s-result-item[data-component-type='s-search-result'][data-asin]")

    for card in cards:
        asin = str(card.get("data-asin") or "").strip()
        if not asin:
            continue

        title_span = card.select_one("h2 a span")
        title = title_span.get_text(" ", strip=True) if title_span else ""
        title = re.sub(r"\s+", " ", str(title or "")).strip()
        if not title or re.fullmatch(r"[\d\W_]+", title):
            continue

        title_link = card.select_one("h2 a")
        url = _normalize_amazon_url(title_link.get("href") if title_link else "")

        author = ""
        author_link = card.select_one("a.a-size-base.a-link-normal")
        if author_link:
            author = author_link.get_text(" ", strip=True)
        if not author:
            byline = card.find(string=re.compile(r"\bby\b", re.IGNORECASE))
            if byline:
                author = re.sub(r"^\s*by\s+", "", str(byline).strip(), flags=re.IGNORECASE)

        metadata_parts = [el.get_text(" ", strip=True) for el in card.select("span.a-size-base.a-color-secondary")]
        metadata_text = re.sub(r"\s+", " ", " ".join(part for part in metadata_parts if part).strip())
        card_text = card.get_text(" ", strip=True)

        release_date = _extract_publication_date_from_text(f"{metadata_text} {card_text}".strip())
        status_hint = _status_hint_from_text(f"{metadata_text} {card_text}".strip(), release_date)
        book_number = _extract_book_number(f"{title} {metadata_text}")
        if isinstance(book_number, int) and (book_number <= 0 or book_number > 1000):
            book_number = None

        candidates.append(
            {
                "asin_or_id": asin,
                "asin": asin,
                "title": title,
                "author": author or _extract_author_from_text(card_text),
                "url": url,
                "release_date": release_date,
                "publication_date": release_date,
                "book_number": book_number,
                "snippet": metadata_text,
                "status_hint": status_hint,
            }
        )

    print(f"HTML PARSER: candidates found = {len(candidates)}")
    return candidates
