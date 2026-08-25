"""Series-state intelligence: aggregate/missing-book computation, admin
repair tools, and external book-summary lookup.

RT-4: intelligence.py (a single ~650-line module mixing admin, external
lookup, and core series-state logic) was split into this package:

- core.py: omnibus-range parsing and the recount_series_aggregates_for_
  series/compute_series_intelligence_for_series/recalculate_intelligence
  family every caller outside this package actually cares about.
- admin.py: the orphaned-book purge and ghost-profile/fractional-identity
  repair tools backing the /admin routes.
- external.py: lookup_book_summary, the one piece of this package that
  calls out to an external provider (via discovery_engine.py) rather than
  only querying the local database.

This module re-exports every public and private name the three submodules
define, so existing external callers (agents/series_agent.py,
routers/series.py, routers/admin.py, routers/books.py,
services/series_check_engine.py, crud/books.py, bootstrap.py,
importer/pipeline.py, tests, etc.) are unaffected by the split -- they can
keep doing `import intelligence; intelligence.recalculate_intelligence(...)`
or `from intelligence import lookup_book_summary` exactly as before.
`discovery_engine` is re-imported here too (not just inside external.py) so
`intelligence.discovery_engine` keeps resolving to the same module object
external.py calls into, which some tests patch directly at that path.

DC-3: recompute_series_intelligence() (core.py) and _extract_series_position()
(external.py) were removed from this package -- both had zero callers
anywhere in the repo (the importer loops profile-scoped series IDs
directly via recalculate_intelligence(), see importer/pipeline.py's
run_import).
"""
from __future__ import annotations

import discovery_engine

from intelligence.core import (
    OMNIBUS_RANGE_CAPTURE_PATTERN,
    OMNIBUS_RANGE_PATTERN,
    _WORD_TO_NUMBER,
    _roman_to_int,
    _token_to_int,
    compute_series_intelligence_for_series,
    extract_omnibus_ranges,
    recalculate_intelligence,
    recalculate_series_state_for_series,
    recount_series_aggregates_for_series,
)
from intelligence.admin import (
    _truncated_identity_number,
    find_fractional_identity_collisions,
    find_ghost_profile_books,
    list_soft_deleted_books,
    purge_orphaned_books,
    repair_ghost_profile_books,
    restore_soft_deleted_book,
)
from intelligence.external import (
    logger,
    lookup_book_summary,
)

__all__ = [
    "discovery_engine",
    "OMNIBUS_RANGE_CAPTURE_PATTERN",
    "OMNIBUS_RANGE_PATTERN",
    "_WORD_TO_NUMBER",
    "_roman_to_int",
    "_token_to_int",
    "compute_series_intelligence_for_series",
    "extract_omnibus_ranges",
    "recalculate_intelligence",
    "recalculate_series_state_for_series",
    "recount_series_aggregates_for_series",
    "_truncated_identity_number",
    "find_fractional_identity_collisions",
    "find_ghost_profile_books",
    "list_soft_deleted_books",
    "purge_orphaned_books",
    "repair_ghost_profile_books",
    "restore_soft_deleted_book",
    "logger",
    "lookup_book_summary",
]
