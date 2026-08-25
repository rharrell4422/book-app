"""The "Check Now" background job engine.

This is the persistence layer that runs after `agents.series_agent` returns
discovered candidates: it decides insert vs. update vs. skip against the
existing library rows, runs de-dup collapse passes, rebuilds series
intelligence, and tracks job progress/status for the polling endpoints.
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import date, datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

import discovery_engine
import models
import library_sync
from book_metadata_utils import parse_publication_date
from database import SessionLocal
from intelligence import recalculate_intelligence, recalculate_series_state_for_series
from agents.series_agent import SeriesIntelligenceAgent
from services.discovery_logging import _console_log, log_discovery_summary
from services.discovery_cache import DiscoveryCache
from services.discovery_telemetry import DiscoveryTelemetry
from services.identity import (
    _authors_match_exact,
    _canonical_title_identity_key,
    _edition_priority,
    _normalize_discovered_title,
    owned_title_for_identity,
    _series_book_identity_key,
    _series_number_slot_key,
)
from services.notifications import create_series_discovery_notification
from services.skeleton_store import apply_skeleton_updates

logger = logging.getLogger(__name__)

series_agent = SeriesIntelligenceAgent()
series_check_jobs: dict[int, dict] = {}
SERIES_CHECK_TIMEOUT_SECONDS = 300
SERIES_CHECK_HARD_TIMEOUT_SECONDS = 300
# Bounds the internal catch-up loop below, not a single discovery call --
# see discovery_catchup_architecture_spec.md #2.2/#4. Empirically validated
# against a real long/under-indexed series (Jonathan Hunt, 18 volumes from a
# single starting book): full reconstruction completed within 2 rounds, with
# a 3rd confirming "nothing further" -- 4 would only add cost with no
# observed benefit.
SERIES_CHECK_MAX_ROUNDS = 3
# Gates the cheap catalog-only pre-check (discovery_catchup_architecture_
# spec.md #7.2): a series last fully checked within this many days skips
# straight to a Google Books/OpenLibrary/Hardcover-only fetch (zero
# web-search, zero LLM calls) before committing to the full multi-round
# loop. Chosen to sit safely below services/auto_discovery.py's
# AUTO_DISCOVERY_COOLDOWN (7 days) so it never fires on the primary
# recurring sweep cadence -- only on genuinely redundant close-together
# re-checks.
SERIES_CHECK_PRECHECK_STALENESS_DAYS = 3
# Temporarily OFF while actively developing/debugging discovery itself
# (2026-08-23): this cost optimization is exactly what made a manual Check
# Now on an already-recently-checked series (e.g. Jonathan Hunt, the
# hardest-to-discover series found so far and the one being used to verify
# discovery end-to-end) silently skip the entire web-search/Apify/LLM
# pipeline and return in ~1-2s -- indistinguishable from a real failure
# when the whole point of re-running is to check whether discovery itself
# now works. Series.last_checked is still written normally on every full
# run (see agents/series_agent.py) -- only this pre-check's use of that
# timestamp to skip re-running discovery is disabled here. Flip back to
# True once discovery is confirmed working end-to-end and repeated manual
# re-checks of the same series are no longer the primary dev workflow.
#
# LitRPG-discovery-plan isolated validation (2026-08-25): re-checked
# discovery_engine.precheck_for_new_volumes itself against every fusion/
# gate/delta_engine/known-set fix made since this was disabled (fusion
# grouping, per-number-slot gate confidence, delta_engine's duplicate_number
# narrowing, the known-identity-sets poisoning fix) -- it calls none of
# that code (no _fuse_and_score_candidates, no catalog_providers_are_
# sufficient; enable_web_search=False means _fetch_all_providers_parallel's
# own gate block is never even reached) and its existing test coverage
# (PrecheckForNewVolumesTest) still passes unchanged, so its logic is
# confirmed independent and safe to re-enable on its own merits. Left OFF
# here anyway: the LitRPG test pass this validation was done for is itself
# exactly the "repeated manual re-check to verify discovery" workflow the
# comment above describes, so flipping this now would risk the same
# silent-skip confusion on a LitRPG series checked twice in quick
# succession. Flip to True once that test pass is done.
SERIES_CHECK_PRECHECK_ENABLED = False


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


# Fields a user (or an earlier, richer discovery pass) may have set that
# the dedupe collapse passes below must not silently lose just because the
# row holding them scored lower overall (e.g. a re-discovered duplicate
# with a cleaner title but no date info, competing against an older row
# that has a confirmed release_date on it). Deliberately excludes fields
# the collapse's own scoring already governs (asin, edition/format,
# is_read, publication_date's *presence*) to avoid fighting the keeper
# selection logic -- this only backfills gaps, never overwrites a value the
# keeper already has.
_DEDUPE_MERGE_FIELDS = (
    "release_date",
    "publication_date",
    "read_date",
    "rating",
    "review",
    "notes",
    "source_url",
    "isbn",
    "isbn13",
    "goodreads_id",
    "storygraph_id",
    "google_books_id",
)


def _merge_loser_fields_into_keeper(keeper: models.Book, loser: models.Book) -> None:
    """Copy any of _DEDUPE_MERGE_FIELDS the loser has that the keeper is
    missing, before the loser is marked deleted. Without this, a dedupe
    collapse can silently discard a user-entered or previously-discovered
    date/rating/note just because the *other* row won on edition/is_read/
    asin -- see the Quest Academy / Ultimate Level / The Bad Guys cases
    this was written to fix.
    """
    for field in _DEDUPE_MERGE_FIELDS:
        if getattr(keeper, field, None) in (None, ""):
            loser_value = getattr(loser, field, None)
            if loser_value not in (None, ""):
                setattr(keeper, field, loser_value)

    if bool(loser.is_read) and not bool(keeper.is_read):
        keeper.is_read = True
        keeper.read_date = keeper.read_date or loser.read_date
        keeper.read_status = "read"


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
        # Not crud.get_series: this internal background job is only ever
        # scheduled for a series_id the API layer already validated against
        # the caller's profile (see routers/series.py's /check endpoint) --
        # re-deriving/threading a profile_id all the way through the job
        # queue would be redundant, since a series_id alone already
        # uniquely identifies one profile via its own profile_id column.
        db_series = db.query(models.Series).filter(models.Series.id == series_id).first()
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

        # Per-job, in-memory only -- shared across every round below so
        # web-search/LLM call counts and timings are cumulative for the
        # whole job, not reset each round (see discovery_catchup_
        # architecture_spec.md #2.5).
        telemetry = DiscoveryTelemetry()
        # Also per-job/in-memory/shared-across-rounds, and always active
        # (no policy gate -- see spec #7.1): dedupes provider fetches and
        # LLM verdicts that repeat identically across rounds/passes within
        # this one job. Discarded along with `telemetry` when the job ends.
        discovery_cache = DiscoveryCache()
        job_started_at = time.monotonic()

        # ---- Bounded multi-round catch-up loop (architecture spec #2.2) ----
        # One click must be able to fully reconstruct a long/under-indexed
        # series from a single owned book, without the user needing to
        # press Check Now repeatedly. Each round re-runs discovery against
        # whatever highest_owned_book_number is *now* (the previous round's
        # persistence just advanced it), persists what it found, then
        # decides whether another round is worth running. Persistence
        # itself (dedupe/insert/update, both identity-collapse passes)
        # happens every round -- round N+1 must see round N's inserts -- but
        # the notification, series-intelligence rebuild, and job-status
        # write are deliberately deferred until after the loop (see
        # "Finalization" below), so a multi-round catch-up looks like one
        # atomic Check Now to the user, not several.
        all_persisted_new_books: list[dict] = []
        total_discovery_delta_count = 0
        all_provider_failures: list[dict] = []
        any_all_providers_failed = False
        rounds_run = 0
        timed_out = False
        idle_check = False
        last_result: dict = {}
        # FIX-PB-7: apply_skeleton_updates below is deliberately never
        # allowed to fail the round (a stale/un-swept skeleton self-heals
        # on the next run) -- but a silently-swallowed failure was only
        # ever visible in server logs. Accumulated here and surfaced on the
        # job dict / status endpoint response instead, still with NO
        # rollback of the already-committed Book persistence.
        skeleton_update_failures: list[str] = []

        # ---- Cheap pre-check for a recently-checked series (architecture
        # spec #7.2) ----
        # A series checked within the last SERIES_CHECK_PRECHECK_STALENESS_
        # DAYS days gets one catalog-only (zero web-search, zero LLM) fetch
        # before committing to the full multi-round loop -- if nothing
        # shows up numbered beyond what's already known (owned, upcoming,
        # or a tracked interior gap), skip the full loop entirely. A
        # series with no check history at all (last_checked is None -- the
        # very first Check Now click right after adding it) always runs
        # the full loop; there's no prior baseline to compare against.
        run_full_loop = True
        if SERIES_CHECK_PRECHECK_ENABLED and db_series and db_series.last_checked is not None:
            days_since_checked = (date.today() - db_series.last_checked).days
            if 0 <= days_since_checked <= SERIES_CHECK_PRECHECK_STALENESS_DAYS:
                known_active_books = (
                    db.query(models.Book)
                    .filter(models.Book.series_id == series_id)
                    .filter(or_(models.Book.record_status.is_(None), models.Book.record_status != "deleted"))
                    .all()
                )
                known_numbers = [float(b.book_number) for b in known_active_books if b.book_number is not None]
                known_numbers += [float(v) for v in (db_series.missing_books or []) if v is not None]
                ceiling = max(known_numbers) if known_numbers else 0.0

                found_something_new = discovery_engine.precheck_for_new_volumes(
                    db_series.name, db_series.author, ceiling, telemetry=telemetry
                )
                # Note: precheck_for_new_volumes deliberately does not take
                # `discovery_cache` -- it's a single, un-cacheable catalog
                # fetch (there's no second round within this same job to
                # reuse it against), and its own results shouldn't seed the
                # full loop's cache for a *different* purpose (confirming
                # "nothing new" vs. exhaustively enumerating candidates).
                telemetry.record_gate_outcome(
                    "precheck", "short_circuited" if not found_something_new else "fell_through_to_full_loop"
                )
                if not found_something_new:
                    run_full_loop = False
                    idle_check = True
                    last_result = {
                        "series_id": series_id,
                        "added_books": [],
                        "found": False,
                        "discovery_engine": "precheck",
                        "status": "no_hits",
                        "provider_failures": [],
                        "all_providers_failed": False,
                    }

        for _round_num in range(1, SERIES_CHECK_MAX_ROUNDS + 1) if run_full_loop else []:
            # The shared timeout bounds *scheduling of new rounds for the
            # whole job*, not each round individually -- prevents a naive
            # worst case of SERIES_CHECK_MAX_ROUNDS x
            # SERIES_CHECK_HARD_TIMEOUT_SECONDS. An already-in-flight round
            # can't be forcibly cancelled (ThreadPoolExecutor can't preempt
            # a running thread -- ProcessPoolExecutor-only capability), so
            # the last round may still overrun the shared budget by up to
            # one round's own cost. Documented limitation, not a bug (see
            # architecture spec #2.3).
            remaining_budget = SERIES_CHECK_HARD_TIMEOUT_SECONDS - (time.monotonic() - job_started_at)
            if remaining_budget <= 0:
                timed_out = True
                break

            executor = ThreadPoolExecutor(max_workers=1)
            future = executor.submit(
                series_agent.run_series_check, db, series_id, update_progress, False, telemetry, discovery_cache
            )
            try:
                result = future.result(timeout=remaining_budget)
            except FutureTimeoutError:
                timed_out = True
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
                    # The discovery thread itself is NOT stopped by
                    # cancel_futures=True below (Python can't preempt an
                    # already-running thread) -- it keeps mutating this same
                    # telemetry object in the background even after we've
                    # given up waiting, so this snapshot only reflects what
                    # had been recorded at the moment of timeout, not the
                    # eventual total.
                    "telemetry": telemetry.summary(),
                }
            finally:
                executor.shutdown(wait=False, cancel_futures=True)

            rounds_run += 1
            last_result = result

            round_provider_failures = result.get("provider_failures") or []
            all_provider_failures.extend(round_provider_failures)
            for failure in round_provider_failures:
                logger.info(
                    "Provider %s failed: %s",
                    failure.get("provider"),
                    failure.get("error") or "unknown",
                )
            if result.get("all_providers_failed"):
                any_all_providers_failed = True

            db_series = db.query(models.Series).filter(models.Series.id == series_id).first()
            if not db_series:
                raise RuntimeError(f"Series {series_id} not found during check job")

            today = date.today()
            existing_books = (
                db.query(models.Book)
                .filter(models.Book.series_id == series_id)
                # Defense-in-depth alongside the profile_id fix below on newly
                # created books: series_id should already uniquely scope this to
                # one profile, but matching should never silently consider a
                # stray row from a different profile that somehow ended up
                # pointed at this series_id.
                .filter(models.Book.profile_id == db_series.profile_id)
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

                series_book_key = _series_book_identity_key(
                    series_id, owned_title_for_identity(existing), existing.author, existing.book_number
                )
                if series_book_key and series_book_key not in existing_by_series_book:
                    existing_by_series_book[series_book_key] = existing

                canonical_title_key = _canonical_title_identity_key(owned_title_for_identity(existing))
                if canonical_title_key and canonical_title_key not in existing_by_canonical_title:
                    existing_by_canonical_title[canonical_title_key] = existing

            persisted_new_books_this_round: list[dict] = []
            discovered_candidates = result.get("added_books") or []
            seen_batch_identity_keys: set[str] = set()
            db_changed = False
            # Counts upcoming->available transitions on existing rows for
            # *this* round only -- summed into total_discovery_delta_count
            # below, alongside every other round's count, for the single
            # end-of-job notification (see architecture spec #2.2).
            transitioned_to_available_count = 0

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
                    normalized_author = candidate_author
                    normalized_book_number = canonical_metadata.get("book_number_normalized")
                    if normalized_book_number is None:
                        normalized_book_number = candidate.get("book_number")
                    candidate_asin = str(candidate.get("asin_or_id") or "").strip().upper()

                    series_book_key = _series_book_identity_key(
                        series_id, normalized_title, normalized_author, normalized_book_number
                    )
                    canonical_title_key = _canonical_title_identity_key(normalized_title)

                    matched_existing: models.Book | None = None
                    dedupe_reason_code = ""
                    if candidate_asin and candidate_asin in existing_by_asin:
                        matched_existing = existing_by_asin[candidate_asin]
                        dedupe_reason_code = "DEDUPE_UPDATE_BY_ASIN"
                    elif series_book_key and series_book_key in existing_by_series_book:
                        matched_existing = existing_by_series_book[series_book_key]
                        dedupe_reason_code = "DEDUPE_UPDATE_BY_SERIES_BOOK"
                    elif canonical_title_key and canonical_title_key in existing_by_canonical_title:
                        matched_existing = existing_by_canonical_title[canonical_title_key]
                        dedupe_reason_code = "DEDUPE_UPDATE_BY_TITLE"

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

                        # Captured before this candidate's status overwrites
                        # read_status/is_upcoming_auto below -- this is the only
                        # way to tell "was upcoming, now available" (a
                        # notification trigger, per the Auto Discovery MVP
                        # spec's §3) from "was already available" (not a
                        # trigger) once the fields are updated.
                        was_upcoming_before_update = (
                            str(matched_existing.read_status or "").strip().lower() == "upcoming"
                            or bool(matched_existing.is_upcoming_auto)
                            or bool(matched_existing.is_upcoming_final)
                        )

                        matched_existing.title = normalized_title or matched_existing.title
                        # Check Now is exempt from FIND and already provider-
                        # sourced by construction (see the Add Book metadata
                        # intake design) -- a confirmed match refresh is exactly
                        # the "system enrichment" case allowed to move
                        # metadata_source to "discovery" even if the row was
                        # previously "user"/"provider", and to update
                        # canonical_title even though it's otherwise never
                        # user-editable.
                        matched_existing.canonical_title = normalized_title or matched_existing.canonical_title
                        matched_existing.metadata_source = "discovery"
                        matched_existing.author = normalized_author or matched_existing.author
                        if candidate_asin:
                            matched_existing.asin = candidate_asin
                        if not matched_existing.source_url and candidate.get("source_url"):
                            matched_existing.source_url = str(candidate.get("source_url")).strip()

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

                        if was_upcoming_before_update and status != "upcoming":
                            transitioned_to_available_count += 1

                        continue

                    db_book = models.Book(
                        # This construction used to omit profile_id entirely,
                        # which the Book model's now-removed default (CR-10;
                        # see models.py) silently filled in as "robbie" -- so
                        # every book discovered by "Check for New" on any
                        # *other* profile's series silently got
                        # profile_id="robbie" while staying linked to that
                        # other profile's series_id. The result was an
                        # invisible ghost row: excluded from every
                        # profile-scoped books query (so neither profile could
                        # see or delete it), yet still counted by
                        # series_id-only aggregates like
                        # compute_series_intelligence_for_series, inflating
                        # that series' total_books/upcoming flags with a
                        # "phantom" book.
                        profile_id=db_series.profile_id,
                        title=normalized_title,
                        # Check Now has no user-entered title to preserve, so
                        # both title columns get the same resolved value (see
                        # the Add Book metadata intake design's two-column title
                        # model) -- unlike a FIND bind, there's nothing here
                        # that needs protecting from being overwritten.
                        canonical_title=normalized_title,
                        metadata_source="discovery",
                        book_number_source="provider" if book_number is not None else None,
                        author=normalized_author,
                        series_id=series_id,
                        book_number=book_number,
                        series_order=int(book_number) if book_number is not None and float(book_number).is_integer() else None,
                        publication_date=publication_date,
                        release_date=expected_date,
                        date_added=today,
                        asin=candidate_asin or None,
                        source_url=str(candidate.get("source_url")).strip() if candidate.get("source_url") else None,
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
                    inserted_series_book_key = _series_book_identity_key(
                        series_id, owned_title_for_identity(db_book), db_book.author, db_book.book_number
                    )
                    if inserted_series_book_key:
                        existing_by_series_book[inserted_series_book_key] = db_book
                    inserted_title_key = _canonical_title_identity_key(owned_title_for_identity(db_book))
                    if inserted_title_key:
                        existing_by_canonical_title[inserted_title_key] = db_book

                    persisted_new_books_this_round.append(
                        {
                            "id": int(db_book.id),
                            "title": db_book.title,
                            "author": db_book.author,
                            "asin": db_book.asin,
                            "is_missing": bool(db_book.is_missing),
                            "status": status,
                            "date_published": db_book.publication_date.isoformat() if db_book.publication_date else None,
                            "expected_date": db_book.release_date.isoformat() if db_book.release_date else None,
                            "source_url": db_book.source_url,
                            "series_id": series_id,
                            "library_position": "top",
                        }
                    )

                # Durable series-level discovery notification (see services/
                # notifications.py): counted here per round, but the actual
                # notification fires only once, after the loop, with the
                # total summed across every round (see architecture spec
                # #2.2) -- a per-round fire would spam the user with
                # multiple "new books found" notifications from one click.
                new_available_insert_count = sum(
                    1 for book in persisted_new_books_this_round if book.get("status") != "upcoming"
                )
                round_discovery_delta_count = new_available_insert_count + transitioned_to_available_count
                total_discovery_delta_count += round_discovery_delta_count

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
                        # Lenient series+number-only key (never title/author) --
                        # this pass's job is to collapse rows that already
                        # share a series+number slot despite a title mismatch;
                        # see _series_number_slot_key's own docstring.
                        key = _series_number_slot_key(series_id, existing.book_number) or ""
                    if not key:
                        key = _canonical_title_identity_key(owned_title_for_identity(existing)) or ""
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
                    _merge_loser_fields_into_keeper(identity_keeper[key], loser)
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
                    # Same lenient series+number-only key as the pass above --
                    # this is the "collapse regardless of title" final strict
                    # pass, not candidate-vs-existing matching.
                    series_book_key = _series_number_slot_key(series_id, existing.book_number)
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
                    _merge_loser_fields_into_keeper(series_book_keeper[series_book_key], loser)
                    loser.record_status = "deleted"
                    db_changed = True

                if db_changed:
                    db.commit()
                    db.refresh(db_series)

                logger.info("LIBRARY_SYNC_TRIGGERED series_id=%s", series_id)
                library_sync.update_from_series(series_id, profile_id=db_series.profile_id)

                # Phase 0 write call site #2 for SeriesSkeleton (see
                # discovery_agentic_replacement_recommendation.md §0.1 and
                # discovery_agentic_replacement_evaluation.md §4/§8). No
                # agent exists yet, so result.get("skeleton_updates")/
                # ("probes") are always None today -- this call is a
                # documented no-op beyond running the discovered-entry
                # retention sweep (see services/skeleton_store.py). It's
                # wired here now so: (1) the single-writer-per-row
                # upsert-with-retry protection is exercised at both real
                # write call sites (this one and main.py's boot backfill)
                # before Phase 1 needs either one live under real
                # concurrency, and (2) Phase 1 can start populating
                # `result["skeleton_updates"]`/`["probes"]` in
                # agents/series_agent.py without any change needed here.
                # Never allowed to fail the round itself -- a stale/
                # un-swept skeleton is a staleness issue the next run (or
                # next boot) self-heals, not a reason to lose otherwise-
                # successful persistence.
                try:
                    apply_skeleton_updates(
                        db,
                        series_id,
                        skeleton_updates=result.get("skeleton_updates"),
                        probes=result.get("probes"),
                    )
                    telemetry.record_gate_outcome("skeleton_update", "succeeded")
                except Exception as exc:
                    logger.exception(
                        "Post-persistence skeleton update failed for series_id=%s", series_id
                    )
                    telemetry.record_gate_outcome("skeleton_update", "failed")
                    skeleton_update_failures.append(f"round {rounds_run}: {exc}")
            except Exception:
                db.rollback()
                raise

            all_persisted_new_books.extend(persisted_new_books_this_round)

            # ---- Stop condition (architecture spec #2.2, revised) ----
            # Nothing NEW persisted this round -- another round would just
            # re-discover/re-update the same rows for no benefit.
            #
            # The spec's originally-proposed second stop condition --
            # "stop if this round's candidates didn't reach the top of its
            # own lookahead window" -- was tried and measurably wrong: live
            # Brave results for "book N" queries are noisy enough that a
            # round can legitimately fall short of its own window's top
            # while later volumes still exist (confirmed live against the
            # Jonathan Hunt case: round 1 found up through book 9 against a
            # book-2..11 window and would have stopped there, but books
            # 10-18 were real and only surfaced in a later round). Relying
            # solely on "zero new books" costs at most one extra wasted
            # round (bounded by SERIES_CHECK_MAX_ROUNDS regardless), in
            # exchange for actually guaranteeing one-click full
            # reconstruction, which is the entire point of this loop.
            if not persisted_new_books_this_round:
                break
            if timed_out:
                break

        # ---- Finalization: runs exactly once, after the loop ----
        # Built from a fresh dict rather than mutating last_result in place:
        # series_agent.run_series_check's return value isn't ours to
        # mutate, and tests that mock it with one shared return_value
        # object across multiple calls would otherwise see that same mock
        # corrupted after the first round.
        result = dict(last_result)
        result["added_books"] = all_persisted_new_books
        result["added_count"] = len(all_persisted_new_books)
        # See the durable-notification block above -- new inserts (excluding
        # upcoming-only ones) plus upcoming->available transitions, summed
        # across every round. Kept as a distinct field rather than folding
        # into added_count/added_books, which already have an established
        # "fresh inserts only" meaning relied on elsewhere (e.g. services/
        # discovery_logging.py's debug summary). This is what both the
        # ephemeral popup and the durable notification row use, so the two
        # numbers can never disagree.
        result["discovery_delta_count"] = total_discovery_delta_count
        result["provider_failures"] = all_provider_failures
        result["all_providers_failed"] = any_all_providers_failed
        result["rounds_run"] = rounds_run
        result["timed_out"] = timed_out
        # See architecture spec #7.3 -- distinguishes "confirmed nothing
        # new via the cheap catalog-only pre-check" (rounds_run stays 0)
        # from "ran the full loop and it happened to find nothing".
        result["idle_check"] = idle_check
        result["telemetry"] = telemetry.summary()
        result["cache"] = discovery_cache.summary()
        result["skeleton_update_failures"] = skeleton_update_failures

        db_series = db.query(models.Series).filter(models.Series.id == series_id).first()
        if not db_series:
            raise RuntimeError(f"Series {series_id} not found during check job")

        if total_discovery_delta_count > 0:
            create_series_discovery_notification(
                db,
                profile_id=db_series.profile_id,
                series_id=series_id,
                series_name=db_series.name,
                count_new_books=total_discovery_delta_count,
            )
            db.commit()

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
        elif all_persisted_new_books:
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
            "new_books": all_persisted_new_books,
            "counters": counters,
            "status_bar": status_bar,
            "complete": True,
            "missing_books": status_bar.get("missing") or [],
            "available_missing": result.get("available_missing") or [],
            "upcoming_books": result.get("upcoming_books") or [],
            "validated_candidates": result.get("validated_candidates") or [],
            "found_books": all_persisted_new_books,
            "no_new_books": response_status != "success",
            "discovery_delta_count": total_discovery_delta_count,
            "discovery_engine": result.get("discovery_engine") or "new_book_checker",
            "rounds_run": rounds_run,
            "idle_check": idle_check,
            "skeleton_update_failures": skeleton_update_failures,
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
                "discovery_delta_count": 0,
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
