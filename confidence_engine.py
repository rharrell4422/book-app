"""Phase 3 of agentic discovery (see project design chat): a deterministic,
side-effect-free confidence scoring layer over Phase 2's delta output.

Still side-effect-free -- nothing in this module touches the database,
calls an LLM, makes a network request, or mutates its inputs; it's a pure
function of (skeleton_entries, provider_candidates, delta) -> a confidence
dict. As of the manual-override rollout, `agents/series_agent.py` reads
`overall` per candidate (via `correlation_key`, below) to route it to
auto-accept/needs_review/auto-drop -- see that module's `run_series_check`
for the routing logic. This module still only ever computes; it never
decides anything itself.

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

Title Confidence has a fifth grade, "unverified", distinct from "low":
"low" means a skeleton entry exists for this number and the title
disagrees with it (a real, corroborated mismatch); "unverified" means no
skeleton entry exists to compare against at all -- most commonly a
genuinely new book the library doesn't own yet, which is the exact case
the manual-override routing needs to be able to tell apart from an actual
contradiction (see _overall_confidence). Only "title_confidence" ever
produces "unverified" -- provider/number/series-alignment confidence
never do, so at most one of the four dimensions can carry it. Without
this distinction, every first-time discovery of a brand-new book would
grade "low" purely for being new (no skeleton entry can exist for a
number nothing has seen before) and get auto-dropped by the routing
below -- the exact regression this grade exists to prevent.
"""

from datetime import datetime

import discovery_engine

_LEVEL_RANK = {"zero": 0, "low": 1, "medium": 2, "high": 3}

