from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import models
import schemas
from routers.deps import enforce_access, get_current_profile_id, get_db
from services.notifications import dismiss_all_notifications, get_undismissed_notifications

router = APIRouter(prefix="/notifications", tags=["notifications"], dependencies=[Depends(enforce_access)])


@router.get("/unseen", response_model=list[schemas.NotificationItem])
def read_unseen_notifications(db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    notifications = get_undismissed_notifications(db, profile_id)
    items = []
    for notification in notifications:
        book = db.query(models.Book).filter(models.Book.id == notification.book_id).first() if notification.book_id else None
        series = db.query(models.Series).filter(models.Series.id == notification.series_id).first() if notification.series_id else None
        items.append(
            schemas.NotificationItem(
                id=notification.id,
                kind=notification.kind,
                book_id=notification.book_id,
                book_title=(book.display_title if book else None),
                series_id=notification.series_id,
                series_name=(series.name if series else None),
                created_at=notification.created_at,
            )
        )
    return items


# POST, not GET -- this mutates dismissed_at, so it goes through
# enforce_access's owner-only branch like every other write in this app.
@router.post("/dismiss", response_model=schemas.NotificationDismissResponse)
def dismiss_notifications(db: Session = Depends(get_db), profile_id: str = Depends(get_current_profile_id)):
    dismissed_count = dismiss_all_notifications(db, profile_id)
    return schemas.NotificationDismissResponse(dismissed_count=dismissed_count)
