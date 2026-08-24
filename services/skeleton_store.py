"""Durable per-series book-lineup memory (SeriesSkeleton) -- Phase 1 of
agentic discovery (see project design chat), with the Phase 0 correctness
fixes from `discovery_agentic_replacement_recommendation.md` §0.1 and
`discovery_agentic_replacement_evaluation.md` §4/§8 applied on top.

Two independent write paths land on the same `SeriesSkeleton` row, and both
are implemented here (see the evaluation doc §4: these are "two different
write paths," not one function with two callers):

1. `backfill_skeleton_for_series` -- rebuild-from-`Book`-rows. Called on
   boot (`main.py`'s `backfill_all_skeletons`) and once per Check Now run
   (`agents/series_agent.py`, before confidence scoring). Deterministic,
   zero LLM/network cost, takes no candidate/finding input at all -- its
   only inputs are the series' current owned `Book` rows.
2. `apply_skeleton_updates` -- apply-agent-findings-post-persistence.
   Called from `services/series_check_engine.py` after each round's
   persistence, with `agents/series_agent.py`'s `needs_review` candidates
   mapped onto `skeleton_updates` (PB-1) -- see
   `_needs_review_to_skeleton_updates` there. `probes` is still always
   empty; no probe schema exists yet (Phase 1). This function's
   concurrency protection is exercised at both real call sites (this one
   and the boot backfill) either way, and the retention sweep (below)
   runs on every check, not only at boot.

PB-6 (doc-only, no behavior change): a *third*, unrelated "skeleton"
concept also exists -- `discovery_engine._reconstruct_series_skeleton`,
called from `agents/series_agent.py`'s discovery loop (not this module).
That one is an ephemeral, recomputed-from-scratch-every-call 1..N
expected-volume-numbers model used only to decide which interior gap
numbers deserve a targeted lookahead search *during* one discovery pass --
it is never persisted and has nothing to do with the durable
`models.SeriesSkeleton` row this module owns, despite the shared name.
The duplication (both derive "which numbers are owned/known" from
overlapping inputs) is real and tracked
(`discovery_agentic_migration_architecture_map.md`'s call for
`_reconstruct_series_skeleton` to eventually read the durable skeleton
instead of recomputing), but consolidating them is a structural change
out of scope here -- see `_reconstruct_series_skeleton`'s own docstring
for the cross-reference.

Both paths merge asymmetrically rather than overwrite, and both funnel
through the same `_upsert_skeleton_row` core for single-writer-per-row
protection (see its docstring) -- one merge rule, shared, not reimplemented
per call site.

Asymmetric merge rule (§0.1, finalized in the Phase 0 review loop):
  - Library-sourced entries (`sources[].provider == "library"`, tagged
    `source_class: "library"`) are *fully rebuilt fresh* from current
    `Book` rows every time, keyed by `book_number`. This is what makes a
    user removing/correcting an owned book's number reflect immediately,
    exactly like the old (pre-fix) full-rebuild behavior did.
  - Discovered entries (`source_class: "discovered"`) are *preserved
    across the rebuild* and merged in, not overwritten -- except when a
    library entry now exists for that same `book_number` (the library
    entry always wins; see `_merge_discovered_entries`) or the entry has
    aged out under the retention policy below.
  - This asymmetry is deliberate, not a looser "never overwrite anything"
    rule: a naive "preserve everything already in the row" merge would let
    a stale library-sourced entry linger forever after its owning `Book`
    row is deleted/corrected, silently regressing a case that works
    correctly today.

Retention policy for discovered-but-never-owned entries (the other
Phase 0 gap the evaluation doc flags in §4/§8): three options were on the
table -- persist indefinitely, expire via TTL, or require a second agent
confirmation before becoming durable. TTL is the Phase 0 policy:

  - "Persist indefinitely" is wrong for a slot Phase 1 will populate with
    LLM/provider-inferred guesses -- a wrong guess that's never corrected
    would sit in the skeleton forever, quietly corrupting every future
    `title_confidence`/`number_confidence` computation against it (see
    confidence_engine.py).
  - "Require a second confirmation before becoming durable" can't be
    implemented as a *gate on writing at all* without defeating the whole
    point of durable memory -- the recommendation doc's motivating case is
    capturing "book 14 exists, unowned, confirmed" on the *first* finding,
    not the second. Instead, `apply_skeleton_updates` grants provisional
    durability on the first write and *refreshes* `last_confirmed_at` on
    every subsequent reconfirmation (a later run re-reporting the same
    number), which extends the TTL below. A number nothing ever
    reconfirms simply ages out; one an agent keeps finding never does.
    This gets the safety benefit of "confirmation keeps it alive" without
    losing first-time memory.
  - Net policy: a `discovered` entry not upgraded to `library` (i.e. the
    book is never actually added to the owned library) and not
    reconfirmed within `DISCOVERED_ENTRY_TTL_DAYS` of its
    `last_confirmed_at` is dropped on the next rebuild/merge.
"""

