import os
import re
import logging

import httpx
from datetime import date

try:
    from models import Series, Book
except Exception:
    raise


logger = logging.getLogger(__name__)
OMNIBUS_RANGE_PATTERN = re.compile(r"\bbooks?\s+\d+(?:\.\d+)?\s*[-–]\s*\d+(?:\.\d+)?\b", re.IGNORECASE)
OMNIBUS_RANGE_CAPTURE_PATTERN = re.compile(
    r"\bbooks?\s+(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\b",
    re.IGNORECASE,
            "[MAINTENANCE] Purging invalid book series_id=%s book_id=%s title=%s reasons=%s",
            series_id,
            book.id,
            book.title,
            ",".join(reasons),
        )
        book.record_status = "deleted"

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
    search_markers = (
        'data-component-type="s-search-result"',
        "data-component-type='s-search-result'",
        'id="search"',
        "id='search'",
        'cel_widget_id="MAIN-SEARCH_RESULTS',
    )
    normalized_html = str(html or "")
    return any(marker in normalized_html for marker in search_markers)


def _extract_series_position(text: str | None) -> int | None:
    if not text:
        return None

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
        recalculate_intelligence(db, series.id)

    return True

