from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from intelligence import (
    compute_series_intelligence_for_series,
    recalculate_series_state_for_series,
    recount_series_aggregates_for_series,
)
from models import Series


class SeriesIntelligenceAgent:
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
                "added_count": 0,
                "added_books": [],
                "candidate_diagnostics": [],
                "trusted_series_reconciliation": None,
                "found": False,
            },
        )
        recount_series_aggregates_for_series(db, series.id)

        missing_books = [str(value).strip() for value in (series.missing_books or []) if str(value).strip()]
        return {
            "series_id": series.id,
            "series_name": series.name,
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
            "reason": "legacy-discovery-removed",
            "discovery_mode": None,
            "has_new_books": series.has_new_books,
            "series_state": series.series_state,
            "last_checked": series.last_checked,
            "next_unread_book_number": series.next_unread_book_number,
            "next_upcoming_book_number": series.next_upcoming_book_number,
            "missing_books": missing_books,
            "found": False,
            "discovery_engine": "none",
            "agent_pipeline": False,
        }