import logging
import random
import time
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

# Bumped from 1: entries now carry `source_class` ("library" | "discovered"),
# needed to tell the two halves of the asymmetric merge apart. Pre-existing
# rows (schema_version 1) have no `source_class` at all -- treated as
# "library" wherever it matters (see `_merge_discovered_entries`), which is
# accurate: nothing before this field existed ever wrote anything else.
SCHEMA_VERSION = 2

# Retention policy for discovered-but-never-owned entries -- see module
# docstring for the full rationale and the two alternatives it rejects.
DISCOVERED_ENTRY_TTL_DAYS = 90

# FIX-SS-ENUM: the documented enum for a skeleton entry's `status` field
# (see models.SeriesSkeleton's docstring). Nothing at the DB level enforces
# this today -- skeleton_json is an untyped JSON blob -- so
# `apply_skeleton_updates` validates agent-supplied updates against it
# explicitly rather than persisting an unrecognized value silently.
VALID_SKELETON_STATUSES = {"confirmed", "unconfirmed", "upcoming"}

# Single-writer-per-row protection (see `_upsert_skeleton_row`). SQLite has
# no real row-level locking, so retries are the actual protection; these
# bounds are generous enough to absorb a boot-time backfill sweep racing a
# single in-flight Check Now job without needing operator intervention, but
# still bounded so a genuinely stuck writer fails loudly instead of hanging
# a request.
_UPSERT_MAX_ATTEMPTS = 5
_UPSERT_RETRY_BASE_DELAY_SECONDS = 0.05


def _book_to_skeleton_entry(book: "models.Book", now_iso: str, *, first_seen_at: str | None = None) -> dict:
    """Deterministic, LLM-free mapping from an owned Book row to a skeleton
    entry. An owned row is the strongest possible evidence a book exists,
    so it's always high confidence -- confidence/status only get
    interesting once Phase 2+ starts folding in provider/LLM-derived
    entries that aren't already in the library.

    `first_seen_at`: CR-5 -- when this Book row is the "upgrade" of a prior
    `discovered` skeleton entry for the same `book_number` (an agent's
    predicted-but-unowned find has now actually been added to the library),
    pass that entry's original `first_seen_at` through here so the
    freshly-rebuilt library entry preserves "when this was first found"
    provenance instead of resetting it to now, as if it were a brand-new
    discovery. Defaults to `now_iso` for a genuinely new library entry with
    no prior discovered record.
    """
    is_upcoming = bool(book.is_upcoming_auto or book.is_upcoming_final)
    release_date = book.publication_date or book.release_date
    return {
        "book_number": book.book_number,
        "title": book.title,
        "status": "upcoming" if is_upcoming else "confirmed",
        "confidence": "high",
        "release_date": release_date.isoformat() if release_date else None,
        "edition_hints": [book.edition] if book.edition else [],
        "source_class": "library",
        "sources": [
            {
                "provider": "library",
                "url": book.source_url,
                "fetched_at": now_iso,
            }
        ],
        "first_seen_at": first_seen_at or now_iso,
        "last_confirmed_at": now_iso,
    }


