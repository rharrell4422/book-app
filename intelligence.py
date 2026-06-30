import os
import re
import traceback
from urllib.parse import quote_plus

import httpx
from datetime import date

try:
    from models import Series, Book
except Exception as e:
    print("\n\n🔥 INTELLIGENCE MODULE FAILED DURING IMPORT 🔥")
    traceback.print_exc()
    raise e

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

    read_count = len(read_books)
    unread_count = len(unread_books)

    # Missing orders should use the expected total count, not the current book count.
    expected_orders = set(range(1, int(total_books) + 1)) if total_books else set()
    missing_orders = [str(order) for order in sorted(expected_orders - all_known_orders)]

    # Next unread
    next_unread = unread_books[0] if unread_books else None

    # Upcoming = future release/publication date
    today = date.today()
    upcoming_books = [
        b for b in books
        if (b.release_date or b.publication_date) and (b.release_date or b.publication_date) > today
    ]
    upcoming_books.sort(key=lambda b: b.release_date or b.publication_date)
    next_upcoming = upcoming_books[0] if upcoming_books else None

    return {
        "series_id": series_id,
        "total_books": total_books,
        "read_count": read_count,
        "unread_count": unread_count,
        "missing_orders": missing_orders,
        "next_unread_book_id": next_unread.id if next_unread else None,
        "next_upcoming_book_id": next_upcoming.id if next_upcoming else None,
        "is_series_finished": read_count == total_books,
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
    with httpx.Client(timeout=timeout, headers=headers) as client:
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
            with httpx.Client(timeout=8.0, headers=headers) as client:
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
        with httpx.Client(timeout=8.0, headers=headers) as client:
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
        with httpx.Client(timeout=10.0, headers=headers) as client:
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


def suggest_book_by_series(series_name: str, book_number: int | None = None, author: str | None = None) -> dict:
    from search_orchestrator import SearchOrchestrator

    orchestrator = SearchOrchestrator(
        google_search=search_google_books,
        openlibrary_search=search_openlibrary,
        serp_search=search_serpapi_web,
    )
    return orchestrator.suggest_series(
        series_name=series_name,
        book_number=book_number,
        author=author,
    )


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

