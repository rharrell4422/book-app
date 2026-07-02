"""Book agent with a manual-approval workflow.

This module intentionally returns metadata only. It does not auto-create books.
"""

from book_metadata_utils import normalize_book_metadata
from intelligence import lookup_book_summary, search_google_books, search_openlibrary, search_serpapi_web
from search_orchestrator import SearchOrchestrator


class BookAgent:
    def __init__(self):
        self.search = SearchOrchestrator(
            google_search=search_google_books,
            openlibrary_search=search_openlibrary,
            serp_search=search_serpapi_web,
        )

    def generate_search_queries(self, title: str, author: str | None = None) -> list[str]:
        title = (title or "").strip()
        author = (author or "").strip() or None
        if not title:
            return []

        queries = [title]
        if author:
            queries.append(f"{title} {author}")
        return queries

    def fetch_text(self, queries: list[str], author: str | None = None) -> dict:
        if not queries:
            return {
                "found": False,
                "summary": None,
                "source_url": None,
                "matched_title": None,
                "matched_author": None,
            }
        return lookup_book_summary(queries[0], author)

    def interpret_text(self, fetched: dict, fallback_title: str, fallback_author: str | None = None) -> dict:
        matched_title = fetched.get("matched_title")
        matched_author = fetched.get("matched_author")
        has_confident_match = bool(fetched.get("found")) and bool(matched_title) and bool(matched_author)
        metadata = normalize_book_metadata(
            {
                "title": matched_title or fallback_title,
                "author": matched_author or fallback_author or "Unknown author",
                "auto_summary": fetched.get("summary"),
                "notes": None,
                "source_url": fetched.get("source_url"),
            }
        )
        metadata["found"] = has_confident_match
        return metadata

    def create_book(self, metadata: dict):
        # Book creation is intentionally handled by /agent/approve, not here.
        raise RuntimeError("Book creation is approval-gated; use /agent/approve")

    def run(self, title: str, author: str | None = None) -> dict:
        queries = self.generate_search_queries(title, author)
        fetched = self.fetch_text(queries, author)
        return self.interpret_text(fetched, fallback_title=title, fallback_author=author)
