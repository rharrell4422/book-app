"""Tests for the Phase 0 skeleton merge fix (see services/skeleton_store.py's
module docstring): the asymmetric rebuild rule, the discovered-entry
retention policy, and the single-writer-per-row upsert-with-retry
protection shared by both write call sites.
"""

import copy
import logging
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from database import Base
from models import Book, Series, SeriesSkeleton
from services import skeleton_store
from services.skeleton_store import (
    DISCOVERED_ENTRY_TTL_DAYS,
    apply_skeleton_updates,
    backfill_skeleton_for_series,
)


class SkeletonStoreTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=cls.engine)
        cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setUp(self):
        # Any test module that imports `main` runs Alembic on import, and
        # Alembic's fileConfig() disables every logger that already exists
        # -- including this one, which assertLogs (used by the CR-7
        # regression test below) cannot see through. So whether that test
        # passes would otherwise depend on which other files pytest
        # happened to collect alongside this one (same fix already applied
        # in tests/test_series_discovery.py for "agents.series_agent").
        skeleton_logger = logging.getLogger("services.skeleton_store")
        was_disabled = skeleton_logger.disabled
        skeleton_logger.disabled = False
        self.addCleanup(setattr, skeleton_logger, "disabled", was_disabled)

        self.db = self.SessionLocal()
        series = Series(name="Cherry Blossom Girls", author="Harmon Cooper", profile_id="robbie")
        self.db.add(series)
        self.db.commit()
        self.db.refresh(series)
        self.series = series

        for number in [1, 2, 3]:
            self.db.add(
                Book(
                    title=f"Cherry Blossom Girls Book {number}",
                    author="Harmon Cooper",
                    series_id=series.id,
                    profile_id=series.profile_id,
                    series_order=number,
                    book_number=float(number),
                    record_status="active",
                    is_read=False,
                )
            )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _skeleton_row(self):
        return (
            self.db.query(SeriesSkeleton)
            .filter(SeriesSkeleton.series_id == self.series.id)
            .first()
        )


