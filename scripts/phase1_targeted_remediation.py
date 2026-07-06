#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database import SessionLocal
from intelligence import OMNIBUS_RANGE_PATTERN, purge_orphaned_books, recalculate_intelligence
from models import Book, Series


DIAGNOSTICS_DIR = ROOT_DIR / "diagnostics"
RUN_DATE = datetime.now().date().isoformat()
REPORT_CSV_PATH = DIAGNOSTICS_DIR / f"phase1_targeted_remediation_{RUN_DATE}.csv"
REPORT_MD_PATH = DIAGNOSTICS_DIR / f"phase1_targeted_remediation_{RUN_DATE}.md"

TRAILING_BOOK_MARKER_PATTERN = re.compile(r"\s*\([^)]*book\s+\d+(?:\.\d+)?[^)]*\)\s*$", re.IGNORECASE)
OMNIBUS_COMPRESSED_TAG = "omnibus-compressed"


@dataclass
class SeriesInventory:
    series_id: int
    series_name: str
    total_books: int
    visible_active_rows: int
    non_deleted_rows: int
    delta: int
    ghost_rows: list[Book]
    safe_placeholder_rows: list[Book]
    omnibus_range_rows: int
    numbered_max_non_deleted: int
    classification: str = ""
    action: str = ""
    notes: str = ""
    deleted_count: int = 0
    before_total_books: int = 0
    after_total_books: int = 0
    after_visible_active_rows: int = 0
    remaining_delta: int = 0


def book_number_value(book: Book) -> float | None:
    value = book.book_number if book.book_number is not None else book.series_order
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_non_deleted(book: Book) -> bool:
    return str(book.record_status or "active") != "deleted"


def is_visible_active(book: Book) -> bool:
    return is_non_deleted(book) and (not bool(book.is_upcoming_final))


def is_ghost_row(book: Book) -> bool:
    return bool(book.is_missing) or bool(book.is_upcoming_auto) or bool(book.is_upcoming_final)


def is_omnibus_range_title(title: str | None) -> bool:
    return bool(OMNIBUS_RANGE_PATTERN.search(str(title or "")))


def normalized_placeholder_prefix(title: str | None) -> str:
    raw_title = str(title or "").strip()
    without_suffix = TRAILING_BOOK_MARKER_PATTERN.sub("", raw_title).strip()
    return re.sub(r"\s+", " ", without_suffix.lower())


def is_safe_placeholder_title(book: Book, series_name: str) -> bool:
    if not is_ghost_row(book):
        return False

    title = str(book.title or "").strip()
    if not title:
        return True

    lower_title = title.lower()
    lower_series = str(series_name or "").strip().lower()

    if lower_title.startswith("missing book "):
        return True
    if "books by " in lower_title:
        return True
    if " international:" in lower_title or lower_title.startswith("international:"):
        return True
    if lower_series and (lower_title == lower_series or lower_title.startswith(f"{lower_series}:") or lower_title.startswith(f"{lower_series} series:")):
        return True

    return False


def normalized_series_tags(series: Series) -> set[str]:
    raw_tags = getattr(series, "tags", None)
    if not isinstance(raw_tags, list):
        return set()
    return {
        str(tag).strip().lower()
        for tag in raw_tags
        if str(tag).strip()
    }


def compute_inventory(db) -> list[SeriesInventory]:
    inventories: list[SeriesInventory] = []
    for series in db.query(Series).order_by(Series.id).all():
        if OMNIBUS_COMPRESSED_TAG in normalized_series_tags(series):
            continue

        books = list(series.books or [])
        non_deleted_books = [book for book in books if is_non_deleted(book)]
        visible_active_books = [book for book in non_deleted_books if not bool(book.is_upcoming_final)]
        total_books = int(series.total_books or 0)
        delta = total_books - len(visible_active_books)
        if delta == 0:
            continue

        numbered_values = []
        for book in non_deleted_books:
            number = book_number_value(book)
            if number is None or number <= 0 or not float(number).is_integer():
                continue
            numbered_values.append(int(number))

        ghost_rows = [book for book in non_deleted_books if is_ghost_row(book)]
        safe_placeholder_rows = [book for book in ghost_rows if is_safe_placeholder_title(book, series.name)]
        omnibus_range_rows = sum(1 for book in non_deleted_books if is_omnibus_range_title(book.title))

        inventories.append(
            SeriesInventory(
                series_id=int(series.id),
                series_name=str(series.name),
                total_books=total_books,
                visible_active_rows=len(visible_active_books),
                non_deleted_rows=len(non_deleted_books),
                delta=delta,
                ghost_rows=ghost_rows,
                safe_placeholder_rows=safe_placeholder_rows,
                omnibus_range_rows=omnibus_range_rows,
                numbered_max_non_deleted=max(numbered_values) if numbered_values else 0,
                before_total_books=total_books,
                after_total_books=total_books,
                after_visible_active_rows=len(visible_active_books),
                remaining_delta=delta,
            )
        )
    return inventories


