"""One-time/offline data repair: fix Book rows whose profile_id doesn't
match the profile_id of the Series they're linked to.

See intelligence.find_ghost_profile_books / repair_ghost_profile_books for
the full background on this bug -- this script is a thin CLI wrapper around
those, intended for running against a downloaded database file (e.g. from
scripts/backup_from_railway.sh) rather than the live server. For the live
Railway database, prefer the equivalent admin endpoints instead
(GET/POST /admin/ghost_profile_books), which apply directly with no
download/upload round trip.

Defaults to a dry run (report only). Pass --apply to write changes.

Run with: python scripts/repair_ghost_profile_books.py [--apply]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database import SessionLocal
from intelligence import find_ghost_profile_books, repair_ghost_profile_books


def repair(apply: bool) -> None:
    db = SessionLocal()
    try:
        if not apply:
            ghosts = find_ghost_profile_books(db)
            print(f"Would repair {len(ghosts)} ghost book row(s):\n")
            for entry in ghosts:
                print(
                    f"  book_id={entry['book_id']} title={entry['title']!r} "
                    f"series={entry['series_name']!r} (series_id={entry['series_id']}): "
                    f"profile_id {entry['current_profile_id']!r} -> {entry['correct_profile_id']!r}"
                )
            if ghosts:
                print("\nDry run only -- no changes written. Re-run with --apply to commit.")
            return

        result = repair_ghost_profile_books(db)
        print(f"Repaired {result['repaired_count']} ghost book row(s):\n")
        for entry in result["repaired_entries"]:
            print(
                f"  book_id={entry['book_id']} title={entry['title']!r} "
                f"series={entry['series_name']!r} (series_id={entry['series_id']}): "
                f"profile_id {entry['current_profile_id']!r} -> {entry['correct_profile_id']!r}"
            )
        if result["repaired_count"]:
            print("\nChanges committed.")
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write the repaired profile_id to the database.")
    args = parser.parse_args()
    repair(apply=args.apply)