class AsymmetricMergeTest(SkeletonStoreTestBase):
    def test_backfill_rebuilds_library_entries_fresh_from_book_rows(self):
        backfill_skeleton_for_series(self.db, self.series.id)
        row = self._skeleton_row()
        numbers = {entry["book_number"] for entry in row.skeleton_json}
        self.assertEqual(numbers, {1.0, 2.0, 3.0})
        for entry in row.skeleton_json:
            self.assertEqual(entry["source_class"], "library")
            self.assertEqual(entry["confidence"], "high")

    def test_removing_an_owned_book_immediately_drops_its_library_entry(self):
        # This is the exact regression the asymmetric rule (vs. a naive
        # "never overwrite anything already in the row" merge) exists to
        # prevent -- see skeleton_store.py's module docstring.
        backfill_skeleton_for_series(self.db, self.series.id)
        self.assertEqual(
            {e["book_number"] for e in self._skeleton_row().skeleton_json}, {1.0, 2.0, 3.0}
        )

        book_two = (
            self.db.query(Book)
            .filter(Book.series_id == self.series.id, Book.book_number == 2.0)
            .first()
        )
        book_two.record_status = "deleted"
        self.db.commit()

        backfill_skeleton_for_series(self.db, self.series.id)
        self.assertEqual(
            {e["book_number"] for e in self._skeleton_row().skeleton_json}, {1.0, 3.0}
        )

    def test_discovered_entry_survives_a_library_rebuild(self):
        backfill_skeleton_for_series(self.db, self.series.id)

        apply_skeleton_updates(
            self.db,
            self.series.id,
            skeleton_updates=[{"book_number": 7.0, "title": "Cherry Blossom Girls Book 7", "status": "confirmed", "confidence": "medium"}],
        )
        row = self._skeleton_row()
        discovered = [e for e in row.skeleton_json if e["book_number"] == 7.0]
        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0]["source_class"], "discovered")

        # A boot-time rebuild must not destroy that agent-discovered entry.
        backfill_skeleton_for_series(self.db, self.series.id)
        row = self._skeleton_row()
        numbers = {e["book_number"]: e for e in row.skeleton_json}
        self.assertIn(7.0, numbers)
        self.assertEqual(numbers[7.0]["source_class"], "discovered")
        self.assertEqual(numbers[7.0]["title"], "Cherry Blossom Girls Book 7")
        # Owned numbers are still rebuilt fresh as library entries.
        for number in (1.0, 2.0, 3.0):
            self.assertEqual(numbers[number]["source_class"], "library")

    def test_owning_a_previously_discovered_number_lets_library_entry_win(self):
        backfill_skeleton_for_series(self.db, self.series.id)
        apply_skeleton_updates(
            self.db,
            self.series.id,
            skeleton_updates=[{"book_number": 7.0, "title": "Cherry Blossom Girls Book 7", "status": "confirmed", "confidence": "medium"}],
        )
        discovered_entry = self._skeleton_row_number(7.0)
        self.assertEqual(discovered_entry["source_class"], "discovered")
        original_first_seen_at = discovered_entry["first_seen_at"]

        self.db.add(
            Book(
                title="Cherry Blossom Girls Book 7",
                author="Harmon Cooper",
                series_id=self.series.id,
                profile_id=self.series.profile_id,
                series_order=7,
                book_number=7.0,
                record_status="active",
                is_read=False,
            )
        )
        self.db.commit()

        backfill_skeleton_for_series(self.db, self.series.id)
        entry = self._skeleton_row_number(7.0)
        self.assertEqual(entry["source_class"], "library")
        self.assertEqual(entry["confidence"], "high")
        # CR-5 regression: an "upgrade" from discovered to owned must
        # preserve the original discovery's first_seen_at ("when did we
        # first find this") instead of resetting it to the moment of the
        # upgrade -- the prior behavior lost that provenance entirely by
        # dropping the discovered entry and rebuilding a brand-new library
        # entry with a fresh timestamp.
        self.assertEqual(entry["first_seen_at"], original_first_seen_at)

    def _skeleton_row_number(self, number):
        row = self._skeleton_row()
        by_number = {e["book_number"]: e for e in row.skeleton_json}
        return by_number[number]

    def test_legacy_entry_with_no_source_class_is_treated_as_library(self):
        # schema_version 1 rows predate source_class entirely -- nothing
        # before it existed ever wrote anything but a library entry, so a
        # legacy entry must be dropped and re-derived fresh, not surface
        # as a phantom "discovered" row that never expires.
        self.db.add(
            SeriesSkeleton(
                series_id=self.series.id,
                skeleton_json=[{"book_number": 1.0, "title": "Stale Legacy Title"}],
                schema_version=1,
            )
        )
        self.db.commit()

        backfill_skeleton_for_series(self.db, self.series.id)
        entry = self._skeleton_row_number(1.0)
        self.assertEqual(entry["source_class"], "library")
        self.assertEqual(entry["title"], "Cherry Blossom Girls Book 1")


