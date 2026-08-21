"""Durable series-level discovery notifications (see the "Durable
Series-Level Discovery Notifications" design chat's finalized spec).

One row per series per discovery run (kind="series_discovery_delta"),
written once at the end of a `run_series_check_job_full` call after that
run's brand-new inserts and upcoming->available transitions for the
series have been counted -- shared by both manual Check Now and the batch
Full Auto Discovery sweep, since both go through that same function.
Deliberately not per-book: the old kind="new_book" rows this replaces are
left in the table (retired via migration, filtered out by kind here) --
see models.Notification's docstring.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

import models

SERIES_DISCOVERY_DELTA_KIND = "series_discovery_delta"


def create_series_discovery_notification(
    db: Session, *, profile_id: str, series_id: int, series_name: str, count_new_books: int
) -> models.Notification:
    """Records one aggregated notification for a series' discovery run.
    Does not commit -- the caller (series_check_engine's persistence loop)
    already commits once per change-set; piggybacking on that keeps this
    atomic with the book inserts/updates it's describing.
    """
    notification = models.Notification(
        profile_id=profile_id,
        series_id=series_id,
        series_name=series_name,
        count_new_books=count_new_books,
        kind=SERIES_DISCOVERY_DELTA_KIND,
    )
    db.add(notification)
    return notification


def get_undismissed_notifications(db: Session, profile_id: str) -> list[models.Notification]:
    return (
        db.query(models.Notification)
        .filter(models.Notification.profile_id == profile_id)
        .filter(models.Notification.kind == SERIES_DISCOVERY_DELTA_KIND)
        .filter(models.Notification.dismissed_at.is_(None))
        .order_by(models.Notification.created_at.desc())
        .all()
    )


def dismiss_notification(db: Session, profile_id: str, notification_id: int) -> bool:
    """Per-item dismiss for the Notifications view. Returns False (no-op)
    if the row doesn't exist, isn't this profile's, or is already
    dismissed -- callers decide whether that's a 404 or a quiet success.
    """
    notification = (
        db.query(models.Notification)
        .filter(models.Notification.id == notification_id)
        .filter(models.Notification.profile_id == profile_id)
        .filter(models.Notification.dismissed_at.is_(None))
        .first()
    )
    if not notification:
        return False
    notification.dismissed_at = datetime.utcnow()
    db.commit()
    return True


def dismiss_all_notifications(db: Session, profile_id: str) -> int:
    """Bulk "Dismiss all" action in the Notifications view."""
    now = datetime.utcnow()
    updated = (
        db.query(models.Notification)
        .filter(models.Notification.profile_id == profile_id)
        .filter(models.Notification.kind == SERIES_DISCOVERY_DELTA_KIND)
        .filter(models.Notification.dismissed_at.is_(None))
        .update({models.Notification.dismissed_at: now}, synchronize_session=False)
    )
    db.commit()
    return int(updated or 0)
