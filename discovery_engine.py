"""Book discovery via official, public book-metadata APIs.

This replaces the old Amazon/Google HTML-scraping pipeline
(checker_core.py / checker_providers.py / checker_rules.py) as the primary
discovery data source. HTML scraping is inherently fragile -- sites detect
and block scrapers and change markup without notice -- while Google Books
and OpenLibrary are free, public JSON APIs intended for exactly this use
case, with no bot-blocking risk.

Strategy: query each API for "<series name> <author>" (a combined
free-text search, not a strict field filter) so the API's own relevance
ranking does the hard work of figuring out which of an author's books
belong to this series -- this is far more precise than pulling an author's
entire bibliography and guessing from title text alone, since many authors
(especially prolific indie/self-published authors) write multiple unrelated
series or standalone works. If that targeted search returns nothing, we
fall back to a plain author-bibliography sweep as a lower-confidence
last resort.

Note: Google Books' unauthenticated (no API key) quota is a small pool
shared globally across all callers without a key, so it may return 429s
even under light use. Set the GOOGLE_BOOKS_API_KEY environment variable
(free from Google Cloud Console) for reliable results -- OpenLibrary has
no such restriction and needs no key.
"""
from __future__ import annotations

import os
import re
from datetime import date

import httpx
from dotenv import load_dotenv

# Loaded here (rather than relying on the entry point having done it first)
# so this module reads the right API key regardless of import order.
load_dotenv()

GOOGLE_BOOKS_ENDPOINT = "https://www.googleapis.com/books/v1/volumes"
OPENLIBRARY_ENDPOINT = "https://openlibrary.org/search.json"
HARDCOVER_ENDPOINT = "https://api.hardcover.app/v1/graphql"
REQUEST_TIMEOUT_SECONDS = 12.0

# Hardcover's own search index tags each hit with its position within a
# series (when it has one), which is a far more reliable source of a book's
# number than trying to parse it out of free-text title formatting -- so a
# result from this provider carries that as an explicit hint rather than
# leaving it to title-text inference.
_HARDCOVER_SEARCH_QUERY = """
query Search($query: String!, $perPage: Int!) {
  search(query: $query, query_type: "Book", per_page: $perPage) {
    results
  }
}
"""

# OpenLibrary (and, less aggressively, Google Books) apply basic
# bot-mitigation heuristics that can reject requests using generic HTTP
# client default user agents. A descriptive User-Agent identifying this
# app, per OpenLibrary's own guidance, avoids spurious 403s.
REQUEST_HEADERS = {"User-Agent": "BookAppSeriesTracker/1.0 (personal series-tracking tool)"}

# Titles that are almost never a new story entry in the series -- they
# bundle/repackage existing books rather than introduce a new one.
NON_NEW_RELEASE_TITLE_MARKERS = (
    "omnibus",
    "box set",
    "boxset",
    "collection",
    "compilation",
    "anthology",
    "complete series",
    "bundle",
    "deluxe edition",
    "special edition",
    "collector's edition",
    "anniversary edition",
    "illustrated edition",
    "annotated edition",
    "extended edition",
    "author's cut",
    # Foreign-language editions -- the structured language field is often
    # missing on these records, so title text is the more reliable signal.
    "french edition",
    "spanish edition",
    "german edition",
    "italian edition",
    "portuguese edition",
    "dutch edition",
)

# Word-boundary patterns for non-new-release detection where a plain
# substring check would risk false positives (e.g. "tome" is also an
# ordinary English word meaning "a large book").
NON_NEW_RELEASE_TITLE_PATTERNS = (re.compile(r"\btome\s*\d*\b"),)


def _log(message: str) -> None:
    print(f"[discovery_engine] {message}", flush=True)


def normalize_text(value: str | None) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def core_title_key(title: str | None) -> str:
    """Titles in this app's library are often stored as
    "Core Title: (Series Name Book N)" while API results are usually just
    the bare "Core Title". Comparing on the text before the first ':' or
    '(' gives a stable identity key across both shapes -- *except* for
    series that name every entry "<Series Name> (Volume N): <subtitle>" or
    similar, where the volume number itself lives inside that first
    "(...)"/":" segment. Truncating there would make every volume collapse
    to the exact same key (e.g. book 1 and book 4 both becoming just
    "1 lifesteal"), making it impossible to ever recognize a new volume as
    distinct from an owned one. To avoid that, fold any book/volume number
    found anywhere in the title into the key.
    """
    raw = str(title or "")
    core = re.split(r"[:(]", raw, maxsplit=1)[0]
    normalized_core = normalize_text(core)
    number = infer_number_from_title(raw)
    if number:
        return f"{normalized_core} {number}"
    return normalized_core


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}


