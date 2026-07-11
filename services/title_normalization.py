"""Title normalization / re-formatting for the `/series/{id}/normalize_titles`
endpoint. Pure string logic -- no DB access.
"""

import re

import models

TITLE_NORMALIZATION_MODES = {"keep_original", "clean_up", "new_clean_title", "match_other_titles"}

# Matches generic marketing-blurb subtitles that mention "LitRPG" with filler
# descriptor words on either side (e.g. "An Epic Fantasy LitRPG Adventure",
# "A LitRPG Apocalypse", "LitRPG Novel"). Uses a lookahead for the trailing
# "(Series Name Book #)"/end-of-string boundary instead of consuming it, so
# it still fires when that suffix follows -- which is the common case for
# real titles, not the rare one.
_LITRPG_FILLER_SUBTITLE_RE = re.compile(
    r":\s*(?P<article>(?:a|an)\s+)?"
    r"(?:(?:epic|fantasy|adventures?|novels?|sagas?|apocalyptic|apocalypse|progression(?:\s+fantasy)?)\s+)*"
    r"litrpg"
    r"(?:\s+(?:adventures?|novels?|sagas?|apocalyptic|apocalypse|epic|fantasy|progression(?:\s+fantasy)?))*"
    r":?(?=\s*(?:\([^)]*\))?\s*$)",
    flags=re.IGNORECASE,
)


def _strip_litrpg_filler_subtitle(title: str) -> str:
    def _replace(match: re.Match) -> str:
        return ": A LitRPG" if match.group("article") else ": LitRPG"

    return _LITRPG_FILLER_SUBTITLE_RE.sub(_replace, title)


def normalize_title_normalization_mode(value: str | None) -> str | None:
    if value is None:
        return "keep_original"
    cleaned = str(value).strip().lower()
    if cleaned == "off":
        return "keep_original"
    if cleaned == "book_name":
        return "clean_up"
    if cleaned == "book_name_series":
        return "new_clean_title"
    if cleaned == "series_name_book":
        return "match_other_titles"
    if cleaned == "safe":
        return "clean_up"
    if cleaned == "series_consistent":
        return "match_other_titles"
    return cleaned if cleaned in TITLE_NORMALIZATION_MODES else None


def _format_book_number(value: float | int | None) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number.is_integer():
        return str(int(number))
    return str(number)