class StatusEnumValidationTest(SkeletonStoreTestBase):
    """FIX-SS-ENUM: `status` is documented (models.SeriesSkeleton's
    docstring) as one of "confirmed" | "unconfirmed" | "upcoming", but
    skeleton_json is an untyped JSON blob with no DB-level enforcement.
    `apply_skeleton_updates` must validate agent-supplied updates against
    this enum itself rather than persisting an unrecognized value silently.
    """

    def _skeleton_row_number(self, number):
        row = self._skeleton_row()
        by_number = {e["book_number"]: e for e in row.skeleton_json}
        return by_number[number]

    def test_valid_status_values_pass_through_unchanged(self):
        for status in ("confirmed", "unconfirmed", "upcoming"):
            with self.subTest(status=status):
                book_number = {"confirmed": 11.0, "unconfirmed": 12.0, "upcoming": 13.0}[status]
                apply_skeleton_updates(
                    self.db,
                    self.series.id,
                    skeleton_updates=[{"book_number": book_number, "title": "Some Title", "status": status}],
                )
                entry = self._skeleton_row_number(book_number)
                self.assertEqual(entry["status"], status)

    def test_unrecognized_status_is_dropped_not_persisted(self):
        with self.assertLogs("services.skeleton_store", level="WARNING") as cm:
            apply_skeleton_updates(
                self.db,
                self.series.id,
                skeleton_updates=[{"book_number": 9.0, "title": "Bogus Status Book", "status": "definitely_not_real"}],
            )
        self.assertTrue(any("unrecognized status" in message for message in cm.output))

        entry = self._skeleton_row_number(9.0)
        # The bad value must never land in skeleton_json -- no status key
        # at all, since there was no previous entry to fall back to.
        self.assertNotIn("status", entry)

    def test_unrecognized_status_on_reconfirmation_falls_back_to_previous_valid_status(self):
        apply_skeleton_updates(
            self.db,
            self.series.id,
            skeleton_updates=[{"book_number": 9.0, "title": "First Look", "status": "unconfirmed"}],
        )
        self.assertEqual(self._skeleton_row_number(9.0)["status"], "unconfirmed")

        with self.assertLogs("services.skeleton_store", level="WARNING"):
            apply_skeleton_updates(
                self.db,
                self.series.id,
                skeleton_updates=[{"book_number": 9.0, "title": "Reconfirmed Look", "status": "bogus"}],
            )

        entry = self._skeleton_row_number(9.0)
        # The prior valid status survives; only the bad new value is dropped.
        self.assertEqual(entry["status"], "unconfirmed")
        self.assertEqual(entry["title"], "Reconfirmed Look")

    def test_missing_status_is_left_absent_not_treated_as_an_error(self):
        # status is optional on an update -- omitting it entirely is not
        # the same as supplying an invalid value, and must not warn.
        with self.assertNoLogs("services.skeleton_store", level="WARNING"):
            apply_skeleton_updates(
                self.db,
                self.series.id,
                skeleton_updates=[{"book_number": 9.0, "title": "No Status Given"}],
            )
        entry = self._skeleton_row_number(9.0)
        self.assertNotIn("status", entry)