def classify_inventory(inventory: SeriesInventory) -> None:
    safe_placeholder_count = len(inventory.safe_placeholder_rows)
    ghost_count = len(inventory.ghost_rows)

    if inventory.non_deleted_rows == 0:
        inventory.classification = "false_positive_zero_book_series"
        inventory.action = "no_write"
        inventory.notes = "Residual mismatch is from the legacy left-join counting bug; the series currently has zero linked books."
        return

    if inventory.omnibus_range_rows > 0:
        inventory.classification = "phase1_omnibus_range_no_action"
        inventory.action = "no_write"
        inventory.notes = "Omnibus range titles are present, so Phase 1 leaves this series untouched instead of applying omnibus normalization."
        return

    placeholder_prefixes = {normalized_placeholder_prefix(book.title) for book in inventory.safe_placeholder_rows if str(book.title or "").strip()}
    ghost_prefixes = {normalized_placeholder_prefix(book.title) for book in inventory.ghost_rows if str(book.title or "").strip()}
    ghost_rows_without_source = sum(1 for book in inventory.ghost_rows if not str(book.import_source or "").strip())
    if safe_placeholder_count >= 3 and safe_placeholder_count == ghost_count and len(placeholder_prefixes) <= max(3, safe_placeholder_count):
        inventory.classification = "synthetic_ghost_batch"
        inventory.action = "purge_safe_placeholder_ghosts"
        inventory.notes = "All ghost-flagged rows in this series look like synthetic placeholder inserts, so they are safe to tombstone under Phase 1 rules."
        return

    if ghost_count >= 3 and ghost_rows_without_source == ghost_count and len(ghost_prefixes) <= 3:
        inventory.classification = "synthetic_ghost_batch"
        inventory.action = "purge_safe_placeholder_ghosts"
        inventory.notes = "Ghost-flagged rows form a repeated no-source placeholder batch with a small prefix set, so they are safe to tombstone under Phase 1 rules."
        return

    if inventory.delta < 0:
        inventory.classification = "metadata_undercount_recalc_candidate"
        inventory.action = "recalculate_only"
        inventory.notes = "Visible active rows exceed total_books; a per-series recalc can safely sync counters."
        return

    if ghost_count > 0:
        inventory.classification = "stale_or_mixed_ghost_flags_manual_review"
        inventory.action = "no_write"
        inventory.notes = "Ghost flags are mixed with real catalog rows or too sparse for safe automated deletion."
        return

    inventory.classification = "metadata_overcount_manual_review"
    inventory.action = "no_write"
    inventory.notes = "No Phase 1-safe ghost or orphan correction is available for this mismatch."


def sync_total_to_remaining_catalog(series: Series, remaining_books: list[Book]) -> None:
    numbered_values = []
    for book in remaining_books:
        number = book_number_value(book)
        if number is None or number <= 0 or not float(number).is_integer():
            continue
        numbered_values.append(int(number))
    numbered_max = max(numbered_values) if numbered_values else 0
    series.total_books = max(len(remaining_books), numbered_max)


def recalc_visible_delta(series: Series) -> tuple[int, int, int]:
    non_deleted_books = [book for book in (series.books or []) if is_non_deleted(book)]
    visible_active_books = [book for book in non_deleted_books if not bool(book.is_upcoming_final)]
    total_books = int(series.total_books or 0)
    return total_books, len(visible_active_books), total_books - len(visible_active_books)


