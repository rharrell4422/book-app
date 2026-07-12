import traceback
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import crud
import models
import schemas
from importer.importer import run_import
from intelligence import recalculate_intelligence
from routers.deps import enforce_access, get_db

router = APIRouter(prefix="/import", tags=["import"], dependencies=[Depends(enforce_access)])


@router.post("")
def trigger_import():
    file_path = "Test_LibraryImport_new_fields_28Jun2026.xlsx"

    try:
        result = run_import(file_path)
        return {
            "status": "success",
            "import_summary": result,
        }
    except Exception as e:
        traceback.print_exc()
        raise e


@router.get("/series_confirmations")
def get_import_series_confirmation_queue(include_resolved: bool = False, db: Session = Depends(get_db)):
    books = db.query(models.Book).all()
    queue: list[dict] = []

    for book in books:
        metadata = book.import_raw_row if isinstance(book.import_raw_row, dict) else {}
        if not metadata:
            continue

        required = bool(metadata.get("series_confirmation_required"))
        decision = str(metadata.get("series_confirmation_decision") or "").strip().lower() or None

        if not include_resolved and not required:
            continue

        queue.append(
            {
                "book_id": int(book.id),
                "title": book.title,
                "author": book.author,
                "current_series_id": book.series_id,
                "current_series_name": book.series.name if book.series else None,
                "candidate_series_name": metadata.get("series_candidate_name"),
                "reason": metadata.get("series_confirmation_reason"),
                "decision": decision,
                "title_has_series_number": bool(metadata.get("title_has_series_number")),
                "updated_at": book.updated_at.isoformat() if book.updated_at else None,
            }
        )

    queue.sort(key=lambda row: row.get("book_id") or 0)
    return {
        "pending_count": sum(1 for row in queue if row.get("decision") in (None, "", "dont_know")),
        "total_count": len(queue),
        "items": queue,
    }


@router.post("/series_confirmations/resolve")
def resolve_import_series_confirmations(payload: schemas.SeriesImportConfirmationResolveRequest, db: Session = Depends(get_db)):
    if not payload.decisions:
        return {
            "processed": 0,
            "updated": 0,
            "results": [],
        }

    results: list[dict] = []
    updated = 0
    affected_series_ids: set[int] = set()

    for decision_item in payload.decisions:
        book = crud.get_book(db, decision_item.book_id)
        if not book:
            results.append(
                {
                    "book_id": int(decision_item.book_id),
                    "status": "not_found",
                }
            )
            continue

        metadata = book.import_raw_row if isinstance(book.import_raw_row, dict) else {}
        metadata = dict(metadata)
        old_series_id = int(book.series_id) if book.series_id is not None else None

        candidate_series_name = str(decision_item.series_name or metadata.get("series_candidate_name") or "").strip() or None
        selected_decision = str(decision_item.decision)

        if selected_decision == "yes":
            if not candidate_series_name:
                results.append(
                    {
                        "book_id": int(book.id),
                        "status": "missing_candidate_series",
                        "decision": selected_decision,
                    }
                )
                continue

            canonical_series = crud.get_series_by_name(db, candidate_series_name)
            if not canonical_series:
                results.append(
                    {
                        "book_id": int(book.id),
                        "status": "canonical_series_not_found",
                        "decision": selected_decision,
                        "candidate_series_name": candidate_series_name,
                    }
                )
                continue

            book.series_id = canonical_series.id
            metadata["series_confirmation_required"] = False
            metadata["series_candidate_name"] = canonical_series.name
            metadata["series_confirmation_reason"] = metadata.get("series_confirmation_reason") or "user_confirmed"
            metadata["series_confirmation_decision"] = "yes"
            metadata["series_confirmation_decided_at"] = datetime.utcnow().isoformat()
            if decision_item.note:
                metadata["series_confirmation_note"] = str(decision_item.note)

            if old_series_id is not None:
                affected_series_ids.add(old_series_id)
            affected_series_ids.add(int(canonical_series.id))
            updated += 1
            results.append(
                {
                    "book_id": int(book.id),
                    "status": "linked",
                    "decision": "yes",
                    "series_id": int(canonical_series.id),
                    "series_name": canonical_series.name,
                }
            )

        elif selected_decision == "no":
            book.series_id = None
            metadata["series_confirmation_required"] = False
            metadata["series_confirmation_decision"] = "no"
            metadata["series_confirmation_decided_at"] = datetime.utcnow().isoformat()
            if decision_item.note:
                metadata["series_confirmation_note"] = str(decision_item.note)

            if old_series_id is not None:
                affected_series_ids.add(old_series_id)
            updated += 1
            results.append(
                {
                    "book_id": int(book.id),
                    "status": "left_unlinked",
                    "decision": "no",
                }
            )

        else:
            metadata["series_confirmation_required"] = True
            metadata["series_confirmation_decision"] = "dont_know"
            metadata["series_confirmation_decided_at"] = datetime.utcnow().isoformat()
            if decision_item.note:
                metadata["series_confirmation_note"] = str(decision_item.note)
            updated += 1
            results.append(
                {
                    "book_id": int(book.id),
                    "status": "kept_pending",
                    "decision": "dont_know",
                }
            )

        book.import_raw_row = metadata
        db.add(book)

    db.commit()

    for series_id in sorted(affected_series_ids):
        recalculate_intelligence(db, int(series_id))

    return {
        "processed": len(payload.decisions),
        "updated": updated,
        "recalculated_series_ids": sorted(affected_series_ids),
        "results": results,
    }