_PROVIDER_CONFIDENCE = {
    "hardcover": "high",
    "google_books": "medium",
    "openlibrary": "low",  # see module docstring: collapsed from "medium-low"
    # Structured Amazon-product-page extraction (Phase 1, Check Now only --
    # see apify_provider.py). Starts at "medium" pending production
    # validation; upgrade to "high" once stable (Apify integration design
    # chat's consensus).
    "apify": "medium",
    "web_search": "low",  # frontier web-search snippet (formerly Brave, now Serper), per spec
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

    Both sides of every existing call site (compute_confidence's own
    `provider_candidates` and delta's `malformed_books[*]["candidate"]`)
    are discovery_engine's PRE-_filter_and_merge `unified_candidates`
    (each converted via UnifiedCandidate.model_dump()), so both always
    carry the number under the field name "series_number" -- this
    function must keep assuming that and nothing else. Do not widen it to
    also check "series_number_hint"; that would fix nothing here (both
    sides already agree) and risks masking a real regression if one side
    ever stopped being pre-filter. See `correlation_key` below for the
    unrelated, POST-filter lookup case.
    """
    return (
        str(candidate.get("title") or "").strip().lower(),
        candidate.get("isbn13"),
        _to_float_or_none(candidate.get("series_number")),
    )


def correlation_key(candidate: dict) -> tuple:
    """Same (title, isbn13, number) shape as `_candidate_key`, but tolerant
    of which field name carries the series number -- for correlating a
    scored PRE-_filter_and_merge candidate (this module's own input,
    field name "series_number") against a POST-_filter_and_merge raw
    candidate dict (what agents/series_agent.py's belongs_to_series loop
    iterates, field name "series_number_hint" instead -- see
    discovery_engine._unified_candidate_to_raw_dict, which never copies
    UnifiedCandidate.series_number back onto "series_number_hint").

    Deliberately a separate function rather than a change to
    `_candidate_key` itself: `_candidate_key`'s existing callers are both
    pre-filter and already internally consistent (see its own docstring)
    -- widening it to also check "series_number_hint" would have no effect
    there and would risk silently hiding a future regression instead of
    surfacing it.
    """
    number = candidate.get("series_number")
    if number is None:
        number = candidate.get("series_number_hint")
    return (
        str(candidate.get("title") or "").strip().lower(),
        candidate.get("isbn13"),
        _to_float_or_none(number),
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
    # Provenance entries key their provider name as "source" (see
    # discovery_engine.py's UnifiedCandidate provenance dicts, and
    # delta_engine._candidate_providers, which had the identical bug) --
    # no provenance entry has ever actually used "provider", so a prior
    # version of this line silently always fell through the `not levels`
    # branch below and graded every candidate's provider_confidence "low"
    # regardless of its real source, hardcover included.
    provider_names = {entry.get("source") for entry in provenance if entry.get("source")}
    levels = [_PROVIDER_CONFIDENCE.get(name, "low") for name in provider_names]
    if not levels:
        return "low"
    # Corroboration from multiple providers is worth at least as much as
    # its single best contributing source, not diluted by a weaker one.
    return max(levels, key=lambda level: _LEVEL_RANK[level])


_TITLE_MALFORMED_REASONS = {
    "missing_title",
    "placeholder_title",
    "title_is_series_variant",
    # CR-8: delta_engine._malformed_reason also emits "insufficient_metadata"
    # (metadata_completeness_score below RECONCILIATION_METADATA_COMPLETENESS_
    # THRESHOLD) for a candidate whose title/number individually look
    # well-formed but whose overall metadata is too sparse to trust. That
    # reason was already landing in delta_reasons (compute_confidence reads
    # every malformed_books entry, not a filtered subset), but neither
    # malformed-reason check in this module tested for it, so a candidate
    # delta had already flagged as malformed could still score confidently
    # on every dimension here -- the exact bug this closes. Routed through
    # title, not number: the signal is about overall candidate completeness
    # (title+author+isbn+date combined), not the number field specifically,
    # and title_confidence's "zero" path is the existing, already-proven
    # mechanism for "delta already told us this candidate isn't real" (see
    # the other three members of this set, each a different kind of
    # delta-confirmed structural problem routed through the same place).
    "insufficient_metadata",
}
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
        # wrong, just unverified. Distinct from "low", which is reserved
        # for a title that *does* have a skeleton entry to compare against
        # and disagrees with it (see module docstring).
        return "unverified"

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

    series_name_variants = [
        [token for token in discovery_engine.normalize_text(name).split() if token]
        for name in discovery_engine.split_author_names(series_author) or [series_author]
    ]

    for candidate_author in candidate_authors:
        candidate_token_list = [
            token for token in discovery_engine.normalize_text(candidate_author).split() if token
        ]
        if not candidate_token_list:
            continue
        candidate_tokens = set(candidate_token_list)
        # Surname is the last word as *written*, not the alphabetically-last
        # token. sorted(candidate_tokens)[-1] only happens to work when the
        # given name alphabetically precedes the surname (e.g. "gj" <
        # "wagner") -- it silently breaks the opposite case ("gj" vs
        # "adams": sorted() puts "gj" last, not "adams", so a real surname
        # match is missed and this falls through to "zero" instead of the
        # "medium" the initials-variant case below is built for). Author
        # strings are conventionally "given name(s) surname", so position,
        # not alphabetical rank, is the actual surname signal. (Residual
        # gap: a surname-first string with no comma, e.g. "Wagner GJ", is
        # still read with "GJ" as the surname -- both this function and
        # discovery_engine._author_matches already agree on rejecting that
        # narrower case, so it's out of scope here.)
        candidate_surname = candidate_token_list[-1]
        for series_token_list in series_name_variants:
            if not series_token_list:
                continue
            series_tokens = set(series_token_list)
            series_surname = series_token_list[-1]
            # Token sets share every word -> exact match regardless of
            # order (co-author strings, "Jr."/middle names aside).
            if candidate_tokens == series_tokens:
                return "high"
            if candidate_surname == series_surname:
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
    wins outright, then all-high, then anything that's high/medium (or
    title's "unverified" -- see below) only, then any remaining low pulls
    the whole thing down to low.

    Complete decision table for "unverified" (Phase 0 fix, finalized in
    the discovery-agentic-replacement review loop -- see
    discovery_agentic_replacement_recommendation.md §0.2 and
    discovery_agentic_replacement_evaluation.md §4/§6/§8, which flagged the
    single-worked-example version of this rule as underspecified). Only
    `title_confidence` ever produces "unverified" (see module docstring),
    so it appears in at most one of the four `levels` at a time; the table
    below is therefore a full enumeration of every *reachable* pairing,
    not a partial one:

        title=unverified, other 3 dims (provider/number/alignment)  -> overall  -> routing
        --------------------------------------------------------------------------------
        any of the other 3 == "zero"  (unverified + zero)           -> zero     -> auto-reject
        no zero; any of the other 3 == "low"  (unverified + low)    -> low      -> auto-reject
        no zero/low; any of the other 3 == "medium"  (unverified + medium) -> medium -> accept / escalate*
        no zero/low; all of the other 3 == "high"  (unverified + high)      -> medium -> accept / escalate*
        (title=unverified can never alone reach "high" -- see below)

    "unverified + zero" is listed for completeness per the review loop's
    explicit ask, but resolves via the *first* rule below ("any zero wins
    outright") rather than needing a dedicated branch -- it was already
    correct before this table was written down; this just documents why.

    The identical total order also governs every combination that does
    not involve "unverified" at all (every dimension a real high/medium/
    low/zero grade), and every mixed combination across all four
    dimensions collapses onto one of the same four rows, because the
    logic below is dimension-count-agnostic, not a hardcoded 4-tuple
    lookup:

        any dimension == "zero"                    -> zero    -> auto-reject
        all dimensions == "high"                    -> high    -> auto-accept
        no zero, not all high, no "low" present     -> medium  -> accept / escalate*
        no zero, any remaining "low"                -> low     -> auto-reject

    Acceptance / escalation / rejection rules, as actually consumed by
    `agents/series_agent.py`'s manual-override routing (see that module's
    routing comment for the authoritative behavior; this restates it here
    so the rule is defined in exactly one other place, not re-derived):
      - "zero"  -> always auto-reject (dropped), regardless of whether
        `belongs_to_series` independently accepted the candidate. A
        confirmed skeleton-disagreement or author mismatch is real
        negative information that overrides a cruder textual heuristic.
      - "low"   -> always auto-reject (dropped), same reasoning as "zero".
      - "medium" (including every "unverified"-ceiling case above, which
        can only ever produce "medium", never "high") -> *auto-accept*
        when `belongs_to_series` already confirmed series membership;
        *escalate to needs_review* only when `belongs_to_series` could
        not confirm it (`low_confidence_ambiguous=True`). "unverified" is
        the expected, permanent state of title_confidence for every
        genuinely new book (nothing not already owned can have a
        skeleton entry to compare against), so it is not itself a red
        flag -- only an independent `belongs_to_series` failure escalates
        it to review.
      - "high"  -> always auto-accept. Only reachable when every
        dimension, including title, is a real "high" -- i.e. never for a
        book absent from the skeleton, by construction (see
        `_title_confidence`).

    *"accept / escalate" means the accept-vs-escalate choice is made by
    the caller based on a signal `_overall_confidence` does not have
    (`belongs_to_series`), not by this function -- see routing rules
    above.
    """
    if any(level == "zero" for level in levels):
        return "zero"
    if all(level == "high" for level in levels):
        return "high"
    if all(level in ("high", "medium", "unverified") for level in levels):
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
