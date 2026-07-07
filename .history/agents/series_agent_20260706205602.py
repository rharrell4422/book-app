from __future__ import annotations

import logging
from datetime import date, datetime
from difflib import SequenceMatcher
import re
from urllib.parse import urlparse
from time import monotonic

from collections.abc import Callable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from book_metadata_utils import normalize_book_metadata, parse_publication_date
from intelligence import (
    compute_series_intelligence_for_series,
    recalculate_series_state_for_series,
    recount_series_aggregates_for_series,
    search_web_read_candidates,
    search_amazon_products,
    search_author_site_pages,
    search_book_database_pages,
    search_fantastic_fiction,
    search_google_books,
    search_goodreads_api,
    search_openlibrary,
    search_publisher_pages,
    search_serpapi_web,
)
from models import Book, Series, SeriesCanonicalEntry


BOOK_COLUMN_KEYS = {column.key for column in Book.__table__.columns}
logger = logging.getLogger(__name__)


class Provider:
    name = "provider"

    def search(self, title_variants, author_variants):
        raise NotImplementedError


class AmazonProvider(Provider):
    name = "amazon"

    def search(self, title_variants, author_variants):
        attempt = {
            "provider": "AmazonProvider",
            "attempted": True,
            "matched": False,
            "reason": "no-title-author-match",
            "urls_checked": [],
            "html_title": None,
            "html_author": None,
        }
        author = author_variants[0] if author_variants else None
        for title in title_variants:
            results = search_amazon_products(title, author, 8, debug=attempt)
            if results:
                attempt["matched"] = True
                attempt["reason"] = "matched"
                return results, attempt
        return [], attempt


class FantasticFictionProvider(Provider):
    name = "fantastic_fiction"

    def search(self, title_variants, author_variants):
        attempt = {
            "provider": "FantasticFictionProvider",
            "attempted": True,
            "matched": False,
            "reason": "no-title-author-match",
            "urls_checked": [],
            "html_title": None,
            "html_author": None,
        }
        author = author_variants[0] if author_variants else None
        for title in title_variants[:4]:
            results = search_fantastic_fiction(title, author, 8, debug=attempt)
            if results:
                attempt["matched"] = True
                attempt["reason"] = "matched"
                return results, attempt
        return [], attempt


class AuthorSiteProvider(Provider):
    name = "author_site"

    def search(self, title_variants, author_variants):
        attempt = {
            "provider": "AuthorSiteProvider",
            "attempted": True,
            "matched": False,
            "reason": "no-title-author-match",
            "urls_checked": [],
            "html_title": None,
            "html_author": None,
        }
        results = search_author_site_pages(title_variants, author_variants, 8, debug=attempt)
        if results:
            attempt["matched"] = True
            attempt["reason"] = "matched"
        return results, attempt


class PublisherProvider(Provider):
    name = "publisher"

    def search(self, title_variants, author_variants):
        attempt = {
            "provider": "PublisherProvider",
            "attempted": True,
            "matched": False,
            "reason": "no-title-author-match",
            "urls_checked": [],
            "html_title": None,
            "html_author": None,
        }
        results = search_publisher_pages(title_variants, author_variants, 8, debug=attempt)
        if results:
            attempt["matched"] = True
            attempt["reason"] = "matched"
        return results, attempt


class BookDatabaseProvider(Provider):
    name = "book_database"

    def search(self, title_variants, author_variants):
        attempt = {
            "provider": "BookDatabaseProvider",
            "attempted": True,
            "matched": False,
            "reason": "no-title-author-match",
            "urls_checked": [],
            "html_title": None,
            "html_author": None,
        }
        results = search_book_database_pages(title_variants, author_variants, 8, debug=attempt)
        if results:
            attempt["matched"] = True
            attempt["reason"] = "matched"
        return results, attempt


class WebReadProvider(Provider):
    name = "web_read"

    def search(self, title_variants, author_variants):
        attempt = {
            "provider": "WebReadProvider",
            "attempted": True,
            "matched": False,
            "reason": "no-organic-result-matched",
            "search_engines": ["google", "bing", "duckduckgo"],
            "organic_results": [],
            "urls_fetched": [],
            "html_titles": [],
            "html_authors": [],
            "variant_matches": [],
        }
        author = author_variants[0] if author_variants else None
        query = title_variants[0] if title_variants else ""
        results = search_web_read_candidates(query, title_variants, author, 10, debug=attempt)
        if results:
            attempt["matched"] = True
            attempt["reason"] = "matched"
        return results, attempt


class CatalogFallbackProvider(Provider):
    name = "catalog_fallback"

    def search(self, title_variants, author_variants):
        attempt = {
            "provider": "CatalogFallbackProvider",
            "attempted": True,
            "matched": False,
            "reason": "no-catalog-hit",
        }
        author = author_variants[0] if author_variants else None
        for title in title_variants[:3]:
            results = search_google_books(title, author, 8)
            if results:
                attempt["matched"] = True
                attempt["reason"] = "matched"
                return results, attempt
            results = search_openlibrary(title, author, 8)
            if results:
                attempt["matched"] = True
                attempt["reason"] = "matched"
                return results, attempt
            results = search_goodreads_api(title, author, 8)
            if results:
                attempt["matched"] = True
                attempt["reason"] = "matched"
                return results, attempt
        return [], attempt


class GoogleBooksHtmlProvider(Provider):
    name = "google_books_html"

    def search(self, title_variants, author_variants):
        attempt = {
            "provider": "GoogleBooksHtmlProvider",
            "attempted": True,
            "matched": False,
            "reason": "no-google-books-html-hit",
            "urls_checked": [],
            "html_title": None,
            "html_author": None,
        }
        author = author_variants[0] if author_variants else None
        try:
            results = search_google_books_html(title_variants, author, 8, debug=attempt)
        except Exception:
            attempt["reason"] = "exception"
            return [], attempt
        print(f"[DISCOVERY_DEBUG] provider={self.name} candidates={len(results)} reason={attempt.get('reason')}")
        if results:
            attempt["matched"] = True
            attempt["reason"] = "matched"
        return results, attempt


class GoodreadsHtmlProvider(Provider):
    name = "goodreads_html"

    def search(self, title_variants, author_variants):
        attempt = {
            "provider": "GoodreadsHtmlProvider",
            "attempted": True,
            "matched": False,
            "reason": "no-goodreads-html-hit",
            "urls_checked": [],
            "html_title": None,
            "html_author": None,
        }
        author = author_variants[0] if author_variants else None
        try:
            results = search_goodreads_html(title_variants, author, 8, debug=attempt)
        except Exception:
            attempt["reason"] = "exception"
            return [], attempt
        print(f"[DISCOVERY_DEBUG] provider={self.name} candidates={len(results)} reason={attempt.get('reason')}")
        if results:
            attempt["matched"] = True
            attempt["reason"] = "matched"
        return results, attempt


class OpenLibraryHtmlProvider(Provider):
    name = "openlibrary_html"

    def search(self, title_variants, author_variants):
        attempt = {
            "provider": "OpenLibraryHtmlProvider",
            "attempted": True,
            "matched": False,
            "reason": "no-openlibrary-html-hit",
            "urls_checked": [],
            "html_title": None,
            "html_author": None,
        }
        author = author_variants[0] if author_variants else None
        try:
            results = search_openlibrary_html(title_variants, author, 8, debug=attempt)
        except Exception:
            attempt["reason"] = "exception"
            return [], attempt
        print(f"[DISCOVERY_DEBUG] provider={self.name} candidates={len(results)} reason={attempt.get('reason')}")
        if results:
            attempt["matched"] = True
            attempt["reason"] = "matched"
        return results, attempt


class AmazonHtmlProvider(Provider):
    name = "amazon_html"

    def search(self, title_variants, author_variants):
        attempt = {
            "provider": "AmazonHtmlProvider",
            "attempted": True,
            "matched": False,
            "reason": "no-amazon-html-hit",
            "urls_checked": [],
            "html_title": None,
            "html_author": None,
        }
        author = author_variants[0] if author_variants else None
        try:
            results = search_amazon_html_public(title_variants, author, 8, debug=attempt)
        except Exception:
            attempt["reason"] = "exception"
            return [], attempt
        print(f"[DISCOVERY_DEBUG] provider={self.name} candidates={len(results)} reason={attempt.get('reason')}")
        if results:
            attempt["matched"] = True
            attempt["reason"] = "matched"
        return results, attempt