def _is_expired_discovered_entry(entry: dict, now: datetime) -> bool:
    """Retention check for a `source_class == "discovered"` entry that has
    never been corroborated by an owned Book row (see DISCOVERED_ENTRY_TTL_
    DAYS above). Missing/unparseable `last_confirmed_at` is treated as
    expired -- a discovered entry with no resolvable confirmation timestamp
    should not survive indefinitely just because the check couldn't run.

    CR-7: both of those cases are logged (not just silently returned) --
    this is a data-loss path (a discovered entry is about to be dropped on
    the next merge) and previously left no trace anywhere to diagnose it
    from.
    """
    raw = entry.get("last_confirmed_at") or entry.get("first_seen_at")
    if not raw:
        logger.warning(
            "Dropping discovered skeleton entry with no last_confirmed_at/first_seen_at timestamp: book_number=%s",
            entry.get("book_number"),
        )
        return True
    try:
        last_confirmed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        logger.warning(
            "Dropping discovered skeleton entry with malformed timestamp %r: book_number=%s",
            raw,
            entry.get("book_number"),
        )
        return True
    # CR-6: >=, not > -- an entry exactly at the TTL boundary (down to the
    # second) was surviving one extra cycle past its intended expiry.
    return (now - last_confirmed) >= timedelta(days=DISCOVERED_ENTRY_TTL_DAYS)


def _merge_discovered_entries(existing_entries: list, library_numbers: set, now: datetime) -> list[dict]:
    """The non-library half of the asymmetric merge (§0.1): any entry
    already tagged `source_class == "discovered"` survives a rebuild
    untouched, *except*:
      - one now superseded by a freshly-rebuilt library entry for the same
        `book_number` (the library entry always wins -- see module
        docstring), or
      - one that has aged out under the TTL retention policy above.
    Legacy entries with no `source_class` at all (pre-dating this field)
    are treated as library-sourced -- accurate, since nothing before this
    field existed ever wrote anything else -- so they are dropped here and
    re-derived fresh from `Book` rows instead of surfacing as a phantom
    "discovered" row.
    """
    survivors = []
    for entry in existing_entries or []:
        if not isinstance(entry, dict) or entry.get("source_class") != "discovered":
            continue
        if entry.get("book_number") in library_numbers:
            continue
        if _is_expired_discovered_entry(entry, now):
            continue
        survivors.append(entry)
    return survivors


def _sort_key(entry: dict):
    number = entry.get("book_number")
    return number if isinstance(number, (int, float)) else float("inf")


