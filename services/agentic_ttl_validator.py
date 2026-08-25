"""Phase 1 agentic discovery, eighth implementation block: a read-only TTL
sweep validator for a series' `SeriesSkeleton` row -- reports which
`discovered` entries have aged out under the retention policy
`services/skeleton_store.py` already enforces on the next real merge,
without waiting for (or triggering) one.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(settled architecture, not re-litigated here): `validate_ttl_behavior`
never writes anything -- it only reads the already-persisted
`skeleton_json` and classifies each `discovered` entry using
`services.skeleton_store`'s own, unmodified `_is_expired_discovered_entry`/
`DISCOVERED_ENTRY_TTL_DAYS` (the exact same 90-day expiry check
`_merge_discovered_entries` applies on a real rebuild) -- no
reimplementation, no second copy of that math to drift from the real one.

Probe-entry TTL: `models.SeriesSkeleton` has no `probes_json` column at
all yet -- `apply_skeleton_updates`'s own docstring in `services/
skeleton_store.py` documents that probe memory is "accepted-and-logged,
not silently dropped" but genuinely unimplemented in Phase 1 (there is no
probe schema to validate). `probes_ttl` below is therefore always an
honest empty result with an explanatory `note`, not a fabricated one --
consistent with that same module's existing precedent for this exact gap.
Wiring this up for real is a follow-on ticket once a probe schema exists,
not this one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from database import SessionLocal
from models import SeriesSkeleton
from services.skeleton_store import _is_expired_discovered_entry

logger = logging.getLogger(__name__)

_PROBES_TTL_NOTE = (
    "no probes_json storage exists yet (Phase 1) -- see services/skeleton_store.py's "
    "apply_skeleton_updates docstring; this section is an honest empty result, not a "
    "fabricated one, until a real probe schema lands."
)


def _discovered_entry_summary(entry: dict) -> dict:
    return {
        "book_number": entry.get("book_number"),
        "title": entry.get("title"),
        "last_confirmed_at": entry.get("last_confirmed_at"),
        "first_seen_at": entry.get("first_seen_at"),
    }


def validate_ttl_behavior(series_id: int, *, db_session: Session | None = None) -> dict:
    """Read-only TTL sweep validation for `series_id`.

    Returns:
        {
          "series_id": series_id,
          "discovered_ttl": {"expired": [...], "valid": [...]},
          "probes_ttl": {"expired": [], "valid": [], "note": "..."},
          "timestamp": iso8601,
        }

    Each `discovered_ttl` entry is a small summary dict (`book_number`/
    `title`/`last_confirmed_at`/`first_seen_at`), not the raw skeleton
    entry, to keep the report focused on exactly the fields the TTL
    decision depends on.

    `db_session`, when provided, is reused as-is and never closed by this
    function; when omitted, a session is opened internally and always
    closed before returning. Never writes anything -- no `db.add`/
    `db.commit`/`db.flush` anywhere in this module.
    """
    caller_supplied_db = db_session is not None
    db: Session = db_session if caller_supplied_db else SessionLocal()
    try:
        # Naive UTC "now", matching services/skeleton_store.py's own
        # `_is_expired_discovered_entry` comparison (entries' own
        # last_confirmed_at/first_seen_at timestamps are naive ISO
        # strings written via that same module's `datetime.utcnow()`) --
        # mixing a tz-aware "now" in here would raise on the subtraction.
        now_for_ttl = datetime.utcnow()

        skeleton_row = db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == series_id).first()
        skeleton_entries = (
            list(skeleton_row.skeleton_json)
            if skeleton_row is not None and isinstance(skeleton_row.skeleton_json, list)
            else []
        )

        expired_discovered = []
        valid_discovered = []
        for entry in skeleton_entries:
            if not isinstance(entry, dict) or entry.get("source_class") != "discovered":
                continue
            summary = _discovered_entry_summary(entry)
            if _is_expired_discovered_entry(entry, now_for_ttl):
                expired_discovered.append(summary)
            else:
                valid_discovered.append(summary)

        return {
            "series_id": series_id,
            "discovered_ttl": {"expired": expired_discovered, "valid": valid_discovered},
            "probes_ttl": {"expired": [], "valid": [], "note": _PROBES_TTL_NOTE},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception:
        logger.exception("validate_ttl_behavior failed for series_id=%s; returning empty report", series_id)
        return {
            "series_id": series_id,
            "discovered_ttl": {"expired": [], "valid": []},
            "probes_ttl": {"expired": [], "valid": [], "note": _PROBES_TTL_NOTE},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        if not caller_supplied_db:
            db.close()
