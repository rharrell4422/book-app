"""Diagnostic-only computations for book discovery: external-vs-owned
volume comparisons and human-readable drop explanations.

Split out of discovery_engine.py (RT-1a). Pure functions only -- no I/O, no
ORM, no module state (see the module comment preserved below, carried over
from the original file). Kept as its own small leaf module (rather than
folded into discovery_text.py) since _record_drop_diagnostic is a shared
dependency of both provider_io.py and deterministic_fusion.py, and putting
it in a module that depends on neither avoids a provider_io<->
deterministic_fusion import cycle.

discovery_engine.py re-exports everything below, so existing external
callers (agents/series_agent.py, tests, etc.) are unaffected by this split.
"""
from __future__ import annotations

from discovery_text import _integral_or_none, infer_number_from_title

def _record_drop_diagnostic(
    stage: str,
    candidate_identity: dict | None,
    reason: str,
    diagnostics_list: list[dict] | None,
) -> None:
    """Phase 3.5 of agentic discovery, PURE SHADOW MODE: records that a
    candidate/provider result was silently dropped and why, at points that
    previously discarded it with no trace at all (a JSON parse failure
    voiding the whole web provider, a cross-series contamination filter, an
    LLM reconciliation exclusion, or series_agent.py's own already-known
    suppression). A no-op whenever diagnostics_list is None, so every
    existing caller that doesn't pass one stays completely unaffected --
    this can only ever add entries to a list a caller explicitly opted
    into, never change what gets accepted/rejected.
    """
    if diagnostics_list is None:
        return
    diagnostics_list.append(
        {
            "stage": stage,
            "candidate_identity": candidate_identity,
            "reason": reason,
        }
    )




# ---------------------------------------------------------------------------
# Phase 4 of agentic discovery, PURE SHADOW MODE.
#
# Pure functions only -- no I/O, no ORM, no module state. series_agent.py
# calls them after its candidate loop has finished and logs the results into
# the same consolidated series_external_reality entry Phase 3.5 already
# emits; nothing here can reach candidate acceptance, the skeleton, the
# delta, confidence scoring, or the lookahead. Kept as standalone helpers
# (rather than inline in series_agent) so a later phase can reuse the exact
# same logic without going through the log.
# ---------------------------------------------------------------------------

MAX_DROP_EXPLANATIONS = 50

_DROP_EXPLANATIONS = {
    # web_structuring's diagnostic covers a whole provider batch (its
    # candidate_identity is all-None by construction -- see
    # _structure_web_results_with_llm), not one candidate, so its wording
    # deliberately says "structuring pass" rather than "candidate".
    ("web_structuring", "json_parse_failure"): (
        "Provider returned unstructured or invalid JSON; the entire structuring pass was discarded."
    ),
    ("cross_series_filter", "series_name_mismatch"): (
        "Candidate dropped because its series name did not match the target series."
    ),
    ("llm_reconciliation", "excluded_by_llm"): (
        "LLM reconciliation excluded this candidate as inconsistent or low-confidence."
    ),
    ("already_known", "suppressed_as_known"): (
        "Candidate suppressed because it matches an already-owned book."
    ),
}

_DEFAULT_DROP_EXPLANATION = "Candidate dropped for an unclassified reason."


def compute_external_missing_vs_owned(
    external_expected_total: int | None, owned_books: list[dict]
) -> list[int]:
    """Externally-expected volume numbers the library does not own --
    deliberately owned-only, unlike Phase 3.5's external_missing_numbers,
    which also subtracts every discovered candidate.

    That difference is the whole point: because Phase 3.5 subtracts the
    candidates too, no discovered candidate's number can ever appear in
    external_missing_numbers, so "does this candidate fill an externally-
    expected gap" is unanswerable from it. Subtracting only owned books
    leaves exactly the slots a candidate could be filling.

    owned_books is the same list[dict] shape _reconstruct_series_skeleton
    takes (series_agent passes its owned_books_for_skeleton straight
    through), so this stays free of any ORM dependency.

    Guarantees external_missing_numbers is a subset of what this returns:
    Phase 3.5 subtracts a superset of the numbers subtracted here.
    """
    if external_expected_total is None or external_expected_total <= 0:
        return []
    owned_integral = {
        number
        for number in (_integral_or_none(book.get("book_number")) for book in owned_books)
        if number is not None
    }
    return sorted(set(range(1, external_expected_total + 1)) - owned_integral)


