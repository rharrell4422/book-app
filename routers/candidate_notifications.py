from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import schemas
from routers.deps import enforce_access, get_current_profile_id, get_db
from services.candidate_notifications import (
    build_review_urls,
    get_unresolved_candidate_notifications,
    resolve_add_to_series,
    resolve_do_not_add,
)

router = APIRouter(
    prefix="/notifications/candidates", tags=["notifications"], dependencies=[Depends(enforce_access)]
)


# "Review Candidate Book" notifications (LitRPG Enhanced Discovery design
# chat's finalized spec) -- durable, actionable surface for ambiguous/
# low-confidence discovery candidates that agents/series_agent.py's
# needs_review routing branch no longer silently folds into SeriesSkeleton.
# review_urls is computed here rather than requiring a second round-trip,
# so the frontend's Review action can open both links directly from the
# list response.
@router.get("", response_model=list[schemas.CandidateNotificationItem])
def read_candidate_notifications(db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    notifications = get_unresolved_candidate_notifications(db, profile_id)
    return [
        schemas.CandidateNotificationItem(
            id=notification.id,
            series_id=notification.series_id,
            series_name=notification.series_name,
            candidate_title=notification.candidate_title,
            candidate_number=notification.candidate_number,
            overall_confidence=notification.overall_confidence,
            provider_confidence=notification.provider_confidence,
            isbn13=notification.isbn13,
            publication_date=notification.publication_date,
            asin=notification.asin,
            author=notification.author,
            source_url=notification.source_url,
            provider=notification.provider,
            series_name_hint=notification.series_name_hint,
            reason_flags=list(notification.reason_flags or []),
            created_at=notification.created_at,
            last_seen_at=notification.last_seen_at,
            review_urls=build_review_urls(notification),
        )
        for notification in notifications
    ]


# POST, not GET -- persists a real Book row and resolves the notification.
@router.post("/{notification_id}/add", response_model=schemas.CandidateNotificationAddResponse)
def add_candidate_to_series(
    notification_id: int, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)
):
    book = resolve_add_to_series(db, profile_id=profile_id, notification_id=notification_id)
    if not book:
        raise HTTPException(status_code=404, detail="Candidate notification not found")
    return schemas.CandidateNotificationAddResponse(book_id=book.id, series_id=book.series_id, title=book.title)


# "Do Not Add" -- permanently suppresses this candidate (see
# services/candidate_notifications.resolve_do_not_add and
# create_or_refresh_candidate_notification's ignore-lookup docstring).
@router.post("/{notification_id}/ignore", response_model=schemas.CandidateNotificationResolveResponse)
def ignore_candidate_notification(
    notification_id: int, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)
):
    resolved = resolve_do_not_add(db, profile_id=profile_id, notification_id=notification_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="Candidate notification not found")
    return schemas.CandidateNotificationResolveResponse(resolved=True)