def _upsert_skeleton_row(
    db: Session,
    series_id: int,
    merge_fn: Callable[[list], list],
    max_attempts: int = _UPSERT_MAX_ATTEMPTS,
) -> "models.SeriesSkeleton":
    """Single-writer-per-row core shared by both write paths in this module.

    `merge_fn(existing_entries: list[dict]) -> list[dict]` computes the new
    `skeleton_json` from whatever is *currently persisted* -- re-read fresh
    on every retry attempt, not captured once outside the loop. That is
    what makes the retry actually safe against a concurrent writer's
    change, not just against a concurrent-insert race.

    Concurrency: `SeriesSkeleton` has two independent write call sites that
    can legitimately overlap in time (a boot-time backfill sweep racing an
    in-flight Check Now job's post-persistence update). SQLite has no real
    row-level locking -- `with_for_update()` compiles away to a no-op on
    this dialect -- so the actual protection is upsert-with-retry, covering
    two distinct races:

    - Concurrent INSERT (two writers both find no existing row and both
      try to INSERT): the loser's commit fails with `IntegrityError` on the
      `series_id` primary key; caught, rolled back, and retried as an
      UPDATE against the winner's now-committed row.
    - Concurrent UPDATE (CR-4): without a version check, two writers can
      each read the row, each compute `merge_fn` against their own
      (possibly already-stale) read, and both successfully UPDATE -- the
      second commit silently overwrites the first's result with one that
      never saw it. `version` (bumped by 1 on every successful write) is
      read alongside `skeleton_json`, and the UPDATE is conditioned on
      `version == <value just read>`. If another writer's UPDATE landed in
      between, this UPDATE affects zero rows -- treated as a conflict,
      rolled back, and retried against a fresh read, same as the
      IntegrityError path above.

    A transient `OperationalError` ("database is locked", possible under
    SQLite WAL with concurrent writers) is retried the same way as both of
    the above. `with_for_update()` is kept anyway so this degrades to real
    row-level locking for free if this app ever moves to a database that
    supports it (e.g. Postgres) -- the version check remains correct
    either way, it just becomes a second, redundant layer of protection.

    Commits internally (one commit per successful attempt) rather than
    leaving that to the caller -- required for the retry loop to actually
    observe/rollback its own conflicting writes. Callers must not rely on
    this call being folded into a larger uncommitted transaction.
    """
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            skeleton = (
                db.query(models.SeriesSkeleton)
                .filter(models.SeriesSkeleton.series_id == series_id)
                .with_for_update()
                .first()
            )
            existing_entries = skeleton.skeleton_json if skeleton and isinstance(skeleton.skeleton_json, list) else []
            new_entries = merge_fn(existing_entries)

            if skeleton is None:
                skeleton = models.SeriesSkeleton(
                    series_id=series_id,
                    skeleton_json=new_entries,
                    schema_version=SCHEMA_VERSION,
                    version=0,
                )
                db.add(skeleton)
                db.commit()
                return skeleton

            read_version = skeleton.version
            updated_rowcount = (
                db.query(models.SeriesSkeleton)
                .filter(
                    models.SeriesSkeleton.series_id == series_id,
                    models.SeriesSkeleton.version == read_version,
                )
                .update(
                    {
                        "skeleton_json": new_entries,
                        "schema_version": SCHEMA_VERSION,
                        "version": read_version + 1,
                    },
                    synchronize_session=False,
                )
            )
            if updated_rowcount == 0:
                # Lost the race: some other writer already advanced
                # `version` past what we read, so our merge_fn result was
                # computed against stale data. Not an exception -- just a
                # signal to retry against a fresh read, identical in effect
                # to the IntegrityError/OperationalError handling below.
                db.rollback()
                last_error = RuntimeError(
                    f"optimistic concurrency conflict on SeriesSkeleton series_id={series_id} "
                    f"(expected version={read_version})"
                )
                if attempt == max_attempts:
                    break
                time.sleep(_UPSERT_RETRY_BASE_DELAY_SECONDS * attempt + random.uniform(0, 0.02))
                continue

            db.commit()
            db.refresh(skeleton)
            return skeleton
        except (IntegrityError, OperationalError) as exc:
            db.rollback()
            last_error = exc
            if attempt == max_attempts:
                break
            # Small jittered backoff -- avoids two racing writers retrying
            # in lockstep and losing again on the very next attempt.
            time.sleep(_UPSERT_RETRY_BASE_DELAY_SECONDS * attempt + random.uniform(0, 0.02))

    logger.error(
        "Failed to upsert SeriesSkeleton for series_id=%s after %s attempts: %s",
        series_id,
        max_attempts,
        last_error,
    )
    raise RuntimeError(f"Failed to upsert SeriesSkeleton for series_id={series_id}") from last_error