def compute_inferred_number(raw: dict, series_name: str | None):
    """The series number series_agent's own candidate loop resolves for a
    raw candidate dict, reproduced exactly: the provider's structured hint
    when present, else a title-text inference.

    Kept byte-for-byte in step with the loop (including the `or` rather
    than an `is None` check, so a falsy hint still falls through to
    inference, and including series_name, without which the bare
    "<Series Name> <N>" pattern in infer_number_from_title never fires).
    Returns the resolved value as-is -- int, float (a 3.5 novella), or None.
    """
    title = str(raw.get("title") or "").strip()
    return raw.get("series_number_hint") or infer_number_from_title(title, series_name)


def compute_owned_number_coverage(owned_books: list[dict]) -> dict:
    """How much of the owned library carries a usable integer book number.

    external_gap_ratio silently overstates how incomplete a series is when
    owned books have a NULL/fractional book_number, since those contribute
    nothing to the owned side of the subtraction. Logging the coverage
    alongside the ratio is what lets a reader tell "genuinely missing
    volumes" from "our own numbering is patchy".
    """
    return {
        "owned_books_total": len(owned_books),
        "owned_books_with_numbers": sum(
            1 for book in owned_books if _integral_or_none(book.get("book_number")) is not None
        ),
    }


def compute_new_volume_flags(
    candidates: list[dict],
    series_name: str | None,
    external_missing_vs_owned: list[int],
    belongs_indices: set[int],
    known_indices: set[int],
) -> list[dict]:
    """One diagnostic entry per scanned candidate: the number it resolved
    to, whether that number is an externally-expected volume the library
    doesn't own, and which of the candidate loop's two gates it passed.

    belongs_indices/known_indices are positional into `candidates` and are
    captured by the loop itself -- belongs_to_series only reaches its final
    value after the universe-tie-in and compilation downgrades, so it can't
    be recomputed here.

    Emits the resolved number exactly as the loop saw it (a 3.5 novella
    stays 3.5) while is_new_volume stays integral-only, so this list and
    candidate_diagnostics never disagree about a candidate's number.
    Phase 5 derives "proposed" as belongs_to_series and not
    suppressed_as_known.
    """
    missing_set = set(external_missing_vs_owned)
    flags: list[dict] = []
    for index, raw in enumerate(candidates):
        inferred = compute_inferred_number(raw, series_name)
        number = _integral_or_none(inferred)
        flags.append(
            {
                "title": str(raw.get("title") or "").strip(),
                "isbn13": str(raw.get("isbn13") or "").strip() or None,
                "series_number": inferred,
                "is_new_volume": number is not None and number in missing_set,
                "belongs_to_series": index in belongs_indices,
                "suppressed_as_known": index in known_indices,
            }
        )
    return flags


def compute_external_gap_ratio(
    external_expected_total: int | None, external_missing_vs_owned: list[int]
) -> float | None:
    """How incomplete the owned library looks against the external series
    total, as a 0..1 scalar. None (rather than 0.0) whenever there's no
    usable external total, so "no external data" stays distinguishable
    from "owns everything".
    """
    if external_expected_total is None or external_expected_total <= 0:
        return None
    return round(len(external_missing_vs_owned) / external_expected_total, 4)


def compute_drop_explanations(drop_diagnostics: list[dict]) -> dict:
    """Flattens Phase 3.5's drop diagnostics (which nest the candidate's
    identity, and allow it to be absent entirely) into one flat entry per
    drop, each carrying a human-readable explanation of the stage/reason
    pair that produced it.

    Returns the explanation list capped at MAX_DROP_EXPLANATIONS, the
    pre-cap total, and a per-"stage:reason" count map computed over *every*
    entry rather than just the retained ones -- the aggregate shape is the
    part worth keeping when a run drops more than the cap, and it's what
    makes truncating the list safe. Entries stay in pipeline order; the cap
    keeps the first N.
    """
    explanations: list[dict] = []
    counts: dict[str, int] = {}
    for entry in drop_diagnostics:
        identity = entry.get("candidate_identity") or {}
        stage = entry.get("stage")
        reason = entry.get("reason")
        key = f"{stage}:{reason}"
        counts[key] = counts.get(key, 0) + 1
        explanations.append(
            {
                "stage": stage,
                "reason": reason,
                "title": identity.get("title"),
                "isbn13": identity.get("isbn13"),
                "series_number": identity.get("series_number"),
                "explanation": _DROP_EXPLANATIONS.get((stage, reason), _DEFAULT_DROP_EXPLANATION),
            }
        )
    return {
        "drop_explanations": explanations[:MAX_DROP_EXPLANATIONS],
        "drop_explanations_total": len(explanations),
        "drop_explanation_counts": counts,
    }
