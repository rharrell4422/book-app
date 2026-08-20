"""Phase 3 of agentic discovery (see project design chat): a deterministic,
side-effect-free confidence scoring layer over Phase 2's delta output.

Shadow mode only. Nothing in this module touches the database, calls an
LLM, makes a network request, or mutates its inputs -- it's a pure
function of (skeleton_entries, provider_candidates, delta) -> a confidence
dict. The only current caller (agents/series_agent.py) logs the result and
discards it; nothing reads or acts on it yet.

Reuses discovery_engine's own text-normalization/title guards
(normalize_text, core_title_key, _title_is_series_variant) rather than
re-implementing "does this title look real" a second time, and cross-
checks Phase 2's delta output (malformed_books) rather than re-deriving
that judgment independently -- if delta already flagged a candidate as a
series-name-variant stub, this module's title dimension agrees rather than
possibly disagreeing with itself.

Two deliberate deviations from a literal reading of the spec, both to
avoid reintroducing bugs this app has already fixed elsewhere:

- "Provider Confidence" collapses OpenLibrary's "medium-low" grade to
  "low". Every other dimension (and the overall-confidence rule) only
  ever uses four levels (high/medium/low/zero) -- a fifth grade that only
  exists for one dimension would make "overall" ambiguous for any
  candidate OpenLibrary alone contributed to.

- "Number Confidence" does NOT grade a candidate "low" just for being
  non-integer. book_number is a Float throughout this app specifically to
  support legitimate .5 companion/novella entries (see models.Book's own
  comment, and services/identity.py's _normalized_book_number_value,
  which was fixed to stop truncating 3.5 to 3). Grading every companion
  novella "low" purely for being fractional would be a straight
  regression of that fix. A valid fractional number is judged on the same
  high/medium footing as a valid integer; only a genuinely malformed
  value (non-numeric, negative) is graded low.
"""

from datetime import datetime

import discovery_engine

_LEVEL_RANK = {"zero": 0, "low": 1, "medium": 2, "high": 3}

_PROVIDER_CONFIDENCE = {
    "hardcover": "high",
    "google_books": "medium",
    "openlibrary": "low",  # see module docstring: collapsed from "medium-low"
    "web_search": "low",  # Brave snippet, per spec
}


def _to_float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _candidate_key(candidate: dict) -> tuple:
    """Matching key used to cross-reference a candidate against Phase 2's
    delta output -- not python object identity, so this still works if a
    caller passes an equivalent but separately-built dict/list.
    """
    return (
        str(candidate.get("title") or "").strip().lower(),
        candidate.get("isbn13"),
        _to_float_or_none(candidate.get("series_number")),
    )


def _skeleton_by_number(skeleton_entries: list[dict]) -> dict[float, dict]:
    by_number: dict[float, dict] = {}
    for entry in skeleton_entries:
        number = _to_float_or_none(entry.get("book_number"))
        if number is not None:
            by_number[number] = entry
    return by_number


def _provider_confidence(candidate: dict) -> str:
    provenance = candidate.get("source_provenance") or []
    provider_names = {entry.get("provider") for entry in provenance if entry.get("provider")}
    levels = [_PROVIDER_CONFIDENCE.get(name, "low") for name in provider_names]
    if not levels:
        return "low"
    # Corroboration from multiple providers is worth at least as much as
    # its single best contributing source, not diluted by a weaker one.
    return max(levels, key=lambda level: _LEVEL_RANK[level])


_TITLE_MALFORMED_REASONS = {"missing_title", "placeholder_title", "title_is_series_variant"}
_NUMBER_MALFORMED_REASON_PREFIXES = ("invalid_number", "negative_number", "duplicate_number")


def _title_confidence(
    candidate: dict,
    skeleton_by_number: dict[float, dict],
    series_name: str | None,
    delta_reasons: set[str],
) -> str:
    title = str(candidate.get("title") or "").strip()
    isbn13 = candidate.get("isbn13")
    number = _to_float_or_none(candidate.get("series_number"))

    already_flagged = any(reason in _TITLE_MALFORMED_REASONS for reason in delta_reasons)
    if already_flagged or discovery_engine._title_is_series_variant(title, series_name, isbn13, number):
        return "zero"

    skeleton_entry = skeleton_by_number.get(number) if number is not None else None
    if skeleton_entry is None:
        # Nothing in the skeleton to corroborate this title against yet
        # (a genuinely new book, or one with no resolvable number) -- not
        # wrong, just unverified.
        return "low"

    skeleton_title = str(skeleton_entry.get("title") or "").strip()
    if discovery_engine.normalize_text(title) == discovery_engine.normalize_text(skeleton_title):
        return "high"
    if discovery_engine.core_title_key(title) == discovery_engine.core_title_key(skeleton_title):
        return "medium"
    return "low"


def _number_confidence(candidate: dict, skeleton_numbers: set[float], delta_reasons: set[str]) -> str:
    # Phase 2 already caught invalid/negative/duplicate numbers -- a
    # duplicate specifically is a cross-candidate signal this function
    # can't see on its own (it only looks at one candidate's own value),
    # so it's folded in here rather than re-derived.
    if any(reason.startswith(_NUMBER_MALFORMED_REASON_PREFIXES) for reason in delta_reasons):
        return "low"

    number = _to_float_or_none(candidate.get("series_number"))
    if candidate.get("series_number") is not None and number is None:
        return "low"  # present but not a real number at all
    if number is None:
        return "low"  # no structured number to place at all
    if number <= 0:
        return "low"
    if number in skeleton_numbers:
        return "high"
    return "medium"  # valid, well-formed, but a number the skeleton doesn't have yet