def apply_correction(db, inventory: SeriesInventory) -> None:
    series = db.query(Series).filter(Series.id == inventory.series_id).first()
    if not series:
        inventory.notes = f"{inventory.notes} Series row not found during execution."
        return

    if inventory.action == "purge_safe_placeholder_ghosts":
        deletion_candidates = inventory.safe_placeholder_rows
        if inventory.classification == "synthetic_ghost_batch":
            deletion_candidates = inventory.ghost_rows

        deleted_count = 0
        for book in deletion_candidates:
            live_book = db.query(Book).filter(Book.id == book.id).first()
            if not live_book or not is_non_deleted(live_book):
                continue
            live_book.record_status = "deleted"
            deleted_count += 1
        if deleted_count:
            db.commit()

        remaining_books = [book for book in (series.books or []) if is_non_deleted(book)]
        sync_total_to_remaining_catalog(series, remaining_books)
        db.commit()
        recalculate_intelligence(db, inventory.series_id)
        inventory.deleted_count = deleted_count
        inventory.notes = f"{inventory.notes} Deleted {deleted_count} synthetic placeholder ghost rows."

    elif inventory.action == "recalculate_only":
        recalculate_intelligence(db, inventory.series_id)
        inventory.notes = f"{inventory.notes} Recalculated per-series intelligence only."

    refreshed_series = db.query(Series).filter(Series.id == inventory.series_id).first()
    if refreshed_series:
        total_books, visible_active_rows, delta = recalc_visible_delta(refreshed_series)
        inventory.after_total_books = total_books
        inventory.after_visible_active_rows = visible_active_rows
        inventory.remaining_delta = delta


def build_reports(inventories: list[SeriesInventory], orphaned_deleted_count: int, execute: bool) -> None:
    inventories = sorted(inventories, key=lambda item: (item.classification, item.series_id))
    classification_counts = Counter(item.classification for item in inventories)
    action_counts = Counter(item.action for item in inventories)
    corrected_count = sum(1 for item in inventories if item.action != "no_write")
    resolved_count = sum(1 for item in inventories if item.action != "no_write" and item.remaining_delta == 0)
    remaining_count = sum(1 for item in inventories if item.remaining_delta != 0)

    DIAGNOSTICS_DIR.mkdir(parents=True, exist_ok=True)

    with REPORT_CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "series_id",
            "series_name",
            "classification",
            "action",
            "before_total_books",
            "before_visible_active_rows",
            "before_delta",
            "ghost_rows",
            "safe_placeholder_rows",
            "omnibus_range_rows",
            "deleted_count",
            "after_total_books",
            "after_visible_active_rows",
            "remaining_delta",
            "notes",
        ])
        for item in inventories:
            writer.writerow([
                item.series_id,
                item.series_name,
                item.classification,
                item.action,
                item.before_total_books,
                item.visible_active_rows,
                item.delta,
                len(item.ghost_rows),
                len(item.safe_placeholder_rows),
                item.omnibus_range_rows,
                item.deleted_count,
                item.after_total_books,
                item.after_visible_active_rows,
                item.remaining_delta,
                item.notes,
            ])

    with REPORT_MD_PATH.open("w", encoding="utf-8") as handle:
        handle.write(f"# Phase 1 Targeted Remediation Report ({RUN_DATE})\n\n")
        handle.write(f"- Mode: {'execute' if execute else 'dry-run'}\n")
        handle.write(f"- Requested batch note: user requested 58 residual mismatch series; live database inventory at runtime found {len(inventories)} residual series before Phase 1 execution.\n")
        handle.write(f"- Orphaned rows purged: {orphaned_deleted_count}\n")
        handle.write(f"- Series with automated action: {corrected_count}\n")
        handle.write(f"- Series fully resolved by automated action: {resolved_count}\n")
        handle.write(f"- Residual series still mismatched after execution: {remaining_count}\n\n")

        handle.write("## Grouped By Classification\n")
        for classification, count in sorted(classification_counts.items()):
            handle.write(f"- {classification}: {count}\n")
        handle.write("\n## Grouped By Action\n")
        for action, count in sorted(action_counts.items()):
            handle.write(f"- {action}: {count}\n")

        handle.write("\n## Series Details\n")
        for item in inventories:
            handle.write(
                f"- [{item.series_id}] {item.series_name}: {item.classification}; action={item.action}; "
                f"before_delta={item.delta}; deleted={item.deleted_count}; remaining_delta={item.remaining_delta}. {item.notes}\n"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 1 targeted remediation for residual series mismatches.")
    parser.add_argument("--execute", action="store_true", help="Apply safe Phase 1 corrections instead of running in dry-run mode.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        inventories = compute_inventory(db)
        for inventory in inventories:
            classify_inventory(inventory)

        orphaned_result = purge_orphaned_books(db) if args.execute else {"deleted_count": 0}

        if args.execute:
            for inventory in inventories:
                if inventory.action == "no_write":
                    continue
                apply_correction(db, inventory)

        build_reports(inventories, int(orphaned_result.get("deleted_count") or 0), args.execute)
        print(f"report_csv={REPORT_CSV_PATH}")
        print(f"report_md={REPORT_MD_PATH}")
        print(f"series_count={len(inventories)}")
        print(f"execute={args.execute}")
    finally:
        db.close()


if __name__ == "__main__":
    main()