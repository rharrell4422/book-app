"""Deterministic, no-I/O candidate fusion/scoring/edition-collapse logic for
book discovery.

Split out of discovery_engine.py (RT-1a). Pure functions only: merging raw
per-provider results into UnifiedCandidate objects, scoring/ranking them,
collapsing duplicate editions of the same underlying book, and the
deterministic (pre-LLM) cross-series-contamination/author-fallback gating
that decides what's worth showing at all. provider_io.py is the I/O
counterpart that produces this module's inputs (raw provider/web-search
results) and, in a couple of cases (LLM reconciliation), consumes this
module's UnifiedCandidate type and scoring helpers.

discovery_engine.py re-exports everything below, so existing external
callers (agents/series_agent.py, tests, etc.) are unaffected by this split.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field

from services.identity import _edition_priority, _normalize_title_for_identity

from discovery_text import (
    _author_matches,
    _log,
    _series_names_compatible,
    _title_is_series_variant,
    _to_int_or_none,
    core_title_key,
    infer_number_from_title,
    infer_series_hint_from_title_text,
    is_english_or_unknown,
    looks_like_non_new_release,
    looks_like_placeholder_title,
    looks_like_series_index_entry,
    normalize_text,
)
from diagnostics import _record_drop_diagnostic

def _filter_and_merge(
    raw_results: list[dict],
    author: str,
    exclude_title_keys: set[str],
    confidence: str,
    series_name: str | None = None,
) -> list[dict]:
    merged: list[dict] = []
    seen_keys: set[str] = set()
    for raw in raw_results:
        if not _author_matches(raw.get("authors") or [], author):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        if looks_like_non_new_release(title):
            continue
        if looks_like_placeholder_title(title):
            continue
        if not is_english_or_unknown(raw.get("language")):
            continue

        isbn13 = str(raw.get("isbn13") or "").strip()
        has_number_hint = bool(raw.get("series_number_hint")) or bool(
            infer_number_from_title(title, series_name)
        )
        # Google Books/OpenLibrary never carry a structured series field, so
        # for candidates missing one, fall back to a narrow title-text
        # pattern (see infer_series_hint_from_title_text) before giving up.
        series_name_hint = raw.get("series_name_hint") or infer_series_hint_from_title_text(title)
        # discover_candidates_for_series always knows the one series it's
        # checking, so that fixed name is authoritative here. Author-wide
        # discovery has no such fixed name (series_name is None) and instead
        # falls back to each individual candidate's own guessed series name
        # (from Hardcover's index, the web-search LLM pass, or the title-text
        # fallback above) so the same stub-listing check still applies
        # per-candidate.
        effective_series_name = series_name or series_name_hint
        if looks_like_series_index_entry(
            title, effective_series_name, isbn13, has_number_hint
        ) or _title_is_series_variant(title, effective_series_name, isbn13, raw.get("series_number_hint")):
            continue

        title_key = core_title_key(title)
        if title_key and title_key in exclude_title_keys:
            continue

        dedupe_key = isbn13 or title_key or normalize_text(title)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        merged.append(
            {
                **raw,
                # Preserve a candidate's own already-assigned confidence
                # (present when `raw` came from re-merging previously-fused
                # UnifiedCandidates, e.g. series_agent.py's missing-volume
                # skeleton reconstruction -- see _unified_candidate_to_raw_dict)
                # rather than unconditionally overwriting it with this call's
                # single blanket `confidence` argument. Fresh provider hits
                # (the normal targeted/fallback fetch passes) never carry a
                # "confidence" key yet at this point, so they still get
                # stamped with `confidence` exactly as before -- this only
                # changes behavior for candidates that already have one.
                # Without this, re-merging a series' full candidate set after
                # skeleton recovery collapsed EVERY candidate's confidence
                # (including originals from a clean "targeted" hit) down to
                # a single "author_fallback" whenever fallback triggered at
                # all, which silently defeated series_agent.py's
                # targeted_with_number acceptance check for every candidate
                # in a series whose titles don't textually reference the
                # series name (regression: Georgia Wagner's "Jonathan Hunt
                # Thriller Series" -- author-fallback always triggers because
                # providers under-index it, so real sequels like "Desert
                # Protocol" and "The Levee Ghosts" never had any other way to
                # clear the gate and "Check Now" always reported zero new
                # books despite discovery correctly finding them).
                "confidence": raw.get("confidence") or confidence,
                "series_name_hint": series_name_hint,
                "series_total_hint": raw.get("series_total_hint"),
            }
        )
    return merged




_METADATA_COMPLETENESS_FIELDS = (
    "title",
    "authors",
    "series_name_hint",
    "series_number_hint",
    "isbn13",
    "published_date",
    "description",
)

# Rough per-provider weight for confidence_score, mirroring the same
# hardcover > google_books > openlibrary > web_search trust ordering
# discover_candidates_for_series's own merge-priority comment already
# documents elsewhere (Hardcover's series data is structured/curated;
# web_search's is an LLM's best-effort read of free-text search snippets).
# Corroboration across multiple *different* providers and a real ISBN both
# add on top of this base weight rather than replacing it.
_PROVIDER_CONFIDENCE_WEIGHT = {
    "hardcover": 0.4,
    "google_books": 0.3,
    "openlibrary": 0.25,
    # Structured Amazon-product-page extraction -- trusted above
    # web_search's LLM-parsed-snippet guesses, but below the three catalog
    # APIs until proven stable in production (Apify integration design
    # chat's consensus). See _fetch_apify_discovery for how Apify
    # candidates also get positional (not just weight) primacy over
    # web_search inside _fuse_and_score_candidates.
    "apify": 0.20,
    "web_search": 0.15,
}


class UnifiedCandidate(BaseModel):
    """One real-world book, after _fuse_and_score_candidates has merged
    every raw provider hit that plausibly refers to it -- matched by the
    same isbn13 -> title_key -> normalized-title identity chain
    _filter_and_merge's own seen_keys dedupe already uses -- into a single
    representation.

    This does not replace _filter_and_merge's author/language/placeholder/
    bundle-title/series-index/already-owned filtering. See
    _fuse_and_score_candidates and _unified_candidate_to_raw_dict, which
    converts instances of this back into the exact flat dict shape every
    _fetch_* provider (and _filter_and_merge) already expects, so that
    filtering keeps running completely unchanged on the fused result.
    confidence_score/metadata_completeness_score/source_provenance are new,
    additive fields -- nothing downstream reads them yet, but they're
    carried through the dict conversion so a later phase can.
    """

    title: str
    authors: list[str] = Field(default_factory=list)
    series_name: str | None = None
    series_number: float | None = None
    isbn13: str | None = None
    edition_type: str = "unknown"
    published_date: str | None = None
    source_provenance: list[dict] = Field(default_factory=list)
    confidence_score: float = 0.0
    metadata_completeness_score: float = 0.0
    upcoming_hint: bool | None = None


def _authors_overlap(authors_a: list[str], authors_b: list[str]) -> bool:
    """True if any name in authors_a fuzzy-matches (same token-subset check
    as _author_matches) any name in authors_b -- used by
    _fuse_and_score_candidates' TITLE+AUTHOR identity fallback (see its own
    docstring) to decide whether two same-titled hits plausibly refer to
    the same real book. Only ONE overlapping name is required, not the
    full list matching -- e.g. a novel's single author vs. a graphic-novel
    adaptation's author+illustrator+colorist list should still be
    recognized as the same underlying book off the one shared name.

    Two empty/author-less lists (or one empty, one not) return False --
    "no data to compare" is handled by the caller (title alone still
    governs there), not treated as a false claim of overlap here.
    """
    if not authors_a or not authors_b:
        return False
    return any(_author_matches(authors_b, author) for author in authors_a if author)


def _first_present_field(members: list[dict], field: str, *, exclude_sources: set[str] | None = None):
    """First non-empty value for `field` across a group of raw candidate
    dicts already confirmed to be the same real book (see
    _fuse_and_score_candidates) -- used to backfill a gap in the group's
    primary/representative member from one of its duplicates. `exclude_sources`
    lets a caller withhold a specific provider from being trusted as a
    backfill source for one particular field (see isbn13 handling below)
    without affecting any other field.
    """
    for member in members:
        if exclude_sources and str(member.get("source") or "") in exclude_sources:
            continue
        value = member.get(field)
        if isinstance(value, list):
            if value:
                return value
        elif value not in (None, ""):
            return value
    return None


def _fuse_and_score_candidates(
    provider_results: dict,
    author: str,
    series_name: str | None,
) -> list[UnifiedCandidate]:
    """Groups every raw candidate _fetch_all_providers_parallel returned --
    across all four providers -- by real-world-book identity (the same
    isbn13 -> title_key -> normalized-title chain _filter_and_merge's own
    seen_keys dedupe already uses), then fuses each group into one
    UnifiedCandidate: a representative dict (the highest-priority member --
    hardcover > google_books > openlibrary > web_search, the same priority
    order callers already concatenate provider_results in) with any
    *missing* fields backfilled from the other members of that same
    confirmed-duplicate group, plus a confidence_score and
    metadata_completeness_score.

    Deliberately leaves ALL of _filter_and_merge's own filtering untouched --
    this only pre-deduplicates and enriches what _filter_and_merge receives,
    it doesn't decide what survives.

    `authors`/`language` are backfilled slightly differently from every
    other field: instead of always preferring the primary member's own
    value, they prefer whichever group member's value would actually pass
    _filter_and_merge's author-match/language checks (falling back to the
    primary's own value if none do). Without this, collapsing straight to
    the primary member's fields could make a group *more* likely to be
    dropped than before fusion existed -- pre-fusion, _filter_and_merge
    evaluated every duplicate separately and any single one of them passing
    was enough for that identity key to survive; post-fusion there's only
    one fused dict per identity, so it needs the best-available author/
    language value among the group, not just whichever provider happened to
    sort first.

    isbn13 is also handled differently from the other backfilled fields:
    web_search's isbn13 is an LLM's guess parsed out of unstructured
    search-result text (see _fetch_web_search), not a catalog-verified
    identifier like the other three providers'. It's trusted the same as
    any provider for *identity grouping* (a wrong guess there just fails to
    group, it doesn't wrongly merge two different books), but it is not
    trusted to *backfill* an ISBN onto a duplicate that arrived from a
    different, ISBN-less provider, since a wrong backfilled ISBN would
    directly change that candidate's dedupe key and stub-listing check
    inside _filter_and_merge.

    PB-10 grouping fix (Percy Jackson catalog-sufficiency investigation):
    identity grouping used to be strict isbn13-or-title-key -- two hits for
    the exact same real book (e.g. Hardcover's "The Lightning Thief" with
    an ISBN, OpenLibrary's "The Lightning Thief" with none) landed in
    different groups purely because only one of them carried an ISBN, so
    they never got credit for corroborating each other. Grouping now falls
    back to a TITLE+AUTHOR match (same title-key, at least one overlapping
    author -- see _authors_overlap) whenever *at least one side of the
    comparison lacks an ISBN*. Deliberately narrow: if both sides carry an
    ISBN and it differs, that's a real, unresolved edition question (e.g.
    US vs. UK printing) left to _finalize_candidates' own dominance-based
    edition-collapse logic downstream, not silently decided here by title
    text alone.
    """
    ordered_raw: list[dict] = [
        *(provider_results.get("hardcover") or []),
        *(provider_results.get("google") or []),
        *(provider_results.get("openlibrary") or []),
        *(provider_results.get("web") or []),
    ]

    groups: dict[str, list[dict]] = {}
    group_order: list[str] = []
    # Parallel per-group bookkeeping the matching loop below needs, kept
    # in lockstep with `groups`/`group_order` rather than recomputed from
    # `groups` each time -- group_isbn is None until some member supplies
    # one, and by construction (see the ISBN-conflict check below) never
    # holds more than one distinct value.
    group_title_key: dict[str, str] = {}
    group_authors: dict[str, list[str]] = {}
    group_isbn: dict[str, str | None] = {}
    isbn_to_group: dict[str, str] = {}
    next_group_id = 0

    for raw in ordered_raw:
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        isbn13 = str(raw.get("isbn13") or "").strip() or None
        title_key = core_title_key(title) or normalize_text(title)
        raw_authors = [a for a in (raw.get("authors") or []) if a]

        target_group_id: str | None = None
        if isbn13 and isbn13 in isbn_to_group:
            target_group_id = isbn_to_group[isbn13]
        else:
            for candidate_id in group_order:
                if group_title_key.get(candidate_id) != title_key:
                    continue
                existing_isbn = group_isbn.get(candidate_id)
                if existing_isbn and isbn13 and existing_isbn != isbn13:
                    # Both sides have an ISBN and it differs -- a real
                    # edition question, not this function's to resolve.
                    continue
                if raw_authors and group_authors.get(candidate_id) and not _authors_overlap(
                    raw_authors, group_authors[candidate_id]
                ):
                    continue  # same title, but no author in common at all -- different book
                target_group_id = candidate_id
                break

        if target_group_id is None:
            target_group_id = f"g{next_group_id}"
            next_group_id += 1
            groups[target_group_id] = []
            group_order.append(target_group_id)
            group_title_key[target_group_id] = title_key
            group_authors[target_group_id] = []
            group_isbn[target_group_id] = None

        groups[target_group_id].append(raw)
        if raw_authors:
            group_authors[target_group_id] = list(dict.fromkeys(group_authors[target_group_id] + raw_authors))
        if isbn13 and not group_isbn[target_group_id]:
            group_isbn[target_group_id] = isbn13
            isbn_to_group[isbn13] = target_group_id

    fused: list[UnifiedCandidate] = []
    for identity_key in group_order:
        members = groups[identity_key]
        primary = members[0]

        author_matching_authors = next(
            (member.get("authors") for member in members if _author_matches(member.get("authors") or [], author)),
            None,
        )
        merged_authors = list(author_matching_authors or _first_present_field(members, "authors") or [])

        language_ok_member = next(
            (member for member in members if is_english_or_unknown(member.get("language"))), primary
        )
        merged_language = language_ok_member.get("language")

        merged_isbn13 = str(primary.get("isbn13") or "").strip() or None
        if not merged_isbn13:
            backfilled_isbn = _first_present_field(members, "isbn13", exclude_sources={"web_search"})
            merged_isbn13 = str(backfilled_isbn).strip() if backfilled_isbn else None

        # Same backfill treatment as isbn13 above -- asin is only ever set
        # by apify_provider.py (confirmed by grep; no exclude_sources
        # equivalent needed, unlike isbn13's web_search exclusion, since
        # there's only one truthful source to backfill from in the first
        # place). Without this, an ASIN captured on an Apify hit silently
        # vanishes whenever that hit gets grouped with any other provider's
        # hit for the same book -- members[0]/primary becomes that other
        # provider's dict, which never had an "asin" key at all -- exactly
        # the multi-source case the "Review Candidate Book" notification's
        # optional ASIN lookup most wants it for.
        merged_asin = str(primary.get("asin") or "").strip() or None
        if not merged_asin:
            backfilled_asin = _first_present_field(members, "asin")
            merged_asin = str(backfilled_asin).strip() if backfilled_asin else None

        merged_published_date = str(
            primary.get("published_date") or _first_present_field(members, "published_date") or ""
        ).strip()
        merged_description = primary.get("description") or _first_present_field(members, "description")
        merged_series_name_hint = primary.get("series_name_hint") or _first_present_field(members, "series_name_hint")
        merged_series_number_hint = primary.get("series_number_hint") or _first_present_field(
            members, "series_number_hint"
        )
        merged_series_total_hint = primary.get("series_total_hint") or _first_present_field(
            members, "series_total_hint"
        )
        merged_upcoming_hint = primary.get("upcoming_hint")
        if merged_upcoming_hint is None:
            merged_upcoming_hint = _first_present_field(members, "upcoming_hint")
        merged_source_url = primary.get("source_url") or _first_present_field(members, "source_url")

        unique_sources = list(dict.fromkeys(str(member.get("source") or "unknown") for member in members))
        confidence = sum(_PROVIDER_CONFIDENCE_WEIGHT.get(source, 0.1) for source in unique_sources)
        if len(unique_sources) > 1:
            confidence += 0.1
        if merged_isbn13:
            confidence += 0.1
        if merged_authors and _author_matches(merged_authors, author):
            confidence += 0.1

        # Series-name agreement: the same kind of provider-disagreement
        # signal _candidate_has_provenance_disagreement checks for
        # number/ISBN, applied to series identity, plus a down-score for
        # candidates that carry no series-name signal at all or explicitly
        # point at a different series than the one being searched for --
        # both weaker/contradicting evidence that this candidate actually
        # belongs to the target series (see _is_cross_series_contamination,
        # which hard-excludes explicit mismatches only on the fallback
        # pass -- this applies more broadly, as a soft penalty, to every
        # fused candidate regardless of which pass produced it). Uses
        # _series_names_compatible rather than strict equality so a
        # differently-branded-but-real hint for this same series (e.g.
        # Hardcover's bare "Jonathan Hunt" against a target tracked as
        # "Jonathan Hunt Thriller Series") isn't penalized as a mismatch.
        raw_provenance_series_names = [
            str(member.get("series_name_hint") or "")
            for member in members
            if member.get("series_name_hint")
        ]
        if any(
            not _series_names_compatible(a, b)
            for i, a in enumerate(raw_provenance_series_names)
            for b in raw_provenance_series_names[i + 1 :]
        ):
            confidence -= 0.1
        if not merged_series_name_hint:
            confidence -= 0.05
        elif series_name and not _series_names_compatible(merged_series_name_hint, series_name):
            confidence -= 0.1
        confidence_score = round(min(max(confidence, 0.0), 1.0), 4)

        metadata_completeness_score = _metadata_completeness_score(
            {
                "title": primary.get("title"),
                "authors": merged_authors,
                "series_name_hint": merged_series_name_hint,
                "series_number_hint": merged_series_number_hint,
                "isbn13": merged_isbn13,
                "published_date": merged_published_date,
                "description": merged_description,
            }
        )

        try:
            series_number_value = float(merged_series_number_hint) if merged_series_number_hint is not None else None
        except (TypeError, ValueError):
            series_number_value = None

        # Carry the backfilled (not just the primary's raw) hint fields
        # forward via the provenance entries themselves, so
        # _unified_candidate_to_raw_dict can recover them without needing
        # extra non-spec fields on the model -- see its own docstring.
        provenance = [dict(member) for member in members]
        provenance[0] = {
            **provenance[0],
            "authors": merged_authors,
            "language": merged_language,
            "isbn13": merged_isbn13,
            "asin": merged_asin,
            "published_date": merged_published_date,
            "description": merged_description,
            "series_name_hint": merged_series_name_hint,
            "series_number_hint": merged_series_number_hint,
            "series_total_hint": merged_series_total_hint,
            "upcoming_hint": merged_upcoming_hint,
            "source_url": merged_source_url,
        }

        fused.append(
            UnifiedCandidate(
                title=str(primary.get("title") or "").strip(),
                authors=merged_authors,
                series_name=(str(merged_series_name_hint).strip() if merged_series_name_hint else None) or series_name,
                series_number=series_number_value,
                isbn13=merged_isbn13,
                edition_type="unknown",
                published_date=merged_published_date or None,
                source_provenance=provenance,
                confidence_score=confidence_score,
                metadata_completeness_score=metadata_completeness_score,
                upcoming_hint=bool(merged_upcoming_hint) if merged_upcoming_hint is not None else None,
            )
        )

    return fused


def _unified_candidate_to_raw_dict(candidate: UnifiedCandidate) -> dict:
    """Converts a fused UnifiedCandidate back into the flat dict shape every
    _fetch_* provider (and _filter_and_merge) already expects, so fusion is
    a drop-in step in front of unchanged merge/filter logic.

    Starts from the fused/backfilled representative dict fusion already
    built (source_provenance[0] -- see _fuse_and_score_candidates, which
    overwrites that entry's own author/language/isbn13/hint fields with the
    group's backfilled values while leaving source/source_id/source_url on
    it) and overlays the UnifiedCandidate's own title/authors/isbn13/
    published_date/upcoming_hint on top, since those are the fields fusion
    computed with the extra author-match/isbn-trust care described in
    _fuse_and_score_candidates.

    NS-9: this is the deliberate seam where this pipeline's two candidate
    shapes meet -- the typed UnifiedCandidate dataclass fusion just built,
    and the flat dict shape every downstream stage (_filter_and_merge,
    reconciliation, series_agent.py) has always operated on. Rather than
    migrate those stages to the dataclass, fusion converts back to a dict
    immediately so the rest of the pipeline is unaffected; the extra
    confidence_score/metadata_completeness_score/source_provenance fields
    ride along on that dict only as far as finalize_discovery_output
    (below), which strips them back off before anything outside this
    pipeline ever sees a candidate.
    """
    provenance = candidate.source_provenance or [{}]
    base = dict(provenance[0])
    base.update(
        {
            "title": candidate.title,
            "authors": list(candidate.authors),
            "isbn13": candidate.isbn13,
            "published_date": candidate.published_date or "",
            "upcoming_hint": candidate.upcoming_hint,
            # New, additive fields -- _filter_and_merge doesn't read these
            # today (nothing downstream does yet), but they ride along on
            # the dict unchanged since _filter_and_merge spreads **raw into
            # its own output.
            "confidence_score": candidate.confidence_score,
            "metadata_completeness_score": candidate.metadata_completeness_score,
            "source_provenance": candidate.source_provenance,
            "edition_type": candidate.edition_type,
        }
    )
    return base



def _resolve_candidate_number(candidate: "UnifiedCandidate", series_name: str | None) -> float | None:
    """A candidate's own fused series_number (from series_number_hint, see
    _fuse_and_score_candidates) is preferred; title-text inference is only
    a fallback for a candidate whose contributing providers never supplied
    a structured number at all -- the same trust ordering
    discover_candidates_for_series/series_agent already use elsewhere.
    """
    if candidate.series_number is not None:
        return candidate.series_number
    inferred = infer_number_from_title(candidate.title, series_name)
    return float(inferred) if inferred is not None else None


def _metadata_completeness_score(completeness_values: dict) -> float:
    """NS-7: shared present-field-count scorer for _METADATA_COMPLETENESS_
    FIELDS, extracted out from what used to be near-identical inline blocks
    in both _fuse_and_score_candidates and _reconciled_completeness_score
    (below) -- keeping a plain-fusion candidate's score and a reconciled
    candidate's score computed by literally the same code, not just the
    same field list, so the two stay comparable by construction rather
    than by two implementations happening to agree.
    """
    present_count = sum(
        1
        for field in _METADATA_COMPLETENESS_FIELDS
        for value in (completeness_values.get(field),)
        if (value if not isinstance(value, list) else bool(value)) not in (None, "", False)
    )
    return round(present_count / len(_METADATA_COMPLETENESS_FIELDS), 4)


def _reconciled_completeness_score(
    title: str, authors: list[str], series_name: str | None, series_number: float | None, isbn13: str | None,
    published_date: str | None, description,
) -> float:
    return _metadata_completeness_score(
        {
            "title": title,
            "authors": authors,
            "series_name_hint": series_name,
            "series_number_hint": series_number,
            "isbn13": isbn13,
            "published_date": published_date,
            "description": description,
        }
    )




_EDITION_TITLE_MARKERS: tuple[tuple[str, str], ...] = (
    (r"\b(?:audible(?:\s+audio)?|audio\s*cd|audiobook)\b", "audio"),
    (r"\bkindle(?:\s+edition)?\b", "ebook"),
    (r"\bmass\s+market\s+paperback\b", "paperback"),
    (r"\bpaperback\b", "paperback"),
    (r"\bhardcover\b", "hardcover"),
)


def _infer_edition_type_from_title(title: str | None) -> str:
    text = str(title or "").lower()
    for pattern, edition in _EDITION_TITLE_MARKERS:
        if re.search(pattern, text):
            return edition
    return "unknown"


def _resolved_edition_type(candidate: "UnifiedCandidate") -> str:
    if candidate.edition_type and candidate.edition_type not in ("unknown", "bundle"):
        return candidate.edition_type
    return _infer_edition_type_from_title(candidate.title)


def _same_underlying_book(a: "UnifiedCandidate", b: "UnifiedCandidate") -> bool:
    """True if a and b are plausibly two different editions of the exact
    same real book, rather than two different books.

    Plain identity-key fusion (_fuse_and_score_candidates) already grouped
    strictly by isbn13 -> title_key -> normalized title, so a and b (two
    already-distinct UnifiedCandidates) got here precisely *because*
    neither their ISBNs nor their exact title text matched. This is a
    looser second check using _normalize_title_for_identity (services/
    identity.py), which strips format/edition qualifiers -- "(Kindle
    Edition)", "(Audible Audio)", "SIGNED", etc. -- that a plain exact-title
    match doesn't, so "Iron Flame" and "Iron Flame (Audible Audio Edition)"
    normalize to the same identity even though fusion's own stricter key
    kept them apart.

    A mismatched series_number (when BOTH sides actually have one) blocks
    the match regardless of title -- two genuinely different volumes can
    share a generic normalized title, and a real number disagreement is a
    stronger signal than a title-text coincidence. When only one (or
    neither) side has a resolved number, that can't rule anything out, so
    the title match alone decides.
    """
    normalized_a = _normalize_title_for_identity(a.title)
    normalized_b = _normalize_title_for_identity(b.title)
    if not normalized_a or normalized_a != normalized_b:
        return False
    if a.series_number is not None and b.series_number is not None and a.series_number != b.series_number:
        return False
    return True


def _edition_strength(candidate: "UnifiedCandidate") -> tuple[int, float, float]:
    return (
        _edition_priority(_resolved_edition_type(candidate)),
        candidate.metadata_completeness_score,
        candidate.confidence_score,
    )


def _strictly_better_metadata(a: "UnifiedCandidate", b: "UnifiedCandidate") -> bool:
    """True if a's metadata is unambiguously better than b's: at least as
    good on every one of edition priority / metadata completeness /
    confidence, and strictly better on at least one. This is genuine Pareto
    dominance, not a simple tuple/lexicographic comparison -- lexicographic
    ordering would let a single-dimension edge (e.g. a slightly higher
    edition priority) declare a winner even while b is clearly better on
    every other measure, which is exactly the "different, not better"
    situation _finalize_candidates is supposed to leave alone. Only when a
    is at least tied everywhere and ahead somewhere does one edition count
    as "strictly better metadata" rather than merely a different edition.
    """
    a_values = _edition_strength(a)
    b_values = _edition_strength(b)
    return all(x >= y for x, y in zip(a_values, b_values)) and any(x > y for x, y in zip(a_values, b_values))


def _collapse_edition_group(ranked_group: list["UnifiedCandidate"]) -> "UnifiedCandidate":
    """Merges a group of same-underlying-book candidates (see
    _same_underlying_book) that _finalize_candidates has already confirmed
    has one unambiguous best edition (ranked_group[0], by _edition_strength)
    into a single UnifiedCandidate -- the winning edition's own fields take
    priority, backfilled with whichever field a losing edition has that the
    winner lacks, exactly the same "never lose information just because a
    row scored lower overall" philosophy series_check_engine.py's own
    _merge_loser_fields_into_keeper already applies on the DB-write side.
    """
    keeper, losers = ranked_group[0], ranked_group[1:]

    title = keeper.title
    authors = keeper.authors or next((loser.authors for loser in losers if loser.authors), [])
    series_name = keeper.series_name or next((loser.series_name for loser in losers if loser.series_name), None)
    series_number = (
        keeper.series_number
        if keeper.series_number is not None
        else next((loser.series_number for loser in losers if loser.series_number is not None), None)
    )
    isbn13 = keeper.isbn13 or next((loser.isbn13 for loser in losers if loser.isbn13), None)
    published_date = keeper.published_date or next((loser.published_date for loser in losers if loser.published_date), None)
    upcoming_hint = (
        keeper.upcoming_hint
        if keeper.upcoming_hint is not None
        else next((loser.upcoming_hint for loser in losers if loser.upcoming_hint is not None), None)
    )

    provenance: list[dict] = [dict(item) for item in keeper.source_provenance]
    for loser in losers:
        provenance.extend(dict(item) for item in loser.source_provenance)
    if not provenance:
        provenance = [{}]
    provenance[0] = {
        **provenance[0],
        "authors": authors,
        "isbn13": isbn13,
        "published_date": published_date,
        "series_name_hint": series_name,
        "series_number_hint": series_number,
    }

    unique_sources = list(dict.fromkeys(str(item.get("source") or "unknown") for item in provenance))
    confidence_score = round(
        min(max(member.confidence_score for member in ranked_group) + (0.1 if len(unique_sources) > 1 else 0.0), 1.0), 4
    )
    completeness = _reconciled_completeness_score(
        title, authors, series_name, series_number, isbn13, published_date, provenance[0].get("description")
    )

    return UnifiedCandidate(
        title=title,
        authors=list(authors or []),
        series_name=series_name,
        series_number=series_number,
        isbn13=isbn13,
        edition_type=_resolved_edition_type(keeper),
        published_date=published_date,
        source_provenance=provenance,
        confidence_score=confidence_score,
        metadata_completeness_score=max(completeness, max(member.metadata_completeness_score for member in ranked_group)),
        upcoming_hint=upcoming_hint,
    )


def _finalize_candidates(unified_candidates: list["UnifiedCandidate"]) -> list["UnifiedCandidate"]:
    """Final edition-aware pass over an already-fused (and, if
    _needs_llm_reconciliation triggered it, already-reconciled) candidate
    list, run immediately before the candidates are handed back to
    series_agent.py.

    Plain identity-key fusion (_fuse_and_score_candidates) groups strictly
    by isbn13 -> title_key -> normalized title, so two different editions of
    the exact same book -- a hardcover with its own ISBN and a completely
    different-ISBN audiobook, say "Iron Flame" and "Iron Flame (Audible
    Audio Edition)" -- survive fusion as two SEPARATE UnifiedCandidates,
    since neither their ISBNs nor their exact titles match. Left alone, the
    same real, already-discovered book could be reported as two different
    "new" candidates.

    Multiple editions are deliberately kept apart until this point --
    _fuse_and_score_candidates and _reconcile_candidates_with_llm both
    still see them as distinct, so their own scoring (confidence,
    completeness) is computed per-edition, not prematurely averaged
    together. Here, candidates are grouped a second, looser time by
    _same_underlying_book, and a group is only ever collapsed into one
    candidate when _edition_strength (edition priority, then metadata
    completeness, then confidence) gives a single, unambiguous best edition
    -- no tie at the top. If two editions in a group are genuinely
    ambiguous (e.g. one has a better edition type but the other has richer
    metadata), every edition in that group is kept as a separate candidate
    rather than guessing which one the user would actually want --
    "collapse only when one edition has strictly better metadata", not
    merely a different one.

    This is a separate, discovery-side concept from the DB-write-path
    edition collapse in services/series_check_engine.py (which decides
    keeper vs. loser against rows already *owned* in the library, during
    persistence) and does not change that logic at all -- by the time a
    candidate here reaches series_check_engine.py, it has already been
    through this pass, so that logic still only ever needs to know how to
    compare one incoming candidate against existing DB rows, exactly as
    before.
    """
    groups: list[list[UnifiedCandidate]] = []
    for candidate in unified_candidates:
        for group in groups:
            if any(_same_underlying_book(candidate, member) for member in group):
                group.append(candidate)
                break
        else:
            groups.append([candidate])

    finalized: list[UnifiedCandidate] = []
    for group in groups:
        if len(group) == 1:
            finalized.append(group[0])
            continue

        # A collapse requires one member to dominate every OTHER member of
        # the group (see _strictly_better_metadata) -- not just outrank the
        # group's own runner-up, since a mixed group of 3+ editions could
        # have a clear #1-vs-#2 gap while still disagreeing with a #3 on
        # some dimension.
        winner = next(
            (candidate for candidate in group if all(_strictly_better_metadata(candidate, other) for other in group if other is not candidate)),
            None,
        )
        if winner is not None:
            ranked_group = [winner] + [candidate for candidate in group if candidate is not winner]
            finalized.append(_collapse_edition_group(ranked_group))
        else:
            # No single edition dominates every other -- genuinely
            # ambiguous which one is "better", so keep every edition in
            # the group separate rather than guessing.
            finalized.extend(group)

    return finalized


# Gates the author-bibliography fallback in discover_candidates_for_series --
# replaces the old binary "only if the targeted pass found absolutely
# nothing" trigger. An empty targeted pass still triggers it (0.0 scores on
# both signals below, well under either threshold) but so does a targeted
# pass that found *something*, just not enough of it or not confidently
# enough -- e.g. a series where only book 1 of a known 5 turned up, or
# every hit came from a single low-trust source with no ISBN. Deliberately
# less aggressive than _needs_llm_reconciliation's own thresholds (0.8/0.5):
# broadening the search author-wide is a bigger, costlier escalation than an
# LLM reconciliation pass over data already in hand, so it's reserved for
# cases that look more seriously incomplete/unreliable, not just messy.
FALLBACK_SERIES_COMPLETENESS_THRESHOLD = 0.5
FALLBACK_CONFIDENCE_THRESHOLD = 0.35


def _series_completeness_and_confidence(
    fused_candidates: list["UnifiedCandidate"],
    series_name: str | None,
    highest_owned_book_number: int | None,
) -> tuple[float, float]:
    """Cheap, discovery-side-only proxy for "did the targeted pass give us a
    complete, confident picture of this series" -- feeds
    _should_trigger_author_fallback. Deliberately simpler than
    _reconstruct_series_skeleton's own completeness math (that one also has
    the caller's full owned_books list to work with, and is used to decide
    which *specific* volumes to search for -- this only has
    highest_owned_book_number on hand, and only needs a rough signal to
    decide whether broadening the search author-wide is worth it at all).
    """
    if not fused_candidates:
        return 0.0, 0.0

    known_numbers: set[int] = set()
    if highest_owned_book_number:
        known_numbers.add(int(highest_owned_book_number))
    for candidate in fused_candidates:
        number = _resolve_candidate_number(candidate, series_name)
        if number is not None and float(number).is_integer():
            known_numbers.add(int(number))

    expected_total = max(known_numbers) if known_numbers else 0
    series_completeness = (len(known_numbers) / expected_total) if expected_total > 0 else 1.0
    avg_confidence = sum(candidate.confidence_score for candidate in fused_candidates) / len(fused_candidates)
    return series_completeness, avg_confidence


def _should_trigger_author_fallback(
    fused_candidates: list["UnifiedCandidate"],
    series_name: str | None,
    highest_owned_book_number: int | None,
) -> bool:
    series_completeness, avg_confidence = _series_completeness_and_confidence(
        fused_candidates, series_name, highest_owned_book_number
    )
    triggered = (
        series_completeness < FALLBACK_SERIES_COMPLETENESS_THRESHOLD or avg_confidence < FALLBACK_CONFIDENCE_THRESHOLD
    )
    if triggered:
        _log(
            f"Author-fallback triggered: series completeness {series_completeness:.0%}, "
            f"avg confidence {avg_confidence:.0%}"
        )
    return triggered


# Gates the web-search(+Apify) pass in provider_io._fetch_all_providers_
# parallel: skip it entirely when the free catalog APIs (Google Books/
# OpenLibrary/Hardcover) already produced a complete, high-confidence
# picture of the series on their own. Live incident that motivated this
# (Percy Jackson and the Olympians -- 5 well-catalogued mainline books plus
# 2 recent, well-catalogued sequels and one companion short story, all
# already present across Google Books/OpenLibrary/Hardcover): every single
# Check Now fired ~12-15 Serper web-search calls and 1-2 full Apify
# Amazon-scrapes regardless, because nothing ever checked whether the
# catalog-only picture was already good enough to skip them.
#
# Deliberately much stricter than FALLBACK_SERIES_COMPLETENESS_THRESHOLD/
# FALLBACK_CONFIDENCE_THRESHOLD above: those decide whether it's worth
# *broadening* a search that already ran; this decides whether it's safe
# to *skip* a search (web/Apify) entirely, which must not happen on
# anything less than near-total confidence -- a genuinely new/upcoming
# volume the catalog APIs haven't indexed yet is exactly the case
# web-search exists to catch, and this gate must never be the reason it's
# missed.
CATALOG_SUFFICIENCY_COMPLETENESS_THRESHOLD = 1.0
CATALOG_SUFFICIENCY_CONFIDENCE_THRESHOLD = 0.75
# "The catalogs agree" requires more than one catalog to have actually
# spoken -- a single source can't corroborate itself. Hardcover in
# particular is frequently unconfigured (no API key) in a given
# environment; when it is, it simply doesn't count toward or against this
# minimum, it isn't treated as a disagreeing source.
CATALOG_SUFFICIENCY_MIN_CONTRIBUTING_PROVIDERS = 2


def _catalog_sufficiency_confidence(
    fused_catalog_candidates: list["UnifiedCandidate"],
    series_name: str | None,
) -> float:
    """Gate-only confidence signal -- deliberately NOT a change to
    _series_completeness_and_confidence, which _should_trigger_author_
    fallback also depends on; this is a separate calculation used only by
    catalog_providers_are_sufficient below.

    PB-10 fix: a flat average of confidence_score across every raw fused
    candidate is dominated by non-canonical noise (box sets, coloring
    books, companion guides) and by a real series' own bundle/box-set
    listings that all resolve to the same book number as the real
    standalone volume (e.g. Hardcover alone returning nine different
    "book 1"-numbered titles for Percy Jackson -- the boxed set, three
    different omnibus editions, the plain novel, etc.) -- none of that
    should drag down how confidently book 1 itself is known. Per real
    book NUMBER, only the single best-corroborated candidate matters,
    not how many lower-quality duplicates/bundles also happen to carry
    that same number; averaging across those deduplicated number slots
    (not across every raw candidate) is what should decide whether the
    catalogs "agree" on the series.

    Only integer-numbered slots count, mirroring _series_completeness_and_
    confidence's own known_numbers convention -- a fractional companion
    entry (2.5) isn't a numbered "volume" this proxy reasons about, it's
    still fully visible to the user elsewhere, just not part of this
    scoring pass. Non-canonical candidates with no resolvable number at
    all are excluded entirely (they don't even get a slot).
    """
    best_confidence_by_number: dict[int, float] = {}
    for candidate in fused_catalog_candidates:
        number = _resolve_candidate_number(candidate, series_name)
        if number is None or not float(number).is_integer():
            continue
        slot = int(number)
        best_confidence_by_number[slot] = max(
            best_confidence_by_number.get(slot, 0.0), candidate.confidence_score
        )
    if not best_confidence_by_number:
        return 0.0
    return sum(best_confidence_by_number.values()) / len(best_confidence_by_number)


def catalog_providers_are_sufficient(
    fused_catalog_candidates: list["UnifiedCandidate"],
    series_name: str | None,
    highest_owned_book_number: int | None,
    *,
    contributing_provider_count: int,
    pass_label: str = "targeted",
) -> bool:
    """True only when Google Books/OpenLibrary/Hardcover, fused on their
    own (no web-search/Apify candidates mixed in -- see the `fused_catalog_
    candidates` caller contract), already look complete and confidently
    corroborated enough that a web-search+Apify pass would be spending
    money to reconfirm something already known, not to find something
    genuinely missing.

    Reuses _series_completeness_and_confidence's existing proxy signal --
    unchanged, not a new metric -- just applied to catalog-only fusion
    output and gated on much stricter thresholds than that function's other
    caller (_should_trigger_author_fallback) uses, per this function's own
    docstring on why "skip a search" and "broaden a search" need very
    different confidence bars. `confidence_score` per candidate already
    reflects cross-provider corroboration (_fuse_and_score_candidates sums
    a weight per distinct provider that agrees on the same candidate, plus
    bonuses for ISBN/author agreement and penalties for series-name
    disagreement) -- so "avg confidence is high" here already substantively
    means "the catalog providers agree with each other", not just "each
    provider individually seems confident".

    PB-10 diagnostic pass: every candidate this evaluates, plus the
    completeness/confidence/provider-count numbers actually compared
    against the thresholds above, is logged unconditionally (pass or
    fail) -- the original version of this gate only logged on the
    "sufficient" branch, which made a live "why didn't this skip web
    search" investigation (the Percy Jackson re-run) impossible to answer
    from logs alone.

    PB-10 confidence fix: completeness still comes from the shared
    _series_completeness_and_confidence (unchanged -- _should_trigger_
    author_fallback also depends on it), but confidence here comes from
    _catalog_sufficiency_confidence instead of that same shared helper's
    own flat-average confidence -- see its docstring for why a flat
    average across every raw fused candidate structurally never clears a
    realistic bar for a real series (bundle/box-set noise all sharing one
    book's number, no per-candidate reward for a *different* candidate at
    the same number already being well-corroborated).
    """
    completeness, _unused_flat_confidence = _series_completeness_and_confidence(
        fused_catalog_candidates, series_name, highest_owned_book_number
    )
    confidence = _catalog_sufficiency_confidence(fused_catalog_candidates, series_name)
    sufficient = (
        contributing_provider_count >= CATALOG_SUFFICIENCY_MIN_CONTRIBUTING_PROVIDERS
        and completeness >= CATALOG_SUFFICIENCY_COMPLETENESS_THRESHOLD
        and confidence >= CATALOG_SUFFICIENCY_CONFIDENCE_THRESHOLD
    )
    _log(
        f"catalog_providers_are_sufficient [{pass_label}]: series={series_name!r} "
        f"highest_owned_book_number={highest_owned_book_number} "
        f"fused_candidate_count={len(fused_catalog_candidates)} "
        f"contributing_providers={contributing_provider_count} "
        f"(min_required={CATALOG_SUFFICIENCY_MIN_CONTRIBUTING_PROVIDERS}) "
        f"completeness={completeness:.0%} (threshold={CATALOG_SUFFICIENCY_COMPLETENESS_THRESHOLD:.0%}) "
        f"confidence={confidence:.0%} (threshold={CATALOG_SUFFICIENCY_CONFIDENCE_THRESHOLD:.0%}) "
        f"-> sufficient={sufficient}"
    )
    for candidate in fused_catalog_candidates:
        resolved_number = _resolve_candidate_number(candidate, series_name)
        _log(
            f"  [{pass_label}] fused catalog candidate: title={candidate.title!r} "
            f"authors={candidate.authors!r} number={resolved_number!r} "
            f"confidence_score={candidate.confidence_score:.2f} "
            f"sources={[m.get('source') for m in candidate.source_provenance]}"
        )
    return sufficient


def _is_cross_series_contamination(raw: dict, target_series_name: str | None) -> bool:
    """True only when a fallback candidate is EXPLICITLY tagged -- by its
    own series_name_hint, whether from Hardcover's structured field, the
    web-search LLM pass, or _filter_and_merge's own title-text fallback --
    as belonging to a DIFFERENT series than the one actually being checked.
    A candidate with no series_name_hint at all is never excluded here:
    "unless EXPLICIT cross-series contamination is detected" means an
    absence of information is not itself evidence of contamination.
    Compatibility is judged via _series_names_compatible rather than exact
    text equality, so a real, differently-branded hint for the SAME series
    (e.g. Hardcover's bare "Jonathan Hunt" against a target tracked as
    "Jonathan Hunt Thriller Series") isn't misread as contamination -- but
    a real, distinct sub-series/rebrand with only superficial overlap still
    is (see _series_names_compatible's own docstring for the token-overlap
    guard that keeps this narrow).

    DC-10: this used to also take an `other_known_series_names` parameter,
    but any explicit, incompatible series_name_hint has always been treated
    as contamination unconditionally (regression: an author tracked under
    only ONE series -- e.g. George Wagner's "Jonathan Hunt Thriller
    Series" -- had contamination detection effectively disabled entirely
    under the older, narrower rule, since there were no "other tracked
    series" to compare against, even though the hint on a contaminating
    candidate was plainly a different series) -- so the parameter never
    actually gated or narrowed this check and has been removed, along with
    every call site that threaded it through.
    """
    hint = str(raw.get("series_name_hint") or "").strip()
    if not hint:
        return False
    if _series_names_compatible(hint, target_series_name):
        return False
    return True


def _filter_cross_series_contamination(
    fetch_results: dict,
    target_series_name: str | None,
    *,
    diagnostics: list[dict] | None = None,
) -> dict:
    filtered = dict(fetch_results)
    for provider in ("google", "openlibrary", "hardcover", "web"):
        kept: list[dict] = []
        for raw in fetch_results.get(provider) or []:
            if _is_cross_series_contamination(raw, target_series_name):
                _record_drop_diagnostic(
                    "cross_series_filter",
                    {
                        "title": raw.get("title"),
                        "isbn13": raw.get("isbn13"),
                        "series_number": _to_int_or_none(raw.get("series_number_hint")),
                    },
                    "series_name_mismatch",
                    diagnostics,
                )
                continue
            kept.append(raw)
        filtered[provider] = kept
    return filtered


# NS-1: this is one of two parallel provider-name vocabularies in this
# pipeline, and they're deliberately NOT the same strings. The internal
# fetch_results dict (_fetch_all_providers_parallel's return value, keyed
# "google"/"openlibrary"/"hardcover"/"web") names providers by which raw
# _fetch_* function produced them; the candidate-level "source" field
# below (and on every UnifiedCandidate/raw dict from that point on) names
# providers by their public catalog identity instead -- "google_books" not
# "google", "web_search" not "web", plus "apify" (which has no key of its
# own in fetch_results at all: Apify candidates are prepended into the
# "web" list upstream, in provider_io.py's web-search sub-flow, before
# they ever reach fusion). Once a raw dict has a "source" field, nothing
# downstream of fusion should ever see the fetch_results spelling again.
#
# Explicit provider trust ranking, as an ordinal rather than a float weight
# -- mirrors _PROVIDER_CONFIDENCE_WEIGHT's own hardcover > google_books >
# openlibrary > web_search ordering, but only used here to break a sort
# tie between two candidates that share the exact same resolved series
# number and title (which _filter_and_merge's own dedupe should mostly
# already prevent, but isn't guaranteed to for every code path feeding
# finalize_discovery_output -- e.g. a targeted-pass hit and a fallback-pass
# hit that plain title-key dedupe didn't recognize as the same book). Any
# other/unrecognized source sorts last.
_PROVIDER_SORT_RANK = {"hardcover": 0, "google_books": 1, "openlibrary": 2, "apify": 3, "web_search": 4}

_TRANSIENT_CANDIDATE_FIELDS = ("confidence_score", "metadata_completeness_score", "source_provenance")


def _candidate_sort_key(candidate: dict) -> tuple:
    title = str(candidate.get("title") or "")
    number = candidate.get("series_number_hint")
    if number is None:
        number = infer_number_from_title(title, candidate.get("series_name_hint"))
    try:
        numeric_number = float(number) if number is not None else None
    except (TypeError, ValueError):
        numeric_number = None

    # Numbered candidates sort ahead of unnumbered ones (tier 0 vs. 1) and
    # ascending by number within that tier; unnumbered candidates fall back
    # to title alone within their own tier.
    number_tier = (0, numeric_number) if numeric_number is not None else (1, 0.0)
    provider_rank = _PROVIDER_SORT_RANK.get(str(candidate.get("source") or ""), len(_PROVIDER_SORT_RANK))
    return (number_tier, normalize_text(title), provider_rank)


def finalize_discovery_output(candidates: list[dict]) -> list[dict]:
    """Last step before a candidate list -- whether from
    discover_candidates_for_series/discover_candidates_for_author directly,
    or from series_agent.py's own missing-volume-enriched re-merge (see
    _reconstruct_series_skeleton) -- is handed off for belongs_to_series
    filtering, so the shape series_agent.py (and anything downstream of
    it -- API responses, logs, tests) sees is always the same regardless of
    run-to-run timing.

    Sorts by series number (when resolvable, else title-only), then title,
    then provider priority as a final tie-breaker. This matters because
    _fetch_all_providers_parallel runs Google/OpenLibrary/Hardcover/web
    search concurrently (see its own docstring) -- any of the four can
    finish first depending on network timing, so without an explicit sort
    here the exact same underlying set of discovered books could come back
    in a different order between two otherwise-identical runs, which would
    make output diffing/testing unreliable and could subtly change which
    duplicate "wins" in any downstream logic that processes candidates in
    list order.

    Also strips the transient, fusion-internal fields
    _unified_candidate_to_raw_dict rode along on every raw dict --
    confidence_score, metadata_completeness_score, source_provenance --
    which are useful *during* the fuse/reconcile/finalize pipeline itself
    but were never meant to leak into series_agent.py's own candidate shape
    or anything built on top of it.
    """
    sorted_candidates = sorted(candidates, key=_candidate_sort_key)
    return [
        {key: value for key, value in candidate.items() if key not in _TRANSIENT_CANDIDATE_FIELDS}
        for candidate in sorted_candidates
    ]


