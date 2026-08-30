from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import schemas
from routers.deps import enforce_access, get_current_profile_id, get_db
from services.notifications import (
    dismiss_all_notifications,
    dismiss_notification,
    get_undismissed_notifications,
)

router = APIRouter(prefix="/notifications", tags=["notifications"], dependencies=[Depends(enforce_access)])


# Durable series-level discovery notifications (see the "Durable
# Series-Level Discovery Notifications" design chat's finalized spec) --
# one row per series per discovery run. series_name/count_new_books are
# cached directly on the row at write time, so no join is needed here the
# way the old per-book shape needed one against Book/Series.
@router.get("/unseen", response_model=list[schemas.NotificationItem])
def read_unseen_notifications(db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    notifications = get_undismissed_notifications(db, profile_id)
    return [
        schemas.NotificationItem(
            id=notification.id,
            series_id=notification.series_id,
            series_name=notification.series_name,
            count_new_books=notification.count_new_books or 0,
            # Nullable at the DB level for rows written before this column
            # existed (see models.Notification's docstring) -- those fall
            # back to an empty list rather than erroring.
            book_titles=notification.book_titles_json or [],
            created_at=notification.created_at,
        )
        for notification in notifications
    ]


# POST, not GET -- this mutates dismissed_at, so it goes through
# enforce_access's owner-only branch like every other write in this app.
@router.post("/{notification_id}/dismiss", response_model=schemas.NotificationDismissResponse)
def dismiss_one_notification(
    notification_id: int, db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)
):
    dismissed = dismiss_notification(db, profile_id, notification_id)
    if not dismissed:
        raise HTTPException(status_code=404, detail="Notification not found")
    return schemas.NotificationDismissResponse(dismissed_count=1)


# Bulk "Dismiss all" action in the Notifications view -- registered after
# the per-item route above; FastAPI matches literal path segments before
# path params of the same depth, so "/notifications/dismiss" here can
# never be mistaken for "/notifications/{notification_id}/dismiss" (one
# segment vs. two) regardless of declaration order, but keeping this last
# still reads clearly as "the item-level route is the primary one now".
@router.post("/dismiss", response_model=schemas.NotificationDismissResponse)
def dismiss_notifications(db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    dismissed_count = dismiss_all_notifications(db, profile_id)
    return schemas.NotificationDismissResponse(dismissed_count=dismissed_count)