def _series_alignment_confidence(candidate: dict, series_author: str | None) -> str:
    candidate_authors = candidate.get("authors") or []
    if not series_author or not candidate_authors:
        # No author to compare against on one side or the other --
        # inconclusive, not a mismatch, so this must not read as "zero"
        # (which is reserved for an actual, confirmed mismatch).
        return "low"

    series_tokens_by_name = [
        {token for token in discovery_engine.normalize_text(name).split() if token}
        for name in discovery_engine.split_author_names(series_author) or [series_author]
    ]

    for candidate_author in candidate_authors:
        candidate_tokens = {
            token for token in discovery_engine.normalize_text(candidate_author).split() if token
        }
        if not candidate_tokens:
            continue
        for series_tokens in series_tokens_by_name:
            if not series_tokens:
                continue
            candidate_surname = sorted(candidate_tokens)[-1] if candidate_tokens else None
            series_surname = sorted(series_tokens)[-1] if series_tokens else None
            # Token sets share every word -> exact match regardless of
            # order (co-author strings, "Jr."/middle names aside).
            if candidate_tokens == series_tokens:
                return "high"
            if candidate_surname and candidate_surname == series_surname:
                candidate_given = candidate_tokens - {candidate_surname}
                series_given = series_tokens - {series_surname}
                if _given_names_are_initials_variant(candidate_given, series_given):
                    return "medium"
    return "zero"


def _given_names_are_initials_variant(tokens_a: set[str], tokens_b: set[str]) -> bool:
    """True if the shorter side's given name(s) plausibly abbreviate the
    longer side's (e.g. "gj" vs "georgia" -- same leading letter, or a
    literal prefix like "geo" vs "georgia"), covering the "GJ Wagner vs
    Georgia Wagner" case the spec calls out. Deliberately loose -- a full
    "GJ" can't be verified against "Georgia" alone without a stored middle
    name -- since this only ever produces "medium", never "high": it's
    flagging a plausible variant for human review, not asserting identity.
    Given-name sets that are simply different names entirely (not a
    prefix/initial of one another) are NOT a variant -- more likely a
    different person who happens to share a surname, so that case falls
    through to "zero" in the caller rather than over-crediting a
    coincidental surname match.
    """
    if not tokens_a or not tokens_b:
        return True  # one side has no given name at all -- not a contradiction
    joined_a = "".join(sorted(tokens_a))
    joined_b = "".join(sorted(tokens_b))
    shorter, longer = (joined_a, joined_b) if len(joined_a) <= len(joined_b) else (joined_b, joined_a)
    if not shorter or len(shorter) > 6:
        return False  # too long on both sides to plausibly be a mere abbreviation
    return longer.startswith(shorter[0]) or longer.startswith(shorter)


def _overall_confidence(levels: list[str]) -> str:
    """Deterministic total order over every combination of the four
    dimensions -- the spec only names four example combinations (all
    high/mix of high+medium/mostly low/any zero), so the remaining cases
    (e.g. a mix including both high and low, or all medium) are resolved
    here by always rounding DOWN toward caution rather than up: any zero
    wins outright, then all-high, then anything that's high/medium only,
    then any remaining low pulls the whole thing down to low.
    """
    if any(level == "zero" for level in levels):
        return "zero"
    if all(level == "high" for level in levels):
        return "high"
    if all(level in ("high", "medium") for level in levels):
        return "medium"
    return "low"


def compute_confidence(
    series_id: int,
    skeleton_entries: list[dict],
    provider_candidates: list[dict],
    delta: dict,
    *,
    series_name: str | None = None,
    series_author: str | None = None,
) -> dict:
    """Pure scoring pass -- does not mutate skeleton_entries,
    provider_candidates, or delta, makes no LLM/network/DB calls, and
    infers nothing beyond structured fields already present on each
    candidate.
    """
    skeleton_by_number = _skeleton_by_number(skeleton_entries)
    skeleton_numbers = set(skeleton_by_number.keys())

    reasons_by_candidate_key: dict[tuple, set[str]] = {}
    for entry in delta.get("malformed_books") or []:
        candidate_dict = entry.get("candidate")
        reason = entry.get("reason")
        if isinstance(candidate_dict, dict) and reason:
            reasons_by_candidate_key.setdefault(_candidate_key(candidate_dict), set()).add(reason)

    scored = []
    for candidate in provider_candidates:
        delta_reasons = reasons_by_candidate_key.get(_candidate_key(candidate), set())

        provider_confidence = _provider_confidence(candidate)
        title_confidence = _title_confidence(candidate, skeleton_by_number, series_name, delta_reasons)
        number_confidence = _number_confidence(candidate, skeleton_numbers, delta_reasons)
        series_alignment_confidence = _series_alignment_confidence(candidate, series_author)

        overall = _overall_confidence(
            [provider_confidence, title_confidence, number_confidence, series_alignment_confidence]
        )

        scored.append(
            {
                "candidate": candidate,
                "provider_confidence": provider_confidence,
                "title_confidence": title_confidence,
                "number_confidence": number_confidence,
                "series_alignment_confidence": series_alignment_confidence,
                "overall": overall,
            }
        )

    return {
        "series_id": series_id,
        "confidence": scored,
        "timestamp": datetime.utcnow().isoformat(),
    }
