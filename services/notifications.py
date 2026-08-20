"""Minimal "New Books Added to Library" notifications (Auto Discovery MVP
spec, §3).

Deliberately not a full inbox: one row per triggering event, a single
"unseen" list, and one bulk dismiss action. Rows are created from the
shared low-level persistence path in services/series_check_engine.py so
both manual Check Now and the batch Full Auto Discovery sweep get the same
behavior for free.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

import models


def create_new_book_notification(db: Session, book: "models.Book", kind: str = "new_book") -> models.Notification:
    """Records a notification for `book`. Does not commit -- the caller
    (series_check_engine's persistence loop) already commits once per
    change-set; piggybacking on that keeps this atomic with the book
    insert/update it's describing.
    """
    notification = models.Notification(
        profile_id=book.profile_id,
        book_id=book.id,
        series_id=book.series_id,
        kind=kind,
    )
    db.add(notification)
    return notification


def get_undismissed_notifications(db: Session, profile_id: str) -> list[models.Notification]:
    return (
        db.query(models.Notification)
        .filter(models.Notification.profile_id == profile_id)
        .filter(models.Notification.dismissed_at.is_(None))
        .order_by(models.Notification.created_at.desc())
        .all()
    )


def dismiss_all_notifications(db: Session, profile_id: str) -> int:
    """Bulk dismiss -- the modal has one "Got it" action, not a per-item
    dismiss, so there's no need for a notification_ids parameter here.
    """
    now = datetime.utcnow()
    updated = (
        db.query(models.Notification)
        .filter(models.Notification.profile_id == profile_id)
        .filter(models.Notification.dismissed_at.is_(None))
        .update({models.Notification.dismissed_at: now}, synchronize_session=False)
    )
    db.commit()
    return int(updated or 0)