def backfill_skeleton_for_series(db: Session, series_id: int) -> "models.SeriesSkeleton | None":
    """(Re)builds the library-sourced half of the skeleton for one series
    from its current active Book rows, and asymmetrically merges in
    whatever `discovered` entries survive the retention policy -- see
    module docstring for the full merge rule. Safe to call repeatedly, and
    now also safe to call concurrently with `apply_skeleton_updates` or
    another call to this same function for the same series (see
    `_upsert_skeleton_row`).
    """
    series = db.query(models.Series).filter(models.Series.id == series_id).first()
    if series is None:
        return None

    active_books = [
        book
        for book in (series.books or [])
        if str(book.record_status or "active") != "deleted" and book.book_number is not None
    ]
    active_books.sort(key=lambda book: book.book_number)

    now = datetime.utcnow()
    now_iso = now.isoformat()

    def merge_fn(existing_entries: list) -> list:
        # CR-5: a discovered entry being upgraded to a real owned Book row
        # (the recommendation doc's "upgrade" case) preserves its original
        # first_seen_at instead of losing that provenance to a fresh
        # "now" timestamp -- see _book_to_skeleton_entry's first_seen_at
        # parameter. Read fresh from existing_entries on every attempt,
        # same as the rest of this merge, so it stays correct under retry.
        discovered_first_seen_by_number = {
            entry.get("book_number"): entry.get("first_seen_at")
            for entry in (existing_entries or [])
            if isinstance(entry, dict) and entry.get("source_class") == "discovered" and entry.get("first_seen_at")
        }
        library_entries = [
            _book_to_skeleton_entry(
                book, now_iso, first_seen_at=discovered_first_seen_by_number.get(book.book_number)
            )
            for book in active_books
        ]
        library_numbers = {entry["book_number"] for entry in library_entries}
        discovered_survivors = _merge_discovered_entries(existing_entries, library_numbers, now)
        return sorted(library_entries + discovered_survivors, key=_sort_key)

    return _upsert_skeleton_row(db, series_id, merge_fn)