class SeriesIntelligenceAgent:
    # Team rule: All discovery logic must be implemented inside SeriesIntelligenceAgent.
    # No new discovery code may be added anywhere else.
    FUTURE_SCAN_MAX_AHEAD = 20
    FUTURE_SCAN_EMPTY_STREAK_STOP = 3
    MIN_FUZZY_SCORE = 0.46
    DISCOVERY_TIME_BUDGET_SECONDS = 25.0
    CREDIBLE_DOMAIN_SUFFIXES = (
        "amazon.com",
        "fantasticfiction.com",
        "goodreads.com",
        "openlibrary.org",
        "books.google.com",
        "googleapis.com",
        "tor.com",
        "orbitbooks.net",
        "baen.com",
        "penguinrandomhouse.com",
        "bookseriesinorder.com",
        "bookbrowse.com",
        "fictiondb.com",
    )
    UNKNOWN_DOMAIN_BLOCKLIST_KEYWORDS = (
        "rumor",
        "speculation",
        "release date",
        "spoilers",
        "fanfic",
        "prediction",
    )
    UNKNOWN_DOMAIN_BLOCKLIST_NETWORK_TOKENS = (
        "updates",
        "fandom",
        "wiki",
        "blogspot",
        "weebly",
        "wix",
        "medium",
    )
    USER_CREATED_IMPORT_SOURCES = {
        "user",
        "manual",
        "user_added",
        "ui",
        "hand_entered",
    }
    AUTO_CLEANUP_ELIGIBLE_IMPORT_SOURCES = {
        "agent_v2",
        "discovery",
        "system",
        "import",
        "rejected",
        "unverified",
    }

    def _book_number_value(self, book: Book) -> float | None:
        raw_value = book.book_number if book.book_number is not None else book.series_order
        if raw_value is None:
            return None
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            return None

    def _series_total_books_int(self, book: Book) -> int | None:
        raw_value = book.series_total_books
        if raw_value is None:
            return None
        try:
            numeric = float(raw_value)
        except (TypeError, ValueError):
            return None
        if not numeric.is_integer():
            return None
        value = int(numeric)
        if value <= 0:
            return None
        return value

    def _completed_series_known_max(self, series: Series, books: list[Book], intelligence_snapshot: dict) -> int | None:
        trusted_declared_totals: list[int] = []
        fallback_declared_totals: list[int] = []

        for book in books:
            declared_total = self._series_total_books_int(book)
            if declared_total is None:
                continue
            fallback_declared_totals.append(declared_total)
            if self._is_cleanup_source_eligible(book):
                continue
            trusted_declared_totals.append(declared_total)

        if trusted_declared_totals:
            return max(trusted_declared_totals)
        if fallback_declared_totals:
            return max(fallback_declared_totals)

        try:
            if series.total_books is not None:
                return int(float(series.total_books))
        except (TypeError, ValueError):
            pass

        fallback_total = intelligence_snapshot.get("total_books")
        try:
            if fallback_total is not None:
                return int(float(fallback_total))
        except (TypeError, ValueError):
            pass
        return None

    def _is_user_created_book(self, book: Book) -> bool:
        source = str(book.import_source or "").strip().lower()
        if not source:
            # Ambiguous source metadata: never delete.
            return True
        return source in self.USER_CREATED_IMPORT_SOURCES

    def _is_cleanup_source_eligible(self, book: Book) -> bool:
        source = str(book.import_source or "").strip().lower()
        return source in self.AUTO_CLEANUP_ELIGIBLE_IMPORT_SOURCES

    def _is_ghost_book(self, book: Book) -> bool:
        return bool(book.is_missing) or bool(book.is_upcoming_auto) or bool(book.is_upcoming_final)

    def _find_deleted_ghost_tombstone(self, db: Session, series_id: int, book_number: float | int) -> Book | None:
        normalized_number = float(book_number)
        deleted_matches = (
            db.query(Book)
            .filter(Book.series_id == series_id)
            .filter(or_(Book.book_number == normalized_number, Book.series_order == normalized_number))
            .filter(Book.record_status == "deleted")
            .all()
        )
        for match in deleted_matches:
            if self._is_ghost_book(match):
                return match
        return None

    def _strict_post_discovery_cleanup(
        self,
        db: Session,
        series: Series,
        *,
        known_authors: list[str],
        known_series_max: int | None,
        series_complete: bool,
    ) -> dict:
        if not bool(series_complete) or known_series_max is None:
            return {"series_id": series.id, "deleted_count": 0, "deleted_entries": []}

        candidates = (
            db.query(Book)
            .filter(Book.series_id == series.id)
            .filter(or_(Book.record_status.is_(None), Book.record_status != "deleted"))
            .all()
        )
        deleted_entries: list[dict] = []

        for book in candidates:
            book_number = self._book_number_value(book)
            if book_number is None:
                continue
            if not float(book_number).is_integer():
                continue
            if book_number <= float(known_series_max):
                continue
            if self._is_user_created_book(book):
                continue
            if not self._is_cleanup_source_eligible(book):
                continue

            target_number = int(book_number)
            discovery_result = self._discover_with_fallback(
                series.name,
                series.id,
                target_number,
                known_authors,
                known_series_max,
                series_complete,
            )
            candidate_results = discovery_result.get("results") or []
            if candidate_results:
                # Any corroborated candidate means deletion is not allowed.
                continue

            reason = "completed_series_known_max_no_candidates_non_user_source"
            logger.warning(
                "[MAINTENANCE] Purging legacy ghost entry series_id=%s book_number=%s title=%s reason=%s",
                series.id,
                target_number,
                book.title,
                reason,
            )
            deleted_entries.append(
                {
                    "series_id": series.id,
                    "book_number": target_number,
                    "title": book.title,
                    "reason": reason,
                }
            )
            book.record_status = "deleted"

        if deleted_entries:
            db.commit()

        recount_series_aggregates_for_series(db, series.id)
        return {"series_id": series.id, "deleted_count": len(deleted_entries), "deleted_entries": deleted_entries}

    def _all_title_variants(self, series_name: str, book_number: int) -> list[str]:
        variants = self._title_search_variants(series_name, book_number)
        combined = [*variants["exact"], *variants["normalized"], *variants["fuzzy"]]
        deduped: list[str] = []
        seen: set[str] = set()
        for item in combined:
            key = item.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return deduped

    def _provider_plan(self) -> list[tuple[str, list[Provider]]]:
        return [
            (
                "direct",
                [
                    AmazonProvider(),
                    FantasticFictionProvider(),
                    AuthorSiteProvider(),
                    PublisherProvider(),
                    BookDatabaseProvider(),
                ],
            ),
            ("web_read", [WebReadProvider()]),
            ("catalog_fallback", [CatalogFallbackProvider()]),
        ]

    def _normalize_text(self, value: str | None) -> str:
        lowered = str(value or "").strip().lower()
        lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
        return re.sub(r"\s+", " ", lowered).strip()

    def _series_variants(self, series_name: str) -> list[str]:
        base = re.sub(r"\s+", " ", str(series_name or "").strip()).strip(" -,:;")
        if not base:
            return []

        variants = [base]
        if base.lower().startswith("the "):
            variants.append(base[4:].strip())

        for marker in ["series", "saga", "chronicles", "trilogy", "novels", "files"]:
            if marker in base.lower():
                root = re.split(rf"\b{marker}\b", base, flags=re.IGNORECASE)[0].strip(" -,:;")
                if root:
                    variants.append(root)
                    if root.lower().startswith("the "):
                        variants.append(root[4:].strip())

        deduped: list[str] = []
        seen: set[str] = set()
        for variant in variants:
            key = variant.lower()
            if key in seen or not variant:
                continue
            seen.add(key)
            deduped.append(variant)
        return deduped

    def _series_title_variants(self, series_name: str) -> list[str]:
        base_variants = self._series_variants(series_name)
        if not base_variants:
            return []

        labels = list(base_variants)
        for base in base_variants:
            if ":" in base:
                prefix = base.split(":", 1)[0].strip(" -,:;")
                if prefix:
                    labels.append(prefix)

        deduped: list[str] = []
        seen: set[str] = set()
        for label in labels:
            key = label.lower()
            if key in seen or not label:
                continue
            seen.add(key)
            deduped.append(label)
        return deduped

    def _book_number_variants(self, book_number: int | float) -> tuple[str, str]:
        try:
            numeric_value = float(book_number)
        except (TypeError, ValueError):
            numeric_value = 0.0

        if numeric_value.is_integer():
            numeric = str(int(numeric_value))
            words = {
                0: "Zero",
                1: "One",
                2: "Two",
                3: "Three",
                4: "Four",
                5: "Five",
                6: "Six",
                7: "Seven",
                8: "Eight",
                9: "Nine",
                10: "Ten",
                11: "Eleven",
                12: "Twelve",
                13: "Thirteen",
                14: "Fourteen",
                15: "Fifteen",
                16: "Sixteen",
                17: "Seventeen",
                18: "Eighteen",
                19: "Nineteen",
                20: "Twenty",
            }
            word = words.get(int(numeric_value), numeric)
        else:
            numeric = re.sub(r"0+$", "", f"{numeric_value}").rstrip(".")
            word = numeric

        return numeric, word

    def _title_search_variants(self, series_name: str, book_number: int | float) -> dict[str, list[str]]:
        labels = self._series_title_variants(series_name)
        if not labels:
            return {"exact": [], "normalized": [], "fuzzy": []}

        full_label = labels[0]
        root_label = labels[1] if len(labels) > 1 else full_label
        numeric, word = self._book_number_variants(book_number)

        exact_variants = [
            f"{full_label}: (Book {word})",
            f"{full_label} (Book {word})",
            f"{full_label}: Book {word}",
            f"{full_label} Book {word}",
            f"{full_label} #{numeric}",
            f"{full_label} {word}",
            f"{root_label} Book {numeric}",
            f"{root_label}: Book {word}",
            f"{root_label} #{numeric}",
            f"{root_label} Book {word}",
            f"{full_label} {numeric}",
        ]

        normalized_variants = [
            f"{full_label} Book {numeric}",
            f"{full_label} Book {word}",
            f"{full_label} {numeric}",
            f"{full_label} {word}",
            f"{root_label} Book {numeric}",
            f"{root_label} Book {word}",
            f"{root_label} {numeric}",
            f"{root_label} {word}",
        ]

        fuzzy_variants = [
            full_label,
            root_label,
            f"{full_label} {numeric}",
            f"{root_label} {numeric}",
            f"{full_label} {word}",
            f"{root_label} {word}",
        ]

        def dedupe(values: list[str]) -> list[str]:
            ordered: list[str] = []
            seen: set[str] = set()
            for value in values:
                key = value.lower().strip()
                if key in seen or not key:
                    continue
                seen.add(key)
                ordered.append(value)
            return ordered

        return {
            "exact": dedupe(exact_variants),
            "normalized": dedupe(normalized_variants),
            "fuzzy": dedupe(fuzzy_variants),
        }

    def _build_discovery_passes(self, series_name: str, book_number: int, known_authors: list[str]) -> list[dict]:
        variants = self._title_search_variants(series_name, book_number)
        primary_author = known_authors[0] if known_authors else None

        passes: list[dict] = []
        for variant in variants["exact"]:
            passes.append(
                {
                    "stage": "web_read",
                    "provider": "web_read",
                    "query": variant,
                    "title_variants": variants["exact"],
                    "author": primary_author,
                }
            )

        # Catalog API fallback only; not primary discovery.
        for variant in variants["normalized"][:3]:
            quoted = f'"{variant}"'
            passes.append(
                {
                    "stage": "catalog_fallback",
                    "provider": "google_books",
                    "query": f'intitle:{quoted}',
                    "author": primary_author,
                }
            )
            passes.append(
                {
                    "stage": "catalog_fallback",
                    "provider": "openlibrary",
                    "query": variant,
                    "author": primary_author,
                }
            )
            passes.append(
                {
                    "stage": "catalog_fallback",
                    "provider": "goodreads",
                    "query": variant,
                    "author": primary_author,
                }
            )

        deduped: list[dict] = []
        seen_keys: set[tuple[str, str]] = set()
        for item in passes:
            key = (item["provider"], item["query"].strip().lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append(item)
        return deduped

    def _run_provider_query(self, provider: str, query: str, author: str | None, title_variants: list[str] | None = None) -> list[dict]:
        if provider == "web_read":
            return search_web_read_candidates(query, title_variants or [query], author, 10)
        if provider == "google_books":
            return search_google_books(query, author, 8)
        if provider == "openlibrary":
            return search_openlibrary(query, author, 8)
        if provider == "goodreads":
            return search_goodreads_api(query, author, 8)
        return []

    def _source_label(self, source: str | None) -> str:
        source_key = str(source or "").strip().lower()
        if source_key == "google_books":
            return "GoogleBooks"
        if source_key == "google_books_html":
            return "GoogleBooks"
        if source_key == "openlibrary":
            return "OpenLibrary"
        if source_key == "openlibrary_html":
            return "OpenLibrary"
        if source_key == "amazon":
            return "Amazon"
        if source_key == "amazon_html":
            return "Amazon"
        if source_key == "fantastic_fiction":
            return "FantasticFiction"
        if source_key == "serpapi":
            return "SerpAPI"
        if source_key in {"otherwebsources", "other_web_sources"}:
            return "OtherWebSources"
        if source_key == "web_read":
            return "OtherWebSources"
        if source_key == "author_site":
            return "AuthorSite"
        if source_key == "publisher":
            return "Publisher"
        if source_key == "book_database":
            return "BookDatabase"
        if source_key == "goodreads":
            return "Goodreads"
        if source_key == "goodreads_html":
            return "Goodreads"
        return source_key or "unknown"

    def _extract_numeric_mentions(self, title: str) -> set[int]:
        values = {int(match) for match in re.findall(r"\b(?:book|volume|vol\.?|#)\s*(\d+)\b", title, flags=re.IGNORECASE)}
        values.update(int(match) for match in re.findall(r"\((\d+)\)", title))
        return values

    def _normalize_provider_record(self, record: dict, fallback_source: str) -> dict:
        source = str(record.get("source") or fallback_source)
        return {
            **record,
            "title": record.get("title"),
            "author": record.get("author"),
            "series_name": record.get("series_name"),
            "series_position": record.get("series_position"),
            "description": record.get("description"),
            "cover_image": record.get("cover_image"),
            "publication_date": record.get("publication_date") or record.get("year"),
            "year": record.get("year") or record.get("publication_date"),
            "source": source,
            "isbn": record.get("isbn"),
            "edition": record.get("edition"),
        }

    def _normalized_identity_key(self, result: dict) -> tuple[str, str, str]:
        title = self._normalize_text(result.get("title"))
        author = self._normalize_text(result.get("author"))
        number = str(result.get("series_position") or "")
        return title, author, number

    def _publication_date_value(self, result: dict) -> date | None:
        raw = result.get("publication_date") or result.get("year")
        if not raw:
            return None
        try:
            parsed = parse_publication_date(str(raw))
        except Exception:
            parsed = None
        return parsed

    def _result_domain(self, result: dict) -> str:
        source_url = str(result.get("source_url") or "").strip()
        return (urlparse(source_url).netloc or "").lower()

    def _extract_root_domain(self, domain: str) -> str:
        cleaned = str(domain or "").strip().lower()
        if not cleaned:
            return ""
        labels = [label for label in cleaned.split(".") if label]
        if len(labels) < 2:
            return cleaned

        second_level_markers = {"co", "com", "org", "net", "gov", "ac"}
        country_tlds = {"uk", "au", "nz", "za", "jp", "in"}
        if len(labels) >= 3 and labels[-1] in country_tlds and labels[-2] in second_level_markers:
            return labels[-3]
        return labels[-2]

    def _is_credible_source(self, result: dict) -> bool:
        source_key = str(result.get("source") or "").strip().lower()
        if source_key in {
            "amazon",
            "amazon_html",
            "fantastic_fiction",
            "google_books",
            "google_books_html",
            "openlibrary",
            "openlibrary_html",
            "goodreads",
            "goodreads_html",
            "publisher",
            "book_database",
            "author_site",
        }:
            return True
        domain = self._result_domain(result)
        return any(domain.endswith(suffix) for suffix in self.CREDIBLE_DOMAIN_SUFFIXES)

    def _metadata_corroboration_key(self, result: dict) -> tuple[str, str, str, str, str]:
        title = self._normalize_text(result.get("title"))
        author = self._normalize_text(result.get("author"))
        series_name = self._normalize_text(result.get("series_name"))
        series_position = ""
        if result.get("series_position") is not None:
            try:
                series_position = str(float(result.get("series_position")))
            except (TypeError, ValueError):
                series_position = ""

        publication_date = ""
        parsed_pub_date = self._publication_date_value(result)
        if parsed_pub_date is not None:
            publication_date = parsed_pub_date.isoformat()

        return title, author, series_name, series_position, publication_date

    def _is_unknown_domain_keyword_blocked(self, result: dict) -> bool:
        haystack = " ".join(
            [
                str(result.get("title") or ""),
                str(result.get("author") or ""),
                str(result.get("series_name") or ""),
                str(result.get("description") or ""),
                str(result.get("source_url") or ""),
            ]
        ).lower()
        return any(keyword in haystack for keyword in self.UNKNOWN_DOMAIN_BLOCKLIST_KEYWORDS)

    def _is_unknown_domain_network_blocked(self, domain: str) -> bool:
        cleaned = str(domain or "").lower()
        return any(token in cleaned for token in self.UNKNOWN_DOMAIN_BLOCKLIST_NETWORK_TOKENS)

    def _corroboration_count(self, result: dict, corroboration_map: dict[tuple[str, str, str, str, str], set[str]]) -> int:
        key = self._metadata_corroboration_key(result)
        return len(corroboration_map.get(key) or set())

    def _publication_date_filter(self, result: dict, *, series_complete: bool) -> dict:
        parsed_pub_date = self._publication_date_value(result)
        date_missing = parsed_pub_date is None
        date_future = bool(parsed_pub_date and parsed_pub_date > date.today())

        if date_missing:
            return {
                "series_complete": bool(series_complete),
                "date_future": False,
                "date_missing": True,
                "accepted": False,
                "reason": "publication_date_missing",
            }

        if bool(series_complete) and date_future:
            return {
                "series_complete": True,
                "date_future": True,
                "date_missing": False,
                "accepted": False,
                "reason": "publication_date_future_on_complete_series",
            }

        if date_future:
            return {
                "series_complete": False,
                "date_future": True,
                "date_missing": False,
                "accepted": True,
                "reason": "future_date_allowed_series_not_complete",
            }

        return {
            "series_complete": bool(series_complete),
            "date_future": False,
            "date_missing": False,
            "accepted": True,
            "reason": "publication_date_valid",
        }

    def _passes_false_positive_filters(
        self,
        result: dict,
        *,
        series_complete: bool,
        known_series_max: int | None,
        corroboration_map: dict[tuple[str, str, str, str, str], set[str]],
    ) -> tuple[bool, list[str]]:
        failures: list[str] = []
        publication_filter = self._publication_date_filter(result, series_complete=series_complete)
        if publication_filter["date_missing"]:
            failures.append("publication_date_missing")
        elif publication_filter["date_future"] and bool(series_complete):
            failures.append("publication_date_future")

        is_credible = self._is_credible_source(result)
        domain = self._result_domain(result)
        root_domain = self._extract_root_domain(domain)
        corroboration = self._corroboration_count(result, corroboration_map)

        if not is_credible and bool(series_complete):
            failures.append("unknown_domain_on_complete_series")

        if not is_credible and self._is_unknown_domain_network_blocked(domain):
            failures.append("unknown_domain_network_blocked")

        if not is_credible and self._is_unknown_domain_keyword_blocked(result):
            failures.append("unknown_domain_keyword_blocked")

        if not is_credible and not root_domain:
            failures.append("unknown_domain_missing_root")

        if not is_credible and corroboration < 2:
            failures.append("source_not_credible")

        if known_series_max is not None:
            try:
                position = float(result.get("series_position")) if result.get("series_position") is not None else None
            except (TypeError, ValueError):
                position = None
            if position is not None and position > float(known_series_max) and corroboration < 2:
                failures.append("series_number_speculative")

        return len(failures) == 0, failures

    def _apply_false_positive_filter(
        self,
        ranked_candidates: list[dict],
        *,
        series_complete: bool,
        known_series_max: int | None,
        rejection_counts: dict[str, int],
    ) -> list[dict]:
        corroboration_map: dict[tuple[str, str, str, str, str], set[str]] = {}
        for item in ranked_candidates:
            if self._is_credible_source(item):
                continue
            key = self._metadata_corroboration_key(item)
            root_domain = self._extract_root_domain(self._result_domain(item))
            if not root_domain:
                continue
            corroboration_map.setdefault(key, set()).add(root_domain)

        filtered_candidates: list[dict] = []
        for item in ranked_candidates:
            if float(item.get("_score") or 0.0) < 35.0:
                continue
            allowed, failures = self._passes_false_positive_filters(
                item,
                series_complete=series_complete,
                known_series_max=known_series_max,
                corroboration_map=corroboration_map,
            )
            if allowed:
                filtered_candidates.append(item)
            else:
                for failure in failures:
                    rejection_counts[failure] = rejection_counts.get(failure, 0) + 1

        return filtered_candidates

    def _score_candidate(self, result: dict, series_name: str, book_number: int, known_authors: list[str]) -> tuple[float, list[str]]:
        title = str(result.get("title") or "")
        title_norm = self._normalize_text(title)
        series_norm = self._normalize_text(series_name)
        title_variants = [self._normalize_text(item) for item in self._title_search_variants(series_name, book_number)["exact"]]
        result_author = str(result.get("author") or "").strip()

        score = 0.0
        reasons: list[str] = []

        fuzzy_scores = [SequenceMatcher(None, title_norm, variant).ratio() for variant in title_variants if variant]
        best_fuzzy = max(fuzzy_scores) if fuzzy_scores else 0.0
        if best_fuzzy >= self.MIN_FUZZY_SCORE:
            score += best_fuzzy * 40.0
            reasons.append(f"fuzzy:{best_fuzzy:.2f}")

        if series_norm and series_norm in title_norm:
            score += 25.0
            reasons.append("series_phrase")

        mentions = self._extract_numeric_mentions(title)
        if book_number in mentions:
            score += 35.0
            reasons.append("book_number_exact")
        elif mentions:
            score -= 30.0
            reasons.append("book_number_conflict")

        series_position = result.get("series_position")
        if series_position is not None:
            try:
                if float(series_position) == float(book_number):
                    score += 25.0
                    reasons.append("series_position_exact")
                else:
                    score -= 25.0
                    reasons.append("series_position_conflict")
            except (TypeError, ValueError):
                pass

        if known_authors and self._author_matches(result_author, known_authors):
            score += 25.0
            reasons.append("author_match")
        elif known_authors and result_author:
            score -= 20.0
            reasons.append("author_mismatch")

        source = str(result.get("source") or "")
        if source == "google_books":
            score += 6.0
        elif source == "openlibrary":
            score += 5.0
        elif source == "amazon":
            score += 6.0
        elif source == "fantastic_fiction":
            score += 6.0
        elif source == "serpapi":
            score += 2.0
        elif source in {"otherwebsources", "other_web_sources", "web_read"}:
            score += 6.0
        elif source in {"author_site", "publisher", "book_database", "goodreads"}:
            score += 5.0

        return score, reasons

    def _fuse_metadata(self, ranked_candidates: list[dict], book_number: int, series_name: str) -> dict | None:
        if not ranked_candidates:
            return None

        anchor = ranked_candidates[0]
        fields = [
            "title",
            "author",
            "year",
            "publication_date",
            "description",
            "cover_image",
            "source_url",
            "series_name",
            "series_position",
            "source",
        ]
        fused = {}
        for field in fields:
            value = None
            for candidate in ranked_candidates:
                candidate_value = candidate.get(field)
                if candidate_value not in (None, "", []):
                    value = candidate_value
                    break
            fused[field] = value

        if not fused.get("title"):
            fused["title"] = anchor.get("title") or f"Book {book_number}"
        if fused.get("series_position") is None:
            fused["series_position"] = book_number
        if fused.get("series_name") is None:
            fused["series_name"] = series_name

        fused["fusion_sources"] = [
            {
                "source": item.get("source"),
                "query": item.get("_query"),
                "score": item.get("_score"),
            }
            for item in ranked_candidates[:5]
        ]
        return fused

    def _empty_discovery_result(self, series_name: str, book_number: int, reason: str, error: str | None = None) -> dict:
        diagnostics = {
            "selected_stage": "none",
            "provider_counts": {},
            "rejection_counts": {},
            "stages": [],
            "provider_order": [],
            "accepted_total": 0,
            "top_score": 0.0,
            "timed_out": False,
            "elapsed_seconds": 0.0,
            "passes_completed": 0,
            "sources_used": [],
            "current_pass": "none",
            "publication_date_filter": {
                "series_complete": False,
                "date_future": False,
                "date_missing": True,
                "accepted": False,
                "reason": "no_candidates",
            },
        }
        if error:
            diagnostics["provider_error"] = error
        return {
            "query": f"{series_name} Book {book_number}",
            "results": [],
            "diagnostics": diagnostics,
            "passes_completed": 0,
            "sources_used": [],
            "missing_books": [book_number],
            "status": "no_hits",
            "reason": reason,
            "discovery_engine": "agent_v2",
            "discovery_mode": "error",
            "provider_attempt_order": [],
            "provider_attempts": [],
            "provider_selected": None,
            "final_reason": reason,
            "agent_pipeline": True,
        }

    def _discover_with_fallback(
        self,
        series_name: str,
        series_id: int | None,
        book_number: int,
        known_authors: list[str],
        known_series_max: int | None,
        series_complete: bool,
    ) -> dict:
        try:
            return self.discover(
                series_name,
                book_number,
                known_authors,
                known_series_max=known_series_max,
                series_complete=series_complete,
                series_id=series_id,
            )
        except Exception as exc:
            logger.exception("[DISCOVERY] Provider pass failed for series=%s book=%s", series_name, book_number)
            return self._empty_discovery_result(series_name, book_number, "provider-exception", str(exc))

    def discover(
        self,
        series_name: str,
        book_number: int | None = None,
        author: str | list[str] | None = None,
        known_series_max: int | None = None,
        series_complete: bool = False,
        series_id: int | None = None,
    ) -> dict:
        book_number = int(book_number or 1)
        known_authors = self._resolve_known_authors(author)
        logger.info("[DISCOVERY] Using SeriesIntelligenceAgent (agent_v2)")

        variant_groups = self._title_search_variants(series_name, book_number)
        title_variants = self._all_title_variants(series_name, book_number)
        direct_providers = [
            AmazonProvider(),
            FantasticFictionProvider(),
            AuthorSiteProvider(),
            PublisherProvider(),
            BookDatabaseProvider(),
        ]
        html_providers = [
            AmazonProvider(),
            FantasticFictionProvider(),
            AuthorSiteProvider(),
            PublisherProvider(),
            BookDatabaseProvider(),
            WebReadProvider(),
        ]
        print(f"[DISCOVERY_DEBUG] HTML_PROVIDER_LIST providers={[provider.name for provider in html_providers]}")
        pass_plan: list[tuple[str, list[str], list[Provider]]] = [
            ("exact_match", variant_groups.get("exact") or title_variants, direct_providers),
            ("canonical_match", variant_groups.get("normalized") or title_variants, direct_providers),
            ("fuzzy_match", variant_groups.get("fuzzy") or title_variants, [*direct_providers, WebReadProvider()]),
            ("fallback_pass", (variant_groups.get("normalized") or variant_groups.get("fuzzy") or title_variants), [CatalogFallbackProvider()]),
            ("html_discovery_pass", (variant_groups.get("normalized") or title_variants), html_providers),
        ]

        provider_attempt_order: list[str] = []
        for _, _, providers in pass_plan:
            for provider in providers:
                provider_name = provider.__class__.__name__
                if provider_name not in provider_attempt_order:
                    provider_attempt_order.append(provider_name)

        provider_counts = {
            "amazon": 0,
            "fantastic_fiction": 0,
            "author_site": 0,
            "publisher": 0,
            "book_database": 0,
            "web_read": 0,
            "catalog_fallback": 0,
            "google_books_html": 0,
            "goodreads_html": 0,
            "openlibrary_html": 0,
            "amazon_html": 0,
            "google": 0,
            "openlibrary": 0,
            "serpapi": 0,
            "goodreads": 0,
        }
        rejection_counts: dict[str, int] = {}
        stages: list[dict] = []
        all_candidates: list[dict] = []
        seen_keys: set[tuple[str, str | None, str]] = set()
        sources_used: set[str] = set()
        started_at = monotonic()
        timed_out = False
        selected_discovery_mode = "exact_match"
        provider_selected: str | None = None

        provider_attempts_by_name: dict[str, dict] = {}
        for provider_name in provider_attempt_order:
            if provider_name == "WebReadProvider":
                provider_attempts_by_name[provider_name] = {
                    "provider": provider_name,
                    "attempted": False,
                    "matched": False,
                    "reason": "not-attempted",
                    "search_engines": ["google", "bing", "duckduckgo"],
                    "organic_results": [],
                    "urls_fetched": [],
                    "html_titles": [],
                    "html_authors": [],
                    "variant_matches": [],
                }
            elif provider_name == "CatalogFallbackProvider":
                provider_attempts_by_name[provider_name] = {
                    "provider": provider_name,
                    "attempted": False,
                    "matched": False,
                    "reason": "not-attempted",
                }
            elif provider_name in {"GoogleBooksHtmlProvider", "GoodreadsHtmlProvider", "OpenLibraryHtmlProvider", "AmazonHtmlProvider"}:
                provider_attempts_by_name[provider_name] = {
                    "provider": provider_name,
                    "attempted": False,
                    "matched": False,
                    "reason": "not-attempted",
                    "urls_checked": [],
                    "html_title": None,
                    "html_author": None,
                }
            else:
                provider_attempts_by_name[provider_name] = {
                    "provider": provider_name,
                    "attempted": False,
                    "matched": False,
                    "reason": "not-attempted",
                    "urls_checked": [],
                    "html_title": None,
                    "html_author": None,
                }

        pass_index = 0
        stop_discovery = False
        for pass_name, pass_title_variants, providers in pass_plan:
            if pass_name == "html_discovery_pass":
                print(
                    f"[DISCOVERY_DEBUG] HTML_GUARD_VALUE "
                    f"stop_discovery={stop_discovery} "
                    f"series_id={series_id} book_number={book_number}"
                )
                print(
                    f"[DISCOVERY_DEBUG] HTML_PROVIDER_LOOP_GUARD "
                    f"condition='stop_discovery' value={stop_discovery} "
                    f"series_id={series_id} book_number={book_number}"
                )
                print(
                    f"[DISCOVERY_DEBUG] HTML_GUARD_CHECK "
                    f"condition='if stop_discovery:' result={bool(stop_discovery)} "
                    f"series_id={series_id} book_number={book_number}"
                )
            if stop_discovery and pass_name != "html_discovery_pass":
                if pass_name == "html_discovery_pass":
                    print(
                        f"[DISCOVERY_DEBUG] HTML_GUARD_BLOCKED "
                        f"series_id={series_id} book_number={book_number}"
                    )
                break
            if pass_name == "html_discovery_pass":
                print(
                    f"[DISCOVERY_DEBUG] HTML_PROVIDER_LOOP_ENTRY "
                    f"series_id={series_id} book_number={book_number}"
                )
            for provider in providers:
                if pass_name == "html_discovery_pass":
                    print(
                        f"[DISCOVERY_DEBUG] HTML_PROVIDER_LOOP provider_name={provider.name} "
                        f"provider_type={getattr(provider, 'type', None)} series_id={series_id} book_number={book_number}"
                    )
                    print(
                        f"[DISCOVERY_DEBUG] HTML_PROVIDER_ATTRIBUTES "
                        f"name={provider.name} "
                        f"type={getattr(provider, 'type', None)} "
                        f"disabled={getattr(provider, 'disabled', None)} "
                        f"supports_html={getattr(provider, 'supports_html', None)} "
                        f"timeout_exceeded={getattr(provider, 'timeout_exceeded', None)} "
                        f"html_enabled={getattr(provider, 'html_enabled', None)}"
                    )
                pass_index += 1
                if monotonic() - started_at >= self.DISCOVERY_TIME_BUDGET_SECONDS:
                    if pass_name == "html_discovery_pass":
                        print(
                            f"[DISCOVERY_DEBUG] HTML_PROVIDER_SKIPPED provider_name={provider.name} "
                            f"reason='monotonic() - started_at >= self.DISCOVERY_TIME_BUDGET_SECONDS' "
                            f"series_id={series_id} book_number={book_number}"
                        )
                    timed_out = True
                    stop_discovery = True
                    break

                logger.info("[DISCOVERY] Pass %s: %s/%s (%s)", pass_index, pass_name, provider.name, datetime.utcnow().isoformat())
                if pass_name == "html_discovery_pass":
                    print(f"[DISCOVERY_DEBUG] HTML_SEARCH_CALL series_id={series_id} book_number={book_number}")
                try:
                    raw_results, attempt_info = provider.search(pass_title_variants, known_authors)
                except Exception as exc:
                    logger.exception("[DISCOVERY] Provider %s failed in %s", provider.__class__.__name__, pass_name)
                    raw_results = []
                    attempt_info = {
                        "provider": provider.__class__.__name__,
                        "attempted": True,
                        "matched": False,
                        "reason": f"exception:{exc.__class__.__name__}",
                        "urls_checked": [],
                    }
                if pass_name == "html_discovery_pass":
                    print(f"[DISCOVERY_DEBUG] HTML_SEARCH_RETURN series_id={series_id} book_number={book_number}")
                provider_attempts_by_name[attempt_info["provider"]] = attempt_info
                provider_counts[provider.name] = provider_counts.get(provider.name, 0) + len(raw_results)
                logger.info("[DISCOVERY] Source: %s", self._source_label(provider.name))

                # Keep provider-level counts that existed before.
                for result in raw_results:
                    source_key = str(result.get("source") or "").lower()
                    if source_key == "google_books":
                        provider_counts["google"] += 1
                    elif source_key == "openlibrary":
                        provider_counts["openlibrary"] += 1
                    elif source_key == "serpapi":
                        provider_counts["serpapi"] += 1
                    elif source_key == "goodreads":
                        provider_counts["goodreads"] += 1

                accepted_count = 0
                first_query = pass_title_variants[0] if pass_title_variants else ""
                for result in raw_results:
                    result = self._normalize_provider_record(result, provider.name)
                    key = (
                        str(result.get("title") or "").strip().lower(),
                        str(result.get("author") or "").strip().lower() or None,
                        self._result_domain(result) or self._source_label(result.get("source") or provider.name),
                    )
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)

                    score, reasons = self._score_candidate(result, series_name, book_number, known_authors)
                    if "author_mismatch" in reasons:
                        rejection_counts["author_mismatch"] = rejection_counts.get("author_mismatch", 0) + 1
                    if "book_number_conflict" in reasons or "series_position_conflict" in reasons:
                        rejection_counts["number_mismatch"] = rejection_counts.get("number_mismatch", 0) + 1
                    if score < 35.0:
                        rejection_counts["low_confidence"] = rejection_counts.get("low_confidence", 0) + 1

                    enriched = {
                        **result,
                        "_provider": provider.name,
                        "_query": first_query,
                        "_score": score,
                        "_score_reasons": reasons,
                    }
                    all_candidates.append(enriched)
                    sources_used.add(self._source_label(result.get("source") or provider.name))
                    if score >= 35.0:
                        accepted_count += 1

                stages.append(
                    {
                        "stage": pass_name,
                        "provider": provider.name,
                        "query": first_query,
                        "raw_count": len(raw_results),
                        "accepted_count": accepted_count,
                    }
                )

                if accepted_count > 0:
                    selected_discovery_mode = pass_name
                    provider_selected = provider.__class__.__name__
            if pass_name == "html_discovery_pass":
                print(
                    f"[DISCOVERY_DEBUG] HTML_PROVIDER_LOOP_EXIT "
                    f"series_id={series_id} book_number={book_number}"
                )

        ranked = sorted(all_candidates, key=lambda item: float(item.get("_score") or 0.0), reverse=True)
        filtered_candidates = self._apply_false_positive_filter(
            ranked,
            series_complete=series_complete,
            known_series_max=known_series_max,
            rejection_counts=rejection_counts,
        )

        publication_date_filter = {
            "series_complete": bool(series_complete),
            "date_future": False,
            "date_missing": True,
            "accepted": False,
            "reason": "no_candidates",
        }
        if filtered_candidates:
            publication_date_filter = self._publication_date_filter(filtered_candidates[0], series_complete=series_complete)
        elif ranked:
            publication_date_filter = self._publication_date_filter(ranked[0], series_complete=series_complete)

        if filtered_candidates:
            first = filtered_candidates[0]
            provider_selected = {
                "amazon": "AmazonProvider",
                "fantastic_fiction": "FantasticFictionProvider",
                "author_site": "AuthorSiteProvider",
                "publisher": "PublisherProvider",
                "book_database": "BookDatabaseProvider",
                "web_read": "WebReadProvider",
                "catalog_fallback": "CatalogFallbackProvider",
                "google_books_html": "GoogleBooksHtmlProvider",
                "goodreads_html": "GoodreadsHtmlProvider",
                "openlibrary_html": "OpenLibraryHtmlProvider",
                "amazon_html": "AmazonHtmlProvider",
            }.get(str(first.get("_provider") or ""), provider_selected)
            selected_discovery_mode = next(
                (stage["stage"] for stage in stages if int(stage.get("accepted_count") or 0) > 0),
                selected_discovery_mode,
            )

        fused = self._fuse_metadata(filtered_candidates, book_number, series_name)
        response_results = [fused] if fused else []

        diagnostics = {
            "selected_stage": selected_discovery_mode if filtered_candidates else "none",
            "provider_counts": provider_counts,
            "rejection_counts": rejection_counts,
            "stages": stages,
            "provider_order": provider_attempt_order,
            "accepted_total": len(filtered_candidates),
            "top_score": float(ranked[0].get("_score") or 0.0) if ranked else 0.0,
            "timed_out": timed_out,
            "elapsed_seconds": round(monotonic() - started_at, 2),
            "passes_completed": len(stages),
            "sources_used": sorted(sources_used),
            "current_pass": stages[-1]["stage"] if stages else "none",
            "false_positive_filter": {
                "publication_date_check": True,
                "source_credibility_check": True,
                "series_number_check": True,
            },
            "publication_date_filter": publication_date_filter,
        }
        provider_attempts = [provider_attempts_by_name[name] for name in provider_attempt_order]
        logger.info("[DISCOVERY] Status: %s", "complete" if fused else "no_hits")

        completed_series_no_hits = bool(series_complete) and known_series_max is not None and not response_results
        no_hit_discovery_mode = "complete" if completed_series_no_hits else (selected_discovery_mode if stages else "direct")

        # Graceful failure: return empty results with diagnostics instead of throwing.
        if not response_results:
            return {
                "query": title_variants[0] if title_variants else "",
                "results": response_results,
                "diagnostics": diagnostics,
                "passes_completed": len(stages),
                "sources_used": sorted(sources_used),
                "missing_books": [book_number],
                "status": "no_hits",
                "reason": "no-hit-after-all-passes",
                "discovery_engine": "agent_v2",
                "discovery_mode": no_hit_discovery_mode,
                "provider_attempt_order": provider_attempt_order,
                "provider_attempts": provider_attempts,
                "provider_selected": None,
                "final_reason": "no-hit-after-all-providers",
                "agent_pipeline": True,
            }

        return {
            "query": fused.get("_query") or (title_variants[0] if title_variants else ""),
            "results": response_results,
            "diagnostics": diagnostics,
            "passes_completed": len(stages),
            "sources_used": sorted(sources_used),
            "missing_books": [],
            "status": "complete",
            "discovery_engine": "agent_v2",
            "discovery_mode": selected_discovery_mode,
            "provider_attempt_order": provider_attempt_order,
            "provider_attempts": provider_attempts,
            "provider_selected": provider_selected,
            "final_reason": None,
            "agent_pipeline": True,
        }

    def _diagnostic_reason(self, suggestion: dict) -> str | None:
        diagnostics = suggestion.get("diagnostics") or {}
        provider_counts = diagnostics.get("provider_counts") or {}
        rejection_counts = diagnostics.get("rejection_counts") or {}
        total_provider_results = sum(int(value or 0) for value in provider_counts.values())
        accepted_total = int(diagnostics.get("accepted_total") or 0)
        top_score = float(diagnostics.get("top_score") or 0.0)
        timed_out = bool(diagnostics.get("timed_out"))

        if timed_out:
            return "timed_out"
        if total_provider_results <= 0:
            return "no_provider_results"
        if accepted_total > 0:
            return None
        if top_score > 0 and top_score < 35.0:
            return "low_confidence"
        if int(rejection_counts.get("author_mismatch", 0)) > 0:
            return "author_filtered"
        if int(rejection_counts.get("missing_author", 0)) > 0:
            return "low_confidence"
        if sum(int(value or 0) for value in rejection_counts.values()) > 0:
            return "low_confidence"
        return None

    def _diagnostic_message(self, reason: str | None) -> str | None:
        if reason == "timed_out":
            return "Discovery timed out before confidence threshold was reached."
        if reason == "no_provider_results":
            return "No provider results were returned."
        if reason == "author_filtered":
            return "Results were found but rejected by author matching."
        if reason == "low_confidence":
            return "Results were found but not confident enough to auto-add."
        return None

    def _infer_book_number_from_title(self, title: str | None) -> float | None:
        if not title:
            return None

        text = str(title)
        match = re.search(r"#\s*(\d+(?:\.\d+)?)\b", text)
        if not match:
            match = re.search(r"\bbook\s+(\d+(?:\.\d+)?)\b", text, flags=re.IGNORECASE)
        if not match:
            return None

        try:
            return float(match.group(1))
        except ValueError:
            return None

    def _get_series(self, db: Session, series_id: int) -> Series | None:
        return db.query(Series).filter(Series.id == series_id).first()

    def _owned_books(self, db: Session, series_id: int) -> list[Book]:
        return (
            db.query(Book)
            .filter(Book.series_id == series_id)
            .filter(or_(Book.record_status.is_(None), Book.record_status != "deleted"))
            .all()
        )

    def _actual_owned_books(self, books: list[Book]) -> list[Book]:
        return [
            book
            for book in books
            if not bool(book.is_missing)
            and not bool(book.is_upcoming_auto)
            and not bool(book.is_upcoming_final)
            and str(book.record_status or "active") != "deleted"
        ]

    def _canonical_entries(self, db: Session, series_id: int) -> list[SeriesCanonicalEntry]:
        return (
            db.query(SeriesCanonicalEntry)
            .filter(SeriesCanonicalEntry.series_id == series_id)
            .order_by(SeriesCanonicalEntry.book_number.asc())
            .all()
        )

    def _book_number_value(self, book: Book) -> float | None:
        if book.book_number is not None:
            return float(book.book_number)
        if book.series_order is not None:
            return float(book.series_order)
        inferred = self._infer_book_number_from_title(book.title)
        if inferred is not None:
            return inferred
        return None

    def _highest_owned_book_number(self, books: list[Book]) -> float | None:
        values = [value for value in (self._book_number_value(book) for book in books) if value is not None]
        return max(values) if values else None

    def _split_author_names(self, value: str | None) -> list[str]:
        if not value:
            return []

        parts = [part.strip() for part in re.split(r"\s*(?:,|&|\band\b)\s*", value, flags=re.IGNORECASE)]
        authors = [part for part in parts if part]
        seen: set[str] = set()
        ordered: list[str] = []
        for author in authors:
            key = author.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(author)
        return ordered

    def _series_author_candidates(self, series: Series, books: list[Book]) -> list[str]:
        authors: list[str] = []

        for source_value in [series.author, *[book.author for book in books if book.author]]:
            authors.extend(self._split_author_names(source_value))

        seen: set[str] = set()
        ordered: list[str] = []
        for author in authors:
            key = author.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(author)
        return ordered

    def _canonical_author_candidates(self, series: Series, books: list[Book], canonical_entries: list[SeriesCanonicalEntry]) -> list[str]:
        authors = self._series_author_candidates(series, books)
        for entry in canonical_entries:
            if entry.canonical_author:
                authors.extend(self._split_author_names(entry.canonical_author))
            if isinstance(entry.author_aliases, list):
                for alias in entry.author_aliases:
                    authors.extend(self._split_author_names(str(alias)))

        seen: set[str] = set()
        ordered: list[str] = []
        for author in authors:
            key = author.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(author)
        return ordered

    def _resolve_known_authors(self, author: str | list[str] | None) -> list[str]:
        if isinstance(author, list):
            authors: list[str] = []
            for value in author:
                authors.extend(self._split_author_names(str(value)))
            return authors
        return self._split_author_names(author)

    def _author_matches(self, result_author: str | None, known_authors: list[str]) -> bool:
        if not result_author or not known_authors:
            return False

        result_lower = result_author.lower()
        result_tokens = [token.strip() for token in re.split(r"\s*(?:,|&|\band\b)\s*", result_lower) if token.strip()]
        flattened = {token for token in result_tokens}

        for author in known_authors:
            candidate = author.lower()
            if candidate in result_lower or result_lower in candidate:
                return True
            candidate_parts = [part for part in re.split(r"\s+", candidate) if len(part) > 1]
            if candidate_parts and all(part in result_lower for part in candidate_parts):
                return True
            if candidate in flattened:
                return True

        return False

    def _existing_numbers(self, books: list[Book]) -> set[float]:
        return {value for value in (self._book_number_value(book) for book in books) if value is not None}

    def _ordered_existing_numbers(self, books: list[Book]) -> list[int]:
        numbers = {
            int(value)
            for value in (self._book_number_value(book) for book in books)
            if value is not None and float(value).is_integer()
        }
        return sorted(numbers)

    def _missing_candidate_numbers(self, books: list[Book]) -> list[int]:
        highest_owned = self._highest_owned_book_number(books)
        if highest_owned is None:
            return []

        existing_numbers = self._existing_numbers(books)
        floor_highest = int(highest_owned)

        return [
            number
            for number in range(floor_highest - 1, 0, -1)
            if float(number) not in existing_numbers
        ]

    def _future_candidate_numbers(self, series: Series, books: list[Book]) -> list[int]:
        highest_owned = self._highest_owned_book_number(books)
        if highest_owned is None:
            return []

        floor_highest = int(highest_owned)
        max_ahead = self.FUTURE_SCAN_MAX_AHEAD

        return [floor_highest + offset for offset in range(1, max_ahead + 1)]

    def _candidate_numbers(self, series: Series, books: list[Book]) -> list[int]:
        future_candidates = self._future_candidate_numbers(series, books)
        missing_candidates = self._missing_candidate_numbers(books)
        # Fill known gaps before probing ahead for unreleased/unknown entries.
        return list(dict.fromkeys([*missing_candidates, *future_candidates]))

    def _find_existing_book(self, db: Session, series_id: int, book_number: float | int) -> Book | None:
        normalized_number = float(book_number)
        return (
            db.query(Book)
            .filter(Book.series_id == series_id)
            .filter(or_(Book.book_number == normalized_number, Book.series_order == normalized_number))
            .filter(or_(Book.record_status.is_(None), Book.record_status != "deleted"))
            .first()
        )

    def _validate_against_canonical(self, result: dict, canonical_entry: SeriesCanonicalEntry) -> bool:
        result_author = str(result.get("author") or "").strip()
        alias_pool = [canonical_entry.canonical_author or ""]
        if isinstance(canonical_entry.author_aliases, list):
            alias_pool.extend(str(alias) for alias in canonical_entry.author_aliases)
        alias_pool = [alias for alias in alias_pool if alias]
        if alias_pool and not self._author_matches(result_author, alias_pool):
            return False

        result_title = str(result.get("title") or "").strip().lower()
        canonical_title = str(canonical_entry.canonical_title or "").strip().lower()
        if canonical_title and result_title and canonical_title not in result_title and result_title not in canonical_title:
            return False

        position = result.get("series_position")
        if position is not None:
            try:
                if float(position) != float(canonical_entry.book_number):
                    return False
            except (TypeError, ValueError):
                return False

        return True

    def _sync_canonical_entry_book(
        self,
        db: Session,
        series: Series,
        canonical_entry: SeriesCanonicalEntry,
        books_by_number: dict[float, Book],
        known_authors: list[str],
        *,
        is_missing: bool,
        known_series_max: int | None,
        series_complete: bool,
    ) -> tuple[Book | None, dict]:
        suggestion = self._discover_with_fallback(
            series.name,
            series.id,
            int(canonical_entry.book_number),
            known_authors,
            known_series_max,
            series_complete,
        )
        diagnostics = suggestion.get("diagnostics") or {}
        results = suggestion.get("results") or []
        validated_result = None
        discarded_results = 0
        for result in results:
            if self._validate_against_canonical(result, canonical_entry):
                validated_result = result
                break
            discarded_results += 1

        metadata_source = validated_result or {
            "title": canonical_entry.canonical_title,
            "author": canonical_entry.canonical_author,
            "year": canonical_entry.publication_year,
        }
        normalized = normalize_book_metadata(
            metadata_source,
            series_name=series.name,
            book_number=canonical_entry.book_number,
        )

        canonical_author = canonical_entry.canonical_author or (known_authors[0] if known_authors else series.author) or "Unknown author"
        payload = {
            "title": str(canonical_entry.canonical_title or normalized.get("title") or f"Book {canonical_entry.book_number}").strip(),
            "author": canonical_author,
            "series_id": series.id,
            "series_order": canonical_entry.book_number,
            "book_number": float(canonical_entry.book_number),
            "publication_date": parse_publication_date(f"{canonical_entry.publication_year}-01-01") if canonical_entry.publication_year else None,
            "release_date": parse_publication_date(f"{canonical_entry.publication_year}-01-01") if canonical_entry.publication_year else None,
            "read_status": "unread" if is_missing else "upcoming",
            "is_read": False,
            "is_missing": is_missing,
            "is_upcoming_auto": not is_missing,
            "is_upcoming_final": not is_missing,
            "record_status": "active",
        }

        existing = books_by_number.get(float(canonical_entry.book_number)) or self._find_existing_book(db, series.id, canonical_entry.book_number)
        deleted_ghost_tombstone = self._find_deleted_ghost_tombstone(db, series.id, canonical_entry.book_number)
        if deleted_ghost_tombstone and not existing:
            return None, {
                "book_number": canonical_entry.book_number,
                "query": suggestion.get("query"),
                "diagnostics": diagnostics,
                "discarded_google_results": discarded_results,
                "validated_with_google": validated_result is not None,
                "reason": "ghost_tombstone_deleted",
                "was_added": False,
            }
        if existing:
            for key, value in payload.items():
                setattr(existing, key, value)
            db.commit()
            db.refresh(existing)
            book = existing
            was_added = False
        else:
            book = Book(**payload)
            db.add(book)
            db.commit()
            db.refresh(book)
            books_by_number[float(canonical_entry.book_number)] = book
            was_added = True

        return book, {
            "book_number": canonical_entry.book_number,
            "query": suggestion.get("query"),
            "diagnostics": diagnostics,
            "discarded_google_results": discarded_results,
            "validated_with_google": validated_result is not None,
            "reason": self._diagnostic_reason(suggestion) if validated_result is None else None,
            "was_added": was_added,
        }

    def _build_book_payload(self, series: Series, book_number: int, suggestion: dict, *, is_missing: bool, known_authors: list[str]) -> dict | None:
        results = suggestion.get("results") or []
        if not results:
            return None

        selected = results[0]
        if not self._author_matches(selected.get("author"), known_authors):
            return None

        normalized = normalize_book_metadata(selected, series_name=series.name, book_number=book_number)

        payload: dict = {
            key: value
            for key, value in normalized.items()
            if key in BOOK_COLUMN_KEYS
        }
        payload.update(
            {
                "series_id": series.id,
                "title": normalized.get("title") or f"Book {book_number}",
                "author": normalized.get("author") or series.author or "Unknown author",
                "series_order": book_number,
                "book_number": float(book_number),
                "is_missing": is_missing,
                "is_upcoming_auto": not is_missing,
                "is_upcoming_final": not is_missing,
                "is_read": False,
                "read_status": "unread",
                "record_status": "active",
            }
        )

        for date_key in ["publication_date", "release_date"]:
            raw_value = payload.get(date_key)
            if isinstance(raw_value, str):
                payload[date_key] = parse_publication_date(raw_value)

        publication_value = normalized.get("publication_date") or selected.get("year")
        if publication_value and "publication_date" not in payload:
            payload["publication_date"] = parse_publication_date(str(publication_value))

        return payload

    def _persist_book(self, db: Session, series: Series, book_number: int, suggestion: dict, *, is_missing: bool, known_authors: list[str]) -> Book | None:
        payload = self._build_book_payload(series, book_number, suggestion, is_missing=is_missing, known_authors=known_authors)
        if not payload:
            return None

        deleted_ghost_tombstone = self._find_deleted_ghost_tombstone(db, series.id, book_number)
        if deleted_ghost_tombstone is not None:
            return None

        existing = self._find_existing_book(db, series.id, book_number)
        if existing:
            for key, value in payload.items():
                if key in BOOK_COLUMN_KEYS and value is not None:
                    setattr(existing, key, value)
            db.commit()
            db.refresh(existing)
            return existing

        book = Book(**payload)
        db.add(book)
        db.commit()
        db.refresh(book)
        return book

    def _reconcile_from_trusted_series_sources(
        self,
        db: Session,
        series: Series,
        books: list[Book],
        *,
        known_authors: list[str],
    ) -> dict:
        return {
            "source": None,
            "source_attempts": [],
            "candidate_entries": [],
            "added_books": [],
            "trusted_total_books": None,
        }

    def _normalize_missing_books(self, missing_books: list[str] | list[int] | list[float] | None) -> list[str]:
        normalized: list[str] = []
        for value in missing_books or []:
            text = str(value).strip()
            if text:
                normalized.append(text)
        return normalized

    def _build_series_check_result(
        self,
        *,
        series: Series,
        highest_owned_book_number: float | None,
        candidate_numbers: list[int] | list[float],
        added_books: list[dict],
        candidate_diagnostics: list[dict] | None = None,
        trusted_series_reconciliation: dict | None = None,
        canonical_missing_entries: list[dict] | None = None,
        canonical_found_entries: list[dict] | None = None,
        canonical_upcoming_entries: list[dict] | None = None,
        canonical_rejected_entries: list[dict] | None = None,
        status: str = "no_hits",
        reason: str | None = None,
        discovery_mode: str | None = None,
    ) -> dict:
        found = bool(added_books)
        no_new_books = not found

        return {
            "series_id": series.id,
            "series_name": series.name,
            "highest_owned_book_number": highest_owned_book_number,
            "candidate_numbers": candidate_numbers,
            "added_count": len(added_books),
            "added_books": added_books,
            "found_books": added_books,
            "candidate_diagnostics": candidate_diagnostics or [],
            "trusted_series_reconciliation": trusted_series_reconciliation,
            "canonical_missing_entries": canonical_missing_entries,
            "canonical_found_entries": canonical_found_entries,
            "canonical_upcoming_entries": canonical_upcoming_entries,
            "canonical_rejected_entries": canonical_rejected_entries,
            "complete": True,
            "status": status,
            "no_new_books": no_new_books,
            "reason": reason,
            "discovery_mode": discovery_mode,
            "has_new_books": series.has_new_books,
            "series_state": series.series_state,
            "last_checked": series.last_checked,
            "next_unread_book_number": series.next_unread_book_number,
            "next_upcoming_book_number": series.next_upcoming_book_number,
            "missing_books": self._normalize_missing_books(series.missing_books),
            "found": found,
            "discovery_engine": "agent_v2",
            "agent_pipeline": True,
        }

    def run_series_check(
        self,
        db: Session,
        series_id: int,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> dict:
        series = self._get_series(db, series_id)
        if not series:
            return {
                "series_id": None,
                "found": False,
                "added_books": [],
                "found_books": [],
                "added_count": 0,
                "no_new_books": True,
                "status": "no_hits",
                "complete": True,
                "reason": "series-not-found",
                "has_new_books": False,
                "discovery_engine": "agent_v2",
                "agent_pipeline": True,
            }

        books = self._owned_books(db, series_id)
        canonical_entries = self._canonical_entries(db, series_id)
        series_complete = bool(series.is_finished) or str(series.series_status or "").strip().lower() in {"completed", "finished"}
        if canonical_entries:
            known_series_max = max(
                [int(float(entry.book_number)) for entry in canonical_entries if float(entry.book_number).is_integer()] or [0]
            )
            actual_owned_books = self._actual_owned_books(books)
            highest_owned = self._highest_owned_book_number(actual_owned_books)
            known_authors = self._canonical_author_candidates(series, books, canonical_entries)
            existing_numbers = self._existing_numbers(books)
            books_by_number = {float(value): book for book in books for value in [self._book_number_value(book)] if value is not None}

            found_entries: list[dict] = []
            missing_entries: list[dict] = []
            upcoming_entries: list[dict] = []
            added_books: list[dict] = []
            rejected_entries: list[dict] = []
            candidate_diagnostics: list[dict] = []

            if progress_callback is not None:
                progress_callback({
                    "total": len(canonical_entries),
                    "completed": 0,
                    "current_book_number": None,
                    "current_pass": "canonical-sync",
                })

            for index, entry in enumerate(canonical_entries, start=1):
                if progress_callback is not None:
                    progress_callback({
                        "total": len(canonical_entries),
                        "completed": index - 1,
                        "current_book_number": entry.book_number,
                        "current_pass": "canonical-sync",
                    })

                entry_payload = {
                    "book_number": entry.book_number,
                    "title": entry.canonical_title,
                    "author": entry.canonical_author,
                    "entry_type": entry.entry_type,
                    "is_fractional": entry.is_fractional,
                    "is_anthology": entry.is_anthology,
                }

                if float(entry.book_number) in existing_numbers:
                    found_entries.append(entry_payload)
                    continue

                is_missing = highest_owned is not None and float(entry.book_number) <= float(highest_owned)
                target_collection = missing_entries if is_missing else upcoming_entries
                target_collection.append(entry_payload)

                book, diagnostic = self._sync_canonical_entry_book(
                    db,
                    series,
                    entry,
                    books_by_number,
                    known_authors,
                    is_missing=is_missing,
                    known_series_max=known_series_max,
                    series_complete=series_complete,
                )
                candidate_diagnostics.append(diagnostic)
                if progress_callback is not None:
                    progress_callback({
                        "total": len(canonical_entries),
                        "completed": index - 1,
                        "current_book_number": entry.book_number,
                        "current_pass": (diagnostic.get("diagnostics") or {}).get("current_pass") or "canonical-sync",
                    })
                    for stage in (diagnostic.get("diagnostics") or {}).get("stages") or []:
                        progress_callback({
                            "total": len(canonical_entries),
                            "completed": index - 1,
                            "current_book_number": entry.book_number,
                            "current_pass": stage.get("stage") or "canonical-sync",
                        })
                if book and diagnostic.get("was_added"):
                    added_books.append(
                        {
                            "id": book.id,
                            "title": book.title,
                            "author": book.author,
                            "book_number": book.book_number,
                            "is_missing": book.is_missing,
                            "is_upcoming_auto": book.is_upcoming_auto,
                        }
                    )
                if diagnostic.get("reason"):
                    rejected_entries.append(
                        {
                            **entry_payload,
                            "reason": diagnostic.get("reason"),
                            "discarded_google_results": diagnostic.get("discarded_google_results", 0),
                        }
                    )

            intelligence = compute_series_intelligence_for_series(db, series_id) or {}
            series.total_books = max(intelligence.get("total_books", 0), series.total_books or 0, max((int(entry.book_number) for entry in canonical_entries if float(entry.book_number).is_integer()), default=0))
            series.is_finished = intelligence.get("is_series_finished", series.is_finished)
            series.series_status = "finished" if series.is_finished else "ongoing"
            series.missing_books = [str(entry["book_number"]) for entry in missing_entries]
            series.next_unread_book_number = intelligence.get("next_unread_book_number", series.next_unread_book_number)
            series.next_upcoming_book_number = next((entry["book_number"] for entry in upcoming_entries), None)
            series.last_checked = date.today()
            db.commit()
            db.refresh(series)

            recalculate_series_state_for_series(
                db,
                series_id,
                scan_result={
                    "added_count": len(added_books),
                    "added_books": added_books,
                    "canonical_missing_entries": missing_entries,
                    "canonical_upcoming_entries": upcoming_entries,
                    "canonical_rejected_entries": rejected_entries,
                    "found": bool(added_books),
                },
            )

            if progress_callback is not None:
                progress_callback({
                    "total": len(canonical_entries),
                    "completed": len(canonical_entries),
                    "current_book_number": None,
                    "current_pass": None,
                })

            self._strict_post_discovery_cleanup(
                db,
                series,
                known_authors=known_authors,
                known_series_max=known_series_max,
                series_complete=series_complete,
            )

            recount_series_aggregates_for_series(db, series.id)

            print(
                f"[DISCOVERY_DEBUG] canonical-sync series_id={series.id} "
                f"added={len(added_books)} missing={len(missing_entries)} upcoming={len(upcoming_entries)} "
                f"rejected={len(rejected_entries)} found={len(found_entries)}"
            )
            print(
                "[DISCOVERY_DEBUG] canonical-sync diagnostics "
                f"series_id={series.id} reasons={[
                    {
                        'book_number': item.get('book_number'),
                        'reason': item.get('reason'),
                        'selected_stage': (item.get('diagnostics') or {}).get('selected_stage'),
                        'accepted_total': (item.get('diagnostics') or {}).get('accepted_total'),
                        'top_score': (item.get('diagnostics') or {}).get('top_score'),
                    }
                    for item in candidate_diagnostics
                ]}"
            )

            return self._build_series_check_result(
                series=series,
                highest_owned_book_number=highest_owned,
                candidate_numbers=[entry.book_number for entry in canonical_entries],
                added_books=added_books,
                candidate_diagnostics=candidate_diagnostics,
                canonical_missing_entries=missing_entries,
                canonical_found_entries=found_entries,
                canonical_upcoming_entries=upcoming_entries,
                canonical_rejected_entries=rejected_entries,
                status="complete" if added_books else "no_hits",
                reason=None if added_books else "no-hit-after-all-passes",
                discovery_mode="canonical_sync",
            )

        highest_owned = self._highest_owned_book_number(books)
        known_authors = self._series_author_candidates(series, books)
        intelligence_snapshot = compute_series_intelligence_for_series(db, series_id) or {}
        if series_complete:
            known_series_max = self._completed_series_known_max(series, books, intelligence_snapshot)
        else:
            known_series_max_value = intelligence_snapshot.get("total_books") or series.total_books
            known_series_max = int(known_series_max_value) if known_series_max_value else None
        if not known_authors:
            intelligence = intelligence_snapshot
            series.last_checked = date.today()
            series.total_books = intelligence.get("total_books", series.total_books)
            series.is_finished = intelligence.get("is_series_finished", series.is_finished)
            series.series_status = "finished" if series.is_finished else "ongoing"
            series.missing_books = intelligence.get("missing_orders", series.missing_books)
            series.next_unread_book_number = intelligence.get("next_unread_book_number", series.next_unread_book_number)
            series.next_upcoming_book_number = intelligence.get("next_upcoming_book_number", series.next_upcoming_book_number)
            db.commit()
            db.refresh(series)
            recalculate_series_state_for_series(db, series_id)
            self._strict_post_discovery_cleanup(
                db,
                series,
                known_authors=known_authors,
                known_series_max=known_series_max,
                series_complete=series_complete,
            )
            recount_series_aggregates_for_series(db, series.id)
            return self._build_series_check_result(
                series=series,
                highest_owned_book_number=highest_owned,
                candidate_numbers=[],
                added_books=[],
                status="no_hits",
                reason="missing-author-context",
                discovery_mode="no_author_context",
            )

        candidate_numbers = self._candidate_numbers(series, books)
        added_books: list[dict] = []
        candidate_diagnostics: list[dict] = []
        trusted_reconciliation: dict | None = None
        future_candidates = set(self._future_candidate_numbers(series, books))
        future_empty_streak = 0
        future_scan_exhausted = False

        if progress_callback is not None:
            progress_callback(
                {
                    "total": len(candidate_numbers),
                    "completed": 0,
                    "current_book_number": None,
                }
            )

        for index, book_number in enumerate(candidate_numbers, start=1):
            if future_scan_exhausted and book_number in future_candidates:
                continue

            if progress_callback is not None:
                progress_callback(
                    {
                        "total": len(candidate_numbers),
                        "completed": index - 1,
                        "current_book_number": book_number,
                        "current_pass": "exact match",
                    }
                )

            is_missing = highest_owned is not None and book_number <= int(highest_owned)
            print(f"[DISCOVERY_DEBUG] HTML_STAGE_ENTER series_id={series.id} book_number={book_number}")
            suggestion = self._discover_with_fallback(
                series.name,
                series.id,
                book_number,
                known_authors,
                known_series_max,
                series_complete,
            )
            if progress_callback is not None:
                diagnostics = suggestion.get("diagnostics") or {}
                for stage in diagnostics.get("stages") or []:
                    progress_callback(
                        {
                            "total": len(candidate_numbers),
                            "completed": index - 1,
                            "current_book_number": book_number,
                            "current_pass": stage.get("stage") or diagnostics.get("current_pass") or "fuzzy-with-author-correlation",
                        }
                    )
                progress_callback(
                    {
                        "total": len(candidate_numbers),
                        "completed": index - 1,
                        "current_book_number": book_number,
                        "current_pass": diagnostics.get("current_pass") or "fuzzy-with-author-correlation",
                    }
                )
            diagnostic_reason = self._diagnostic_reason(suggestion)
            candidate_diagnostics.append(
                {
                    "book_number": book_number,
                    "reason": diagnostic_reason,
                    "message": self._diagnostic_message(diagnostic_reason),
                    "diagnostics": suggestion.get("diagnostics"),
                    "query": suggestion.get("query"),
                }
            )
            created = self._persist_book(db, series, book_number, suggestion, is_missing=is_missing, known_authors=known_authors)
            if not created:
                if book_number in future_candidates:
                    future_empty_streak += 1
                    if future_empty_streak >= self.FUTURE_SCAN_EMPTY_STREAK_STOP:
                        future_scan_exhausted = True
                continue

            if book_number in future_candidates:
                future_empty_streak = 0

            added_books.append(
                {
                    "id": created.id,
                    "title": created.title,
                    "author": created.author,
                    "book_number": created.book_number,
                    "is_missing": created.is_missing,
                    "is_upcoming_auto": created.is_upcoming_auto,
                }
            )

        if not added_books:
            trusted_reconciliation = self._reconcile_from_trusted_series_sources(
                db,
                series,
                self._owned_books(db, series_id),
                known_authors=known_authors,
            )
            added_books.extend(trusted_reconciliation.get("added_books") or [])

        if progress_callback is not None:
            progress_callback(
                {
                    "total": len(candidate_numbers),
                    "completed": len(candidate_numbers),
                    "current_book_number": None,
                }
            )

        intelligence = compute_series_intelligence_for_series(db, series_id) or {}
        series.total_books = intelligence.get("total_books", series.total_books)
        series.is_finished = intelligence.get("is_series_finished", series.is_finished)
        series.series_status = "finished" if series.is_finished else "ongoing"
        series.missing_books = intelligence.get("missing_orders", series.missing_books)
        series.next_unread_book_number = intelligence.get("next_unread_book_number", series.next_unread_book_number)
        series.next_upcoming_book_number = intelligence.get("next_upcoming_book_number", series.next_upcoming_book_number)
        series.last_checked = date.today()
        db.commit()
        db.refresh(series)

        recalculate_series_state_for_series(
            db,
            series_id,
            scan_result={
                "added_count": len(added_books),
                "added_books": added_books,
                "candidate_diagnostics": candidate_diagnostics,
                "trusted_series_reconciliation": trusted_reconciliation,
                "found": bool(added_books),
            },
        )
        self._strict_post_discovery_cleanup(
            db,
            series,
            known_authors=known_authors,
            known_series_max=known_series_max,
            series_complete=series_complete,
        )
        recount_series_aggregates_for_series(db, series.id)

        print(
            f"[DISCOVERY_DEBUG] series-check summary series_id={series.id} "
            f"added={len(added_books)} candidate_numbers={candidate_numbers} "
            f"missing_books={series.missing_books} trusted_source={(trusted_reconciliation or {}).get('source')}"
        )
        print(
            "[DISCOVERY_DEBUG] series-check diagnostics "
            f"series_id={series.id} candidate_diagnostics={[
                {
                    'book_number': item.get('book_number'),
                    'reason': item.get('reason'),
                    'selected_stage': (item.get('diagnostics') or {}).get('selected_stage'),
                    'accepted_total': (item.get('diagnostics') or {}).get('accepted_total'),
                    'top_score': (item.get('diagnostics') or {}).get('top_score'),
                    'provider_counts': (item.get('diagnostics') or {}).get('provider_counts'),
                    'rejection_counts': (item.get('diagnostics') or {}).get('rejection_counts'),
                }
                for item in candidate_diagnostics
            ]}"
        )

        return self._build_series_check_result(
            series=series,
            highest_owned_book_number=highest_owned,
            candidate_numbers=candidate_numbers,
            added_books=added_books,
            candidate_diagnostics=candidate_diagnostics,
            trusted_series_reconciliation=trusted_reconciliation,
            status="complete" if added_books else "no_hits",
            reason=None if added_books else "no-hit-after-all-passes",
            discovery_mode="agent_discovery",
        )

    def run_daily_scan(self, db: Session) -> list[dict]:
        # Batch scanning is intentionally disabled to keep discovery entrypoints
        # single-series only. Route-triggered jobs must call run_series_check().
        return []