"""Discovery Health Indicator (Auto Discovery MVP spec, §1).

Pure date-math, deliberately free of any `models`/`Session` imports so
`models.Series.discovery_health` (a plain @property) can call it directly
without creating an import cycle. Also independently unit-testable without a
database.
"""

from datetime import date

_STALE_AFTER_DAYS = 183  # ~6 months
_VERY_STALE_AFTER_DAYS = 365  # ~12 months

DiscoveryHealth = str  # "never_checked" | "healthy" | "stale" | "very_stale"


def compute_discovery_health(last_checked: date | None, is_finished: bool, today: date | None = None) -> DiscoveryHealth:
    """Series.last_checked -> one of "never_checked"/"healthy"/"stale"/
    "very_stale". Finished series are intentionally NOT special-cased to a
    fifth state here -- callers that want the badge suppressed for
    `is_finished` series (per the spec) should check `is_finished`
    themselves before deciding whether to render it at all; this function
    still returns a real value so it stays meaningful if a caller wants it
    anyway (e.g. a debug view).
    """
    if last_checked is None:
        return "never_checked"

    reference_day = today or date.today()
    age_days = (reference_day - last_checked).days
    if age_days < 0:
        age_days = 0

    if age_days > _VERY_STALE_AFTER_DAYS:
        return "very_stale"
    if age_days > _STALE_AFTER_DAYS:
        return "stale"
    return "healthy"
