"""Durable "Review Candidate Book" notifications (see the LitRPG Enhanced
Discovery design chat's finalized spec, and models.SeriesCandidateNotification's
docstring for the full rationale).

`agents/series_agent.py`'s `needs_review` routing branch
(`low_confidence_ambiguous and (overall_grade in {"medium", None})`) is
the sole write path into this table -- see that module's `run_series_check`.
That candidate is no longer appended to `needs_review` (and therefore no
longer written into `SeriesSkeleton` as an "unconfirmed" entry either);
this table is its full replacement, giving the human a durable, actionable
surface (Add to Series / Review / Do Not Add) instead.

Identity/dedupe: matches the same normalized-identity cascade
`agents/series_agent._is_known_candidate` already uses for owned books
(isbn13, then title_key+number, then bare_title_key for numberless
candidates) -- reused here, not reinvented, since provider titles for the
same real book vary run to run (see `create_or_refresh_candidate_notification`'s
own docstring).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Session

import discovery_engine
import models
from intelligence import recalculate_intelligence
from services.availability_bridge import derive_legacy_fields
from services.skeleton_store import backfill_skeleton_for_series

AMAZON_KU_SEARCH_URL = "https://www.amazon.com/kindle-dbs/hz/search?ie=UTF8&fieldTargetedSeries={query}"
GOOGLE_SEARCH_URL = "https://www.google.com/search?q={query}"
AMAZON_ASIN_URL = "https://www.amazon.com/dp/{asin}"


def _quote(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value)


def _numbers_equal(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def _find_matching_row(
    db: Session,
    *,
    profile_id: str,
    series_id: int | None,
    isbn13: str | None,
    title_key: str,
    bare_title_key: str,
    candidate_number: float | None,
    resolution: str | None,
) -> "models.SeriesCandidateNotification | None":
    query = db.query(models.SeriesCandidateNotification).filter(
        models.SeriesCandidateNotification.profile_id == profile_id
    )
    query = query.filter(models.SeriesCandidateNotification.series_id == series_id)
    if resolution is None:
        query = query.filter(models.SeriesCandidateNotification.resolution.is_(None))
    else:
        query = query.filter(models.SeriesCandidateNotification.resolution == resolution)
    rows = query.all()

    if isbn13:
        for row in rows:
            if row.isbn13 and row.isbn13 == isbn13:
                return row
    if title_key:
        for row in rows:
            if row.title_key == title_key and _numbers_equal(row.candidate_number, candidate_number):
                return row
    if candidate_number is None and bare_title_key:
        for row in rows:
            if row.bare_title_key == bare_title_key and row.candidate_number is None:
                return row
    return None


def create_or_refresh_candidate_notification(
    db: Session,
    *,
    profile_id: str,
    series_id: int | None,
    series_name: str | None,
    canonical: dict,
    overall_confidence: str | None,
    provider_confidence: str | None,
    series_name_hint: str | None,
    reason_flags: list[str],
    tier_c_disagreement: dict | None = None,
) -> "models.SeriesCandidateNotification | None":
    """Creates a new unresolved row for this candidate, refreshes an
    existing unresolved row's `last_seen_at`/metadata in place when the
    same candidate is rediscovered on a later run, or suppresses creation
    entirely when this exact candidate was already dismissed via "Do Not
    Add" (`resolution="ignored"`) -- see class docstring for why these are
    two separate lookups rather than one, and module docstring for the
    matching rule. Returns `None` only in the ignored case; does not
    commit (piggybacks on the caller's own commit, same convention as
    `services/notifications.create_series_discovery_notification`).

    `tier_c_disagreement` (Step 8, "Tier C Shadow Scoring Persistence +
    Promotion Path"): optional, purely additive -- `None` for every
    existing caller (unchanged behavior) and for every candidate where
    either Tier C shadow didn't fire or agreed with the deterministic
    gate. Only ever populated by `agents/series_agent.py`'s Tier C shadow
    call site when a series' Tier C promotion state is "shadow_advisory"
    and Tier C disagreed with the gate on `belongs_to_series` for this
    exact candidate -- see `models.SeriesCandidateNotification.tier_c_
    disagreement`'s docstring.
    """
    title = str(canonical.get("title") or "").strip()
    isbn13 = str(canonical.get("isbn13") or "").strip() or None
    title_key = discovery_engine.core_title_key(title)
    bare_title_key = discovery_engine.bare_title_key(title)
    try:
        raw_number = canonical.get("series_number")
        candidate_number = float(raw_number) if raw_number not in (None, "") else None
    except (TypeError, ValueError):
        candidate_number = None

    now = datetime.utcnow()

    ignored = _find_matching_row(
        db,
        profile_id=profile_id,
        series_id=series_id,
        isbn13=isbn13,
        title_key=title_key,
        bare_title_key=bare_title_key,
        candidate_number=candidate_number,
        resolution="ignored",
    )
    if ignored is not None:
        return None

    existing = _find_matching_row(
        db,
        profile_id=profile_id,
        series_id=series_id,
        isbn13=isbn13,
        title_key=title_key,
        bare_title_key=bare_title_key,
        candidate_number=candidate_number,
        resolution=None,
    )
    if existing is not None:
        existing.last_seen_at = now
        existing.candidate_title = title or existing.candidate_title
        existing.overall_confidence = overall_confidence
        existing.provider_confidence = provider_confidence
        existing.isbn13 = isbn13 or existing.isbn13
        existing.publication_date = canonical.get("date_iso") or existing.publication_date
        existing.asin = canonical.get("asin") or existing.asin
        existing.author = canonical.get("author") or existing.author
        existing.source_url = canonical.get("url") or existing.source_url
        existing.provider = canonical.get("provider") or existing.provider
        existing.series_name_hint = series_name_hint or existing.series_name_hint
        existing.reason_flags = list(reason_flags)
        if tier_c_disagreement is not None:
            existing.tier_c_disagreement = tier_c_disagreement
        db.flush()
        return existing

    row = models.SeriesCandidateNotification(
        profile_id=profile_id,
        series_id=series_id,
        series_name=series_name,
        candidate_title=title,
        candidate_number=candidate_number,
        overall_confidence=overall_confidence,
        provider_confidence=provider_confidence,
        isbn13=isbn13,
        publication_date=canonical.get("date_iso"),
        asin=canonical.get("asin"),
        author=canonical.get("author"),
        source_url=canonical.get("url"),
        provider=canonical.get("provider"),
        series_name_hint=series_name_hint,
        reason_flags=list(reason_flags),
        title_key=title_key,
        bare_title_key=bare_title_key,
        resolution=None,
        created_at=now,
        last_seen_at=now,
        tier_c_disagreement=tier_c_disagreement,
    )
    db.add(row)
    # autoflush is off for this app's sessions (see database.py) --
    # without an explicit flush here, a second ambiguous candidate later
    # in the *same* run_series_check loop that matches this same identity
    # wouldn't see this row yet via the query above and would insert a
    # duplicate instead of refreshing it.
    db.flush()
    return row


def get_unresolved_candidate_notifications(
    db: Session, profile_id: str
) -> list["models.SeriesCandidateNotification"]:
    return (
        db.query(models.SeriesCandidateNotification)
        .filter(models.SeriesCandidateNotification.profile_id == profile_id)
        .filter(models.SeriesCandidateNotification.resolution.is_(None))
        .order_by(models.SeriesCandidateNotification.created_at.desc())
        .all()
    )


def build_review_urls(notification: "models.SeriesCandidateNotification") -> dict:
    """Builds "surface a link, don't scrape it yourself" URLs for the
    Review action (precedent: Book.source_url's own docstring) -- no
    persistence, the notification stays unresolved.
    """
    series_query = notification.series_name or ""
    title_author_query = " ".join(
        part for part in [notification.candidate_title, notification.author] if part
    )
    urls = {
        "amazon_ku_search": AMAZON_KU_SEARCH_URL.format(query=_quote(series_query)),
        "google_search": GOOGLE_SEARCH_URL.format(query=_quote(title_author_query)),
        "asin_lookup": None,
    }
    if notification.asin:
        urls["asin_lookup"] = AMAZON_ASIN_URL.format(asin=notification.asin)
    return urls


def resolve_add_to_series(
    db: Session, *, profile_id: str, notification_id: int
) -> "models.Book | None":
    """"Add to Series" action: persists the candidate as a real Book row,
    marks the notification resolved (`resolution="added"`), and refreshes
    the series' durable skeleton so the newly-owned book_number shows up
    as a confirmed/library-sourced entry right away rather than waiting
    for the next Check Now's own backfill.
    """
    row = (
        db.query(models.SeriesCandidateNotification)
        .filter(models.SeriesCandidateNotification.id == notification_id)
        .filter(models.SeriesCandidateNotification.profile_id == profile_id)
        .filter(models.SeriesCandidateNotification.resolution.is_(None))
        .first()
    )
    if row is None or row.series_id is None:
        return None

    series = db.query(models.Series).filter(models.Series.id == row.series_id).first()
    if series is None:
        return None

    book_number = row.candidate_number
    parsed_date = discovery_engine.parse_flexible_date(row.publication_date)
    is_upcoming = bool(parsed_date and parsed_date > date.today())
    number_inferred = "number_inferred_from_title" in (row.reason_flags or [])

    discovered_availability = "upcoming" if is_upcoming else "available"
    book = models.Book(
        profile_id=profile_id,
        title=row.candidate_title,
        canonical_title=row.candidate_title,
        metadata_source="discovery",
        book_number_source=("title_inferred" if number_inferred else "provider") if book_number is not None else None,
        author=row.author or series.author,
        series_id=series.id,
        book_number=book_number,
        series_order=int(book_number) if book_number is not None and float(book_number).is_integer() else None,
        publication_date=parsed_date,
        isbn13=row.isbn13,
        asin=row.asin,
        source_url=row.source_url,
        date_added=date.today(),
        is_read=False,
        # Unlocked -- "Add to Series" confirms this candidate is real, but
        # the upcoming/available call itself is still provider-date-driven,
        # not a user decision about availability -- same as a fresh Check
        # Now insert (see models.Book's docstring on availability_locked).
        availability_status=discovered_availability,
        availability_locked=False,
        **derive_legacy_fields(is_read=False, availability_status=discovered_availability, availability_locked=False),
        is_missing=False,
        record_status="active",
    )
    db.add(book)
    db.flush()

    row.resolution = "added"
    row.resolved_at = datetime.utcnow()
    db.commit()
    db.refresh(book)

    backfill_skeleton_for_series(db, series.id)
    recalculate_intelligence(db, series.id)
    return book


def resolve_do_not_add(db: Session, *, profile_id: str, notification_id: int) -> bool:
    """"Do Not Add" action: permanently suppresses this candidate
    (`resolution="ignored"`) -- future rediscovery of the same normalized
    identity is caught by `create_or_refresh_candidate_notification`'s
    ignore lookup, so it never resurfaces as a new row.
    """
    row = (
        db.query(models.SeriesCandidateNotification)
        .filter(models.SeriesCandidateNotification.id == notification_id)
        .filter(models.SeriesCandidateNotification.profile_id == profile_id)
        .filter(models.SeriesCandidateNotification.resolution.is_(None))
        .first()
    )
    if row is None:
        return False
    row.resolution = "ignored"
    row.resolved_at = datetime.utcnow()
    db.commit()
    return True
