"""Series Fingerprint system -- durable per-series *identity/pattern*
memory (see `discovery_agentic_fingerprint_recommendation.md` for the
full ten-round design chain this module implements; `models.
SeriesFingerprint`'s docstring states the exact boundary against
`SeriesSkeleton`, which this module never touches).

Mirrors `services/skeleton_store.py`'s Builder/Consumer split and its
single-writer-per-row upsert-with-retry concurrency pattern, but as a
deliberately separate, parallel implementation rather than a
generalization of `_upsert_skeleton_row` (design chain Round 4's explicit
call: that helper is a stable, already-hardened primitive `SeriesSkeleton`
depends on in production; a second, additive `_upsert_fingerprint_row`
carries zero regression risk to it, versus touching it to serve a second
consumer for the sake of one shared implementation).

Two independent halves, same shape as skeleton_store.py:
  - Builder (`apply_fingerprint_updates`): merges one round's raw
    observations (`build_fingerprint_observations` -- pure, no DB access,
    computed inside `agents/series_agent.py` from that round's already-
    computed skeleton/delta/confidence output) into the durable row.
    Called from `services/series_check_engine.py`'s post-persistence
    block, the exact call site that already calls `skeleton_store.
    apply_skeleton_updates`.
  - Consumer (`get_effective_fingerprint`): read once per job, before
    confidence scoring, gated by the dedicated `settings.
    FINGERPRINT_INFLUENCE_ENABLED` + `settings.is_fingerprint_activated`
    pair -- resolved exactly once, at this one call boundary.
    `confidence_engine.py` never checks either flag itself and stays a
    pure function of its arguments (its own docstring's guarantee); a
    `None` fingerprint uniformly means "no influence," identical in
    effect to a real-but-empty fingerprint with no data yet.

Building is unconditional and free (design chain item 9): the Builder
only ever reads this round's already-fetched/already-scored output, and
runs regardless of whether the two gates above are open -- "shadow-
first," same discipline as every other agentic addition in this codebase.
"""

import logging
import random
import statistics
import time
from datetime import date
from typing import Callable

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

import agentic_hooks
import models
import settings
from discovery_text import _DASH_SERIES_MARKER_PATTERN, _TITLE_SERIES_MARKER_PATTERN, _to_float_or_none

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Single-writer-per-row protection -- identical bounds/rationale to
# services/skeleton_store.py's _UPSERT_MAX_ATTEMPTS/_UPSERT_RETRY_BASE_
# DELAY_SECONDS; SeriesFingerprint has the exact same two-write-path
# concurrency shape (a Check Now round's post-persistence apply is the
# only real writer today, but the retry loop is the actual protection,
# not an assumption about how many writers ever exist).
_UPSERT_MAX_ATTEMPTS = 5
_UPSERT_RETRY_BASE_DELAY_SECONDS = 0.05

# Provider-bias running EMA (design chain Round 5 item 5 / Round 4's "name
# the exact formula, not a bare 'an EMA'"). The *mechanism* -- incremental
# EMA merge, full-history, no windowing -- is the design decision; these
# four numbers are the implementation-time tuning knob the design chain
# explicitly left open (Round 11's "remains open: implementation tuning,
# not architecture").
PROVIDER_BIAS_EMA_ALPHA = 0.2
PROVIDER_BIAS_MIN = 0.5
PROVIDER_BIAS_MAX = 1.5
PROVIDER_BIAS_ACCEPT_SIGNAL = 1.3
PROVIDER_BIAS_REJECT_SIGNAL = 0.7

# author_aliases/naming_patterns are append-only observation lists (unlike
# provider_bias/release_cadence, which resolve to a small, bounded stat
# blob) -- capped so an old, one-off variant observed years ago can't
# accumulate forever. Implementation-time tuning constant.
_MAX_STORED_STRINGS_PER_FIELD = 25


def _empty_fingerprint() -> dict:
    return {
        "author_aliases": [],
        "naming_patterns": [],
        "provider_bias": {},
        "release_cadence": {"mean_interval_days": None, "stddev_interval_days": None, "interval_count": 0},
    }


