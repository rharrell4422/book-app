"""One-time data repair: restore fields that the "Check Now" dedupe collapse
passes (services/series_check_engine.py) previously discarded when pruning a
duplicate book row.

Background: the dedupe collapse picks a single "keeper" row per
(series, book_number) or per identity key and marks every other matching row
`record_status = "deleted"`. Before the accompanying code fix in this same
change, it never copied over fields the loser had that the keeper was
missing -- so a duplicate created by a later, cleaner-titled re-discovery
could silently blank out a previously-confirmed release_date/publication_date
(and, in principle, rating/review/notes/read_date) on the surviving active
row. This is what caused e.g. "Quest Academy" to regress to "Needs Date
Verification" despite having a correct date on file weeks earlier.

This script finds every (series_id, book_number) group that has both an
active row and a soft-deleted row, and backfills any of the same
_DEDUPE_MERGE_FIELDS onto the active survivor if it's missing them and the
deleted sibling has them. It never un-deletes anything and never overwrites
a value the active row already has.

Defaults to a dry run (report only). Pass --apply to write changes.

Run with: python scripts/repair_dedupe_lost_data.py [--apply]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import or_

from database import SessionLocal
from models import Book, Series
from services.series_check_engine import _DEDUPE_MERGE_FIELDS, _merge_loser_fields_into_keeper


def repair(apply: bool) -> None:
    db = SessionLocal()
    try:
        groups = (
            db.query(Book.series_id, Book.book_number)
            .filter(Book.series_id.isnot(None), Book.book_number.isnot(None))
            .group_by(Book.series_id, Book.book_number)
            .all()
        )

        series_names = {s.id: s.name for s in db.query(Series.id, Series.name).all()}
        repaired: list[tuple[int, str, float, int, int, list[str]]] = []

        for series_id, book_number in groups:
            rows = (
                db.query(Book)
                .filter(Book.series_id == series_id, Book.book_number == book_number)
                .all()
            )
            actives = [r for r in rows if (r.record_status or "active") != "deleted"]
            deleteds = [r for r in rows if r.record_status == "deleted"]
            if not actives or not deleteds:
                continue

            for active in actives:
                before = {field: getattr(active, field, None) for field in _DEDUPE_MERGE_FIELDS}
                for deleted in deleteds:
                    _merge_loser_fields_into_keeper(active, deleted)
                changed = [
                    field
                    for field in _DEDUPE_MERGE_FIELDS
                    if before[field] != getattr(active, field, None)
                ]
                if changed:
                    repaired.append(
                        (series_id, series_names.get(series_id, "?"), book_number, active.id, deleteds[0].id, changed)
                    )

        print(f"{'Would repair' if not apply else 'Repaired'} {len(repaired)} active book row(s):\n")
        for series_id, series_name, book_number, active_id, deleted_id, changed in repaired:
            print(
                f"  series={series_name!r} (id={series_id}) book_number={book_number} "
                f"active_id={active_id} (from deleted sibling id={deleted_id}): backfilled {changed}"
            )

        if apply and repaired:
            db.commit()
            print("\nChanges committed.")
        elif repaired:
            print("\nDry run only -- no changes written. Re-run with --apply to commit.")
            db.rollback()
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write the repaired fields to the database.")
    args = parser.parse_args()
    repair(apply=args.apply)
