from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.orm import Session

from models import Book, Series
from new_book_checker import check_for_new_book


logger = logging.getLogger(__name__)


def _console_log(message: str) -> None:
    print(f"[series_agent] {message}", flush=True)


class SeriesIntelligenceAgent:
    def run_daily_scan(self, db) -> None:
        return None

    def run_series_check(
        self,
        db: Session,
        series_id: int,
        progress_callback=None,
    ) -> dict:
        series = db.query(Series).filter(Series.id == series_id).first()
        if not series:
            return {
                "series_id": None,
                "series_name": None,
                "highest_owned_book_number": None,
                "candidate_numbers": [],
                "added_count": 0,
                "added_books": [],
                "found_books": [],
                "candidate_diagnostics": [],
                "trusted_series_reconciliation": None,
                "canonical_missing_entries": [],
                "canonical_found_entries": [],
                "canonical_upcoming_entries": [],
                "canonical_rejected_entries": [],
                "complete": True,
                "status": "no_hits",
                "no_new_books": True,
                "reason": "series-not-found",
                "discovery_mode": None,
                "has_new_books": False,
                "series_state": None,
                "last_checked": None,
                "next_unread_book_number": None,
                "next_upcoming_book_number": None,
                "missing_books": [],
                "found": False,
                "discovery_engine": "none",
                "agent_pipeline": False,
            }

        _console_log(f"CHECK NOW triggered for series: {series.name}")

        active_series_books = [
            book
            for book in db.query(Book).filter(Book.series_id == series_id).all()
            if (book.record_status or "") != "deleted"
        ]
        highest_owned_book_number = max(
            (
                int(float(book.book_number))
                for book in active_series_books
                if book.book_number is not None and not bool(book.is_missing)
            ),
            default=None,
        )
        setattr(series, "highest_owned_book_number", highest_owned_book_number)

        checker_result = check_for_new_book(series, progress_callback=progress_callback)
        found = bool(checker_result.get("found"))
        candidate = checker_result.get("candidate") or None
        provider_failures = checker_result.get("provider_failures") or []
        all_providers_failed = bool(checker_result.get("all_providers_failed"))
        amazon_book_candidates = checker_result.get("amazon_book_candidates") or []

        _console_log(f"Candidates found: {len(amazon_book_candidates) + (1 if candidate else 0)}")

        series.has_new_books = found
        series.last_checked = date.today()
        db.commit()
        db.refresh(series)

        missing_books = [str(value).strip() for value in (series.missing_books or []) if str(value).strip()]
        added_books = []
        if candidate:
            added_books = [
                {
                    "title": candidate.get("title"),
                    "author": candidate.get("author") or series.author,
                    "book_number": candidate.get("number"),
                    "source_url": candidate.get("url"),
                    "provider": candidate.get("provider"),
                    "publication_date": candidate.get("publication_date"),
                    "expected_date": candidate.get("expected_date"),
                    "status_hint": candidate.get("status_hint"),
                    "asin_or_id": candidate.get("asin_or_id"),
                    "is_missing": False,
                }
            ]

        seen_added_asins = {
            str(item.get("asin_or_id") or "").strip().upper()
            for item in added_books
            if str(item.get("asin_or_id") or "").strip()
        }

        for amazon_candidate in amazon_book_candidates:
            asin = str(amazon_candidate.get("asin_or_id") or "").strip().upper()
            if not asin:
                continue
            if asin in seen_added_asins:
                continue

            _console_log(
                f"Candidate: {str(amazon_candidate.get('title') or series.name).strip() or series.name} {asin}"
            )

            added_books.append(
                {
                    "title": str(amazon_candidate.get("title") or series.name).strip() or series.name,
                    "author": str(amazon_candidate.get("author") or series.author or "").strip() or series.author,
                    "series_name": str(amazon_candidate.get("series_name") or series.name or "").strip() or series.name,
                    "book_number": amazon_candidate.get("book_number"),
                    "source_url": str(amazon_candidate.get("url") or "").strip(),
                    "provider": "amazon_books",
                    "publication_date": amazon_candidate.get("publish_date") or amazon_candidate.get("release_date"),
                    "expected_date": amazon_candidate.get("upcoming_date"),
                    "status_hint": str(amazon_candidate.get("availability") or "unknown").strip() or "unknown",
                    "asin_or_id": asin,
                    "is_missing": True,
                    "canonical_metadata": {
                        "title_normalized": str(amazon_candidate.get("title") or series.name).strip() or series.name,
                        "series_name_normalized": str(amazon_candidate.get("series_name") or series.name or "").strip() or series.name,
                        "book_number_normalized": amazon_candidate.get("book_number"),
                        "publish_date_normalized": amazon_candidate.get("publish_date") or amazon_candidate.get("release_date"),
                        "upcoming_date_normalized": amazon_candidate.get("upcoming_date"),
                        "availability": amazon_candidate.get("availability") or "unknown",
                        "edition_type": amazon_candidate.get("edition_type") or "unknown",
                        "title_selector": amazon_candidate.get("title_selector"),
                    },
                }
            )
            seen_added_asins.add(asin)

        _console_log(f"CHECK NOW completed successfully for series: {series.name}")

        return {
            "series_id": series.id,
            "series_name": series.name,
            "highest_owned_book_number": highest_owned_book_number,
            "candidate_numbers": [],
            "added_count": len(added_books),
            "added_books": added_books,
            "found_books": added_books,
            "candidate_diagnostics": [],
            "trusted_series_reconciliation": None,
            "canonical_missing_entries": [],
            "canonical_found_entries": [],
            "canonical_upcoming_entries": [],
            "canonical_rejected_entries": [],
            "complete": True,
            "status": "complete" if found else "no_hits",
            "no_new_books": not found,
            "reason": None if found else "no-hit-after-new-book-check",
            "discovery_mode": None,
            "has_new_books": series.has_new_books,
            "series_state": series.series_state,
            "last_checked": series.last_checked,
            "next_unread_book_number": series.next_unread_book_number,
            "next_upcoming_book_number": series.next_upcoming_book_number,
            "missing_books": missing_books,
            "found": found,
            "candidate": candidate,
            "provider_failures": provider_failures,
            "all_providers_failed": all_providers_failed,
            "asin_discovery": checker_result.get("asin_discovery") or {
                "discovered": 0,
                "processed": 0,
                "fetch_success": 0,
                "fetch_failed": 0,
                "metadata_hits": 0,
            },
            "amazon_asin_candidates": checker_result.get("amazon_asin_candidates") or [],
            "first_extracted_product_metadata": checker_result.get("first_extracted_product_metadata"),
            "first_product_extraction_failure": checker_result.get("first_product_extraction_failure"),
            "discovery_engine": "new_book_checker",
            "agent_pipeline": True,
        }