def _upsert_fingerprint_row(
    db: Session,
    series_id: int,
    merge_fn: Callable[[dict], dict],
    max_attempts: int = _UPSERT_MAX_ATTEMPTS,
) -> "models.SeriesFingerprint":
    """Single-writer-per-row core for `SeriesFingerprint` -- same
    optimistic-concurrency shape as `services.skeleton_store.
    _upsert_skeleton_row` (see that function's docstring for the full
    concurrency rationale, not repeated here), operating on a flat dict
    (`fingerprint_json`) instead of a list of book-number-keyed entries.

    `merge_fn(existing_fingerprint: dict) -> dict` computes the new
    `fingerprint_json` from whatever is *currently persisted* -- re-read
    fresh on every retry attempt, exactly like skeleton's `merge_fn`, so a
    concurrent writer's change is never silently clobbered.
    """
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            row = (
                db.query(models.SeriesFingerprint)
                .filter(models.SeriesFingerprint.series_id == series_id)
                .with_for_update()
                .first()
            )
            existing = row.fingerprint_json if row and isinstance(row.fingerprint_json, dict) else _empty_fingerprint()
            new_fingerprint = merge_fn(existing)

            if row is None:
                row = models.SeriesFingerprint(
                    series_id=series_id,
                    fingerprint_json=new_fingerprint,
                    schema_version=SCHEMA_VERSION,
                    version=0,
                )
                db.add(row)
                db.commit()
                agentic_hooks.shadow_fingerprint_merge_trace({"series_id": series_id}, existing, new_fingerprint)
                return row

            read_version = row.version
            updated_rowcount = (
                db.query(models.SeriesFingerprint)
                .filter(
                    models.SeriesFingerprint.series_id == series_id,
                    models.SeriesFingerprint.version == read_version,
                )
                .update(
                    {
                        "fingerprint_json": new_fingerprint,
                        "schema_version": SCHEMA_VERSION,
                        "version": read_version + 1,
                    },
                    synchronize_session=False,
                )
            )
            if updated_rowcount == 0:
                db.rollback()
                last_error = RuntimeError(
                    f"optimistic concurrency conflict on SeriesFingerprint series_id={series_id} "
                    f"(expected version={read_version})"
                )
                if attempt == max_attempts:
                    break
                time.sleep(_UPSERT_RETRY_BASE_DELAY_SECONDS * attempt + random.uniform(0, 0.02))
                continue

            db.commit()
            db.refresh(row)
            agentic_hooks.shadow_fingerprint_merge_trace({"series_id": series_id}, existing, new_fingerprint)
            return row
        except (IntegrityError, OperationalError) as exc:
            db.rollback()
            last_error = exc
            if attempt == max_attempts:
                break
            time.sleep(_UPSERT_RETRY_BASE_DELAY_SECONDS * attempt + random.uniform(0, 0.02))

    logger.error(
        "Failed to upsert SeriesFingerprint for series_id=%s after %s attempts: %s",
        series_id,
        max_attempts,
        last_error,
    )
    raise RuntimeError(f"Failed to upsert SeriesFingerprint for series_id={series_id}") from last_error


def get_fingerprint_row(db: Session, series_id: int) -> "models.SeriesFingerprint | None":
    return db.query(models.SeriesFingerprint).filter(models.SeriesFingerprint.series_id == series_id).first()


def get_effective_fingerprint(db: Session, series_id: int) -> dict | None:
    """Resolves the two-tier activation gate exactly once, at this one
    call boundary (design chain Round 5 item 4's "activation-gate
    convention"): `None` whenever either `settings.
    FINGERPRINT_INFLUENCE_ENABLED` is off or `settings.
    is_fingerprint_activated(series_id)` is `False` for this series. Every
    pure downstream consumer (`confidence_engine.py`) treats `None`
    identically to "a fingerprint exists but has no data yet" -- callers
    must never check either settings flag themselves; this is the one
    place that does.
    """
    if not settings.FINGERPRINT_INFLUENCE_ENABLED or not settings.is_fingerprint_activated(series_id):
        return None
    row = get_fingerprint_row(db, series_id)
    if row is None or not isinstance(row.fingerprint_json, dict):
        return _empty_fingerprint()
    return row.fingerprint_json


def _candidate_providers(candidate: dict) -> set[str]:
    provenance = candidate.get("source_provenance") or []
    return {entry.get("source") for entry in provenance if isinstance(entry, dict) and entry.get("source")}


def _provider_bias_observations(confidence: dict) -> dict[str, float]:
    """One EMA-input signal per provider that contributed to at least one
    of this round's scored candidates (design chain item 6): providers
    whose hits keep landing as an accepted (`overall` high/medium)
    candidate for this series earn a positive signal; providers whose
    hits only ever contribute to a low/zero-`overall` candidate earn a
    negative one. A provider absent from this round entirely produces no
    observation at all -- its existing bias is left untouched, not
    decayed toward neutral (see `_merge_provider_bias`).
    """
    samples: dict[str, list[float]] = {}
    for entry in confidence.get("confidence", []) or []:
        candidate = entry.get("candidate") if isinstance(entry, dict) else None
        if not isinstance(candidate, dict):
            continue
        overall = entry.get("overall")
        signal = PROVIDER_BIAS_ACCEPT_SIGNAL if overall in ("high", "medium") else PROVIDER_BIAS_REJECT_SIGNAL
        for provider in _candidate_providers(candidate):
            samples.setdefault(provider, []).append(signal)
    return {provider: sum(values) / len(values) for provider, values in samples.items()}


