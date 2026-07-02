"""
BookAgent: Full production-ready agent with manual approval workflow.

This agent performs a complete reasoning loop:
1. Generate search queries
2. Fetch text from search results
3. Interpret the text into structured metadata
4. Return metadata for manual approval
5. (Optional) Create the book in the database when approved

All backend integrations are wired in:
- search_orchestrator.py
- intelligence.py
- crud/books.py

The agent returns a Python dict for safety and clarity.
"""

from search_orchestrator import SearchOrchestrator
from intelligence import Intelligence
from crud.books import create_book


class BookAgent:
    """
    The BookAgent coordinates the entire reasoning loop.
    It does NOT automatically create books — it returns metadata first.
    You approve the metadata, then call create_book(metadata).
    """

    def __init__(self):
        # Dependency injection
        self.search = SearchOrchestrator()
        self.intel = Intelligence()

    # ------------------------------------------------------------
    # 1. Generate Search Queries
    # ------------------------------------------------------------
    def generate_search_queries(self, title: str, author: str = None) -> list[str]:
        """
        Uses SearchOrchestrator to generate search queries.
        """
        queries = self.search.generate_queries(title, author)
        return queries

    # ------------------------------------------------------------
    # 2. Fetch Text
    # ------------------------------------------------------------
    def fetch_text(self, queries: list[str]) -> str:
        """
        Uses SearchOrchestrator to fetch raw text from the web.
        """
        raw_text = self.search.fetch_text_from_queries(queries)
        return raw_text

    # ------------------------------------------------------------
    # 3. Interpret Text
    # ------------------------------------------------------------
    def interpret_text(self, raw_text: str) -> dict:
        """
        Uses Intelligence to interpret raw text and extract metadata.
        Returns a Python dict for safety and clarity.
        """
        metadata = self.intel.extract_metadata(raw_text)
        return metadata

    # ------------------------------------------------------------
    # 4. Manual Approval Step
    # ------------------------------------------------------------
    def create_book(self, metadata: dict):
        """
        Creates a book in the database.
        This is ONLY called after you manually approve the metadata.
        """
        return create_book(metadata)

    # ------------------------------------------------------------
    # 5. Reasoning Loop
    # ------------------------------------------------------------
    def run(self, title: str, author: str = None) -> dict:
        """
        Full reasoning loop:
        1. Generate search queries
        2. Fetch text
        3. Interpret text
        4. Return metadata (manual approval required)

        This function NEVER writes to the database automatically.
        """
        queries = self.generate_search_queries(title, author)
        raw_text = self.fetch_text(queries)
        metadata = self.interpret_text(raw_text)
        return metadata
