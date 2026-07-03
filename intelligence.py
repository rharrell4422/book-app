import os
import re
import traceback
from urllib.parse import quote_plus
from urllib.parse import urlparse
import logging

import httpx
from datetime import date

try:
    from models import Series, Book
except Exception as e:
    print("\n\n🔥 INTELLIGENCE MODULE FAILED DURING IMPORT 🔥")
    traceback.print_exc()
    raise e


logger = logging.getLogger(__name__)

def compute_series_intelligence_for_series(db, series_id: int):
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        return None

    books = db.query(Book).filter(Book.series_id == series_id).all()

    if not books:
        return {
            "series_id": series_id,
            "total_books": 0,
            "read_count": 0,
            "unread_count": 0,
            "missing_orders": [],
            "next_unread_book_id": None,
            "next_upcoming_book_id": None,
            "next_unread_book_number": None,
            "next_upcoming_book_number": None,
            "is_series_finished": False,
        }

    # Sort by series_order
    books.sort(key=lambda b: b.series_order or 0)

    # Determine expected series length from explicit total or known order numbers.
    explicit_total = series.total_books if series.total_books and series.total_books > 0 else None
    actual_orders = set(int(b.series_order) for b in books if b.series_order is not None)
    actual_book_numbers = set(int(b.book_number) for b in books if b.book_number is not None)
    all_known_orders = actual_orders.union(actual_book_numbers)

    inferred_max_order = max(all_known_orders) if all_known_orders else None
    if explicit_total is not None and inferred_max_order is not None:
        total_books = max(explicit_total, inferred_max_order)
    else:
        total_books = explicit_total or inferred_max_order or len(books)

    read_books = [b for b in books if b.is_read]
    unread_books = [b for b in books if not b.is_read]
    has_no_series_finished_flag = any(b.is_series_finished is False for b in books)

    read_count = len(read_books)
    unread_count = len(unread_books)

    # Missing orders should use the expected total count, not the current book count.
    expected_orders = set(range(1, int(total_books) + 1)) if total_books else set()
    missing_orders = [str(order) for order in sorted(expected_orders - all_known_orders)]

    # Next unread
    next_unread = unread_books[0] if unread_books else None

    # Upcoming = explicit upcoming status/flags OR future release/publication date.
    today = date.today()
    upcoming_statuses = {"upcoming", "tbr", "to be read"}

    def is_book_upcoming(book):
        status = str(book.read_status or "").strip().lower()
        has_status_upcoming = status in upcoming_statuses
        has_upcoming_flag = bool(book.is_upcoming_auto or book.is_upcoming_final)
        dated_release = book.release_date or book.publication_date
        has_future_date = bool(dated_release and dated_release > today)
        return has_status_upcoming or has_upcoming_flag or has_future_date

    upcoming_books = [b for b in books if is_book_upcoming(b)]

    def upcoming_sort_key(book):
        dated_release = book.release_date or book.publication_date
        number = book.book_number if book.book_number is not None else book.series_order
        return (dated_release or date.max, number if number is not None else float("inf"), book.id)

    upcoming_books.sort(key=upcoming_sort_key)
    next_upcoming = upcoming_books[0] if upcoming_books else None
    has_upcoming = len(upcoming_books) > 0
    has_unread = len(unread_books) > 0
    is_caught_up = (not has_unread) and (not has_upcoming)

    def resolve_book_number(book):
        if not book:
            return None
        if book.book_number is not None:
            return float(book.book_number)
        if book.series_order is not None:
            return float(book.series_order)
        return None

    return {
        "series_id": series_id,
        "total_books": total_books,
        "read_count": read_count,
        "unread_count": unread_count,
        "missing_orders": missing_orders,
        "next_unread_book_id": next_unread.id if next_unread else None,
        "next_upcoming_book_id": next_upcoming.id if next_upcoming else None,
        "next_unread_book_number": resolve_book_number(next_unread),
        "next_upcoming_book_number": resolve_book_number(next_upcoming),
        "is_series_finished": (not has_upcoming) and (not has_no_series_finished_flag),
        "has_unread_books": has_unread,
        "has_upcoming_books": has_upcoming,
        "is_caught_up": is_caught_up,
    }


def _series_state_from_books(series, books: list[Book]) -> dict:
    today = date.today()
    upcoming_statuses = {"upcoming", "tbr", "to be read"}

    def is_book_upcoming(book):
        status = str(book.read_status or "").strip().lower()
        has_status_upcoming = status in upcoming_statuses
        has_upcoming_flag = bool(book.is_upcoming_auto or book.is_upcoming_final)
        dated_release = book.release_date or book.publication_date
        has_future_date = bool(dated_release and dated_release > today)
        return has_status_upcoming or has_upcoming_flag or has_future_date

    active_books = [book for book in books if str(book.record_status or "active") != "deleted"]
    has_unread_books = any(not bool(book.is_read) for book in active_books)
    has_upcoming_books = any(is_book_upcoming(book) for book in active_books)
    is_caught_up = (not has_unread_books) and (not has_upcoming_books)

    return {
        "has_new_books": bool(series.has_new_books),
        "has_unread_books": has_unread_books,
        "has_upcoming_books": has_upcoming_books,
        "is_caught_up": is_caught_up,
    }