def _author_alias_observations(confidence: dict) -> list[str]:
    """Design chain item 6: seeded from `confidence_engine.
    _series_alignment_confidence`'s existing `"medium"` (initials-variant)
    branch -- a candidate author string that plausibly abbreviates/
    expands the series' own author string without yet being a confirmed
    exact match. Building from this signal is independent of the item 5
    corroboration rule, which governs *consuming* naming/author signals
    to infer a rename -- it does not restrict what gets recorded here.
    """
    aliases: list[str] = []
    for entry in confidence.get("confidence", []) or []:
        if entry.get("series_alignment_confidence") != "medium":
            continue
        candidate = entry.get("candidate") if isinstance(entry, dict) else None
        if not isinstance(candidate, dict):
            continue
        for author in candidate.get("authors") or []:
            author = str(author or "").strip()
            if author:
                aliases.append(author)
    return aliases


def _naming_pattern_observations(confidence: dict) -> list[str]:
    """Design chain item 6 (naming_patterns): built from this round's
    accepted (`overall` high/medium) candidates' raw title structure --
    specifically, whether the title carries either of the two catalog-
    branding-noise shapes `discovery_text.py` already recognizes globally
    (`_DASH_SERIES_MARKER_PATTERN`, `_TITLE_SERIES_MARKER_PATTERN`).
    Recording *which* shape this series' own listings tend to use is the
    per-series memory being built here; per item 5, consuming this field
    to infer a renamed volume requires three-way corroboration
    (numbering + author + provider) and is out of scope for this pass --
    this function only ever appends observation tags, it never reads them
    back to make a decision.
    """
    tags: set[str] = set()
    for entry in confidence.get("confidence", []) or []:
        if entry.get("overall") not in ("high", "medium"):
            continue
        candidate = entry.get("candidate") if isinstance(entry, dict) else None
        if not isinstance(candidate, dict):
            continue
        title = str(candidate.get("title") or "")
        if _DASH_SERIES_MARKER_PATTERN.search(title):
            tags.add("dash_series_marker")
        if _TITLE_SERIES_MARKER_PATTERN.search(title):
            tags.add("colon_universe_marker")
    return sorted(tags)


def _compute_release_cadence(skeleton_entries: list[dict]) -> dict:
    """Builder half of the cadence feature (design chain items 4-11):
    derived exclusively from `SeriesSkeleton` entries' `release_date`
    (never `first_seen_at`/`last_confirmed_at`, which are discovery
    bookkeeping timestamps, not real-world publish dates), restricted to
    `source_class == "library"` -- owned, trustworthy release history,
    the same population the cadence *consumer*'s reference-entry lookup
    (`confidence_engine._cadence_reference_entry`) is required to match
    (Round 10/11's converged catch). Entries with no resolvable
    `release_date` are excluded from interval math, not treated as zero
    (item 4).

    Deliberately a full recompute from the current skeleton snapshot on
    every call, not an incremental merge against a prior stat -- unlike
    `provider_bias` (whose only source of history is this fingerprint row
    itself), `release_cadence`'s source (`SeriesSkeleton`) is itself
    fully rebuilt from ground truth every round, so recomputing fresh
    here is strictly simpler, cannot drift, and produces the identical
    "full-history, no windowing" running-stat semantics the design chain
    settled on (Round 4 item 5) without an incremental-merge formula that
    could accumulate float error over hundreds of rounds.
    """
    dated_entries: list[tuple[float, date]] = []
    for entry in skeleton_entries or []:
        if not isinstance(entry, dict) or entry.get("source_class") != "library":
            continue
        number = _to_float_or_none(entry.get("book_number"))
        release_date_raw = entry.get("release_date")
        if number is None or not release_date_raw:
            continue
        try:
            parsed = date.fromisoformat(str(release_date_raw))
        except ValueError:
            continue
        dated_entries.append((number, parsed))
    dated_entries.sort(key=lambda pair: pair[0])

    intervals = [
        (later[1] - earlier[1]).days
        for earlier, later in zip(dated_entries, dated_entries[1:])
        if (later[1] - earlier[1]).days > 0
    ]
    if not intervals:
        return {"mean_interval_days": None, "stddev_interval_days": None, "interval_count": 0}
    mean_interval = statistics.mean(intervals)
    stddev_interval = statistics.stdev(intervals) if len(intervals) >= 2 else 0.0
    return {
        "mean_interval_days": mean_interval,
        "stddev_interval_days": stddev_interval,
        "interval_count": len(intervals),
    }


