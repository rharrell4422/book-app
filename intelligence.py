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
    with httpx.Client(timeout=10.0, headers=headers) as client:
        response = client.get("https://www.googleapis.com/books/v1/volumes", params=params)
        if response.status_code == 429:
            return []
        response.raise_for_status()
        data = response.json()

    items = data.get("items", [])
    if not items and safe_author and "inauthor:" in query:
        fallback_query = query.split("+inauthor:")[0].strip()
        params["q"] = fallback_query
        response = client.get("https://www.googleapis.com/books/v1/volumes", params=params)
        if response.status_code != 429:
            response.raise_for_status()
            data = response.json()
            items = data.get("items", [])

    if not items and safe_author:
        params["q"] = f'{query.split("+inauthor:")[0].strip()} "{safe_author}"'
        response = client.get("https://www.googleapis.com/books/v1/volumes", params=params)
        if response.status_code != 429:
            response.raise_for_status()
            data = response.json()
            items = data.get("items", [])

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
    with httpx.Client(timeout=10.0, headers=headers) as client:
        response = client.get("https://openlibrary.org/search.json", params=params)
        response.raise_for_status()
        data = response.json()

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


def lookup_book_summary(title: str, author: str | None = None) -> dict:
    if not title:
        return {
            "found": False,
            "summary": None,
            "source_url": None,
            "matched_title": None,
            "matched_author": None,
        }

    google_results = search_google_books(title, author, max_results=3)
    for result in google_results:
        if result.get("description"):
            return {
                "found": True,
                "summary": result.get("description"),
                "source_url": result.get("source_url"),
                "matched_title": result.get("title"),
                "matched_author": result.get("author"),
            }

    open_results = search_openlibrary(title, author, max_results=3)
    if open_results:
        first = open_results[0]
        return {
            "found": True,
            "summary": first.get("description"),
            "source_url": first.get("source_url"),
            "matched_title": first.get("title"),
            "matched_author": first.get("author"),
        }

    if google_results:
        first = google_results[0]
        return {
            "found": True,
            "summary": first.get("description"),
            "source_url": first.get("source_url"),
            "matched_title": first.get("title"),
            "matched_author": first.get("author"),
        }

    return {
        "found": False,
        "summary": None,
        "source_url": None,
        "matched_title": None,
        "matched_author": None,
    }


