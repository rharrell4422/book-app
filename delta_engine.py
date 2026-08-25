"""Phase 0 of agentic discovery (see project design chat): a deterministic,
side-effect-free comparison between a series' durable SeriesSkeleton
baseline and one Check Now run's discovered candidates.

Nothing in this module touches the database, calls an LLM, makes a
network request, or mutates its inputs -- it's a pure function of
(skeleton_entries, provider_candidates) -> a delta dict. No longer
shadow-only: its output feeds confidence_engine.compute_confidence, which
drives live accept/drop/needs-review routing in
agents/series_agent.py's "Manual-override routing" block (see that
comment for the authoritative behavior).

Deliberately reuses discovery_engine's own title/metadata guards
(_title_is_series_variant, looks_like_placeholder_title,
metadata_completeness_score, RECONCILIATION_METADATA_COMPLETENESS_THRESHOLD)
rather than re-implementing "does this look like a real book" a second
time with rules that could quietly drift from the ones _filter_and_merge
already applies.

Deliberately does NOT use discovery_engine._resolve_candidate_number's
title-text-inference fallback for book numbers -- only each candidate's
own structured `series_number` (from a provider field or an LLM
structuring pass upstream, never guessed here) is used. This module's own
"no inference" rule is about not guessing on top of what discovery already
produced, not about refusing structured data discovery itself already
resolved.
"""

from datetime import datetime

import discovery_engine
from discovery_text import _to_float_or_none  # NS-5: shared with confidence_engine.py


def _candidate_providers(candidate: dict) -> list[str]:
    # Provenance entries key their provider name as "source" (see
    # discovery_engine.py's _unified_candidate_to_raw_dict/
    # _format_candidate_for_reconciliation, both of which read
    # member.get("source")) -- a prior version of this function read
    # "provider" instead, which no provenance entry has ever actually
    # used, so it silently always returned an empty list.
    provenance = candidate.get("source_provenance") or []
    providers = {entry.get("source") for entry in provenance if entry.get("source")}
    return sorted(providers)


def _malformed_reason(series_name: str | None, candidate: dict) -> str | None:
    """First matching reason a candidate looks structurally unsound, or
    None if it looks like a real, distinctly-titled book. Order matters
    only in which single reason gets reported when several apply -- every
    check still runs deterministically off the same inputs each time.
    """
    title = str(candidate.get("title") or "").strip()
    if not title:
        return "missing_title"

    if discovery_engine.looks_like_placeholder_title(title):
        return "placeholder_title"

    isbn13 = candidate.get("isbn13")
    structured_number_hint = candidate.get("series_number")
    if discovery_engine._title_is_series_variant(title, series_name, isbn13, structured_number_hint):
        return "title_is_series_variant"

    number = _to_float_or_none(structured_number_hint)
    if structured_number_hint is not None and number is None:
        return "invalid_number"
    if number is not None and number <= 0:
        return "negative_number"

    completeness = candidate.get("metadata_completeness_score")
    if (
        completeness is not None
        and completeness < discovery_engine.RECONCILIATION_METADATA_COMPLETENESS_THRESHOLD
    ):
        return "insufficient_metadata"

    return None


def compute_series_delta(
    series_id: int,
    skeleton_entries: list[dict],
    provider_candidates: list[dict],
    *,
    series_name: str | None = None,
) -> dict:
    """Pure comparison -- does not mutate skeleton_entries or
    provider_candidates, makes no LLM/network/DB calls, and does not guess
    at any number or title beyond what's already structurally present on
    each candidate.

    provider_candidates is expected to be discovery_engine's pre-filter
    `unified_candidates` (see discover_candidates_for_series), each
    converted to a dict via UnifiedCandidate.model_dump(). Deliberately the
    PRE-_filter_and_merge candidate set: _filter_and_merge already silently
    drops exactly the malformed/placeholder/series-variant candidates this
    function exists to surface for review, so by the time a candidate
    reaches the post-filter `candidates` list that signal is already gone.
    """
    skeleton_numbers = {
        number
        for number in (_to_float_or_none(entry.get("book_number")) for entry in skeleton_entries)
        if number is not None
    }

    missing_books: list[dict] = []
    malformed_books: list[dict] = []
    found_numbers: set[float] = set()
    number_to_candidates: dict[float, list[dict]] = {}

    for candidate in provider_candidates:
        reason = _malformed_reason(series_name, candidate)
        if reason:
            malformed_books.append(
                {
                    "type": "malformed_book",
                    "series_id": series_id,
                    "candidate": candidate,
                    "reason": reason,
                }
            )
            continue

        number = _to_float_or_none(candidate.get("series_number"))
        if number is not None:
            found_numbers.add(number)
            number_to_candidates.setdefault(number, []).append(candidate)

        if number is None or number not in skeleton_numbers:
            missing_books.append(
                {
                    "type": "missing_book",
                    "series_id": series_id,
                    "book_number": number,
                    "title": candidate.get("title"),
                    "providers": _candidate_providers(candidate),
                }
            )

    # Two (non-malformed) candidates both resolving to the same structured
    # number CAN be a real data problem fusion's own dedupe (isbn13 ->
    # title_key -> normalized title) should already have collapsed -- but
    # it is just as often two genuinely different, legitimately co-existing
    # products correctly left unmerged by fusion's own ISBN-first identity
    # rule (see deterministic_fusion._fuse_and_score_candidates): a novel
    # and its own graphic-novel adaptation, or a boxed set alongside the
    # standalone edition, both correctly tagged with the same series
    # position. Percy Jackson incident (2026-08-25): "The Sea of Monsters"
    # (95% confidence, 2 corroborating sources) and "The Sea of Monsters:
    # The Graphic Novel" (a different real product, its own distinct ISBN)
    # both resolve to number=2 -- flagging *both* as malformed dragged the
    # genuinely well-corroborated novel down to a "low" confidence grade
    # and got it silently auto-dropped by agents/series_agent.py's routing,
    # even though it was exactly the missing volume the user needed
    # surfaced. A candidate whose own isbn13 is non-empty and not shared by
    # any sibling at this number is distinguishable from those siblings on
    # its own terms -- that's what fusion's ISBN-first rule already used to
    # decide these are different books, not a sign it failed to merge
    # anything. Only candidates that are NOT distinguishable this way (no
    # isbn13 at all, or an isbn13 shared with another candidate at the same
    # number) still look like the "fusion should have merged these" case
    # this check exists to catch.
    for number, candidates_for_number in number_to_candidates.items():
        if len(candidates_for_number) < 2:
            continue
        isbn_counts: dict[str, int] = {}
        for candidate in candidates_for_number:
            isbn13 = str(candidate.get("isbn13") or "").strip()
            if isbn13:
                isbn_counts[isbn13] = isbn_counts.get(isbn13, 0) + 1
        for candidate in candidates_for_number:
            isbn13 = str(candidate.get("isbn13") or "").strip()
            if isbn13 and isbn_counts.get(isbn13, 0) < 2:
                continue
            malformed_books.append(
                {
                    "type": "malformed_book",
                    "series_id": series_id,
                    "candidate": candidate,
                    "reason": f"duplicate_number:{number}",
                }
            )

    numbering_gaps = sorted(skeleton_numbers - found_numbers)

    return {
        "series_id": series_id,
        "missing_books": missing_books,
        "malformed_books": malformed_books,
        "numbering_gaps": numbering_gaps,
        "timestamp": datetime.utcnow().isoformat(),
    }
