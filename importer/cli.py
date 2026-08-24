"""Command-line entry point for running the importer directly (`python -m
importer.importer <file>`).

Split out of importer/importer.py (RT-6). Thin wrapper around
reset.reset_database and pipeline.run_import -- all the actual import
logic lives in those modules; this just parses argv and wires them
together the same way the old `if __name__ == "__main__":` block did.

importer/importer.py re-exports parse_args and calls main() from its own
`if __name__ == "__main__":` block, so `python -m importer.importer ...`
keeps working exactly as before.
"""
from __future__ import annotations

import argparse

from sqlalchemy.orm import Session

from database import SessionLocal
from importer.pipeline import DEFAULT_IMPORT_PROFILE_ID, run_import
from importer.reset import reset_database


def parse_args():
    parser = argparse.ArgumentParser(description="Import books from CSV/XLSX into Book App database")
    parser.add_argument("file", help="Path to import file (.csv/.xlsx/.xls)")
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Wipe books and series tables before import",
    )
    parser.add_argument(
        "--profile",
        default=DEFAULT_IMPORT_PROFILE_ID,
        help=f"Profile id to attribute imported rows to (default: {DEFAULT_IMPORT_PROFILE_ID})",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.reset_db:
        reset_session: Session = SessionLocal()
        try:
            deleted_books, deleted_series = reset_database(reset_session)
            print(f"Database reset complete. Deleted {deleted_books} books and {deleted_series} series.")
        finally:
            reset_session.close()

    run_import(args.file, profile_id=args.profile)