def apply_skeleton_updates(
    db: Session,
    series_id: int,
    skeleton_updates: list[dict] | None = None,
    probes: list[dict] | None = None,
) -> "models.SeriesSkeleton | None":
    """Apply agent-returned findings to the skeleton, post-persistence.

    Distinct from `backfill_skeleton_for_series` (see module docstring and
    `discovery_agentic_replacement_evaluation.md` §4: "two different write
    paths ... it isn't a variant of `backfill_skeleton_for_series` -- that
    function's whole shape is 'rebuild from Book rows,' it takes no
    candidate/finding input at all"). This function takes findings as
    input and never touches `Book` rows or library-sourced entries.

    No true agentic engine exists yet in Phase 0 (see the recommendation
    doc), but as of PB-1, `SeriesIntelligenceAgent.run_series_check`
    (`agents/series_agent.py`) does populate `skeleton_updates` from this
    round's `needs_review` candidates -- see
    `_needs_review_to_skeleton_updates` there for why that bucket and not
    `available_missing`/`upcoming_books`. `probes` is still always `[]`;
    no probe schema exists yet (Phase 1). `services/series_check_engine.py`
    calls this once per round with whatever
    `result.get("skeleton_updates")`/`result.get("probes")` happen to be.
    Called every round regardless of whether either is non-empty, so that:
      (1) the single-writer-per-row protection in `_upsert_skeleton_row` is
          exercised at *both* real call sites (this one and `main.py`'s
          boot backfill), and
      (2) the retention sweep below runs on every check, not only at boot,
          so a `discovered` entry actually ages out on schedule rather
          than only at the next server restart.

    `skeleton_updates`: list of dicts shaped like a skeleton entry (at
    minimum `book_number`; anything else -- `title`, `status`,
    `confidence`, `release_date`, `edition_hints`, `sources` -- is passed
    through as given). Applied by `book_number`:
      - A `book_number` already owned by a library entry is never
        overwritten by a discovered update -- library entries are only
        ever produced by `backfill_skeleton_for_series` and are always
        authoritative over an agent's unconfirmed finding for the same
        number.
      - A `book_number` with no existing entry, or an existing
        `discovered` entry, is upserted as `source_class: "discovered"`.
        `first_seen_at` is preserved from the prior entry if one existed
        (this is the "when did the agent first find this" timestamp the
        recommendation doc calls out); `last_confirmed_at` is always
        refreshed to now -- this is the reconfirmation that extends the
        retention TTL (see module docstring).

    `probes`: reserved for Phase 1's negative/inconclusive-probe memory
    (`discovery_agentic_replacement_recommendation.md` §2.1). No probe
    schema exists yet, so this is accepted-and-logged, not silently
    dropped -- the signature already reflects the intended future shape so
    `series_check_engine.py` needs no changes when Phase 1 starts
    populating it.
    """
    series = db.query(models.Series).filter(models.Series.id == series_id).first()
    if series is None:
        return None

    if probes:
        logger.info(
            "apply_skeleton_updates: probe memory is not implemented yet (Phase 1) -- "
            "ignoring %d probe(s) for series_id=%s",
            len(probes),
            series_id,
        )

    updates = [update for update in (skeleton_updates or []) if isinstance(update, dict) and update.get("book_number") is not None]
    now = datetime.utcnow()
    now_iso = now.isoformat()

    def merge_fn(existing_entries: list) -> list:
        by_number: dict = {}
        for entry in existing_entries or []:
            if isinstance(entry, dict) and entry.get("book_number") is not None:
                by_number[entry["book_number"]] = entry

        library_numbers = {
            number
            for number, entry in by_number.items()
            # Legacy (schema_version 1) entries have no source_class at all
            # and are always library-sourced -- see module docstring.
            if entry.get("source_class", "library") == "library"
        }

        for update in updates:
            number = update["book_number"]
            if number in library_numbers:
                # The library is always authoritative over an unconfirmed
                # agent finding for a number it already owns.
                continue
            previous = by_number.get(number)
            merged_entry = dict(update)

            # FIX-SS-ENUM: reject/drop an unrecognized status instead of
            # silently persisting it -- fall back to the previous entry's
            # status (if any) rather than inventing one.
            status_value = merged_entry.get("status")
            if status_value is not None and status_value not in VALID_SKELETON_STATUSES:
                logger.warning(
                    "apply_skeleton_updates: dropping unrecognized status %r for "
                    "series_id=%s book_number=%s (valid: %s)",
                    status_value,
                    series_id,
                    number,
                    sorted(VALID_SKELETON_STATUSES),
                )
                if previous is not None and previous.get("status") in VALID_SKELETON_STATUSES:
                    merged_entry["status"] = previous["status"]
                else:
                    merged_entry.pop("status", None)

            merged_entry["source_class"] = "discovered"
            merged_entry["first_seen_at"] = (previous or {}).get("first_seen_at") or update.get("first_seen_at") or now_iso
            merged_entry["last_confirmed_at"] = now_iso
            by_number[number] = merged_entry

        survivors = [
            entry
            for entry in by_number.values()
            if entry.get("source_class") != "discovered" or not _is_expired_discovered_entry(entry, now)
        ]
        return sorted(survivors, key=_sort_key)

    return _upsert_skeleton_row(db, series_id, merge_fn)


def backfill_all_skeletons() -> None:
    """One-time (and re-runnable) backfill across every series, called on
    boot the same way bootstrap.backfill_series_state already is. Owns its
    own session since it runs standalone at startup, not nested inside an
    existing request's db session. Each series now commits independently
    (see `_upsert_skeleton_row`), so one series' failure -- concurrency
    conflict exhausted, or otherwise -- is logged and skipped rather than
    losing every other series' backfill along with it.
    """
    from database import SessionLocal

    db = SessionLocal()
    try:
        series_ids = [row.id for row in db.query(models.Series.id).all()]
        for series_id in series_ids:
            try:
                backfill_skeleton_for_series(db, series_id)
            except Exception:
                logger.exception("Boot-time skeleton backfill failed for series_id=%s", series_id)
    finally:
        db.close()