class RetentionPolicyTest(SkeletonStoreTestBase):
    def test_fresh_discovered_entry_is_not_expired(self):
        apply_skeleton_updates(
            self.db,
            self.series.id,
            skeleton_updates=[{"book_number": 9.0, "title": "New Find"}],
        )
        backfill_skeleton_for_series(self.db, self.series.id)
        numbers = {e["book_number"] for e in self._skeleton_row().skeleton_json}
        self.assertIn(9.0, numbers)

    def test_discovered_entry_past_ttl_is_dropped_on_next_rebuild(self):
        apply_skeleton_updates(
            self.db,
            self.series.id,
            skeleton_updates=[{"book_number": 9.0, "title": "Old Find"}],
        )
        row = self._skeleton_row()
        stale_timestamp = (
            datetime.utcnow() - timedelta(days=DISCOVERED_ENTRY_TTL_DAYS + 1)
        ).isoformat()
        # Deep-copy, not list(): shallow-copying and mutating the *same*
        # dict objects SQLAlchemy already has loaded would mutate its
        # cached "old" value too, making old == new at flush time and
        # silently skipping the UPDATE entirely (a real, easy-to-hit
        # SQLAlchemy JSON-column gotcha, not a skeleton_store.py bug --
        # production code never mutates in place, see merge_fn above).
        entries = copy.deepcopy(row.skeleton_json)
        for entry in entries:
            if entry["book_number"] == 9.0:
                entry["first_seen_at"] = stale_timestamp
                entry["last_confirmed_at"] = stale_timestamp
        row.skeleton_json = entries
        self.db.commit()

        backfill_skeleton_for_series(self.db, self.series.id)
        numbers = {e["book_number"] for e in self._skeleton_row().skeleton_json}
        self.assertNotIn(9.0, numbers)

    def test_discovered_entry_exactly_at_ttl_boundary_is_expired(self):
        # CR-6 regression: the boundary comparison used strict `>`, so an
        # entry exactly DISCOVERED_ENTRY_TTL_DAYS old survived one extra
        # rebuild past its intended expiry. Must now use `>=`.
        apply_skeleton_updates(
            self.db,
            self.series.id,
            skeleton_updates=[{"book_number": 9.0, "title": "Exactly At Boundary"}],
        )
        row = self._skeleton_row()
        boundary_timestamp = (datetime.utcnow() - timedelta(days=DISCOVERED_ENTRY_TTL_DAYS)).isoformat()
        entries = copy.deepcopy(row.skeleton_json)
        for entry in entries:
            if entry["book_number"] == 9.0:
                entry["first_seen_at"] = boundary_timestamp
                entry["last_confirmed_at"] = boundary_timestamp
        row.skeleton_json = entries
        self.db.commit()

        backfill_skeleton_for_series(self.db, self.series.id)
        numbers = {e["book_number"] for e in self._skeleton_row().skeleton_json}
        self.assertNotIn(9.0, numbers)

    def test_missing_or_malformed_timestamp_expiry_is_logged(self):
        # CR-7 regression: dropping a discovered entry for a missing or
        # unparseable timestamp is a data-loss path that previously left no
        # trace anywhere -- it must now be logged.
        apply_skeleton_updates(
            self.db,
            self.series.id,
            skeleton_updates=[{"book_number": 9.0, "title": "No Timestamp"}],
        )
        row = self._skeleton_row()
        entries = copy.deepcopy(row.skeleton_json)
        for entry in entries:
            if entry["book_number"] == 9.0:
                entry["first_seen_at"] = "not-a-real-timestamp"
                entry["last_confirmed_at"] = "not-a-real-timestamp"
        row.skeleton_json = entries
        self.db.commit()

        with self.assertLogs("services.skeleton_store", level="WARNING") as logs:
            backfill_skeleton_for_series(self.db, self.series.id)

        numbers = {e["book_number"] for e in self._skeleton_row().skeleton_json}
        self.assertNotIn(9.0, numbers)
        self.assertTrue(any("malformed timestamp" in message for message in logs.output))

    def test_reconfirming_a_discovered_entry_extends_its_ttl(self):
        apply_skeleton_updates(
            self.db,
            self.series.id,
            skeleton_updates=[{"book_number": 9.0, "title": "Repeatedly Found"}],
        )
        row = self._skeleton_row()
        near_expiry = (
            datetime.utcnow() - timedelta(days=DISCOVERED_ENTRY_TTL_DAYS - 1)
        ).isoformat()
        entries = copy.deepcopy(row.skeleton_json)
        original_first_seen = None
        for entry in entries:
            if entry["book_number"] == 9.0:
                original_first_seen = entry["first_seen_at"]
                entry["last_confirmed_at"] = near_expiry
        row.skeleton_json = entries
        self.db.commit()

        # A later run reconfirms the same finding -- this refreshes
        # last_confirmed_at (extending the TTL) without resetting
        # first_seen_at.
        apply_skeleton_updates(
            self.db,
            self.series.id,
            skeleton_updates=[{"book_number": 9.0, "title": "Repeatedly Found"}],
        )
        row = self._skeleton_row()
        entry = next(e for e in row.skeleton_json if e["book_number"] == 9.0)
        self.assertEqual(entry["first_seen_at"], original_first_seen)
        self.assertNotEqual(entry["last_confirmed_at"], near_expiry)

    def test_apply_skeleton_updates_never_overwrites_an_owned_number(self):
        # book_number 2.0 is already owned (see setUp) -- a discovered
        # update for the same number must never replace the authoritative
        # library entry.
        backfill_skeleton_for_series(self.db, self.series.id)
        apply_skeleton_updates(
            self.db,
            self.series.id,
            skeleton_updates=[{"book_number": 2.0, "title": "Wrong Guess For Book 2"}],
        )
        row = self._skeleton_row()
        entry = next(e for e in row.skeleton_json if e["book_number"] == 2.0)
        self.assertEqual(entry["source_class"], "library")
        self.assertEqual(entry["title"], "Cherry Blossom Girls Book 2")