def suggest_book_by_series(series_name: str, book_number: int | None = None, author: str | None = None) -> dict:
    if not series_name:
        return {"query": "", "results": []}

    def normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text.lower().strip())

    series_name_norm = normalize(series_name)

    def add_base_forms(name: str) -> list[str]:
        forms = [name.strip()]
        if name.lower().startswith("the "):
            forms.append(name[4:].strip())

        for marker in ["novels", "series", "trilogy", "saga", "cycle"]:
            if marker in name.lower():
                prefix = re.split(rf"\b{marker}\b", name, flags=re.IGNORECASE)[0].strip()
                if prefix and prefix not in forms:
                    forms.append(prefix)
        return [f for f in forms if f]

    base_forms = add_base_forms(series_name)
    google_queries: list[str] = []
    for base in base_forms:
        base_quoted = f'"{base}"'
        if book_number is not None:
            google_queries.extend([
                f'intitle:{base_quoted} inauthor:"{author}"' if author else f'intitle:{base_quoted} {book_number}',
                f'intitle:{base_quoted} book {book_number} inauthor:"{author}"' if author else f'intitle:{base_quoted} book {book_number}',
                f'intitle:{base_quoted} volume {book_number} inauthor:"{author}"' if author else f'intitle:{base_quoted} volume {book_number}',
                f'{base_quoted} {book_number} inauthor:"{author}"' if author else f'{base_quoted} {book_number}',
            ])
        google_queries.extend([
            f'intitle:{base_quoted} inauthor:"{author}"' if author else f'intitle:{base_quoted}',
            f'{base_quoted} inauthor:"{author}"' if author else base,
        ])
        if author:
            google_queries.extend([
                f'intitle:{base_quoted} OR "LitRPG" inauthor:"{author}"',
                f'intitle:{base_quoted} OR "Lit RPG" inauthor:"{author}"',
            ])
    candidate_queries: list[str] = []
    if book_number is not None:
        for base in base_forms:
            candidate_queries.extend([
                f'"{base}" {book_number}',
                f'"{base}" book {book_number}',
                f'"{base}" volume {book_number}',
                f'{base} {book_number}',
            ])
    candidate_queries.extend(base_forms)
    if author:
        for base in base_forms:
            candidate_queries.append(f'{base} {author}')
            candidate_queries.append(f'{base} author:{author}')

    def extract_series_names(result: dict) -> list[str]:
        series_names = result.get("series_name") or []
        if isinstance(series_names, str):
            series_names = [series_names]
        return [normalize(str(name)) for name in series_names if name]

    def matches_series(result: dict, title_norm: str) -> bool:
        series_names = extract_series_names(result)
        series_matches = any(
            series_name_norm == name or series_name_norm in name or name in series_name_norm
            for name in series_names
        )
        title_matches = series_name_norm in title_norm or title_norm in series_name_norm
        return series_matches or (author is not None and title_matches)

    def matches_number(result: dict, title_norm: str) -> bool:
        if book_number is None:
            return True
        position = result.get("series_position")
        if position == book_number or position == str(book_number):
            return True

        number_text = str(book_number)
        patterns = [
            rf"\b{re.escape(number_text)}\b",
            rf"\bbook\s+{re.escape(number_text)}\b",
            rf"\bvolume\s+{re.escape(number_text)}\b",
            rf"#\s*{re.escape(number_text)}\b",
            rf"\({re.escape(number_text)}\)",
            rf"{re.escape(number_text)}:",
        ]
        return any(re.search(pattern, title_norm) for pattern in patterns)

    headers = {"User-Agent": "BookApp/1.0 (+https://example.com)"}
    results = []
    seen = set()
    final_query = ""

    google_query_candidates = list(dict.fromkeys(google_queries + candidate_queries))
    for query in google_query_candidates:
        if not query.strip():
            continue
        final_query = query
        google_results = search_google_books(query, author, max_results=5)
        for result in google_results:
            if not result.get("title"):
                continue
            title_norm = normalize(result["title"])
            if not matches_series(result, title_norm):
                continue
            if not matches_number(result, title_norm):
                continue
            key = (title_norm, result.get("author"))
            if key in seen:
                continue
            seen.add(key)
            results.append(result)
        if results:
            break

    if not results and author:
        author_only_query = f'inauthor:"{author}"'
        final_query = author_only_query
        google_results = search_google_books(author_only_query, None, max_results=8)
        for result in google_results:
            if not result.get("title"):
                continue
            title_norm = normalize(result["title"])
            if not matches_number(result, title_norm):
                continue
            key = (title_norm, result.get("author"))
            if key in seen:
                continue
            seen.add(key)
            results.append(result)

    if not results:
        for query in candidate_queries:
            open_results = search_openlibrary(query, author, max_results=5)
            for result in open_results:
                if not result.get("title"):
                    continue
                title_norm = normalize(result["title"])
                if not matches_series(result, title_norm):
                    continue
                if not matches_number(result, title_norm):
                    continue
                key = (title_norm, result.get("author"))
                if key in seen:
                    continue
                seen.add(key)
                results.append(result)
            if results:
                break

    if not results:
        final_query = series_name
        open_results = search_openlibrary(series_name, author, max_results=5)
        for result in open_results:
            title_norm = normalize(result.get("title", ""))
            if not matches_series(result, title_norm):
                continue
            if not matches_number(result, title_norm):
                continue
            key = (title_norm, result.get("author"))
            if key in seen:
                continue
            seen.add(key)
            results.append(result)

    if not results and author:
        final_query = f'author:"{author}"'
        author_results = search_openlibrary(final_query, None, max_results=8)
        for result in author_results:
            if not result.get("title"):
                continue
            title_norm = normalize(result["title"])
            if book_number is not None and not matches_number(result, title_norm):
                continue
            key = (title_norm, result.get("author"))
            if key in seen:
                continue
            seen.add(key)
            results.append(result)

    return {
        "query": final_query,
        "results": results,
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

