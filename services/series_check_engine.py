"""The "Check Now" background job engine.

This is the persistence layer that runs after `agents.series_agent` returns
discovered candidates: it decides insert vs. update vs. skip against the
existing library rows, runs de-dup collapse passes, rebuilds series
intelligence, and tracks job progress/status for the polling endpoints.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import date, datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

import models
import crud
import library_sync
from book_metadata_utils import parse_publication_date
from database import SessionLocal
from intelligence import recalculate_intelligence, recalculate_series_state_for_series
from agents.series_agent import SeriesIntelligenceAgent
from services.discovery_logging import _console_log, log_discovery_summary
from services.identity import (
    _authors_match_exact,
    _canonical_title_identity_key,
    _edition_priority,
    _normalize_discovered_title,
    _series_book_identity_key,
)

logger = logging.getLogger(__name__)

series_agent = SeriesIntelligenceAgent()
series_check_jobs: dict[int, dict] = {}
SERIES_CHECK_TIMEOUT_SECONDS = 300
SERIES_CHECK_HARD_TIMEOUT_SECONDS = 300


def _parse_candidate_date(value: str | None) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    parsed = parse_publication_date(raw)
    return parsed if isinstance(parsed, date) else None


def _classify_discovered_status(candidate: dict, today: date) -> tuple[str, date | None, date | None]:
    publication_date = _parse_candidate_date(candidate.get("publication_date"))
    expected_date = _parse_candidate_date(candidate.get("expected_date"))
    status_hint = str(candidate.get("status_hint") or "").strip().lower()
    title_hint = str(candidate.get("title") or "").strip().lower()

    upcoming_by_hint = any(token in status_hint for token in ("upcoming", "preorder", "pre-order"))
    upcoming_by_title = any(token in title_hint for token in ("upcoming", "preorder", "pre-order"))
    upcoming_by_date = (expected_date is not None and expected_date > today) or (publication_date is not None and publication_date > today)

    if upcoming_by_hint or upcoming_by_title or upcoming_by_date:
        if expected_date is None and publication_date is not None and publication_date > today:
            expected_date = publication_date
        return "upcoming", publication_date, expected_date

    return "available", publication_date, expected_date


def _build_series_counters(db: Session, series_id: int) -> dict:
    books = (
        db.query(models.Book)
        .filter(models.Book.series_id == series_id)
        .filter(or_(models.Book.record_status.is_(None), models.Book.record_status != "deleted"))
        .all()
    )

    read_books = 0
    upcoming_books = 0
    unread_books = 0

    for book in books:
        read_status = str(getattr(book, "read_status", "") or "").strip().lower()
        is_read = bool(getattr(book, "is_read", False)) or read_status == "read"
        is_upcoming = read_status == "upcoming" or bool(getattr(book, "is_upcoming_auto", False)) or bool(getattr(book, "is_upcoming_final", False))
        if is_upcoming:
            upcoming_books += 1
        elif is_read:
            read_books += 1
        else:
            unread_books += 1

    return {
        "total_books": len(books),
        "unread_books": unread_books,
        "read_books": read_books,
        "upcoming_books": upcoming_books,
    }


def _build_status_bar(series: "models.Series") -> dict:
    return {
        "status": "finished" if bool(series.is_finished) else "ongoing",
        "next_unread": series.next_unread_book_number,
        "next_upcoming": series.next_upcoming_book_number,
        "missing": [int(float(value)) for value in (series.missing_books or []) if str(value).strip()],
    }


def run_series_check_job_full(series_id: int) -> None:
    db = SessionLocal()
    try:
        db_series = crud.get_series(db, series_id)
        if db_series:
            logger.info("CHECK NOW triggered for series_id=%s, series_name=%s", series_id, db_series.name)
        fallback_missing = [7]
        if db_series and isinstance(db_series.missing_books, list) and db_series.missing_books:
            try:
                fallback_missing = [int(float(db_series.missing_books[0]))]
            except (TypeError, ValueError):
                fallback_missing = [7]

        def update_progress(progress: dict) -> None:
            existing = series_check_jobs.get(series_id, {})
            total = int(progress.get("total", 0) or 0)
            completed = int(progress.get("completed", 0) or 0)
            series_check_jobs[series_id] = {
                **existing,
                "status": "running",
                "updated_at": datetime.utcnow().isoformat(),
                "progress_total": total,
                "progress_completed": completed,
                "progress_percent": int((completed / total) * 100) if total > 0 else 0,
                "current_book_number": progress.get("current_book_number"),
                "current_pass": progress.get("current_pass") or existing.get("current_pass") or "exact match",
                "current_asin": progress.get("current_asin"),
                "asins_discovered": progress.get("asins_discovered", existing.get("asins_discovered", 0)),
                "asins_processed": progress.get("asins_processed", existing.get("asins_processed", completed)),
                "asin_fetch_success": progress.get("asin_fetch_success", existing.get("asin_fetch_success", 0)),
                "asin_fetch_failed": progress.get("asin_fetch_failed", existing.get("asin_fetch_failed", 0)),
            }

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(series_agent.run_series_check, db, series_id, update_progress, False)
        try:
            result = future.result(timeout=SERIES_CHECK_HARD_TIMEOUT_SECONDS)
        except FutureTimeoutError:
            result = {
                "series_id": series_id,
                "missing_books": fallback_missing,
                "added_books": [],
                "found": False,
                "discovery_engine": "agent_v2",
                "agent_pipeline": True,
                "status": "no_hits",
                "provider_failures": [],
                "all_providers_failed": False,
                "timed_out": True,
            }
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        provider_failures = result.get("provider_failures") or []
        for failure in provider_failures:
            logger.info(
                "Provider %s failed: %s",
                failure.get("provider"),
                failure.get("error") or "unknown",
            )

        db_series = crud.get_series(db, series_id)
        if not db_series:
            raise RuntimeError(f"Series {series_id} not found during check job")

        today = date.today()
        existing_books = (
            db.query(models.Book)
            .filter(models.Book.series_id == series_id)
            .filter(or_(models.Book.record_status.is_(None), models.Book.record_status != "deleted"))
            .all()
        )

        existing_by_asin: dict[str, models.Book] = {}
        existing_by_series_book: dict[str, models.Book] = {}
        existing_by_canonical_title: dict[str, models.Book] = {}

        for existing in existing_books:
            existing_asin = str(existing.asin or "").strip().upper()
            if existing_asin and existing_asin not in existing_by_asin:
                existing_by_asin[existing_asin] = existing

            series_book_key = _series_book_identity_key(existing.series_name or db_series.name, existing.book_number)
            if series_book_key and series_book_key not in existing_by_series_book:
                existing_by_series_book[series_book_key] = existing

            canonical_title_key = _canonical_title_identity_key(existing.title)
            if canonical_title_key and canonical_title_key not in existing_by_canonical_title:
                existing_by_canonical_title[canonical_title_key] = existing

        persisted_new_books: list[dict] = []
        discovered_candidates = result.get("added_books") or []
        seen_batch_identity_keys: set[str] = set()
        db_changed = False

        try:
            for candidate in discovered_candidates:
                title = str(candidate.get("title") or "").strip()
                if not title:
                    continue

                series_author = str(db_series.author or "").strip()
                candidate_author = str(candidate.get("author") or "").strip()
                if not _authors_match_exact(series_author, candidate_author):
                    logger.info("Classification result: INVALID")
                    continue

                canonical_metadata = candidate.get("canonical_metadata") if isinstance(candidate.get("canonical_metadata"), dict) else {}

                normalized_title = str(canonical_metadata.get("title_normalized") or title).strip()
                normalized_series_name = str(
                    canonical_metadata.get("series_name_normalized")
                    or candidate.get("series_name")
                    or db_series.name
                    or ""
                ).strip()
                normalized_author = candidate_author
                normalized_book_number = canonical_metadata.get("book_number_normalized")
                if normalized_book_number is None:
                    normalized_book_number = candidate.get("book_number")
                candidate_asin = str(candidate.get("asin_or_id") or "").strip().upper()

                series_book_key = _series_book_identity_key(normalized_series_name, normalized_book_number)
                canonical_title_key = _canonical_title_identity_key(normalized_title)

                matched_existing: models.Book | None = None
                dedupe_reason_code = ""
                if candidate_asin and candidate_asin in existing_by_asin:
                    matched_existing = existing_by_asin[candidate_asin]
                    dedupe_reason_code = "DEDUPE_UPDATE_BY_ASIN"

                identity_fingerprint = candidate_asin or series_book_key or canonical_title_key or _normalize_discovered_title(normalized_title)
                if identity_fingerprint in seen_batch_identity_keys and matched_existing is None:
                    logger.info(
                        "[DEDUPE_SKIP_BATCH_DUPLICATE] series_id=%s title=%s identity=%s",
                        series_id,
                        normalized_title,
                        identity_fingerprint,
                    )
                    continue
                seen_batch_identity_keys.add(identity_fingerprint)

                status, publication_date, expected_date = _classify_discovered_status(candidate, today)
                if status == "upcoming":
                    logger.info("Classified %s as UPCOMING", normalized_title)
                else:
                    logger.info("Classified %s as AVAILABLE", normalized_title)

                publication_date = publication_date or _parse_candidate_date(canonical_metadata.get("publish_date_normalized"))
                expected_date = expected_date or _parse_candidate_date(canonical_metadata.get("upcoming_date_normalized"))

                raw_book_number = normalized_book_number
                book_number: float | None = None
                try:
                    if raw_book_number is not None and str(raw_book_number).strip() != "":
                        book_number = float(raw_book_number)
                except (TypeError, ValueError):
                    book_number = None

                incoming_edition_type = str(canonical_metadata.get("edition_type") or "unknown").strip().lower()

                if matched_existing is not None:
                    logger.info("Classification result: EXISTING")

                    matched_existing.title = normalized_title or matched_existing.title
                    matched_existing.author = normalized_author or matched_existing.author
                    if candidate_asin:
                        matched_existing.asin = candidate_asin

                    if book_number is not None and (matched_existing.book_number is None or matched_existing.book_number <= 0):
                        matched_existing.book_number = book_number
                    if matched_existing.series_order is None and matched_existing.book_number is not None and float(matched_existing.book_number).is_integer():
                        matched_existing.series_order = int(matched_existing.book_number)

                    if matched_existing.publication_date is None and publication_date is not None:
                        matched_existing.publication_date = publication_date
                    elif matched_existing.publication_date is not None and publication_date is not None:
                        matched_existing.publication_date = min(matched_existing.publication_date, publication_date)

                    if matched_existing.release_date is None and expected_date is not None:
                        matched_existing.release_date = expected_date

                    current_edition_type = (matched_existing.edition or matched_existing.format or "unknown")
                    if _edition_priority(incoming_edition_type) > _edition_priority(current_edition_type):
                        matched_existing.edition = incoming_edition_type
                        matched_existing.format = incoming_edition_type
                        logger.info(
                            "[DEDUPE_MERGE_EDITION] series_id=%s book_id=%s from=%s to=%s",
                            series_id,
                            matched_existing.id,
                            current_edition_type,
                            incoming_edition_type,
                        )

                    if status == "upcoming":
                        matched_existing.read_status = "upcoming"
                        matched_existing.is_upcoming_auto = True
                    elif str(matched_existing.read_status or "").strip().lower() != "read":
                        matched_existing.read_status = "available"
                        matched_existing.is_upcoming_auto = False

                    matched_existing.is_missing = bool(matched_existing.is_missing and bool(candidate.get("is_missing")))
                    matched_existing.record_status = "active"
                    db.flush()
                    db_changed = True
                    continue

                db_book = models.Book(
                    title=normalized_title,
                    author=normalized_author,
                    series_id=series_id,
                    book_number=book_number,
                    series_order=int(book_number) if book_number is not None and float(book_number).is_integer() else None,
                    publication_date=publication_date,
                    release_date=expected_date,
                    date_added=today,
                    asin=candidate_asin or None,
                    format=incoming_edition_type if incoming_edition_type != "unknown" else None,
                    edition=incoming_edition_type if incoming_edition_type != "unknown" else None,
                    is_read=False,
                    read_status="upcoming" if status == "upcoming" else "available",
                    is_upcoming_auto=(status == "upcoming"),
                    is_upcoming_final=False,
                    is_missing=bool(candidate.get("is_missing")),
                    record_status="active",
                )
                logger.info("Classification result: NEW")
                _console_log(f"Persisted new book: {normalized_title}")
                db.add(db_book)
                db.flush()
                db_changed = True

                if db_book.asin:
                    existing_by_asin[str(db_book.asin).strip().upper()] = db_book
                inserted_series_book_key = _series_book_identity_key(db_series.name, db_book.book_number)
                if inserted_series_book_key:
                    existing_by_series_book[inserted_series_book_key] = db_book
                inserted_title_key = _canonical_title_identity_key(db_book.title)
                if inserted_title_key:
                    existing_by_canonical_title[inserted_title_key] = db_book

                persisted_new_books.append(
                    {
                        "id": int(db_book.id),
                        "title": db_book.title,
                        "author": db_book.author,
                        "asin": db_book.asin,
                        "is_missing": bool(db_book.is_missing),
                        "status": status,
                        "date_published": db_book.publication_date.isoformat() if db_book.publication_date else None,
                        "expected_date": db_book.release_date.isoformat() if db_book.release_date else None,
                        "series_id": series_id,
                        "library_position": "top",
                    }
                )

            # NOTE: this used to also delete any existing not-yet-read "ghost"
            # book that this run's candidate set didn't happen to re-surface
            # (a leftover behavior from the old HTML-scraper pipeline, meant
            # to clean up its noisier results). That's actively unsafe with
            # live third-party search APIs: a book correctly discovered on
            # one Check Now can simply not come back in the exact same
            # ranked result set on a later call (pagination/ranking/quota
            # variance), which would silently delete a perfectly valid,
            # already-confirmed book. True duplicate cleanup among rows that
            # currently coexist is handled by the identity-collapse passes
            # below instead, which don't depend on this run's API results.

            # Collapse duplicates that share canonical identity keys.
            identity_keeper: dict[str, models.Book] = {}
            refreshed_active_books = (
                db.query(models.Book)
                .filter(models.Book.series_id == series_id)
                .filter(or_(models.Book.record_status.is_(None), models.Book.record_status != "deleted"))
                .all()
            )
            for existing in refreshed_active_books:
                key = str(existing.asin or "").strip().upper()
                if not key:
                    key = _series_book_identity_key(existing.series_name or db_series.name, existing.book_number) or ""
                if not key:
                    key = _canonical_title_identity_key(existing.title) or ""
                if not key:
                    continue

                keeper = identity_keeper.get(key)
                if keeper is None:
                    identity_keeper[key] = existing
                    continue

                keeper_score = (
                    1 if bool(keeper.is_read) else 0,
                    _edition_priority(keeper.edition or keeper.format),
                    1 if keeper.publication_date else 0,
                )
                existing_score = (
                    1 if bool(existing.is_read) else 0,
                    _edition_priority(existing.edition or existing.format),
                    1 if existing.publication_date else 0,
                )
                if existing_score > keeper_score:
                    loser = keeper
                    identity_keeper[key] = existing
                else:
                    loser = existing

                logger.info(
                    "[DEDUPE_PRUNE_DUPLICATE_EXISTING] series_id=%s keep_id=%s drop_id=%s key=%s",
                    series_id,
                    identity_keeper[key].id,
                    loser.id,
                    key,
                )
                loser.record_status = "deleted"
                db_changed = True

            # Final strict pass: collapse all duplicates by normalized series+book number,
            # even when one row has ASIN and another row does not.
            series_book_keeper: dict[str, models.Book] = {}
            refreshed_after_identity_prune = (
                db.query(models.Book)
                .filter(models.Book.series_id == series_id)
                .filter(or_(models.Book.record_status.is_(None), models.Book.record_status != "deleted"))
                .all()
            )
            for existing in refreshed_after_identity_prune:
                series_book_key = _series_book_identity_key(existing.series_name or db_series.name, existing.book_number)
                if not series_book_key:
                    continue

                keeper = series_book_keeper.get(series_book_key)
                if keeper is None:
                    series_book_keeper[series_book_key] = existing
                    continue

                keeper_score = (
                    1 if str(keeper.asin or "").strip() else 0,
                    1 if bool(keeper.is_read) else 0,
                    _edition_priority(keeper.edition or keeper.format),
                    1 if keeper.publication_date else 0,
                )
                existing_score = (
                    1 if str(existing.asin or "").strip() else 0,
                    1 if bool(existing.is_read) else 0,
                    _edition_priority(existing.edition or existing.format),
                    1 if existing.publication_date else 0,
                )

                if existing_score > keeper_score:
                    loser = keeper
                    series_book_keeper[series_book_key] = existing
                else:
                    loser = existing

                logger.info(
                    "[DEDUPE_PRUNE_SERIES_BOOK_DUPLICATE] series_id=%s keep_id=%s drop_id=%s key=%s",
                    series_id,
                    series_book_keeper[series_book_key].id,
                    loser.id,
                    series_book_key,
                )
                loser.record_status = "deleted"
                db_changed = True

            if db_changed:
                db.commit()
                db.refresh(db_series)

            logger.info("LIBRARY_SYNC_TRIGGERED series_id=%s", series_id)
            library_sync.update_from_series(series_id)
        except Exception:
            db.rollback()
            raise

        result["added_books"] = persisted_new_books
        result["added_count"] = len(persisted_new_books)

        rebuild_snapshot = recalculate_intelligence(db, series_id, scan_result=result if isinstance(result, dict) else None)
        if isinstance(result, dict) and rebuild_snapshot:
            result["series_aggregates"] = {
                "total_books": rebuild_snapshot.get("total_books"),
                "active_count": rebuild_snapshot.get("active_count"),
                "deleted_count": rebuild_snapshot.get("deleted_count"),
                "upcoming_count": rebuild_snapshot.get("upcoming_count"),
            }

        db.refresh(db_series)
        counters = _build_series_counters(db, series_id)
        status_bar = _build_status_bar(db_series)
        logger.info(
            "Updated counters for series_id=%s: total=%s, unread=%s, read=%s, upcoming=%s",
            series_id,
            counters.get("total_books"),
            counters.get("unread_books"),
            counters.get("read_books"),
            counters.get("upcoming_books"),
        )
        logger.info(
            "Updated status bar for series_id=%s: status=%s, next_unread=%s, next_upcoming=%s, missing=%s",
            series_id,
            status_bar.get("status"),
            status_bar.get("next_unread"),
            status_bar.get("next_upcoming"),
            status_bar.get("missing"),
        )
        all_providers_failed = bool(result.get("all_providers_failed"))

        if all_providers_failed:
            response_status = "error"
            response_message = "All providers failed for this series."
            logger.info("CHECK NOW completed successfully for series: %s", db_series.name)
        elif persisted_new_books:
            response_status = "success"
            response_message = "NEW BOOKS found and added to library."
            logger.info("CHECK NOW completed successfully for series: %s", db_series.name)
        else:
            response_status = "no_new_books"
            response_message = "NO NEW BOOKS FOUND."
            logger.info("CHECK NOW completed successfully for series: %s", db_series.name)

        completion = {
            "status": response_status,
            "message": response_message,
            "new_books": persisted_new_books,
            "counters": counters,
            "status_bar": status_bar,
            "complete": True,
            "missing_books": status_bar.get("missing") or [],
            "available_missing": result.get("available_missing") or [],
            "upcoming_books": result.get("upcoming_books") or [],
            "validated_candidates": result.get("validated_candidates") or [],
            "found_books": persisted_new_books,
            "no_new_books": response_status != "success",
            "discovery_engine": result.get("discovery_engine") or "new_book_checker",
            "asin_discovery": result.get("asin_discovery") or {
                "discovered": 0,
                "processed": 0,
                "fetch_success": 0,
                "fetch_failed": 0,
                "metadata_hits": 0,
            },
        }

        log_discovery_summary(result=result)

        logger.info("CHECK NOW completed successfully for series: %s", db_series.name)

        series_check_jobs[series_id] = {
            "status": "completed",
            "result": result,
            "error": None,
            "completion": completion,
            "updated_at": datetime.utcnow().isoformat(),
            "progress_total": int((result.get("asin_discovery") or {}).get("discovered") or len(result.get("candidate_numbers") or []) or 0),
            "progress_completed": int((result.get("asin_discovery") or {}).get("processed") or len(result.get("candidate_numbers") or []) or 0),
            "current_book_number": None,
            "current_pass": None,
            "current_asin": None,
            "asins_discovered": int((result.get("asin_discovery") or {}).get("discovered") or 0),
            "asins_processed": int((result.get("asin_discovery") or {}).get("processed") or 0),
            "asin_fetch_success": int((result.get("asin_discovery") or {}).get("fetch_success") or 0),
            "asin_fetch_failed": int((result.get("asin_discovery") or {}).get("fetch_failed") or 0),
        }
    except Exception as exc:
        logger.exception("Series check job failed for series %s", series_id)
        fallback_result = {
            "series_id": series_id,
            "found": False,
            "added_count": 0,
            "added_books": [],
            "missing_books": fallback_missing,
            "upcoming_books": [],
            "validated_candidates": [],
            "provider_failures": [],
            "all_providers_failed": True,
            "asin_discovery": {
                "discovered": 0,
                "processed": 0,
                "fetch_success": 0,
                "fetch_failed": 0,
                "metadata_hits": 0,
            },
            "status": "no_hits",
            "discovery_engine": "agent_v2",
            "agent_pipeline": True,
        }
        log_discovery_summary(result=fallback_result, terminal_error=f"{type(exc).__name__}: {exc}")
        series_check_jobs[series_id] = {
            "status": "completed",
            "result": fallback_result,
            "error": str(exc),
            "completion": {
                "status": "error",
                "message": "All providers failed for this series.",
                "new_books": [],
                "counters": {
                    "total_books": 0,
                    "unread_books": 0,
                    "read_books": 0,
                    "upcoming_books": 0,
                },
                "status_bar": {
                    "status": "ongoing",
                    "next_unread": None,
                    "next_upcoming": None,
                    "missing": fallback_missing,
                },
                "complete": True,
                "missing_books": fallback_missing,
                "available_missing": [],
                "upcoming_books": [],
                "validated_candidates": [],
                "found_books": [],
                "no_new_books": True,
                "reason": "check-now-error",
                "discovery_engine": "agent_v2",
                "asin_discovery": {
                    "discovered": 0,
                    "processed": 0,
                    "fetch_success": 0,
                    "fetch_failed": 0,
                    "metadata_hits": 0,
                },
            },
            "updated_at": datetime.utcnow().isoformat(),
            "current_book_number": None,
            "current_pass": None,
            "current_asin": None,
            "asins_discovered": 0,
            "asins_processed": 0,
            "asin_fetch_success": 0,
            "asin_fetch_failed": 0,
        }
    finally:
        # Guarantee the cached series intelligence (missing_books, total_books,
        # etc.) always reflects the actual current book rows, even if this run
        # errored out or timed out before reaching its own recalculate call
        # above. Without this, a single failed/interrupted check could leave
        # a series permanently reporting a stale "missing" book that the
        # detail page (which always recomputes fresh) would never agree with.
        try:
            db.rollback()
            recalculate_series_state_for_series(db, series_id)
        except Exception:
            logger.exception("Failed to refresh series intelligence after check job for series %s", series_id)
        db.close()