def _extract_book_number_from_title(title: str) -> float | None:
    match = re.search(r"\bbook\s+(\d+(?:\.\d+)?)\b", title or "", flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return None


def _normalize_title_cleanup_only(raw_title: str) -> str:
    title = str(raw_title or "").strip()
    if not title:
        return ""

    title = re.sub(r"\s+ebook\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+kindle\s+edition\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*\(unabridged\)\s*$", "", title, flags=re.IGNORECASE)
    title = re.sub(r":\s*", ": ", title)
    title = re.sub(r"\(\s+", "(", title)
    title = re.sub(r"\s+\)", ")", title)
    title = re.sub(r"\s{2,}", " ", title)

    title = _strip_litrpg_filler_subtitle(title).strip()

    return re.sub(r"\s{2,}", " ", title).strip()


def _normalize_title_clean_up(raw_title: str, series_name: str | None = None) -> str:
    title = _normalize_title_cleanup_only(raw_title)
    if not title:
        return ""

    title = re.sub(r":\s*:", ": ", title)

    repeated_pattern = re.compile(r"^(.*?):\s*\((book\s+[^)]+)\)\s*:\s*\(([^)]*\bbook\s*\d+[^)]*)\)\s*$", flags=re.IGNORECASE)
    repeated_match = repeated_pattern.match(title)
    if repeated_match:
        stem = str(repeated_match.group(1) or "").strip()
        book_word = str(repeated_match.group(2) or "").strip()
        suffix = str(repeated_match.group(3) or "").strip()
        return re.sub(r"\s{2,}", " ", f"{stem}: {book_word} ({suffix})").strip()

    clean_series_name = str(series_name or "").strip()
    if clean_series_name:
        escaped = re.escape(clean_series_name)
        title = re.sub(rf"^({escaped})\s*:\s*{escaped}\s*", r"\1: ", title, flags=re.IGNORECASE).strip()

    return title


def _normalize_title_book_name_only(raw_title: str) -> str:
    cleaned = _normalize_title_cleanup_only(raw_title)
    if not cleaned:
        return ""

    stripped = re.sub(r"\s*:\s*\([^)]*\)\s*$", "", cleaned, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*:\s*.*$", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s+[-–]\s+.*$", "", stripped, flags=re.IGNORECASE)
    stripped = stripped.strip()
    return stripped or cleaned


def _normalize_title_new_clean(raw_title: str, series_name: str | None = None, book_number: float | int | None = None) -> str:
    cleaned = _normalize_title_clean_up(raw_title, series_name)
    if not cleaned:
        return ""

    inferred_book_number = _extract_book_number_from_title(cleaned)
    resolved_number = book_number if book_number is not None else inferred_book_number

    inferred_series = ""
    inferred_series_match = re.search(r"\(\s*([^()]*?)\s+book\s*\d+(?:\.\d+)?\s*\)\s*$", cleaned, flags=re.IGNORECASE)
    if inferred_series_match:
        inferred_series = str(inferred_series_match.group(1) or "").strip()

    clean_series_name = str(series_name or inferred_series or "").strip()
    if not clean_series_name or resolved_number is None:
        return _normalize_title_book_name_only(cleaned)

    pretty_number = _format_book_number(resolved_number)
    core_title = _normalize_title_book_name_only(cleaned)
    return re.sub(r"\s{2,}", " ", f"{core_title} ({clean_series_name} Book {pretty_number})").strip()


def _infer_series_title_pattern(books: list["models.Book"]) -> str:
    with_suffix = 0
    title_only = 0

    for book in books or []:
        title = str(getattr(book, "title", "") or "").strip()
        if not title:
            continue
        if re.search(r"\([^)]*\bbook\s*\d+(?:\.\d+)?[^)]*\)\s*$", title, flags=re.IGNORECASE):
            with_suffix += 1
        else:
            title_only += 1

    return "with_suffix" if with_suffix >= title_only else "title_only"


def _normalize_title_for_mode(
    raw_title: str,
    mode: str,
    series_name: str | None,
    book_number: float | int | None,
    books: list["models.Book"],
) -> str:
    raw = str(raw_title or "").strip()
    if not raw or mode == "keep_original":
        return raw

    if mode == "clean_up":
        return _normalize_title_clean_up(raw, series_name)

    if mode == "new_clean_title":
        return _normalize_title_new_clean(raw, series_name, book_number)

    clean_title = _normalize_title_clean_up(raw, series_name)
    series_pattern = _infer_series_title_pattern(books)
    if series_pattern == "title_only":
        return _normalize_title_book_name_only(clean_title)
    return _normalize_title_new_clean(clean_title, series_name, book_number)


def _apply_custom_title_pattern(
    pattern: str | None,
    original_title: str,
    series_name: str | None,
    book_number: float | int | None,
    book_subtitle: str | None,
) -> str:
    clean_pattern = str(pattern or "").strip()
    book_title = _normalize_title_book_name_only(original_title)
    if not clean_pattern:
        return book_title

    inferred_subtitle = ""
    cleaned_original = _normalize_title_cleanup_only(original_title)
    without_suffix = re.sub(r"\s*\([^)]*\bbook\s*\d+(?:\.\d+)?[^)]*\)\s*$", "", cleaned_original, flags=re.IGNORECASE).strip()
    if ":" in without_suffix:
        inferred_subtitle = str(without_suffix.split(":", 1)[1] or "").strip()
    elif " - " in without_suffix:
        inferred_subtitle = str(without_suffix.split(" - ", 1)[1] or "").strip()

    resolved_subtitle = str(book_subtitle or inferred_subtitle or "").strip()

    replacements = {
        "{series_name}": str(series_name or "").strip(),
        "{book_number}": _format_book_number(book_number),
        "{book_title}": book_title,
        "{book_subtitle}": resolved_subtitle,
        "{original_title}": str(original_title or "").strip(),
    }

    rendered = clean_pattern
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)

    # Cleans up artifacts left behind when a token (most often
    # {book_subtitle} or {series_name}) substitutes to an empty string --
    # e.g. "Title - ", "Title ()", or "Title ( Book 2)" -- without requiring
    # the author to write conditional template syntax.
    rendered = re.sub(r"\(\s+", "(", rendered)
    rendered = re.sub(r"\(\s*\)", "", rendered)
    rendered = re.sub(r"\s+([,;:.!?])", r"\1", rendered)
    rendered = re.sub(r"\s{2,}", " ", rendered)
    rendered = rendered.strip(" -,:;")

    return rendered or book_title