class ConcurrencyProtectionTest(SkeletonStoreTestBase):
    def test_concurrent_insert_race_retries_as_update(self):
        """Simulates two writers racing to insert the first-ever
        SeriesSkeleton row for a series: the first call's own retry loop
        should recover from an IntegrityError (as if a concurrent writer's
        INSERT had already landed) and complete as an UPDATE instead of
        raising.
        """
        real_commit = self.db.commit
        call_count = {"n": 0}

        def flaky_commit():
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Simulate a concurrent writer having already inserted the
                # row for this series_id by the time this attempt commits.
                self.db.add(SeriesSkeleton(series_id=self.series.id + 1000, skeleton_json=[]))
                raise IntegrityError("UNIQUE constraint failed", None, None)
            return real_commit()

        with patch.object(self.db, "commit", side_effect=flaky_commit):
            result = backfill_skeleton_for_series(self.db, self.series.id)

        self.assertIsNotNone(result)
        self.assertGreaterEqual(call_count["n"], 2)
        row = self._skeleton_row()
        self.assertIsNotNone(row)

    def test_exhausting_retries_raises_instead_of_silently_losing_data(self):
        def always_fails():
            raise IntegrityError("UNIQUE constraint failed", None, None)

        with patch.object(self.db, "commit", side_effect=always_fails), patch.object(
            skeleton_store, "_UPSERT_RETRY_BASE_DELAY_SECONDS", 0
        ):
            with self.assertRaises(RuntimeError):
                backfill_skeleton_for_series(self.db, self.series.id)

    def test_second_of_two_concurrent_updates_retries_against_a_fresh_read_instead_of_clobbering_the_first(self):
        # CR-4 regression: exercises the actual optimistic-version check on
        # a genuine concurrent UPDATE of the *same* series_id row (unlike
        # test_concurrent_insert_race_retries_as_update above, which only
        # forces a generic commit-time IntegrityError against a different
        # series_id and never touches the real race). Session B writes to
        # the row *after* session A has already read it but *before* A's
        # own conditional UPDATE lands -- the exact lost-update window CR-4
        # closes: pre-fix, A's UPDATE had no version guard and would
        # silently overwrite B's already-committed write.
        backfill_skeleton_for_series(self.db, self.series.id)
        self.db.commit()

        session_b = self.SessionLocal()
        seen_existing_entries = []

        def racing_merge_fn(existing_entries):
            seen_existing_entries.append(copy.deepcopy(existing_entries))
            if len(seen_existing_entries) == 1:
                row_b = (
                    session_b.query(SeriesSkeleton)
                    .filter(SeriesSkeleton.series_id == self.series.id)
                    .first()
                )
                row_b.skeleton_json = list(existing_entries) + [
                    {"book_number": 99.0, "title": "From B", "source_class": "discovered"}
                ]
                row_b.version = row_b.version + 1
                session_b.commit()
            return list(existing_entries) + [{"book_number": 1.0, "title": "From A", "source_class": "discovered"}]

        try:
            with patch.object(skeleton_store, "_UPSERT_RETRY_BASE_DELAY_SECONDS", 0):
                result = skeleton_store._upsert_skeleton_row(self.db, self.series.id, racing_merge_fn)
        finally:
            session_b.close()

        # merge_fn must have been re-invoked after the version check caught
        # B's intervening write -- a single call would mean no retry
        # happened at all.
        self.assertEqual(len(seen_existing_entries), 2)
        # The retry's existing_entries must reflect B's committed write --
        # if this were still the stale first read, that's the silent
        # lost-update bug CR-4 fixes.
        self.assertTrue(any(entry.get("title") == "From B" for entry in seen_existing_entries[1]))
        titles = {entry["title"] for entry in result.skeleton_json}
        self.assertIn("From B", titles)
        self.assertIn("From A", titles)
        # version started at 0 from the priming backfill above; B's write
        # bumped it to 1, A's successful retry bumped it to 2.
        self.assertEqual(result.version, 2)


if __name__ == "__main__":
    unittest.main()
