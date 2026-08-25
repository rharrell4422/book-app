"""Bind-time provenance rules for Book.metadata_source / book_number_source
/ needs_reresolution (see models.py's column docstrings and the project
design chat's consolidated Add Book specification).

These three columns intentionally stay decoupled from *how* a value was
produced (FIND bind vs Check Now vs bulk re-resolution vs plain user typing)
so every write path can share one small set of rules instead of each
re-deriving its own notion of "verified".
"""
from __future__ import annotations


def provenance_for_find_bind(confidence: str) -> dict:
    """What to stamp on Book.metadata_source/needs_reresolution when the
    user confirms (Binds) a FIND candidate at the given confidence tier.

    - HIGH/MEDIUM confidence: fully verified -- metadata_source="provider",
      needs_reresolution=False. A medium-confidence bind is NOT queued for
      re-resolution; it's treated as settled the same as high-confidence.
    - LOW confidence: still provider-sourced (the user did pick a real
      catalog entry, not type it from scratch) -- metadata_sourcestays
      "provider", verified, not down-weighted, not excluded from
      discovery -- but flagged needs_reresolution=True so it gets
      re-checked later once provider catalogs might have filled in a
      stronger match. See models.Book.needs_reresolution's own docstring
      for the full lifecycle.

    Rows bound this way are never metadata_source="discovery" (Check Now is
    exempt from FIND confidence entirely and stamps its own provenance
    directly -- see services/series_check_engine.py) and never "user"/
    "import"/NULL (those are the *declined*/unavailable-FIND paths, which
    don't call this function at all).
    """
    if confidence not in ("high", "medium", "low"):
        raise ValueError(f"unknown FIND confidence tier: {confidence!r}")
    return {
        "metadata_source": "provider",
        "needs_reresolution": confidence == "low",
    }


def provenance_for_declined_or_manual_entry() -> dict:
    """What to stamp when the user declines every FIND candidate (or FIND
    found nothing / wasn't run) and types metadata in by hand. Explicitly
    "user", not NULL, so a fresh Add Book row is distinguishable from a
    legacy row whose origin was never recorded at all.
    """
    return {"metadata_source": "user", "needs_reresolution": None}
