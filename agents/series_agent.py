from __future__ import annotations

from datetime import date
import re

from collections.abc import Callable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from book_metadata_utils import normalize_book_metadata, parse_publication_date
from intelligence import compute_series_intelligence_for_series, suggest_book_by_series
from models import Book, Series, SeriesCanonicalEntry


BOOK_COLUMN_KEYS = {column.key for column in Book.__table__.columns}


class SeriesIntelligenceAgent:
    FUTURE_SCAN_MAX_AHEAD = 20
    FUTURE_SCAN_EMPTY_STREAK_STOP = 3

    def _diagnostic_reason(self, suggestion: dict) -> str | None:
        diagnostics = suggestion.get("diagnostics") or {}
        provider_counts = diagnostics.get("provider_counts") or {}
        rejection_counts = diagnostics.get("rejection_counts") or {}
        total_provider_results = sum(int(value or 0) for value in provider_counts.values())

        if total_provider_results <= 0:
            return "no_provider_results"
        if int(rejection_counts.get("author_mismatch", 0)) > 0:
            return "author_filtered"
        if int(rejection_counts.get("missing_author", 0)) > 0:
            return "low_confidence"
        if sum(int(value or 0) for value in rejection_counts.values()) > 0:
            return "low_confidence"
        return None

    def _diagnostic_message(self, reason: str | None) -> str | None:
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
            .filter(Book.record_status != "deleted")
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
        if series.total_books and series.total_books > floor_highest:
            max_ahead = max(max_ahead, int(series.total_books) - floor_highest)

        return [floor_highest + offset for offset in range(1, max_ahead + 1)]

    def _candidate_numbers(self, series: Series, books: list[Book]) -> list[int]:
        future_candidates = self._future_candidate_numbers(series, books)
        missing_candidates = self._missing_candidate_numbers(books)
        return list(dict.fromkeys([*future_candidates, *missing_candidates]))

    def _find_existing_book(self, db: Session, series_id: int, book_number: float | int) -> Book | None:
        normalized_number = float(book_number)
        return (
            db.query(Book)
            .filter(Book.series_id == series_id)
            .filter(or_(Book.book_number == normalized_number, Book.series_order == normalized_number))
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
    ) -> tuple[Book | None, dict]:
        author_query = ", ".join(known_authors)
        suggestion = suggest_book_by_series(series.name, canonical_entry.book_number, author_query)
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

        publication_value = normalized.get("publication_date") or selected.get("year")
        if publication_value and "publication_date" not in payload:
            payload["publication_date"] = parse_publication_date(str(publication_value))

        return payload

    def _persist_book(self, db: Session, series: Series, book_number: int, suggestion: dict, *, is_missing: bool, known_authors: list[str]) -> Book | None:
        payload = self._build_book_payload(series, book_number, suggestion, is_missing=is_missing, known_authors=known_authors)
        if not payload:
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
                "added_count": 0,
                "has_new_books": False,
            }

        books = self._owned_books(db, series_id)
        canonical_entries = self._canonical_entries(db, series_id)
        if canonical_entries:
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
                progress_callback({"total": len(canonical_entries), "completed": 0, "current_book_number": None})

            for index, entry in enumerate(canonical_entries, start=1):
                if progress_callback is not None:
                    progress_callback({"total": len(canonical_entries), "completed": index - 1, "current_book_number": entry.book_number})

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
                )
                candidate_diagnostics.append(diagnostic)
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
            series.has_new_books = bool(added_books)
            db.commit()
            db.refresh(series)

            if progress_callback is not None:
                progress_callback({"total": len(canonical_entries), "completed": len(canonical_entries), "current_book_number": None})

            return {
                "series_id": series.id,
                "series_name": series.name,
                "highest_owned_book_number": highest_owned,
                "candidate_numbers": [entry.book_number for entry in canonical_entries],
                "added_count": len(added_books),
                "added_books": added_books,
                "candidate_diagnostics": candidate_diagnostics,
                "canonical_missing_entries": missing_entries,
                "canonical_found_entries": found_entries,
                "canonical_upcoming_entries": upcoming_entries,
                "canonical_rejected_entries": rejected_entries,
                "has_new_books": series.has_new_books,
                "last_checked": series.last_checked,
                "next_unread_book_number": series.next_unread_book_number,
                "next_upcoming_book_number": series.next_upcoming_book_number,
                "missing_books": series.missing_books,
                "found": bool(added_books),
            }

        highest_owned = self._highest_owned_book_number(books)
        known_authors = self._series_author_candidates(series, books)
        if not known_authors:
            intelligence = compute_series_intelligence_for_series(db, series_id) or {}
            series.has_new_books = False
            series.last_checked = date.today()
            series.total_books = intelligence.get("total_books", series.total_books)
            series.is_finished = intelligence.get("is_series_finished", series.is_finished)
            series.series_status = "finished" if series.is_finished else "ongoing"
            series.missing_books = intelligence.get("missing_orders", series.missing_books)
            series.next_unread_book_number = intelligence.get("next_unread_book_number", series.next_unread_book_number)
            series.next_upcoming_book_number = intelligence.get("next_upcoming_book_number", series.next_upcoming_book_number)
            db.commit()
            db.refresh(series)
            return {
                "series_id": series.id,
                "series_name": series.name,
                "highest_owned_book_number": highest_owned,
                "candidate_numbers": [],
                "added_count": 0,
                "added_books": [],
                "has_new_books": False,
                "last_checked": series.last_checked,
                "next_unread_book_number": series.next_unread_book_number,
                "next_upcoming_book_number": series.next_upcoming_book_number,
                "missing_books": series.missing_books,
                "found": False,
            }

        candidate_numbers = self._candidate_numbers(series, books)
        added_books: list[dict] = []
        candidate_diagnostics: list[dict] = []
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
                    }
                )

            is_missing = highest_owned is not None and book_number <= int(highest_owned)
            author_query = ", ".join(known_authors)
            suggestion = suggest_book_by_series(series.name, book_number, author_query)
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
        series.has_new_books = bool(added_books)
        db.commit()
        db.refresh(series)

        return {
            "series_id": series.id,
            "series_name": series.name,
            "highest_owned_book_number": highest_owned,
            "candidate_numbers": candidate_numbers,
            "added_count": len(added_books),
            "added_books": added_books,
            "candidate_diagnostics": candidate_diagnostics,
            "has_new_books": series.has_new_books,
            "last_checked": series.last_checked,
            "next_unread_book_number": series.next_unread_book_number,
            "next_upcoming_book_number": series.next_upcoming_book_number,
            "missing_books": series.missing_books,
            "found": bool(added_books),
        }

    def run_daily_scan(self, db: Session) -> list[dict]:
        series_list = db.query(Series).filter(Series.is_finished.is_(False)).all()
        results: list[dict] = []
        for series in series_list:
            results.append(self.run_series_check(db, series.id))
        return results