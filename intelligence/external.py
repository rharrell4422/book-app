"""External book-summary lookup: an on-demand Google Books search used to
backfill a description for a book that doesn't have one.

Split out of intelligence.py (RT-4). Independent of core.py/admin.py -- see
intelligence/core.py's module docstring. Kept as its own module since it's
the one piece of intelligence.py that talks to an external provider
(reusing discovery_engine.py's Google Books client) rather than only
querying the local database.

intelligence/__init__.py re-exports everything below, so existing external
callers (routers/books.py, tests, etc.) are unaffected by this split. It
also re-imports discovery_engine itself, so `intelligence.discovery_engine`
keeps resolving to the same module object this file calls into (needed for
tests that patch it at that path).
"""
from __future__ import annotations

import logging
import re

import discovery_engine


logger = logging.getLogger(__name__)


def lookup_book_summary(
    title: str,
    author: str | None = None,
    book_number=None,
    series_name: str | None = None,
) -> dict:
    if not title:
        return {
            "found": False,
            "summary": None,
            "source_url": None,
            "matched_title": None,
            "matched_author": None,
        }

    expected_number = None
    if book_number is not None:
        try:
            expected_number = int(float(book_number))
        except (TypeError, ValueError):
            expected_number = None

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
    not_found = {
        "found": False,
        "summary": None,
        "source_url": None,
        "matched_title": None,
        "matched_author": None,
    }

    # This previously called search_google_books/search_openlibrary/
    # search_serpapi_web, which were removed in an earlier cleanup pass and
    # would raise NameError on every call. discovery_engine.py now has a
    # real, working Google Books/OpenLibrary client (built for series
    # discovery) -- reuse it here too rather than leaving this broken.
    for query in lookup_queries:
        for author_candidate in author_candidates:
            try:
                google_results = discovery_engine._fetch_google_books(  # noqa: SLF001
                    f'intitle:"{query}"' + (f' inauthor:"{author_candidate}"' if author_candidate else "")
                )
            except Exception as exc:
                logger.info("Book summary lookup: Google Books unavailable (%s)", exc)
                google_results = []

            for result in google_results:
                description = result.get("description")
                if not description:
                    continue

                result_title = result.get("title")
                result_number = discovery_engine.infer_number_from_title(result_title, series_name)

                # Google's intitle: search is a relevance-ranked text match, not
                # an exact-phrase lookup -- it doesn't reliably rank the exact
                # volume first. Regression: a book-1 lookup for "1% Lifesteal"
                # got Google's "Volume 4" result back (ranked above the real
                # "Book one" match) purely because it happened to rank higher,
                # silently attaching book 4's summary to book 1. When we know
                # which book number we're after and a result's title clearly
                # identifies itself as a *different* number, it can never be
                # right -- skip it outright regardless of rank.
                if expected_number is not None and result_number is not None and result_number != expected_number:
                    continue

                payload = result_payload(
                    {
                        "description": description,
                        "source_url": result.get("source_url"),
                        "title": result_title,
                        "author": ", ".join(result.get("authors") or []) or None,
                    },
                    author_candidate,
                )

                if expected_number is None or result_number == expected_number:
                    return payload

                # result_number is None -- an ambiguous result with no
                # parseable number of its own. Only usable as a last-resort
                # fallback if nothing confidently-numbered ever turns up.
                if not best_fallback:
                    best_fallback = payload

    if best_fallback:
        return best_fallback

    return not_found
