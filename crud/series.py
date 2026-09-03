from sqlalchemy.orm import Session
from sqlalchemy import func, inspect

from discovery_text import _series_names_compatible, normalize_text, split_author_names
from models import Series, Book

# SeriesBase (the create/update schema) also carries a handful of derived,
# read-only fields -- read_count, unread_count, series_state, etc. -- that
# exist as @property on the Series model for API responses, not as mapped
# columns. Passing them through to the Series(**kwargs) constructor blows up
# with "property 'x' of 'Series' object has no setter", so only forward the
# fields that are actually real columns on the model.
_SERIES_COLUMN_NAMES = {column.key for column in inspect(Series).columns}


def _authors_plausibly_same(existing_author: str | None, candidate_author: str | None) -> bool:
    """Lenient author check for series-creation dedup below -- deliberately
    looser than services.identity._authors_match_exact/agents.series_agent.
    _authors_match_exact (both require the *whole* string to match). A real
    co-author added/dropped between two attempts to track the same series,
    or one side simply left blank, must not read as "different author," or
    every retry with a slightly different exact author string creates a
    fresh duplicate series row instead of being recognized as the one
    already tracked (see the Jonathan Hunt Thriller Series incident,
    2026-09-02: "Georgia Wagner" vs "Georgia Wagner; Scott Cook;" spawned two
    separate empty series shells for what both are the same tracked series).
    No author on either side is "nothing to contradict," not a mismatch --
    same convention confidence_engine._series_alignment_confidence uses.
    """
    existing_names = {normalize_text(name) for name in split_author_names(existing_author)}
    candidate_names = {normalize_text(name) for name in split_author_names(candidate_author)}
    if not existing_names or not candidate_names:
        return True
    return bool(existing_names & candidate_names)


def _find_series_for_dedup(db: Session, profile_id: str, name: str | None, author: str | None) -> Series | None:
    """Looks for an existing series (same profile) that plausibly IS the
    one about to be created, tolerating exactly the kind of drift a human
    re-typing a series name/author across multiple attempts introduces --
    a typo ("Jonathan" vs "Jonathon"), a dropped/added generic suffix word
    ("... Thriller Series" vs "... Thriller"), or a co-author added/missing.
    Reuses discovery's own `_series_names_compatible` (already proven
    against exactly this kind of cross-provider name drift) rather than a
    second, competing normalization scheme.

    Deliberately does NOT require an exact match on anything -- that's
    already what the pre-existing exact-name lookup (`get_series_by_name`,
    used by the importer) does, and it's exactly what let three near-
    duplicate empty shells get created for one series in the first place.
    """
    cleaned_name = str(name or "").strip()
    if not cleaned_name:
        return None
    for existing in db.query(Series).filter(Series.profile_id == profile_id).all():
        if not _series_names_compatible(cleaned_name, existing.name):
            continue
        if not _authors_plausibly_same(existing.author, author):
            continue
        return existing
    return None


def create_series(db: Session, series, profile_id: str):
    payload = {k: v for k, v in series.model_dump().items() if k in _SERIES_COLUMN_NAMES}
    payload["profile_id"] = profile_id

    existing = _find_series_for_dedup(db, profile_id, payload.get("name"), payload.get("author"))
    if existing is not None:
        # Backfill only, never overwrite -- same "fill gaps, don't clobber"
        # convention as services/series_check_engine.py's
        # _merge_loser_fields_into_keeper. An existing empty-shell series
        # with no author recorded yet adopts this attempt's author instead
        # of staying permanently blank; an existing series that already has
        # an author keeps it untouched even if this attempt's string is
        # differently formatted (e.g. missing a co-author) -- there's no
        # reliable way to tell "more complete" from "just different" here,
        # so the first non-empty value wins and stays.
        if not str(existing.author or "").strip() and payload.get("author"):
            existing.author = payload["author"]
            db.commit()
            db.refresh(existing)
        return existing

    db_series = Series(**payload)
    db.add(db_series)
    db.commit()
    db.refresh(db_series)
    return db_series


def get_all_series(db: Session, profile_id: str, limit: int | None = None, offset: int | None = None):
    query = db.query(Series).filter(Series.profile_id == profile_id).order_by(Series.id)
    if offset is not None:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def get_series(db: Session, series_id: int, profile_id: str):
    return db.query(Series).filter(Series.id == series_id, Series.profile_id == profile_id).first()


def get_series_by_name(db: Session, series_name: str, profile_id: str):
    cleaned = str(series_name or "").strip()
    if not cleaned:
        return None
    return (
        db.query(Series)
        .filter(Series.profile_id == profile_id, func.lower(Series.name) == cleaned.lower())
        .first()
    )


def update_series(db: Session, series_id: int, series, profile_id: str):
    db_series = db.query(Series).filter(Series.id == series_id, Series.profile_id == profile_id).first()
    if not db_series:
        return None

    for key, value in series.model_dump(exclude_unset=True).items():
        if key in _SERIES_COLUMN_NAMES:
            setattr(db_series, key, value)

    db.commit()
    db.refresh(db_series)
    return db_series


def delete_series(db: Session, series_id: int, profile_id: str):
    db_series = db.query(Series).filter(Series.id == series_id, Series.profile_id == profile_id).first()
    if not db_series:
        return None

    # Hard-delete all books linked to this series so Library and Series views
    # stay in sync. CR-9: profile_id is included here too, not just on the
    # Series lookup above -- filtering by series_id alone would also
    # delete/mutate any "ghost" cross-profile Book row that happened to
    # share this series_id, which the Series-row check above never
    # protects against.
    deleted_books = (
        db.query(Book)
        .filter(Book.series_id == series_id, Book.profile_id == profile_id)
        .delete(synchronize_session=False)
    )
    db.delete(db_series)
    db.commit()
    return {
        "series_id": series_id,
        "deleted_books": int(deleted_books or 0),
    }
