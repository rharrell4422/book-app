from __future__ import annotations

import re
from datetime import date


def normalize_book_title(raw_title: str, series_name: str | None = None, book_number: float | int | None = None) -> str:
    title = str(raw_title or "").strip()
    if not title:
        return ""

    # Safe cleanup rules: remove common storefront/media suffixes and normalize spacing.
    title = title.replace("\u00a0", " ")
    title = re.sub(r"\s+ebook\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+kindle\s+edition\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*\(unabridged\)\s*$", "", title, flags=re.IGNORECASE)
    title = title.replace(":", ": ")
    title = re.sub(r"\(\s+", "(", title)
    title = re.sub(r"\s+\)", ")", title)
    title = re.sub(r"\s{2,}", " ", title)

    inferred_book_number_match = re.search(r"\bbook\s+(\d+(?:\.\d+)?)\b", title, flags=re.IGNORECASE)
    resolved_book_number = float(book_number) if book_number is not None and str(book_number).strip() else (
        float(inferred_book_number_match.group(1)) if inferred_book_number_match else None
    )
    inferred_series_name_match = re.search(r"\(\s*([^()]*?)\s+book\s*\d+(?:\.\d+)?\s*\)\s*$", title, flags=re.IGNORECASE)
    inferred_series_name = inferred_series_name_match.group(1).strip() if inferred_series_name_match else ""
    clean_series_name = str(series_name or inferred_series_name or "").strip()

    # Remove trailing parenthesized book markers from mixed source formats,
    # including entries that use word-based ordinals (e.g. "Book Nineteen").
    title = title
    title = title.replace("\u00a0", " ")
    title = title.replace(": ", ": ")
    title = re.sub(r"\s*\([^)]*\bbook\b[^)]*\)\s*:\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*\([^)]*\bbook\b[^)]*\)\s*$", "", title, flags=re.IGNORECASE).strip()

    # Canonicalize noisy LitRPG subtitles to a consistent short form.
    title = re.sub(r"\:\s*a\s+litrpg\s+apocalypse\s*\:?\s*$", ": A LitRPG", title, flags=re.IGNORECASE).strip()
    title = re.sub(r"\:\s*a\s+litrpg\s+(?:adventure|novel|saga|epic|fantasy|progression\s+fantasy)\s*\:?\s*$", ": A LitRPG", title, flags=re.IGNORECASE).strip()
    title = re.sub(r"\:\s*litrpg\s+(?:adventure|novel|saga|epic|fantasy|progression\s+fantasy)\s*\:?\s*$", ": LitRPG", title, flags=re.IGNORECASE).strip()

    # Remove embedded series-name tails like ": Series Name, Book N" or ": Series Name".
    if clean_series_name:
        escaped_series_name = re.escape(clean_series_name)
        title = title
        title = re.sub(rf":\s*{escaped_series_name}\s*,?\s*book\s*\d+(?:\.\d+)?\s*$", "", title, flags=re.IGNORECASE)
        title = re.sub(rf":\s*{escaped_series_name}\s*$", "", title, flags=re.IGNORECASE).strip()

    # Replace generic stems like "Book 13" with "Series Name 13" when series context exists.
    if clean_series_name:
        generic_book_stem_match = re.match(r"^book\s+(\d+(?:\.\d+)?)\s*:??\s*$", title, flags=re.IGNORECASE)
        if generic_book_stem_match:
            number_from_stem = float(generic_book_stem_match.group(1))
            normalized_number = number_from_stem if number_from_stem is not None else resolved_book_number
            pretty_number = ""
            if normalized_number is not None:
                pretty_number = str(int(normalized_number)) if float(normalized_number).is_integer() else str(normalized_number)
            title = f"{clean_series_name} {pretty_number}" if pretty_number else clean_series_name

    # Ensure the display standard: "Title: (Series Name Book N)"
    title = re.sub(r"\s{2,}", " ", title).strip()
    is_collection_title = bool(re.search(r"\bbox\s*set\b|\bbooks?\s+\d+\s*[-–]\s*\d+\b", title, flags=re.IGNORECASE))

    if is_collection_title:
        return title

    if not title:
        title = f"Book {resolved_book_number}" if resolved_book_number is not None else "Untitled"
    title = re.sub(r"\s*:\s*$", "", title).strip()
    title = f"{title}:"

    if clean_series_name and resolved_book_number is not None:
        pretty_book_number = str(int(resolved_book_number)) if float(resolved_book_number).is_integer() else str(resolved_book_number)
        title = f"{title} ({clean_series_name} Book {pretty_book_number})"

    return title.strip()


def normalize_book_metadata(metadata: dict, series_name: str | None = None, book_number: float | int | None = None) -> dict:
    normalized = dict(metadata or {})
    title = normalize_book_title(str(normalized.get("title") or ""), series_name=series_name, book_number=book_number)
    if not title and book_number is not None:
        title = f"Book {book_number}"
    normalized["title"] = title or "Untitled"
    if series_name and not normalized.get("series_name"):
        normalized["series_name"] = series_name
    if book_number is not None and normalized.get("book_number") is None:
        normalized["book_number"] = book_number
    return normalized


def parse_publication_date(value: str | None) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        if re.fullmatch(r"\d{4}", raw):
            return date(int(raw), 1, 1)
    except ValueError:
        return None

    return None
