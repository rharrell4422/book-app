"""Shared helpers for the two-axis reading/availability status model (see
the "Two-Axis Status Architecture" design chat's finalized spec).

Two independent axes now govern a Book's status:

- Reading axis: `is_read` (+ `read_date`) -- unchanged, still the single
  source of truth for reading progress.
- Availability axis: `availability_status` (`"upcoming"` / `"available"` /
  `"owned"`) + `availability_locked` (bool) -- new. `availability_locked`
  gates whether discovery/Check Now/library_sync are allowed to keep
  auto-managing this value; once a user (or an explicit CSV token) sets it,
  it stays put until the user changes it again, with one exception (see
  `should_self_heal_stale_upcoming` below).

`read_status` / `is_upcoming_auto` / `is_upcoming_final` are kept as a
derived legacy bridge (this module's `derive_legacy_fields`) purely so any
not-yet-migrated reader of those columns keeps seeing consistent data
during the transition -- they are never themselves a source of truth going
forward.
"""
from __future__ import annotations

from datetime import date

AVAILABILITY_STATUSES = ("upcoming", "available", "owned")
DEFAULT_AVAILABILITY_STATUS = "available"


def normalize_availability_status(value: str | None) -> str:
    """Clamps any incoming value to the fixed 3-value domain, defaulting
    to "available" (the least assumptive claim -- doesn't assert ownership,
    doesn't assert non-existence) for anything unrecognized."""
    candidate = str(value or "").strip().lower()
    return candidate if candidate in AVAILABILITY_STATUSES else DEFAULT_AVAILABILITY_STATUS


def derive_legacy_fields(is_read: bool, availability_status: str, availability_locked: bool) -> dict:
    """Forward derivation table (finalized in the design chat):

    - is_read=true -> read_status="read".
    - is_read=false + availability_status="owned" -> read_status="unread".
    - is_read=false + availability_status="available" -> read_status="available".
    - is_read=false + availability_status="upcoming" -> read_status="upcoming".
    - availability_status="upcoming" + availability_locked=false ->
      is_upcoming_auto=true, is_upcoming_final=false.
    - availability_status="upcoming" + availability_locked=true ->
      is_upcoming_auto=false, is_upcoming_final=true.
    """
    status = normalize_availability_status(availability_status)

    if is_read:
        read_status = "read"
    elif status == "owned":
        read_status = "unread"
    elif status == "upcoming":
        read_status = "upcoming"
    else:
        read_status = "available"

    if status == "upcoming":
        is_upcoming_auto = not bool(availability_locked)
        is_upcoming_final = bool(availability_locked)
    else:
        is_upcoming_auto = False
        is_upcoming_final = False

    return {
        "read_status": read_status,
        "is_upcoming_auto": is_upcoming_auto,
        "is_upcoming_final": is_upcoming_final,
    }


def should_self_heal_stale_upcoming(availability_status: str | None, candidate_date: date | None, today: date | None = None) -> bool:
    """upcoming -> available is a one-directional self-heal that fires even
    when availability_locked is True -- a stored "upcoming" whose release
    date has since passed is stale by definition (see library_sync.py's
    pre-existing `is_marked_upcoming`/date-passed check and the equivalent
    frontend rule in book-format.ts), not a case of second-guessing a
    user's deliberate choice. No other transition self-heals through the
    lock -- this is the one narrow, pre-existing exception carried forward
    into the new model.
    """
    if normalize_availability_status(availability_status) != "upcoming":
        return False
    if candidate_date is None:
        return False
    return candidate_date <= (today or date.today())