def _scan_found_new_books(scan_result: dict | None) -> bool:
    if not scan_result:
        return False

    if bool(scan_result.get("added_count")):
        return True
    if scan_result.get("added_books"):
        return True
    if scan_result.get("canonical_missing_entries"):
        return True
    if scan_result.get("canonical_upcoming_entries"):
        return True
    if scan_result.get("found"):
        return True
    return False


def recalculate_series_state_for_series(db, series_id: int, *, scan_result: dict | None = None) -> dict | None:
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        return None

    books = db.query(Book).filter(Book.series_id == series_id).all()
    state = _series_state_from_books(series, books)
    found_new_books = _scan_found_new_books(scan_result)

    if state["is_caught_up"]:
        state["has_new_books"] = False
        state["has_unread_books"] = False
        state["has_upcoming_books"] = False
    else:
        state["has_new_books"] = bool(series.has_new_books) or found_new_books

    series.has_new_books = state["has_new_books"]
    series.has_unread_books = state["has_unread_books"]
    series.has_upcoming_books = state["has_upcoming_books"]
    series.is_caught_up = state["is_caught_up"]
    db.commit()
    db.refresh(series)

    return {
        "series_id": series_id,
        "series_state": state,
    }


def recount_series_aggregates_for_series(db, series_id: int) -> dict | None:
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        return None

    books = db.query(Book).filter(Book.series_id == series_id).all()
    active_books = [book for book in books if str(book.record_status or "active") != "deleted"]
    read_count = sum(1 for book in active_books if bool(book.is_read))
    unread_count = sum(1 for book in active_books if not bool(book.is_read))

    series.total_books = len(active_books)
    db.commit()
    db.refresh(series)

    return {
        "series_id": series_id,
        "total_books": series.total_books,
        "read_count": read_count,
        "unread_count": unread_count,
    }


def _book_number_value(book: Book) -> float | None:
    value = book.book_number if book.book_number is not None else book.series_order
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_rejected_or_unverified_source(book: Book) -> bool:
    source_blob = " ".join(
        [
            str(book.import_source or ""),
            str(book.import_errors or ""),
            str(getattr(book, "notes", "") or ""),
        ]
    ).lower()
    return ("rejected" in source_blob) or ("unverified" in source_blob)


def purge_invalid_books_for_series(
    db,
    series_id: int,
    *,
    known_series_max: int | None = None,
    series_complete: bool | None = None,
    filtered_book_numbers: set[float] | None = None,
) -> dict | None:
    series = db.query(Series).filter(Series.id == series_id).first()
    if not series:
        return None

    max_known = known_series_max
    if max_known is None:
        intel = compute_series_intelligence_for_series(db, series_id) or {}
        max_known = intel.get("total_books")

    is_complete = bool(series_complete) if series_complete is not None else (
        bool(series.is_finished) or str(series.series_status or "").strip().lower() in {"completed", "finished"}
    )

    normalized_filtered: set[float] | None = None
    if filtered_book_numbers is not None:
        normalized_filtered = set()
        for value in filtered_book_numbers:
            try:
                normalized_filtered.add(float(value))
            except (TypeError, ValueError):
                continue

    filtered_output_authoritative = False
    if normalized_filtered is not None and max_known is not None:
        try:
            filtered_output_authoritative = len(normalized_filtered) >= int(max_known)
        except (TypeError, ValueError):
            filtered_output_authoritative = False

    books = db.query(Book).filter(Book.series_id == series_id).all()
    deleted_entries: list[dict] = []

    for book in books:
        reasons: list[str] = []
        book_number = _book_number_value(book)

        if max_known is not None and book_number is not None and book_number > float(max_known):
            reasons.append("book_number_above_known_series_max")
            if is_complete:
                reasons.append("completed_series_above_known_series_max")

        if _is_rejected_or_unverified_source(book):
            reasons.append("book_source_rejected_or_unverified")

        if normalized_filtered is not None and filtered_output_authoritative:
            if book_number is None or book_number not in normalized_filtered:
                reasons.append("not_in_filtered_discovery_output")

        if not reasons:
            continue

        deleted_entries.append(
            {
                "book_id": book.id,
                "title": book.title,
                "book_number": book_number,
                "reasons": reasons,
            }
        )
        logger.warning(
            "[MAINTENANCE] Purging invalid book series_id=%s book_id=%s title=%s reasons=%s",
            series_id,
            book.id,
            book.title,
            ",".join(reasons),
        )
        db.delete(book)

    if deleted_entries:
        db.commit()

    aggregates = recount_series_aggregates_for_series(db, series_id)
    return {
        "series_id": series_id,
        "deleted_count": len(deleted_entries),
        "deleted_entries": deleted_entries,
        "aggregates": aggregates,
    }


