from sqlalchemy.orm import Session
from typing import List, Optional

import models
import schemas


# ---------------------------------------------------------
# CREATE SERIES
# ---------------------------------------------------------
def create_series(db: Session, series: schemas.SeriesCreate):
    db_series = models.Series(
        name=series.name,
        total_books=series.total_books,
        is_finished=series.is_finished,
    )

    db.add(db_series)
    db.commit()
    db.refresh(db_series)
    return db_series


# ---------------------------------------------------------
# READ ALL SERIES
# ---------------------------------------------------------
def get_all_series(db: Session) -> List[models.Series]:
    return db.query(models.Series).order_by(models.Series.name.asc()).all()


# ---------------------------------------------------------
# READ SERIES BY ID
# ---------------------------------------------------------
def get_series(db: Session, series_id: int) -> Optional[models.Series]:
    return db.query(models.Series).filter(models.Series.id == series_id).first()


# ---------------------------------------------------------
# UPDATE SERIES
# ---------------------------------------------------------
def update_series(db: Session, series_id: int, series: schemas.SeriesUpdate):
    db_series = db.query(models.Series).filter(models.Series.id == series_id).first()
    if not db_series:
        return None

    update_data = series.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_series, key, value)

    db.commit()
    db.refresh(db_series)
    return db_series


# ---------------------------------------------------------
# DELETE SERIES
# ---------------------------------------------------------
def delete_series(db: Session, series_id: int):
    db_series = db.query(models.Series).filter(models.Series.id == series_id).first()
    if not db_series:
        return None

    db.delete(db_series)
    db.commit()
    return True