def build_fingerprint_observations(
    skeleton_entries: list[dict],
    delta: dict,
    confidence: dict,
    *,
    series_author: str | None = None,
) -> dict:
    """Pure function -- no DB access, makes no LLM/network call, computed
    entirely from this round's already-computed skeleton/delta/confidence
    output (design chain item 9: "fingerprint building is free -- it
    operates only on already-fetched data"). Called from
    `agents/series_agent.py` and threaded through
    `result["fingerprint_updates"]` to `services/series_check_engine.py`,
    the only caller of `apply_fingerprint_updates` below -- mirrors
    exactly how `result["skeleton_updates"]` already flows to
    `skeleton_store.apply_skeleton_updates`.

    `delta` is accepted for interface symmetry with the Consumer side and
    reserved for a future Builder signal (e.g. weighting `provider_bias`
    by whether a provider's hit was ever flagged `duplicate_number` --
    design chain §2.2's `delta_engine` note); no behavior depends on it
    yet. `series_author` is likewise reserved (e.g. never recording an
    alias identical to the series' own author string).
    """
    return {
        "author_alias_observations": _author_alias_observations(confidence),
        "naming_pattern_observations": _naming_pattern_observations(confidence),
        "provider_bias_observations": _provider_bias_observations(confidence),
        "release_cadence": _compute_release_cadence(skeleton_entries),
    }


def _merge_string_list(existing: list | None, observed: list | None) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in list(existing or []) + list(observed or []):
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        merged.append(text)
    return merged[-_MAX_STORED_STRINGS_PER_FIELD:]


def _merge_provider_bias(existing: dict | None, observations: dict | None) -> dict[str, float]:
    merged = dict(existing or {})
    for provider, signal in (observations or {}).items():
        previous = merged.get(provider, 1.0)
        updated = previous * (1 - PROVIDER_BIAS_EMA_ALPHA) + signal * PROVIDER_BIAS_EMA_ALPHA
        merged[provider] = round(min(PROVIDER_BIAS_MAX, max(PROVIDER_BIAS_MIN, updated)), 4)
    return merged


def _merge_fingerprint(existing: dict, updates: dict) -> dict:
    """Pure merge computation, extracted the same way `skeleton_store.
    compute_skeleton_updates_merge` is -- callable without going through
    `_upsert_fingerprint_row` (i.e. without any DB access), so it can be
    unit-tested directly. `author_aliases`/`naming_patterns` are append-
    dedup merges (this round's observations added to whatever already
    exists); `provider_bias` is the EMA merge above; `release_cadence` is
    a full, deterministic replacement each round (see
    `_compute_release_cadence`'s docstring for why that's the correct
    "full-history, no windowing" semantics here, not a partial merge).
    """
    existing = existing if isinstance(existing, dict) else {}
    updates = updates if isinstance(updates, dict) else {}
    return {
        "author_aliases": _merge_string_list(existing.get("author_aliases"), updates.get("author_alias_observations")),
        "naming_patterns": _merge_string_list(existing.get("naming_patterns"), updates.get("naming_pattern_observations")),
        "provider_bias": _merge_provider_bias(existing.get("provider_bias"), updates.get("provider_bias_observations")),
        "release_cadence": updates.get("release_cadence") or existing.get("release_cadence") or _empty_fingerprint()["release_cadence"],
    }


def apply_fingerprint_updates(db: Session, series_id: int, updates: dict | None) -> "models.SeriesFingerprint | None":
    """Builder write path -- post-persistence, same call-site pattern as
    `skeleton_store.apply_skeleton_updates` (`services/
    series_check_engine.py` calls both from the same block). `updates` is
    `build_fingerprint_observations`'s pure output, threaded through
    `agents/series_agent.py`'s `result["fingerprint_updates"]` -- never
    computed here, so this function has no delta/confidence/skeleton
    inputs of its own, only the already-reduced observation payload.

    Never allowed to fail the calling round -- the try/except at the
    `series_check_engine.py` call site gives this the same "a stale
    fingerprint self-heals next round" discipline
    `apply_skeleton_updates` already has.
    """
    series = db.query(models.Series).filter(models.Series.id == series_id).first()
    if series is None:
        return None

    updates = updates if isinstance(updates, dict) else {}

    def merge_fn(existing: dict) -> dict:
        return _merge_fingerprint(existing, updates)

    return _upsert_fingerprint_row(db, series_id, merge_fn)