def purge_orphaned_books(db) -> dict:
    books = db.query(Book).filter(Book.series_id.is_not(None)).all()
    existing_series_ids = {int(row[0]) for row in db.query(Series.id).all()}

    deleted_entries: list[dict] = []
    for book in books:
        if int(book.series_id) in existing_series_ids:
            continue
        deleted_entries.append(
            {
                "book_id": book.id,
                "title": book.title,
                "series_id": book.series_id,
            }
        )
        logger.warning(
            "[MAINTENANCE] Purging orphaned book book_id=%s series_id=%s title=%s",
            book.id,
            book.series_id,
            book.title,
        )
        db.delete(book)

    if deleted_entries:
        db.commit()

    return {
        "deleted_count": len(deleted_entries),
        "deleted_entries": deleted_entries,
    }


def run_nightly_book_maintenance(db) -> dict:
    series_rows = db.query(Series).all()
    series_cleanup_results: list[dict] = []

    for series in series_rows:
        result = purge_invalid_books_for_series(
            db,
            series.id,
            known_series_max=series.total_books,
            series_complete=bool(series.is_finished) or str(series.series_status or "").strip().lower() in {"completed", "finished"},
            filtered_book_numbers=None,
        )
        if result:
            series_cleanup_results.append(result)

    orphaned_result = purge_orphaned_books(db)
    return {
        "series_processed": len(series_rows),
        "series_cleanup_results": series_cleanup_results,
        "orphaned_cleanup": orphaned_result,
    }


def generate_google_search_url(query: str) -> str:
    if not query:
        return ""
    return f"https://www.google.com/search?q={quote_plus(query)}"


def generate_goodreads_search_url(title: str, author: str | None = None) -> str:
    query = title or ""
    if author:
        query = f"{query} {author}".strip()
    return f"https://www.goodreads.com/search?q={quote_plus(query)}"


def generate_openlibrary_search_url(title: str, author: str | None = None) -> str:
    query = title or ""
    if author:
        query = f"{query} {author}".strip()
    return f"https://openlibrary.org/search?q={quote_plus(query)}"


def generate_google_books_search_url(title: str, author: str | None = None) -> str:
    query = title or ""
    if author:
        query = f"{query} {author}".strip()
    return f"https://www.googleapis.com/books/v1/volumes?q={quote_plus(query)}&maxResults=5"


def fetch_page_text(url: str, timeout: float = 10.0) -> str:
    if not url:
        return ""
    headers = {"User-Agent": "BookApp/1.0 (+https://example.com)"}
    with httpx.Client(timeout=timeout, headers=headers, trust_env=False) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def get_google_books_api_key() -> str | None:
    return os.getenv("GOOGLE_BOOKS_API_KEY")


def get_serpapi_api_key() -> str | None:
    return os.getenv("SERPAPI_API_KEY")


def search_google_books(query: str, author: str | None = None, max_results: int = 5) -> list[dict]:
    if not query:
        return []

    query = query.strip()
    safe_author = author.replace('"', '') if author else None
    if safe_author and "inauthor:" not in query:
        query = f'{query}+inauthor:"{safe_author}"'

    params = {
        "q": query,
        "maxResults": max_results,
        "printType": "books",
        "orderBy": "relevance",
    }
    api_key = get_google_books_api_key()
    if api_key:
        params["key"] = api_key

    headers = {"User-Agent": "BookApp/1.0 (+https://example.com)"}

    def fetch_items(search_query: str) -> list[dict]:
        request_params = dict(params)
        request_params["q"] = search_query

        try:
            with httpx.Client(timeout=8.0, headers=headers, trust_env=False) as client:
                response = client.get("https://www.googleapis.com/books/v1/volumes", params=request_params)
                if response.status_code == 429:
                    return []
                response.raise_for_status()
                data = response.json()
                return data.get("items", [])
        except httpx.RequestError:
            return []
        except httpx.HTTPStatusError:
            return []

    items = fetch_items(query)
    if not items and safe_author and "+inauthor:" in query:
        fallback_query = query.split("+inauthor:")[0].strip()
        items = fetch_items(fallback_query)

    if not items and safe_author:
        base_query = query.split("+inauthor:")[0].strip()
        items = fetch_items(f'{base_query} "{safe_author}"')

    results = []
    for item in items:
        info = item.get("volumeInfo", {})
        if not info:
            continue
        results.append({
            "title": info.get("title"),
            "author": ", ".join(info.get("authors", [])) if info.get("authors") else author,
            "year": info.get("publishedDate"),
            "description": info.get("description"),
            "source_url": info.get("infoLink") or item.get("selfLink"),
            "source": "google_books",
        })
    return results


