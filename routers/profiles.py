from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

import models
import schemas
from routers.deps import enforce_access, get_db, require_owner

router = APIRouter(prefix="/profiles", tags=["profiles"], dependencies=[Depends(enforce_access)])


def _to_profile_response(profile: models.Profile, book_count: int) -> schemas.ProfileResponse:
    return schemas.ProfileResponse(
        id=profile.id,
        display_name=profile.display_name,
        is_default=profile.is_default,
        created_at=profile.created_at,
        book_count=book_count,
        has_data=book_count > 0,
        last_full_discovery_run_at=profile.last_full_discovery_run_at,
    )


# Registered at both "/" and "" (see main.py's redirect_slashes=False) so a
# request that arrives without its trailing slash is handled directly
# instead of needing a redirect -- same pattern as routers/books.py and
# routers/series.py.
@router.get("/", response_model=List[schemas.ProfileResponse])
@router.get("", response_model=List[schemas.ProfileResponse], include_in_schema=False)
def list_profiles(db: Session = Depends(get_db)):
    profiles = db.query(models.Profile).order_by(models.Profile.created_at.asc()).all()

    # book_count/has_data lets the frontend decide whether a profile needs
    # onboarding without a separate round-trip to /books or /series -- one
    # grouped count query covers every profile at once.
    counts_by_profile = dict(
        db.query(models.Book.profile_id, func.count(models.Book.id)).group_by(models.Book.profile_id).all()
    )

    return [_to_profile_response(profile, counts_by_profile.get(profile.id, 0)) for profile in profiles]


@router.post("/", response_model=schemas.ProfileResponse, dependencies=[Depends(require_owner)])
@router.post("", response_model=schemas.ProfileResponse, include_in_schema=False, dependencies=[Depends(require_owner)])
def create_profile(profile: schemas.ProfileCreate, db: Session = Depends(get_db)):
    profile_id = profile.id.strip().lower()
    if not profile_id:
        raise HTTPException(status_code=422, detail="Profile id is required")

    existing = db.query(models.Profile).filter(models.Profile.id == profile_id).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Profile '{profile_id}' already exists")

    db_profile = models.Profile(id=profile_id, display_name=profile.display_name, is_default=False)
    db.add(db_profile)
    db.commit()
    db.refresh(db_profile)
    return _to_profile_response(db_profile, book_count=0)


@router.patch("/{profile_id}", response_model=schemas.ProfileResponse, dependencies=[Depends(require_owner)])
def rename_profile(profile_id: str, payload: schemas.ProfileUpdate, db: Session = Depends(get_db)):
    """Rename a profile -- e.g. so whoever's library "daughter" is doesn't
    have to live with that placeholder name forever and can call it
    whatever they want. Only the display name is editable; the id (and
    therefore every profile_id-scoped row already linked to it) never
    changes."""
    display_name = payload.display_name.strip()
    if not display_name:
        raise HTTPException(status_code=422, detail="Display name is required")

    db_profile = db.query(models.Profile).filter(models.Profile.id == profile_id).first()
    if not db_profile:
        raise HTTPException(status_code=404, detail=f"Profile '{profile_id}' not found")

    db_profile.display_name = display_name
    db.commit()
    db.refresh(db_profile)

    book_count = db.query(models.Book).filter(models.Book.profile_id == profile_id).count()
    return _to_profile_response(db_profile, book_count)