def infer_number_from_title(title: str | None, series_name: str | None = None) -> int | None:
    # Checked against the raw (non-normalized) title first: normalize_text
    # strips punctuation like "#", so a "#7"-style pattern could never
    # actually match once run against the already-normalized text below.
    hash_match = re.search(r"#\s*(\d+)\b", str(title or ""))
    if hash_match:
        try:
            value = int(hash_match.group(1))
        except ValueError:
            value = 0
        if value > 0:
            return value

    cleaned = normalize_text(title)
    if not cleaned:
        return None
    patterns = (
        r"\bbook\s*(\d+)\b",
        r"\bvolume\s*(\d+)\b",
        r"\bvol\.?\s*(\d+)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if not match:
            continue
        try:
            value = int(match.group(1))
        except ValueError:
            continue
        if value > 0:
            return value

    # Some listings spell the number out ("Book One", "Volume Two") instead
    # of using a digit -- same intent, different formatting.
    word_pattern = r"\b(?:book|volume|vol\.?)\s+(" + "|".join(_WORD_NUMBERS) + r")\b"
    word_match = re.search(word_pattern, cleaned)
    if word_match:
        value = _WORD_NUMBERS.get(word_match.group(1))
        if value:
            return value

    # Many rapid-release indie/LitRPG series just number titles as
    # "<Series Name> <N>" with no "book"/"vol"/"#" keyword at all (e.g.
    # "All the Skills 5"). If the title starts with the series name
    # followed directly by a bare number, treat that as the entry number.
    series_norm = normalize_text(series_name)
    if series_norm and cleaned.startswith(series_norm):
        remainder = cleaned[len(series_norm):].strip()
        match = re.match(r"(\d+)\b", remainder)
        if match:
            try:
                value = int(match.group(1))
            except ValueError:
                value = 0
            if value > 0:
                return value
    return None


_BUNDLE_TITLE_PATTERN = re.compile(r"\b\d+\s+books?\b")


def looks_like_non_new_release(title: str) -> bool:
    title_norm = normalize_text(title)
    if any(marker in title_norm for marker in NON_NEW_RELEASE_TITLE_MARKERS):
        return True
    if any(pattern.search(title_norm) for pattern in NON_NEW_RELEASE_TITLE_PATTERNS):
        return True
    return bool(_BUNDLE_TITLE_PATTERN.search(title_norm))


def is_english_or_unknown(language: str | None) -> bool:
    """This app's library is in English -- exclude editions we can
    positively identify as a different language (translations), but don't
    require the language field to be present since many entries lack one.
    """
    code = str(language or "").strip().lower()
    if not code:
        return True
    return code in {"en", "eng", "en-us", "en-gb"}


def parse_flexible_date(value: str | None) -> date | None:
    """Best-effort parse of Google Books / OpenLibrary date strings, which
    can be full dates, year-month, or just a year.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        try:
            return date.fromisoformat(f"{raw}-01")
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}", raw):
        try:
            return date(int(raw), 1, 1)
        except ValueError:
            return None
    return None


def split_author_names(value: str | None) -> list[str]:
    """Series in this app's library sometimes store multiple co-authors in
    one string (e.g. "J.N Chaney; Terry Maggert"). Split those apart so
    each name can be matched/queried individually -- APIs match one author
    name at a time and rarely list co-authors concatenated like that.
    """
    if not value:
        return []
    parts = re.split(r"\s*(?:;|,|&|\band\b)\s*", str(value), flags=re.IGNORECASE)
    return [p.strip() for p in parts if p and p.strip()]


def primary_author_name(value: str | None) -> str:
    names = split_author_names(value)
    return names[0] if names else str(value or "").strip()


def _author_matches(candidate_authors: list[str], target_author: str) -> bool:
    target_names = split_author_names(target_author) or [target_author]
    for target_name in target_names:
        target_tokens = [token for token in normalize_text(target_name).split() if len(token) > 1]
        if not target_tokens:
            continue
        for candidate in candidate_authors:
            candidate_norm = normalize_text(candidate)
            if all(token in candidate_norm for token in target_tokens):
                return True
    return False


def _fetch_google_books(query: str, max_results: int = 40) -> list[dict]:
    params: dict = {"q": query, "maxResults": max_results}
    api_key = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()
    if api_key:
        params["key"] = api_key
    response = httpx.get(GOOGLE_BOOKS_ENDPOINT, params=params, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    items = (response.json() or {}).get("items") or []
    results: list[dict] = []
    for item in items:
        info = item.get("volumeInfo") or {}
        title = str(info.get("title") or "").strip()
        if not title:
            continue
        subtitle = str(info.get("subtitle") or "").strip()
        full_title = f"{title}: {subtitle}" if subtitle else title
        identifiers = info.get("industryIdentifiers") or []
        isbn13 = next((i.get("identifier") for i in identifiers if i.get("type") == "ISBN_13"), None)
        results.append(
            {
                "source": "google_books",
                "source_id": item.get("id"),
                "title": full_title,
                "authors": info.get("authors") or [],
                "published_date": str(info.get("publishedDate") or "").strip(),
                "description": info.get("description"),
                "isbn13": str(isbn13 or "").strip() or None,
                "source_url": str(info.get("infoLink") or "").strip() or None,
                "language": str(info.get("language") or "").strip(),
            }
        )
    return results


def _fetch_openlibrary(query: str, max_results: int = 40) -> list[dict]:
    params = {"q": query, "limit": max_results}
    response = httpx.get(OPENLIBRARY_ENDPOINT, params=params, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    docs = (response.json() or {}).get("docs") or []
    results: list[dict] = []
    for doc in docs:
        title = str(doc.get("title") or "").strip()
        if not title:
            continue
        year = doc.get("first_publish_year")
        isbn_list = doc.get("isbn") or []
        languages = doc.get("language") or []
        results.append(
            {
                "source": "openlibrary",
                "source_id": doc.get("key"),
                "title": title,
                "authors": doc.get("author_name") or [],
                "published_date": str(year) if year else "",
                "description": None,
                "isbn13": next((i for i in isbn_list if len(str(i)) == 13), None),
                "source_url": f"https://openlibrary.org{doc.get('key')}" if doc.get("key") else None,
                "language": str(languages[0]) if languages else "",
            }
        )
    return results


def _fetch_hardcover(query: str, max_results: int = 25) -> list[dict]:
    api_key = os.environ.get("HARDCOVER_API_KEY", "").strip()
    if not api_key:
        return []

    headers = {**REQUEST_HEADERS, "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"query": _HARDCOVER_SEARCH_QUERY, "variables": {"query": query, "perPage": max_results}}
    response = httpx.post(HARDCOVER_ENDPOINT, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    body = response.json() or {}
    if body.get("errors"):
        raise RuntimeError(str(body["errors"])[:300])

    hits = (((body.get("data") or {}).get("search") or {}).get("results") or {}).get("hits") or []
    results: list[dict] = []
    for hit in hits:
        doc = hit.get("document") or {}
        title = str(doc.get("title") or "").strip()
        if not title:
            continue

        isbns = doc.get("isbns") or []
        isbn13 = next((i for i in isbns if len(str(i)) == 13), None)

        featured_series = doc.get("featured_series") or {}
        series_position = None
        raw_position = featured_series.get("position")
        if raw_position is not None:
            try:
                series_position = int(round(float(raw_position)))
            except (TypeError, ValueError):
                series_position = None

        results.append(
            {
                "source": "hardcover",
                "source_id": doc.get("id"),
                "title": title,
                "authors": doc.get("author_names") or [],
                "published_date": str(doc.get("release_date") or "").strip(),
                "description": doc.get("description"),
                "isbn13": str(isbn13 or "").strip() or None,
                "source_url": f"https://hardcover.app/books/{doc.get('slug')}" if doc.get("slug") else None,
                "language": "",
                "series_number_hint": series_position,
                "upcoming_hint": bool(featured_series.get("unreleased")),
            }
        )
    return results


def _filter_and_merge(raw_results: list[dict], author: str, exclude_title_keys: set[str], confidence: str) -> list[dict]:
    merged: list[dict] = []
    seen_keys: set[str] = set()
    for raw in raw_results:
        if not _author_matches(raw.get("authors") or [], author):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        if looks_like_non_new_release(title):
            continue
        if not is_english_or_unknown(raw.get("language")):
            continue

        title_key = core_title_key(title)
        if title_key and title_key in exclude_title_keys:
            continue

        isbn13 = str(raw.get("isbn13") or "").strip()
        dedupe_key = isbn13 or title_key or normalize_text(title)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        merged.append({**raw, "confidence": confidence})
    return merged


def discover_candidates_for_series(
    series_name: str,
    author: str,
    *,
    exclude_title_keys: set[str] | None = None,
    allow_author_fallback: bool = True,
    progress_callback=None,
) -> dict:
    """Find candidate books for a specific series by a specific author.

    Primary pass: a targeted "<series name> <author>" search on both APIs,
    which leans on each API's own relevance ranking to associate books with
    the series (via title/description text), rather than trying to infer
    series membership purely from title patterns.

    Fallback pass (only if the primary pass finds nothing, and only when
    the caller says it's safe -- i.e. this author has no other tracked
    series in the library): a plain author-bibliography sweep, so a brand
    new release whose indexed text doesn't yet mention the series name can
    still surface.
    """
    exclude_title_keys = exclude_title_keys or set()
    series_name = str(series_name or "").strip()
    author = str(author or "").strip()
    provider_failures: list[dict] = []

    if not author:
        return {"candidates": [], "provider_failures": [], "all_providers_failed": False, "used_author_fallback": False}

    if progress_callback:
        progress_callback({"current_pass": f"Searching for {series_name or author}"})

    # Query APIs with just the first co-author's name (structured author
    # fields rarely contain multiple concatenated names), but keep
    # matching/filtering against the full original string so legitimate
    # co-authored results still pass.
    query_author = primary_author_name(author)
    targeted_query_text = f"{series_name} {query_author}".strip()
    any_provider_succeeded = False

    google_raw: list[dict] = []
    try:
        google_query = f'"{series_name}" inauthor:"{query_author}"' if series_name else f'inauthor:"{query_author}"'
        google_raw = _fetch_google_books(google_query)
        any_provider_succeeded = True
    except Exception as exc:
        provider_failures.append({"provider": "google_books", "error": str(exc)})

    openlibrary_raw: list[dict] = []
    try:
        openlibrary_raw = _fetch_openlibrary(targeted_query_text)
        any_provider_succeeded = True
    except Exception as exc:
        provider_failures.append({"provider": "openlibrary", "error": str(exc)})

    hardcover_raw: list[dict] = []
    try:
        hardcover_raw = _fetch_hardcover(targeted_query_text)
        if hardcover_raw or os.environ.get("HARDCOVER_API_KEY", "").strip():
            any_provider_succeeded = True
    except Exception as exc:
        provider_failures.append({"provider": "hardcover", "error": str(exc)})

    # Hardcover listed first: when multiple sources return the same book,
    # dedup keeps whichever copy appears first, and Hardcover's explicit
    # series-position/release-status fields are more trustworthy than
    # Google Books/OpenLibrary free-text for indie/self-published LitRPG,
    # which both of those APIs tend to index/cover poorly.
    combined = _filter_and_merge(
        [*hardcover_raw, *google_raw, *openlibrary_raw], author, exclude_title_keys, confidence="targeted"
    )

    used_author_fallback = False
    if not combined and allow_author_fallback:
        used_author_fallback = True
        if progress_callback:
            progress_callback({"current_pass": f"Broadening search to all books by {author}"})

        google_fallback: list[dict] = []
        try:
            google_fallback = _fetch_google_books(f'inauthor:"{query_author}"')
            any_provider_succeeded = True
        except Exception as exc:
            provider_failures.append({"provider": "google_books_fallback", "error": str(exc)})

        openlibrary_fallback: list[dict] = []
        try:
            openlibrary_fallback = _fetch_openlibrary(f'author:"{query_author}"')
            any_provider_succeeded = True
        except Exception as exc:
            provider_failures.append({"provider": "openlibrary_fallback", "error": str(exc)})

        hardcover_fallback: list[dict] = []
        try:
            hardcover_fallback = _fetch_hardcover(query_author)
            if hardcover_fallback or os.environ.get("HARDCOVER_API_KEY", "").strip():
                any_provider_succeeded = True
        except Exception as exc:
            provider_failures.append({"provider": "hardcover_fallback", "error": str(exc)})

        combined = _filter_and_merge(
            [*hardcover_fallback, *google_fallback, *openlibrary_fallback],
            author,
            exclude_title_keys,
            confidence="author_fallback",
        )

    # "All providers failed" should mean we got no usable data at all (every
    # call raised), not just that filtering left zero new candidates -- a
    # provider that successfully returned data (even if it was all already
    # owned, or simply had no coverage) is a normal, successful outcome.
    all_providers_failed = bool(provider_failures) and not any_provider_succeeded

    if progress_callback:
        progress_callback({"current_pass": "Done", "total": 1, "completed": 1})

    return {
        "candidates": combined,
        "provider_failures": provider_failures,
        "all_providers_failed": all_providers_failed,
        "used_author_fallback": used_author_fallback,
    }