def search_openlibrary(query: str, author: str | None = None, max_results: int = 5) -> list[dict]:
    if not query:
        return []

    query = query.strip()
    if author and "author:" not in query:
        query = f'{query} author:"{author}"'

    params = {"q": query, "limit": max_results}
    headers = {"User-Agent": "BookApp/1.0 (+https://example.com)"}
    try:
        with httpx.Client(timeout=8.0, headers=headers, trust_env=False) as client:
            response = client.get("https://openlibrary.org/search.json", params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.RequestError:
        return []
    except httpx.HTTPStatusError:
        return []

    docs = data.get("docs", [])
    results = []
    for doc in docs:
        key = doc.get("key")
        if not key:
            continue
        results.append({
            "title": doc.get("title"),
            "author": ", ".join(doc.get("author_name", [])) if doc.get("author_name") else author,
            "year": doc.get("first_publish_year"),
            "description": None,
            "source_url": f"https://openlibrary.org{key}",
            "series_name": doc.get("series_name"),
            "series_position": doc.get("series_position"),
            "source": "openlibrary",
        })
    return results


def search_serpapi_web(query: str, author: str | None = None, max_results: int = 10) -> list[dict]:
    api_key = get_serpapi_api_key()
    if not api_key or not query:
        return []

    composed_query = query.strip()
    if author and author.strip() and author.lower() not in composed_query.lower():
        composed_query = f"{composed_query} {author.strip()}"

    inferred_series_name = re.split(r"\bbook\s+\d+\b", composed_query, flags=re.IGNORECASE)[0].strip()
    if author and inferred_series_name.lower().endswith(author.lower()):
        inferred_series_name = inferred_series_name[: -len(author)].strip()

    params = {
        "engine": "google",
        "q": composed_query,
        "api_key": api_key,
        "num": max_results,
    }
    headers = {"User-Agent": "BookApp/1.0 (+https://example.com)"}

    try:
        with httpx.Client(timeout=10.0, headers=headers, trust_env=False) as client:
            response = client.get("https://serpapi.com/search.json", params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.RequestError:
        return []
    except httpx.HTTPStatusError:
        return []

    book_number_match = re.search(r"\bbook\s+(\d+)\b", composed_query, flags=re.IGNORECASE)
    requested_book_number = int(book_number_match.group(1)) if book_number_match else None

    def extract_title_from_snippet(snippet_text: str | None, book_number: int | None) -> str | None:
        if not snippet_text or book_number is None:
            return None
        pattern = rf"\bbook\s*{book_number}\s*[:\-]\s*([^.;|\n]+)"
        match = re.search(pattern, snippet_text, flags=re.IGNORECASE)
        if not match:
            return None
        candidate = re.sub(r"\s+", " ", match.group(1)).strip(" -:;,.\t")
        # Some snippets list multiple entries in one line (Book 2 ... Book 3 ...).
        candidate = re.split(r"\s*[·|]\s*", candidate, maxsplit=1)[0].strip()
        candidate = re.split(
            rf"\b(?:book|volume)\s+(?!{book_number}\b)\d+\b",
            candidate,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" -:;,.\t")
        return candidate or None

    results: list[dict] = []
    for item in data.get("organic_results", [])[:max_results]:
        title = item.get("title")
        if not title:
            continue

        source_url = item.get("link")
        snippet = item.get("snippet")
        publication_info = item.get("publication_info") or {}
        summary_info = item.get("rich_snippet") or {}

        year = None
        if isinstance(publication_info, dict):
            year = publication_info.get("summary")
        if not year and isinstance(summary_info, dict):
            top = summary_info.get("top") or {}
            year = top.get("detected_extensions", {}).get("year") if isinstance(top, dict) else None

        extracted_title = extract_title_from_snippet(snippet, requested_book_number)
        normalized_title = extracted_title or title
        series_position = requested_book_number if extracted_title else None

        results.append({
            "title": normalized_title,
            "author": author,
            "year": year,
            "description": snippet,
            "source_url": source_url,
            "series_name": inferred_series_name or None,
            "series_position": series_position,
            "source": "serpapi",
        })

    return results


def _extract_first_match(patterns: list[str], text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip()
            if value:
                return value
    return None


def _extract_publication_date(html: str, snippet: str | None = None) -> str | None:
    patterns = [
        r"(?:published|publication\s+date|release\s+date)\s*[:\-]?\s*([A-Za-z]+\s+\d{1,2},\s*\d{4})",
        r"(?:published|publication\s+date|release\s+date)\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})",
        r"(?:published|publication\s+date|release\s+date)\s*[:\-]?\s*(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})",
    ]
    direct = _extract_first_match(patterns, html)
    if direct:
        return direct
    if snippet:
        return _extract_first_match(patterns, snippet)
    return None


def _extract_cover_image(html: str) -> str | None:
    og = _extract_first_match([r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']'], html)
    if og:
        return og
    twitter = _extract_first_match([r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']'], html)
    if twitter:
        return twitter
    img = _extract_first_match([r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>'], html)
    return img


def _classify_web_source(url: str | None) -> str:
    host = (urlparse(url or "").netloc or "").lower()
    if "amazon." in host:
        return "Amazon"
    if "fantasticfiction" in host:
        return "FantasticFiction"
    return "OtherWebSources"


def _matches_title_author(title_candidates: list[str], author: str | None, html: str, snippet: str | None) -> bool:
    source_text = f"{html} {snippet or ''}".lower()
    author_ok = True
    if author:
        author_parts = [part for part in re.split(r"\s+", author.lower()) if len(part) > 1]
        author_ok = bool(author_parts) and all(part in source_text for part in author_parts)
    title_ok = False
    for title in title_candidates:
        normalized = re.sub(r"[^a-z0-9\s]", " ", title.lower())
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if not normalized:
            continue
        if normalized in source_text:
            title_ok = True
            break
        title_tokens = [token for token in normalized.split() if len(token) > 2]
        if title_tokens and sum(1 for token in title_tokens if token in source_text) >= max(2, len(title_tokens) // 2):
            title_ok = True
            break
    return title_ok and author_ok


def _extract_metadata_from_html(url: str, html: str, fallback_title: str | None, fallback_author: str | None, source: str) -> dict:
    extracted_title = _extract_first_match(
        [
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
            r"<title>([^<]+)</title>",
            r"<h1[^>]*>([^<]+)</h1>",
        ],
        html,
    ) or fallback_title

    extracted_author = _extract_first_match(
        [
            r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']+)["\']',
            r'by\s+([A-Z][A-Za-z\-\'\s]+)',
            r'"author"\s*[:=]\s*"([^"]+)"',
        ],
        html,
    ) or fallback_author

    publication_date = _extract_publication_date(html)
    cover_image = _extract_cover_image(html)
    series_position = _extract_series_position(extracted_title) or _extract_series_position(html)
    series_name = _extract_first_match(
        [
            r"([A-Za-z][A-Za-z\s]+)\s*:\s*\(?Book\s+\w+\)?",
            r"series\s*[:\-]\s*([A-Za-z0-9\s\-\'&]+)",
        ],
        extracted_title or html,
    )
    description = _extract_first_match(
        [
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
        ],
        html,
    )

    return {
        "title": extracted_title,
        "author": extracted_author,
        "series_name": series_name,
        "series_position": series_position,
        "description": description,
        "cover_image": cover_image,
        "publication_date": publication_date,
        "year": publication_date,
        "source_url": url,
        "source": source,
    }


def _search_serpapi_engines(query: str, author: str | None = None, max_results: int = 10, engines: tuple[str, ...] = ("google", "bing", "duckduckgo")) -> list[dict]:
    api_key = get_serpapi_api_key()
    if not api_key or not query:
        return []

    composed_query = query.strip()
    if author and author.strip() and author.lower() not in composed_query.lower():
        composed_query = f"{composed_query} {author.strip()}"

    headers = {"User-Agent": "BookApp/1.0 (+https://example.com)"}
    organic_results: list[dict] = []
    for engine in engines:
        params = {
            "engine": engine,
            "q": composed_query,
            "api_key": api_key,
            "num": max_results,
        }
        try:
            with httpx.Client(timeout=10.0, headers=headers, trust_env=False) as client:
                response = client.get("https://serpapi.com/search.json", params=params)
                response.raise_for_status()
                data = response.json()
                for item in data.get("organic_results", [])[:max_results]:
                    organic_results.append({**item, "engine": engine})
        except httpx.RequestError:
            continue
        except httpx.HTTPStatusError:
            continue

    return organic_results


def _search_direct_pages(
    urls: list[str],
    title_variants: list[str],
    author: str | None,
    source: str,
    max_results: int = 6,
    debug: dict | None = None,
) -> list[dict]:
    headers = {"User-Agent": "BookApp/1.0 (+https://example.com)"}
    results: list[dict] = []
    if debug is not None:
        debug.setdefault("urls_checked", [])
        debug.setdefault("html_title", None)
        debug.setdefault("html_author", None)
        debug.setdefault("matched", False)
        debug.setdefault("reason", "no-title-author-match")

    for url in urls[:max_results]:
        if debug is not None:
            debug["urls_checked"].append(url)
        try:
            with httpx.Client(timeout=10.0, headers=headers, trust_env=False) as client:
                page_response = client.get(url)
                if page_response.status_code >= 400:
                    continue
                html = page_response.text
        except httpx.RequestError:
            continue

        preview_title = _extract_first_match(
            [
                r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
                r"<title>([^<]+)</title>",
            ],
            html,
        )
        preview_author = _extract_first_match(
            [
                r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']+)["\']',
                r'"author"\s*[:=]\s*"([^"]+)"',
            ],
            html,
        )
        if debug is not None:
            debug["html_title"] = preview_title
            debug["html_author"] = preview_author

        if not _matches_title_author(title_variants, author, html, None):
            continue

        metadata = _extract_metadata_from_html(
            url=url,
            html=html,
            fallback_title=title_variants[0] if title_variants else None,
            fallback_author=author,
            source=source,
        )
        results.append(metadata)
        if debug is not None:
            debug["matched"] = True
            debug["reason"] = "matched"
        break
    return results


def search_web_read_candidates(
    query: str,
    title_variants: list[str],
    author: str | None = None,
    max_results: int = 10,
    debug: dict | None = None,
) -> list[dict]:
    if not query:
        return []

    logger.info("[WEBREAD] Searching web for %s", query)

    organic = _search_serpapi_engines(query, author, max_results, ("google", "bing", "duckduckgo"))
    if debug is not None:
        debug.setdefault("search_engines", ["google", "bing", "duckduckgo"])
        debug.setdefault("organic_results", [])
        debug.setdefault("urls_fetched", [])
        debug.setdefault("html_titles", [])
        debug.setdefault("html_authors", [])
        debug.setdefault("variant_matches", [])
        debug.setdefault("matched", False)
        debug.setdefault("reason", "no-organic-result-matched")

    if not organic:
        if debug is not None:
            debug["reason"] = "no-organic-results"
        return []

    if debug is not None:
        debug["organic_results"] = [item.get("link") for item in organic if item.get("link")][:max_results]

    headers = {"User-Agent": "BookApp/1.0 (+https://example.com)"}

    results: list[dict] = []
    for item in organic[:max_results]:
        link = item.get("link")
        snippet = item.get("snippet")
        title = item.get("title")
        engine = item.get("engine") or "google"
        if not link:
            continue

        source_label = _classify_web_source(link)
        logger.info("[WEBREAD] Found candidate result: %s (%s)", source_label, engine)

        try:
            with httpx.Client(timeout=10.0, headers=headers, trust_env=False) as client:
                page_response = client.get(link)
                if page_response.status_code >= 400:
                    continue
                html = page_response.text
                if debug is not None:
                    debug["urls_fetched"].append(link)
        except httpx.RequestError:
            continue

        html_title = _extract_first_match(
            [
                r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
                r"<title>([^<]+)</title>",
            ],
            html,
        ) or title
        html_author = _extract_first_match(
            [
                r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']+)["\']',
                r'"author"\s*[:=]\s*"([^"]+)"',
            ],
            html,
        ) or author

        if debug is not None:
            debug["html_titles"].append(html_title)
            debug["html_authors"].append(html_author)
            source_text = f"{html} {snippet or ''}".lower()
            variant_flags: list[bool] = []
            for variant in title_variants:
                normalized_variant = re.sub(r"[^a-z0-9\s]", " ", str(variant).lower())
                normalized_variant = re.sub(r"\s+", " ", normalized_variant).strip()
                variant_flags.append(bool(normalized_variant and normalized_variant in source_text))
            debug["variant_matches"].append(variant_flags)

        if not _matches_title_author(title_variants, author, html, snippet):
            continue

        extracted_title = _extract_first_match(
            [
                r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
                r"<title>([^<]+)</title>",
            ],
            html,
        ) or title

        extracted_author = _extract_first_match(
            [
                r'<meta[^>]+name=["\']author["\'][^>]+content=["\']([^"\']+)["\']',
                r'"author"\s*[:=]\s*"([^"]+)"',
            ],
            html,
        ) or author

        series_position = _extract_series_position(extracted_title) or _extract_series_position(snippet)
        series_name = _extract_first_match([r"([A-Za-z][A-Za-z\s]+)\s*:\s*\(?Book\s+\w+\)?"], extracted_title or "")
        description = _extract_first_match(
            [
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
                r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
            ],
            html,
        ) or snippet

        publication_date = _extract_publication_date(html, snippet)
        cover_image = _extract_cover_image(html)

        results.append(
            {
                "title": extracted_title,
                "author": extracted_author,
                "year": publication_date,
                "publication_date": publication_date,
                "description": description,
                "source_url": link,
                "series_name": series_name,
                "series_position": series_position,
                "cover_image": cover_image,
                "source": source_label.lower(),
                "source_label": source_label,
                "engine": engine,
            }
        )
        if debug is not None:
            debug["matched"] = True
            debug["reason"] = "matched"
        logger.info("[WEBREAD] Extracted metadata successfully")
        break

    return results


def _extract_series_position(text: str | None) -> int | None:
    if not text:
        return None

    match = re.search(r"\b(?:book|volume|vol\.?|#)\s*(\d+)\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _source_from_url(url: str | None, fallback: str) -> str:
    if not url:
        return fallback
    host = (urlparse(url).netloc or "").lower()
    if "amazon." in host:
        return "amazon"
    if "fantasticfiction" in host:
        return "fantastic_fiction"
    return fallback


def _normalize_domain_result(
    *,
    title: str | None,
    snippet: str | None,
    link: str | None,
    author: str | None,
    source: str,
) -> dict | None:
    if not title:
        return None

    cleaned_title = re.sub(r"\s+", " ", str(title)).strip()
    cleaned_title = re.sub(r"\s*[-|]\s*Amazon\.?com\s*$", "", cleaned_title, flags=re.IGNORECASE).strip()
    cleaned_title = re.sub(r"\s*[-|]\s*Fantastic\s+Fiction\s*$", "", cleaned_title, flags=re.IGNORECASE).strip()
    position = _extract_series_position(cleaned_title) or _extract_series_position(snippet)

    return {
        "title": cleaned_title,
        "author": author,
        "year": None,
        "description": snippet,
        "source_url": link,
        "series_position": position,
        "source": _source_from_url(link, source),
    }


def _search_serpapi_domain(query: str, domain: str, author: str | None = None, max_results: int = 8, source: str = "serpapi") -> list[dict]:
    api_key = get_serpapi_api_key()
    if not api_key or not query:
        return []

    composed_query = f"site:{domain} {query.strip()}"
    if author and author.strip() and author.lower() not in composed_query.lower():
        composed_query = f"{composed_query} {author.strip()}"

    params = {
        "engine": "google",
        "q": composed_query,
        "api_key": api_key,
        "num": max_results,
    }
    headers = {"User-Agent": "BookApp/1.0 (+https://example.com)"}

    try:
        with httpx.Client(timeout=10.0, headers=headers, trust_env=False) as client:
            response = client.get("https://serpapi.com/search.json", params=params)
            response.raise_for_status()
            data = response.json()
    except httpx.RequestError:
        return []
    except httpx.HTTPStatusError:
        return []

    results: list[dict] = []
    for item in data.get("organic_results", [])[:max_results]:
        normalized = _normalize_domain_result(
            title=item.get("title"),
            snippet=item.get("snippet"),
            link=item.get("link"),
            author=author,
            source=source,
        )
        if normalized:
            results.append(normalized)

    return results


def search_amazon_products(query: str, author: str | None = None, max_results: int = 8, debug: dict | None = None) -> list[dict]:
    if not query:
        return []

    composed = query.strip()
    if author and author.strip() and author.lower() not in composed.lower():
        composed = f"{composed} {author.strip()}"

    search_url = f"https://www.amazon.com/s?k={quote_plus(composed)}"
    headers = {"User-Agent": "BookApp/1.0 (+https://example.com)"}
    try:
        with httpx.Client(timeout=10.0, headers=headers, trust_env=False) as client:
            response = client.get(search_url)
            if response.status_code >= 400:
                return []
            html = response.text
    except httpx.RequestError:
        return []

    links = re.findall(r'href=["\'](/[^"\']*/dp/[A-Z0-9]{10}[^"\']*)["\']', html, flags=re.IGNORECASE)
    candidate_urls = [f"https://www.amazon.com{path}" for path in links]
    if not candidate_urls:
        candidate_urls = [search_url]

    return _search_direct_pages(candidate_urls, [query], author, "amazon", max_results=max_results, debug=debug)


def search_fantastic_fiction(query: str, author: str | None = None, max_results: int = 8, debug: dict | None = None) -> list[dict]:
    if not query:
        return []

    composed = query.strip()
    if author and author.strip() and author.lower() not in composed.lower():
        composed = f"{composed} {author.strip()}"

    search_url = f"https://www.fantasticfiction.com/search/?searchfor=book&keywords={quote_plus(composed)}"
    headers = {"User-Agent": "BookApp/1.0 (+https://example.com)"}
    try:
        with httpx.Client(timeout=10.0, headers=headers, trust_env=False) as client:
            response = client.get(search_url)
            if response.status_code >= 400:
                return []
            html = response.text
    except httpx.RequestError:
        return []

    links = re.findall(r'href=["\'](https://www\.fantasticfiction\.com/[^"\']+|/[^"\']+)["\']', html, flags=re.IGNORECASE)
    candidate_urls = [link if link.startswith("http") else f"https://www.fantasticfiction.com{link}" for link in links]
    if not candidate_urls:
        candidate_urls = [search_url]

    return _search_direct_pages(candidate_urls, [query], author, "fantastic_fiction", max_results=max_results, debug=debug)


def search_author_site_pages(
    title_variants: list[str],
    author_variants: list[str],
    max_results: int = 6,
    debug: dict | None = None,
) -> list[dict]:
    urls: list[str] = []
    for author in author_variants:
        normalized = re.sub(r"[^a-z0-9]", "", str(author or "").lower())
        if not normalized:
            continue
        urls.extend(
            [
                f"https://{normalized}.com",
                f"https://{normalized}books.com",
                f"https://{normalized}writes.com",
                f"https://{normalized}.com/books",
            ]
        )

    deduped = list(dict.fromkeys(urls))
    return _search_direct_pages(
        deduped,
        title_variants,
        author_variants[0] if author_variants else None,
        "author_site",
        max_results=max_results,
        debug=debug,
    )


def search_publisher_pages(
    title_variants: list[str],
    author_variants: list[str],
    max_results: int = 6,
    debug: dict | None = None,
) -> list[dict]:
    publisher_domains = [
        "https://www.tor.com/search/",
        "https://www.orbitbooks.net/?s=",
        "https://www.baen.com/search/?q=",
        "https://www.penguinrandomhouse.com/search/",
    ]
    query = quote_plus(f"{title_variants[0] if title_variants else ''} {author_variants[0] if author_variants else ''}".strip())
    urls = [f"{base}{query}" for base in publisher_domains]
    return _search_direct_pages(
        urls,
        title_variants,
        author_variants[0] if author_variants else None,
        "publisher",
        max_results=max_results,
        debug=debug,
    )


def search_book_database_pages(
    title_variants: list[str],
    author_variants: list[str],
    max_results: int = 6,
    debug: dict | None = None,
) -> list[dict]:
    book_db_domains = [
        "https://www.bookseriesinorder.com/?s=",
        "https://www.bookbrowse.com/search/?query=",
        "https://www.fictiondb.com/search/search.htm?query=",
    ]
    query = quote_plus(f"{title_variants[0] if title_variants else ''} {author_variants[0] if author_variants else ''}".strip())
    urls = [f"{base}{query}" for base in book_db_domains]
    return _search_direct_pages(
        urls,
        title_variants,
        author_variants[0] if author_variants else None,
        "book_database",
        max_results=max_results,
        debug=debug,
    )


def search_goodreads_api(query: str, author: str | None = None, max_results: int = 5) -> list[dict]:
    api_key = os.getenv("GOODREADS_API_KEY")
    if not api_key or not query:
        return []

    params = {
        "q": query,
        "key": api_key,
        "search[field]": "title",
    }
    headers = {"User-Agent": "BookApp/1.0 (+https://example.com)"}
    try:
        with httpx.Client(timeout=10.0, headers=headers, trust_env=False) as client:
            response = client.get("https://www.goodreads.com/search/index.xml", params=params)
            if response.status_code >= 400:
                return []
            xml_text = response.text
    except httpx.RequestError:
        return []

    titles = re.findall(r"<title>([^<]+)</title>", xml_text)[:max_results]
    authors = re.findall(r"<name>([^<]+)</name>", xml_text)[:max_results]
    links = re.findall(r"<link>([^<]+)</link>", xml_text)[:max_results]

    results: list[dict] = []
    for idx, title in enumerate(titles):
        results.append(
            {
                "title": title,
                "author": authors[idx] if idx < len(authors) else author,
                "series_name": None,
                "series_position": _extract_series_position(title),
                "description": None,
                "cover_image": None,
                "publication_date": None,
                "year": None,
                "source_url": links[idx] if idx < len(links) else None,
                "source": "goodreads",
            }
        )
    return results


def lookup_book_summary(title: str, author: str | None = None) -> dict:
    if not title:
        return {
            "found": False,
            "summary": None,
            "source_url": None,
            "matched_title": None,
            "matched_author": None,
        }

    def build_lookup_queries(raw_title: str) -> list[str]:
        queries: list[str] = []

        def add_query(value: str | None):
            cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
            if cleaned and cleaned not in queries:
                queries.append(cleaned)

        add_query(raw_title)

        stripped_paren = re.sub(r"\s*\([^)]*\bbook\s*\d+[^)]*\)\s*$", "", raw_title, flags=re.IGNORECASE)
        add_query(stripped_paren)

        stripped_series_suffix = re.sub(r"\s*[:\-]\s*[^:()]*\bbook\s*\d+.*$", "", raw_title, flags=re.IGNORECASE)
        add_query(stripped_series_suffix)

        return queries

    def result_payload(result: dict, fallback_author: str | None = None) -> dict:
        return {
            "found": True,
            "summary": result.get("description"),
            "source_url": result.get("source_url"),
            "matched_title": result.get("title"),
            "matched_author": result.get("author") or fallback_author,
        }

    lookup_queries = build_lookup_queries(title)
    author_candidates = []
    if author and author.strip():
        author_candidates.append(author)
    author_candidates.append(None)

    best_fallback: dict | None = None

    for query in lookup_queries:
        for author_candidate in author_candidates:
            google_results = search_google_books(query, author_candidate, max_results=3)
            for result in google_results:
                if result.get("description"):
                    return result_payload(result, author_candidate)

            open_results = search_openlibrary(query, author_candidate, max_results=3)
            for result in open_results:
                if result.get("description"):
                    return result_payload(result, author_candidate)

            serp_results = search_serpapi_web(query, author_candidate, max_results=5)
            for result in serp_results:
                if result.get("description"):
                    return result_payload(result, author_candidate)

            if not best_fallback:
                if open_results:
                    best_fallback = result_payload(open_results[0], author_candidate)
                elif google_results:
                    best_fallback = result_payload(google_results[0], author_candidate)
                elif serp_results:
                    best_fallback = result_payload(serp_results[0], author_candidate)

    if best_fallback:
        return best_fallback

    return {
        "found": False,
        "summary": None,
        "source_url": None,
        "matched_title": None,
        "matched_author": None,
    }


def recompute_series_intelligence(db):
    """
    Recompute intelligence for ALL series in the database.
    This is the function the importer expects.
    """

    all_series = db.query(Series).all()

    for series in all_series:
        intel = compute_series_intelligence_for_series(db, series.id)

        if intel is None:
            continue

        # Update the Series model fields
        series.total_books = intel.get("total_books")
        series.is_finished = intel.get("is_series_finished")

        # Commit updates
        db.commit()
        db.refresh(series)

    return True

