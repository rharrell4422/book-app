from __future__ import annotations

import json
import logging
import re
from datetime import date

from sqlalchemy.orm import Session

import confidence_engine
import delta_engine
import discovery_engine
import intelligence
from models import Book, Series, SeriesSkeleton
from services.discovery_cache import DiscoveryCache
from services.discovery_logging import log_discovery_summary
from services.discovery_telemetry import DiscoveryTelemetry
from services.identity import owned_title_for_identity
from services.skeleton_store import backfill_skeleton_for_series


logger = logging.getLogger(__name__)


def _console_log(message: str) -> None:
    print(f"[series_agent] {message}", flush=True)


def _normalize_author(value: str | None) -> str:
    return str(value or "").strip().lower()


def _authors_match_exact(series_author: str | None, candidate_author: str | None) -> bool:
    series_norm = _normalize_author(series_author)
    candidate_norm = _normalize_author(candidate_author)
    if not series_norm or not candidate_norm:
        return False
    return series_norm == candidate_norm


def _normalize_identity_text(value: str | None) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _normalize_title_text(value: str | None) -> str:
    cleaned = _normalize_identity_text(value)
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _title_stem(value: str | None) -> str:
    title = _normalize_title_text(value)
    title = re.sub(r"\b(book|volume|vol|series)\s*\d+\b", " ", title, flags=re.IGNORECASE)
    title = re.sub(r"\b#\s*\d+\b", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def _token_set(value: str | None) -> set[str]:
    return {token for token in _normalize_title_text(value).split() if token}


def _token_overlap_ratio(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, min(len(left), len(right)))


def _normalize_identity_number(value) -> str:
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    if number.is_integer():
        return str(int(number))
    return str(number).strip()


_UNIVERSE_TIE_IN_PATTERN = re.compile(r"\buniverse\s+(novel|novella|book|story)\b", re.IGNORECASE)


def _looks_like_universe_tie_in(title: str) -> bool:
    """Common self-pub/indie branding convention: a companion/spin-off novel
    set in the same shared "universe" as a flagship series says so right in
    its own subtitle (e.g. "Mage-Provocateur: A Starship's Mage Universe
    Novel") -- but it belongs to its own separate series (here, "Starship's
    Mage: Red Falcon"), not the flagship one being checked, even though the
    flagship series' name is textually present as a substring/token overlap
    (regression: checking "Starship's Mage" pulled in all 3 Red Falcon
    books this way). A plain textual match against the flagship series name
    is too weak a signal for a title phrased like this; it should only be
    accepted if it also has a genuine series-position number tying it to
    *this* series specifically.
    """
    return bool(_UNIVERSE_TIE_IN_PATTERN.search(str(title or "")))


def _title_pattern_match(title: str, series_name: str, known_series_titles: set[str]) -> bool:
    title_norm = _normalize_title_text(title)
    series_norm = _normalize_title_text(series_name)
    if not title_norm:
        return False

    if series_norm and series_norm in title_norm:
        return True

    title_tokens = _token_set(title_norm)
    if series_norm:
        series_tokens = _token_set(series_norm)
        if _token_overlap_ratio(title_tokens, series_tokens) >= 0.75 and len(title_tokens & series_tokens) >= 2:
            return True

    candidate_stem = _title_stem(title_norm)
    for known_title in known_series_titles:
        known_stem = _title_stem(known_title)
        if not known_stem:
            continue
        if known_stem == candidate_stem:
            return True
        if _token_overlap_ratio(_token_set(candidate_stem), _token_set(known_stem)) >= 0.75:
            return True
    return False


def _title_references_series(title: str, series_name: str) -> bool:
    """Whether a title textually identifies the series it belongs to --
    used to decide whether we need to append a "(Series Name Book N)"
    suffix ourselves. Some sources (notably Hardcover, which tracks series
    position as structured data rather than embedding it in the title
    string) return clean, bare titles like "Unmapped" with no series name
    or book number anywhere in the text, which makes an added book hard to
    recognize as part of the series it was found for.
    """
    title_norm = _normalize_title_text(title)
    series_norm = _normalize_title_text(series_name)
    if not title_norm or not series_norm:
        return False
    if series_norm in title_norm:
        return True
    title_tokens = _token_set(title_norm)
    series_tokens = _token_set(series_norm)
    return _token_overlap_ratio(title_tokens, series_tokens) >= 0.75 and len(title_tokens & series_tokens) >= 2


def _partial_series_match(title: str, series_name: str) -> bool:
    title_tokens = _token_set(title)
    target_tokens = _token_set(series_name)
    if not target_tokens:
        return False
    return _token_overlap_ratio(title_tokens, target_tokens) >= 0.5


def _build_known_title_number_keys(books: list[Book]) -> set[str]:
    keys: set[str] = set()
    for book in books:
        title_key = discovery_engine.core_title_key(owned_title_for_identity(book))
        number = _normalize_identity_number(book.book_number)
        if title_key and number:
            keys.add(f"{title_key}|{number}")
    return keys


def _build_series_identity_sets(books: list[Book]) -> tuple[set[str], set[str], set[str]]:
    known_series_titles: set[str] = set()
    known_series_numbers: set[str] = set()
    bare_title_counts: dict[str, int] = {}
    for book in books:
        title_key = discovery_engine.core_title_key(owned_title_for_identity(book))
        number = _normalize_identity_number(book.book_number)
        if title_key:
            known_series_titles.add(title_key)
        if number:
            known_series_numbers.add(number)

        # An owned omnibus/boxed-set edition (e.g. "Safehold Boxed Set 1:
        # (Safehold Books 1-3)") only carries a single book_number on its
        # own row, but it really covers every number in that range -- treat
        # each of those as already-known too, so a newly-discovered
        # single-volume reprint of book 2 or 3 (a real book, just not a new
        # one) is recognized as a duplicate instead of "new available".
        for covered in intelligence.extract_omnibus_ranges(book.title):
            known_series_numbers.add(_normalize_identity_number(covered))
        for covered in intelligence.extract_omnibus_ranges(getattr(book, "subtitle", None)):
            known_series_numbers.add(_normalize_identity_number(covered))

        bare_key = discovery_engine.bare_title_key(owned_title_for_identity(book))
        if bare_key:
            bare_title_counts[bare_key] = bare_title_counts.get(bare_key, 0) + 1

    # Only trust a bare (number-less) title as an identity signal when it's
    # unique across the owned catalog -- otherwise a one-word title shared
    # by two different numbered volumes could get conflated.
    known_bare_titles = {key for key, count in bare_title_counts.items() if count == 1}
    return known_series_titles, known_series_numbers, known_bare_titles


def _build_owned_core_title_texts(books: list[Book]) -> set[str]:
    """Normalized, number-stripped core title text for each owned book --
    used to catch a compilation/anthology listing that spells out several
    already-owned book titles by name instead of using a "Books 1-3" /
    "Boxed Set" / "Omnibus" style label (e.g. "The Safehold Series, Volume
    I: Off Armageddon Reef, By Schism Rent Asunder, By Heresies Distressed,
    A Mighty Fortress, How Firm a Foundation" -- five owned titles strung
    together with no parseable number and no bundle keyword at all).
    Filtered to a minimum length so short/generic titles can't cause false
    matches against unrelated candidates.
    """
    texts: set[str] = set()
    for book in books:
        core = discovery_engine.normalize_text(discovery_engine._title_core_segment(str(book.title or "")))
        if core and len(core) >= 8:
            texts.add(core)
    return texts


def _count_referenced_owned_titles(candidate_title: str, owned_core_title_texts: set[str]) -> int:
    candidate_norm = discovery_engine.normalize_text(candidate_title)
    if not candidate_norm:
        return 0
    return sum(1 for core in owned_core_title_texts if core and core in candidate_norm)


def _is_known_candidate(
    *,
    isbn13: str,
    title_key: str,
    bare_title_key: str,
    normalized_number: str,
    known_series_isbns: set[str],
    known_series_titles: set[str],
    known_series_numbers: set[str],
    known_title_number_keys: set[str],
    known_bare_titles: set[str],
) -> bool:
    if isbn13 and isbn13 in known_series_isbns:
        return True
    if title_key and normalized_number and f"{title_key}|{normalized_number}" in known_title_number_keys:
        return True
    if title_key and title_key in known_series_titles:
        return True
    # A book number this series already owns is treated as known even
    # without a title match -- owned titles vary a lot in formatting, but
    # the position within the series is a reliable, stable identity signal.
    if normalized_number and normalized_number in known_series_numbers:
        return True
    # Fallback for candidates with no parseable number at all (e.g. a bare
    # search-result title like "Crown" with no "(Series Book 9)" suffix):
    # core_title_key can't match it against the number-bearing owned key,
    # so fall back to the number-less title alone when it uniquely
    # identifies one owned book.
    if not normalized_number and bare_title_key and bare_title_key in known_bare_titles:
        return True
    return False


def _empty_result(series_id: int | None, series_name: str | None, reason: str) -> dict:
    return {
        "series_id": series_id,
        "series_name": series_name,
        "highest_owned_book_number": None,
        "candidate_numbers": [],
        "added_count": 0,
        "added_books": [],
        "found_books": [],
        "candidate_diagnostics": [],
        "complete": True,
        "status": "no_hits",
        "no_new_books": True,
        "reason": reason,
        "has_new_books": False,
        "series_state": None,
        "last_checked": None,
        "next_unread_book_number": None,
        "next_upcoming_book_number": None,
        "missing_books": [],
        "available_missing": [],
        "upcoming_books": [],
        "needs_review": [],
        "validated_candidates": [],
        "found": False,
        "candidate": None,
        "provider_failures": [],
        "all_providers_failed": False,
        "asin_discovery": {
            "discovered": 0,
            "processed": 0,
            "fetch_success": 0,
            "fetch_failed": 0,
            "metadata_hits": 0,
        },
        "provider_ledger": [],
        "discovery_engine": "none",
        "agent_pipeline": False,
    }


def _build_added_book_entry(canonical: dict, *, status: str) -> dict:
    is_upcoming = status == "upcoming"
    return {
        "title": canonical.get("title"),
        "author": canonical.get("author"),
        "series_name": canonical.get("series_name"),
        "book_number": canonical.get("series_number"),
        "source_url": canonical.get("url"),
        "provider": canonical.get("provider"),
        "publication_date": None if is_upcoming else canonical.get("date_iso"),
        "expected_date": canonical.get("date_iso") if is_upcoming else None,
        "status_hint": status,
        "asin_or_id": canonical.get("identifier"),
        "is_missing": not is_upcoming,
        "status": status,
        "canonical_metadata": {
            "title_normalized": canonical.get("title"),
            "series_name_normalized": canonical.get("series_name"),
            "book_number_normalized": canonical.get("series_number"),
            "publish_date_normalized": None if is_upcoming else canonical.get("date_iso"),
            "upcoming_date_normalized": canonical.get("date_iso") if is_upcoming else None,
            "availability": status,
            "edition_type": "unknown",
            "title_selector": None,
        },
    }


class SeriesIntelligenceAgent:
    def run_series_check(
        self,
        db: Session,
        series_id: int,
        progress_callback=None,
        emit_summary: bool = True,
        telemetry: "DiscoveryTelemetry | None" = None,
        cache: "DiscoveryCache | None" = None,
    ) -> dict:
        series = db.query(Series).filter(Series.id == series_id).first()
        if not series:
            result = _empty_result(None, None, "series-not-found")
            if emit_summary:
                log_discovery_summary(result=result, terminal_error="series-not-found")
            return result

        _console_log(f"CHECK NOW triggered for series: {series.name}")

        try:
            series_author = str(series.author or "").strip()

            active_series_books = [
                book
                for book in db.query(Book).filter(Book.series_id == series_id).all()
                if (book.record_status or "") != "deleted"
            ]
            highest_owned_book_number = max(
                (
                    int(float(book.book_number))
                    for book in active_series_books
                    if book.book_number is not None and not bool(book.is_missing)
                ),
                default=None,
            )

            if not series_author:
                result = _empty_result(series.id, series.name, "series-missing-author")
                result["highest_owned_book_number"] = highest_owned_book_number
                if emit_summary:
                    log_discovery_summary(result=result, terminal_error="series-missing-author")
                return result

            known_series_titles, known_series_numbers, known_bare_titles = _build_series_identity_sets(active_series_books)
            known_title_number_keys = _build_known_title_number_keys(active_series_books)
            known_series_isbns = {
                str(book.isbn13 or "").strip() for book in active_series_books if str(book.isbn13 or "").strip()
            }
            owned_core_title_texts = _build_owned_core_title_texts(active_series_books)

            # Exclude titles the user already owns anywhere by this exact
            # author (any series), so a same-author's other tracked series
            # doesn't leak candidates into this one.
            other_series_by_author = [
                other
                for other in db.query(Series)
                .filter(Series.author.isnot(None), Series.profile_id == series.profile_id)
                .all()
                if other.id != series_id and _authors_match_exact(series_author, other.author)
            ]
            author_owned_titles = {
                discovery_engine.core_title_key(owned_title_for_identity(book))
                for book in active_series_books
                if book.title
            }
            for other in other_series_by_author:
                other_books = [
                    book
                    for book in db.query(Book).filter(Book.series_id == other.id).all()
                    if (book.record_status or "") != "deleted"
                ]
                author_owned_titles |= {
                    discovery_engine.core_title_key(owned_title_for_identity(book)) for book in other_books if book.title
                }

            # The broad author-bibliography fallback can trigger even when
            # this author has other tracked series -- rather than disabling
            # the whole pass just because other series exist,
            # discover_candidates_for_series itself drops any fallback hit
            # explicitly tagged as belonging to one of those other series
            # (see other_known_series_names / _is_cross_series_contamination
            # there) while still allowing it to surface anything new for
            # *this* series.
            other_known_series_names = {other.name for other in other_series_by_author if other.name}

            discovery = discovery_engine.discover_candidates_for_series(
                series.name,
                series_author,
                exclude_title_keys=author_owned_titles,
                other_known_series_names=other_known_series_names,
                progress_callback=progress_callback,
                highest_owned_book_number=highest_owned_book_number,
                telemetry=telemetry,
                cache=cache,
            )
            candidates = discovery["candidates"]
            provider_failures = discovery["provider_failures"]
            all_providers_failed = discovery["all_providers_failed"]

            # Phase 2 + 3 of agentic discovery: computes a deterministic
            # delta between the Phase 1 SeriesSkeleton baseline and this
            # run's PRE-_filter_and_merge candidates (discovery["unified_
            # candidates"] -- see delta_engine's own docstring for why
            # pre-filter, not the post-filter `candidates` above), then a
            # deterministic confidence score per candidate on top of that
            # delta, and logs both. As of the manual-override rollout,
            # `confidence_lookup` below is consulted by the belongs_to_series
            # loop to route each POST-filter candidate to auto-accept/
            # needs_review/auto-drop -- see `confidence_engine.correlation_key`
            # for why that lookup needs its own key function rather than
            # `confidence_engine._candidate_key`. Still cannot change
            # `candidates` or `provider_failures` themselves, or anything
            # about discovery/fetching -- only which of *this* function's own
            # three output buckets (available_missing/upcoming_books vs.
            # needs_review vs. dropped) an already-discovered candidate lands
            # in.
            series_confidence: dict = {"confidence": []}
            try:
                # Rebuilt here rather than just read, and rather than relying
                # on a boot-time backfill: nothing calls
                # skeleton_store.backfill_all_skeletons() at startup today,
                # so a SeriesSkeleton row would otherwise never exist and
                # title_confidence would grade every candidate "unverified"
                # forever, capping overall confidence at "medium" even for
                # books the library already owns adjacent numbers of (see
                # confidence_engine's own docstring on why "unverified" caps
                # rather than fails, and _title_confidence for the "high"
                # grade this starves without a populated skeleton). This is
                # currently a full rebuild from ground-truth Book rows every
                # run (see backfill_skeleton_for_series's own docstring) --
                # lossless today because nothing yet writes a non-library
                # entry into the skeleton, so there is nothing an overwrite
                # could lose. Not committed here: it shares the single
                # db.commit() at the end of this function alongside
                # series.has_new_books/last_checked.
                backfill_skeleton_for_series(db, series_id)
                skeleton_row = (
                    db.query(SeriesSkeleton).filter(SeriesSkeleton.series_id == series_id).first()
                )
                skeleton_entries = skeleton_row.skeleton_json if skeleton_row else []
                unified_candidate_dicts = [
                    candidate.model_dump() for candidate in discovery.get("unified_candidates", [])
                ]
                series_delta = delta_engine.compute_series_delta(
                    series_id,
                    skeleton_entries,
                    unified_candidate_dicts,
                    series_name=series.name,
                )
                logger.info("series_delta: %s", json.dumps(series_delta, default=str))

                series_confidence = confidence_engine.compute_confidence(
                    series_id,
                    skeleton_entries,
                    unified_candidate_dicts,
                    series_delta,
                    series_name=series.name,
                    series_author=series_author,
                )
                logger.info("series_confidence: %s", json.dumps(series_confidence, default=str))
            except Exception:
                # If this fails, `series_confidence` stays the empty default
                # set above -- confidence_lookup below will then be empty,
                # and the routing logic degrades to "trust belongs_to_series
                # exactly like before this feature existed" (see the
                # comment at the routing site). This computation must never
                # be able to fail a real Check Now run.
                logger.exception("series_delta/series_confidence computation failed for series_id=%s", series_id)

            # Keyed by confidence_engine.correlation_key so the loop below
            # can look up a POST-filter `raw` candidate's grade even though
            # this dict is built from PRE-filter candidates (see that
            # function's docstring for exactly why the two need a shared,
            # field-name-tolerant key rather than object identity or
            # confidence_engine._candidate_key).
            confidence_lookup: dict[tuple, dict] = {
                confidence_engine.correlation_key(entry["candidate"]): entry
                for entry in series_confidence.get("confidence", [])
            }

            # Missing-volume detection: a series can own/find books 1-4 and
            # 6-9 but nothing for 5 -- that's not book 5 ranking low in the
            # targeted search, it's that no provider returned it at all, so
            # no amount of relevance-ranking tuning above recovers it. Fires
            # a few extra, deliberately narrow "<series> <author> book <N>"
            # lookahead queries (bounded by
            # discovery_engine.MAX_MISSING_VOLUME_LOOKAHEAD_QUERIES) for
            # whichever numbers between 1 and the highest known number have
            # neither an owned book nor a discovered candidate, and folds any
            # hits back through the same fuse-then-filter pipeline the
            # targeted/fallback passes already went through -- belongs_to_series
            # below still runs against every recovered candidate exactly like
            # any other, nothing here bypasses it.
            owned_books_for_skeleton = [
                {"title": book.title, "book_number": book.book_number, "isbn13": book.isbn13}
                for book in active_series_books
            ]
            skeleton = discovery_engine._reconstruct_series_skeleton(
                discovery.get("unified_candidates", []),
                owned_books_for_skeleton,
                series_name=series.name,
                author=series_author,
                telemetry=telemetry,
                cache=cache,
            )
            if skeleton["recovered_numbers"]:
                # _filter_and_merge stamps every candidate it's given with
                # ONE blanket confidence value -- fine for the original
                # fetch passes (every raw hit there is genuinely new), but
                # this re-merge feeds it skeleton["candidates"], which is
                # discovery["unified_candidates"] (i.e. every candidate
                # already found by the targeted/fallback passes above) PLUS
                # whatever the missing-volume lookahead recovered. Passing
                # a single "author_fallback"/"targeted" confidence here
                # would downgrade candidates that already had a real,
                # stronger-signal confidence from the original passes down
                # to "author_fallback" purely because *some* recovery ran --
                # which then silently fails belongs_to_series'
                # targeted_with_number check below for a series whose book
                # titles don't textually reference the series name at all
                # (regression: Georgia Wagner's "Jonathan Hunt Thriller
                # Series" -- author-fallback always triggers because
                # providers under-index it, so real sequels like "Desert
                # Protocol" and "The Levee Ghosts" lost their "targeted"
                # confidence here, had no other way to clear the gate, and
                # "Check Now" reported zero new books despite discovery
                # correctly finding them). Snapshotting each already-found
                # candidate's real confidence by identity before the
                # re-merge, then restoring it after, keeps that signal
                # intact for pre-existing candidates while still letting
                # newly-recovered ones get the blanket default below.
                original_confidence_by_key: dict[str, str] = {}
                for original in candidates:
                    original_confidence = original.get("confidence")
                    if not original_confidence:
                        continue
                    original_isbn = str(original.get("isbn13") or "").strip()
                    if original_isbn:
                        original_confidence_by_key[f"isbn:{original_isbn}"] = original_confidence
                    original_title_key = discovery_engine.core_title_key(str(original.get("title") or ""))
                    if original_title_key:
                        original_confidence_by_key[f"title:{original_title_key}"] = original_confidence

                candidates = discovery_engine._filter_and_merge(
                    [discovery_engine._unified_candidate_to_raw_dict(candidate) for candidate in skeleton["candidates"]],
                    series_author,
                    author_owned_titles,
                    confidence="author_fallback" if discovery["used_author_fallback"] else "targeted",
                    series_name=series.name,
                )
                recovered_numbers_set = set(skeleton["recovered_numbers"])
                for candidate in candidates:
                    candidate_isbn = str(candidate.get("isbn13") or "").strip()
                    candidate_title_key = discovery_engine.core_title_key(str(candidate.get("title") or ""))
                    restored_confidence = (
                        (original_confidence_by_key.get(f"isbn:{candidate_isbn}") if candidate_isbn else None)
                        or (original_confidence_by_key.get(f"title:{candidate_title_key}") if candidate_title_key else None)
                    )
                    if restored_confidence:
                        candidate["confidence"] = restored_confidence
                        continue
                    # A candidate with no prior confidence to restore is one
                    # the missing-volume lookahead itself surfaced this run
                    # (the ONLY other way to land in skeleton["candidates"]).
                    # That lookahead already queried for this exact,
                    # specific number ("<series> <author> book <N>") -- a
                    # narrower, more targeted query than even the regular
                    # targeted pass's plain "<series> <author>" -- so it
                    # deserves at least the same trust, not the broader
                    # same-author "author_fallback" sweep's weaker one.
                    # Tagged distinctly (not "targeted" outright) so it's
                    # still visible in diagnostics/logs which candidates
                    # came from which pass.
                    candidate_number = candidate.get("series_number_hint") or discovery_engine.infer_number_from_title(
                        str(candidate.get("title") or ""), series.name
                    )
                    resolved_candidate_number = _normalize_identity_number(candidate_number) if candidate_number else ""
                    if resolved_candidate_number and int(float(resolved_candidate_number)) in recovered_numbers_set:
                        candidate["confidence"] = "missing_volume_recovery"
                # discover_candidates_for_series's own "candidates" already
                # came back deterministically sorted/stripped (see
                # finalize_discovery_output), but this re-merge builds a
                # fresh list straight from _filter_and_merge, which doesn't
                # do either -- re-apply the same step so a recovered missing
                # volume doesn't leave the final list's ordering dependent on
                # dict/set iteration order, or carrying fusion-internal
                # fields belongs_to_series below was never meant to see.
                candidates = discovery_engine.finalize_discovery_output(candidates)

            # A candidate with no published_date at all defaults to
            # "unconfirmed"/upcoming below (see classify_upcoming) even when
            # it's a real, already-released book -- the web-search
            # provider's snippets frequently just don't state a date,
            # especially for under-indexed indie/KU titles (regression:
            # every Jonathan Hunt sequel this run found came back this way,
            # and all but one showed up as "Upcoming" despite being already
            # published).
            # Filling in a real date via a dedicated Hardcover lookup, when
            # one exists, lets a genuinely-released book land in
            # available_missing instead of upcoming_books -- see
            # backfill_missing_publication_dates's own docstring for why a
            # bare title lookup alone isn't trusted, and
            # MAX_PUBLICATION_DATE_BACKFILL_LOOKUPS for why this can't runaway
            # into unbounded extra API calls on top of everything else this
            # run already did.
            try:
                discovery_engine.backfill_missing_publication_dates(candidates, series_author)
            except Exception:
                logger.exception("publication-date backfill failed for series_id=%s", series_id)

            _console_log(
                f"Candidates found: {len(candidates)} (author_fallback_used={discovery['used_author_fallback']}, "
                f"missing_volumes={skeleton['missing_numbers']}, recovered={skeleton['recovered_numbers']})"
            )

            # Phase 3.5 of agentic discovery, PURE SHADOW MODE: reads
            # Hardcover's series_total_hint (already backfilled into each
            # fused candidate's own source_provenance[0] during fusion -- see
            # discovery_engine._fuse_and_score_candidates -- rather than a
            # top-level UnifiedCandidate field) to estimate how many books the
            # series actually has, and which of 1..external_expected_total are
            # neither owned nor discovered. Deliberately computed AFTER, and
            # entirely separate from, the live _reconstruct_series_skeleton
            # call above -- expected_total/missing_numbers there (and
            # therefore the live lookahead queries and `candidates` itself)
            # are completely untouched by anything below. known_numbers uses
            # skeleton["candidates"] (post-recovery) rather than
            # discovery["unified_candidates"] so a number the *existing*
            # narrow lookahead already recovered this same run isn't
            # miscounted as still missing.
            external_expected_total: int | None = None
            external_missing_numbers: list[int] = []
            # Phase 4 reads this to tell "no provider ever gave us a series
            # total" apart from "the total says the series is complete" --
            # both otherwise show up as an empty external_missing_vs_owned.
            # Declared here, alongside the two fields it's derived from,
            # rather than with the rest of the Phase 4 initializers below:
            # those run *after* this block, so re-zeroing it there would
            # throw away the value assigned inside this try.
            external_total_hint_count = 0
            try:
                external_total_hints = [
                    (candidate.model_dump().get("source_provenance") or [{}])[0].get("series_total_hint")
                    for candidate in discovery.get("unified_candidates", [])
                ]
                resolved_total_hints = [
                    discovery_engine._to_int_or_none(hint) for hint in external_total_hints if hint is not None
                ]
                external_total_hint_count = len(resolved_total_hints)
                external_expected_total = max(resolved_total_hints) if resolved_total_hints else None

                known_numbers: set[int] = set()
                for book in active_series_books:
                    number = discovery_engine._to_int_or_none(book.book_number)
                    if number is not None:
                        known_numbers.add(number)
                for candidate in skeleton["candidates"]:
                    number = discovery_engine._to_int_or_none(candidate.model_dump().get("series_number"))
                    if number is not None:
                        known_numbers.add(number)

                if external_expected_total is not None:
                    external_missing_numbers = sorted(set(range(1, external_expected_total + 1)) - known_numbers)
            except Exception:
                external_expected_total = None
                external_missing_numbers = []
                logger.exception(
                    "Phase 3.5 external-series-reality computation failed for series_id=%s", series_id
                )

            today = date.today()
            available_missing: list[dict] = []
            upcoming_books: list[dict] = []
            needs_review: list[dict] = []
            candidate_diagnostics: list[dict] = []
            # Phase 3.5 of agentic discovery, PURE SHADOW MODE: captures the
            # one silent-drop point that lives in this function rather than
            # discovery_engine.py (a real Book N suppressed here because it's
            # already known) -- merged with discovery["drop_diagnostics"]
            # into one consolidated log entry below. See
            # discovery_engine._record_drop_diagnostic.
            agent_drop_diagnostics: list[dict] = []
            # Phase 4 of agentic discovery, PURE SHADOW MODE: positional
            # record of which candidates cleared each of this loop's two
            # gates, consumed after the loop by
            # discovery_engine.compute_new_volume_flags. Captured here
            # rather than recomputed because belongs_to_series is only
            # final after the universe-tie-in/compilation downgrades below,
            # and already_known is only evaluated for candidates that got
            # that far. Two set.add calls -- nothing else in this loop
            # changes.
            belongs_indices: set[int] = set()
            known_indices: set[int] = set()

            for index, raw in enumerate(candidates):
                title = str(raw.get("title") or "").strip()
                title_key = discovery_engine.core_title_key(title)
                # Hardcover's search index tags each hit with its actual
                # position in the series -- when present, that's a more
                # reliable source of the book number than parsing free-text
                # title formatting, so prefer it over inference.
                inferred_number = raw.get("series_number_hint") or discovery_engine.infer_number_from_title(
                    title, series.name
                )
                resolved_number = _normalize_identity_number(inferred_number) if inferred_number else ""

                # Targeted-search results are relevance-ranked by the API
                # against "<series name> <author>", but that ranking isn't a
                # strict filter -- a prolific author's unrelated books (e.g.
                # a different series, an anthology, a companion volume) can
                # still come back as "targeted" hits with zero textual tie to
                # the series being checked (regression: searching "Safehold
                # David Weber" surfaced "Bolo!", "Worlds Of Honor", and "At
                # All Costs" -- unrelated Weber titles from other series).
                # Trusting confidence=="targeted" alone is only safe when the
                # source also gave a real series-position number for it
                # (structured data, e.g. Hardcover's series_number_hint, or a
                # "Book N" pattern in the title itself) -- a same-author hit
                # with no number and no textual series reference is too weak
                # a signal on its own to add to the library as a new book.
                # "missing_volume_recovery" (see the skeleton re-merge above)
                # is trusted the same way: it's tagged only when the
                # candidate came from a lookahead query built for that
                # exact missing number, which is at least as specific as
                # the plain targeted pass's "<series> <author>" query.
                came_from_targeted_search = raw.get("confidence") in ("targeted", "missing_volume_recovery")
                explicit_series_match = _title_pattern_match(title, series.name, known_series_titles)
                partial_match = _partial_series_match(title, series.name)
                continues_numbering = bool(
                    inferred_number and highest_owned_book_number and inferred_number > highest_owned_book_number
                )
                targeted_with_number = bool(came_from_targeted_search and inferred_number)
                # continues_numbering alone is too weak a signal for a
                # prolific multi-series author: a same-author book that
                # simply has a higher inferred number than the highest owned
                # volume says nothing about which of the author's several
                # series it belongs to (regression: "Check Now" on George
                # Wagner's "Jonathan Hunt Thriller Series" pulled in
                # higher-numbered books from his other, unrelated thriller
                # series purely because they continued the numbering).
                # Requiring it to be corroborated by an actual textual tie to
                # *this* series -- explicit or partial title match -- keeps
                # continues_numbering useful for genuine continuations while
                # closing that cross-series contamination path.
                continues_numbering_valid = continues_numbering and (explicit_series_match or partial_match)
                belongs_to_series = bool(
                    targeted_with_number or explicit_series_match or partial_match or continues_numbering_valid
                )

                # A self-identified "<Flagship Series> universe" tie-in novel
                # (see _looks_like_universe_tie_in) can textually match the
                # series name via explicit_series_match/partial_match while
                # actually belonging to a different spin-off series -- for
                # those, downgrade to requiring an actual series-position
                # number tying it to *this* series, same bar as any other
                # same-author-but-unrelated book.
                is_universe_tie_in = _looks_like_universe_tie_in(title)
                if is_universe_tie_in and not (targeted_with_number or continues_numbering):
                    belongs_to_series = False

                # A candidate that spells out two or more already-owned book
                # titles by name (rather than using a "Books 1-3"/"Boxed
                # Set"/"Omnibus" label) is a compilation of existing content,
                # not a new entry -- regardless of how it otherwise matched
                # (regression: "The Safehold Series, Volume I: Off
                # Armageddon Reef, By Schism Rent Asunder, By Heresies
                # Distressed, A Mighty Fortress, How Firm a Foundation"
                # strings together five owned titles with no number and no
                # bundle keyword, so it passed as a new "available" book).
                referenced_owned_titles = _count_referenced_owned_titles(title, owned_core_title_texts)
                is_compilation_of_owned_titles = referenced_owned_titles >= 2
                if is_compilation_of_owned_titles:
                    belongs_to_series = False

                candidate_diagnostics.append(
                    {
                        "title": title,
                        "source": raw.get("source"),
                        "confidence": raw.get("confidence"),
                        "explicit_series_match": explicit_series_match,
                        "partial_match": partial_match,
                        "inferred_number": inferred_number,
                        "continues_numbering": continues_numbering,
                        "targeted_with_number": targeted_with_number,
                        "referenced_owned_titles": referenced_owned_titles,
                        "is_universe_tie_in": is_universe_tie_in,
                        "accepted": belongs_to_series,
                    }
                )
                if belongs_to_series:
                    belongs_indices.add(index)

                # A candidate that fails belongs_to_series no longer stops
                # here. It used to be a hard drop -- correct when this
                # loop's only two outcomes were "add it" or "silently
                # discard it", but it means genuinely ambiguous candidates
                # (no resolvable number, no title match, an unmatched
                # continues_numbering) never got a chance to be looked at,
                # even the ones a human would recognize at a glance (see
                # discovery_agentic_migration_decision_log.md's Copilot-DIFF
                # round on ambiguity preservation). Confidence grading
                # below is what actually keeps this safe: an ambiguous
                # candidate can now only reach needs_review (visible, not
                # auto-added) or get dropped anyway on a "low"/"zero"
                # grade -- it is never auto-accepted into available_missing/
                # upcoming_books off belongs_to_series=False alone. See the
                # routing block below for why "high" is effectively
                # unreachable for these (title_confidence can't be "high"
                # without a skeleton entry, so overall can't be "high"
                # either -- see confidence_engine._overall_confidence).
                low_confidence_ambiguous = not belongs_to_series

                isbn13 = str(raw.get("isbn13") or "").strip()
                already_known = _is_known_candidate(
                    isbn13=isbn13,
                    title_key=title_key,
                    bare_title_key=discovery_engine.bare_title_key(title),
                    normalized_number=resolved_number,
                    known_series_isbns=known_series_isbns,
                    known_series_titles=known_series_titles,
                    known_series_numbers=known_series_numbers,
                    known_title_number_keys=known_title_number_keys,
                    known_bare_titles=known_bare_titles,
                )
                if already_known:
                    known_indices.add(index)
                    discovery_engine._record_drop_diagnostic(
                        "already_known",
                        {
                            "title": title,
                            "isbn13": isbn13 or None,
                            "series_number": discovery_engine._to_int_or_none(inferred_number),
                        },
                        "suppressed_as_known",
                        agent_drop_diagnostics,
                    )
                    continue

                # Different providers can return the same real book under
                # differently-formatted titles within a *single* check run
                # (e.g. Hardcover's "Havoc in the Deathyards, A
                # Completionist Chronicles Short Story" vs OpenLibrary's
                # bare "Havoc in the Deathyards") -- growing these sets as
                # candidates get accepted lets the identity check above
                # catch that on the very next candidate, the same way it
                # catches matches against pre-existing owned books.
                if isbn13:
                    known_series_isbns.add(isbn13)
                if title_key:
                    known_series_titles.add(title_key)
                if title_key and resolved_number:
                    known_title_number_keys.add(f"{title_key}|{resolved_number}")
                if resolved_number:
                    known_series_numbers.add(resolved_number)
                if not resolved_number:
                    candidate_bare_key = discovery_engine.bare_title_key(title)
                    if candidate_bare_key:
                        known_bare_titles.add(candidate_bare_key)

                parsed_date = discovery_engine.parse_flexible_date(raw.get("published_date"))
                is_upcoming = discovery_engine.classify_upcoming(parsed_date, raw.get("upcoming_hint"))

                # Give the stored title a recognizable series suffix when
                # the source didn't provide one (see _title_references_series)
                # so it's obvious at a glance which series/position a newly
                # added book belongs to, instead of a bare title like
                # "Unmapped" that could be mistaken for an unrelated find.
                display_title = title
                if inferred_number and not _title_references_series(title, series.name):
                    display_title = f"{title}: ({series.name} Book {inferred_number})"

                canonical = {
                    "title": display_title,
                    "author": series.author,
                    "series_name": series.name,
                    "series_number": inferred_number,
                    "date_iso": parsed_date.isoformat() if parsed_date else None,
                    "url": raw.get("source_url"),
                    "provider": raw.get("source"),
                    "identifier": isbn13 or f"{raw.get('source')}:{raw.get('source_id')}",
                }

                # Manual-override routing. `confidence_entry` is None when
                # confidence_lookup has nothing for this candidate at all
                # (series_confidence computation failed above, or -- should
                # never happen, but see correlation_key's docstring -- a
                # genuine key mismatch).
                #
                # "low"/"zero" always drop, regardless of belongs_to_series:
                # these are the confidence engine's own genuine negative
                # signals (a skeleton-corroborated title mismatch, a
                # detected author mismatch, malformed data) -- real
                # information belongs_to_series' cruder heuristic can't see
                # on its own, which is exactly why cross-contamination
                # candidates that pass belongs_to_series via a textual title
                # match still need to be caught here.
                #
                # "medium"/"unverified" only route to needs_review when
                # low_confidence_ambiguous is True, i.e. belongs_to_series
                # itself couldn't confirm series membership. For a candidate
                # that already passed belongs_to_series cleanly (explicit
                # title match, targeted-with-number, etc.), "unverified" is
                # not a red flag -- it is the *expected*, permanent state
                # for title_confidence on every genuinely new book, because
                # SeriesSkeleton only ever contains books already owned (see
                # confidence_engine._title_confidence). Gating on it
                # unconditionally was verified live (Jonathan Hunt/Georgia
                # Wagner, 2026-08-22) to silently route every legitimate new
                # discovery to needs_review -- which nothing outside this
                # function reads yet (see the review-list TODO above) --
                # making "Check Now" report "no books found" even when
                # discovery worked correctly. So a clean belongs_to_series
                # pass auto-accepts on medium/unverified/missing-confidence
                # the same as it always did before this feature existed;
                # only the genuinely ambiguous (belongs_to_series=False)
                # case is gated by confidence at all.
                confidence_entry = confidence_lookup.get(confidence_engine.correlation_key(raw))
                overall_grade = confidence_entry.get("overall") if confidence_entry else None

                if overall_grade in ("low", "zero"):
                    continue

                if low_confidence_ambiguous and (overall_grade in ("medium", "unverified") or overall_grade is None):
                    # Same-author/different-series candidates with valid
                    # numbering can land here permanently -- confidence_engine
                    # has no series-identity dimension, so it can score
                    # "medium" on numbering/title alone even when the
                    # candidate is actually an unrelated book by the same
                    # author. series_name_hint (below) is the cheap,
                    # deliberate mitigation: it lets a human dismiss those
                    # at a glance without this module needing new scoring
                    # logic. This is an expected noise floor, not a bug.
                    needs_review.append(
                        {
                            **canonical,
                            "needs_review": True,
                            "low_confidence_ambiguous": low_confidence_ambiguous,
                            "series_name_hint": raw.get("series_name_hint"),
                            "overall_confidence": overall_grade,
                            "provider_confidence": (
                                confidence_entry.get("provider_confidence") if confidence_entry else None
                            ),
                            "title_confidence": confidence_entry.get("title_confidence") if confidence_entry else None,
                            "number_confidence": (
                                confidence_entry.get("number_confidence") if confidence_entry else None
                            ),
                            "series_alignment_confidence": (
                                confidence_entry.get("series_alignment_confidence") if confidence_entry else None
                            ),
                        }
                    )
                    continue

                if is_upcoming:
                    upcoming_books.append(canonical)
                else:
                    available_missing.append(canonical)

            # Phase 3.5 of agentic discovery, PURE SHADOW MODE: one
            # consolidated log entry combining the external-series-reality
            # fields computed above with every drop diagnostic recorded this
            # run, across both discovery_engine.py (discovery_drop_diagnostics,
            # merged into discovery["drop_diagnostics"]) and this function's
            # own already-known suppression (agent_drop_diagnostics). Purely
            # diagnostic -- nothing above or below this reads any of it.
            try:
                all_drop_diagnostics = discovery.get("drop_diagnostics", []) + agent_drop_diagnostics

                # Phase 4 of agentic discovery, PURE SHADOW MODE: three
                # diagnostics derived from the Phase 3.5 fields above and
                # the loop's index sets -- which candidates fill an
                # externally-expected gap the library doesn't own, how
                # incomplete the library looks against the external total,
                # and a readable explanation per drop. Computed after every
                # accept/reject decision has already been made and read by
                # nothing but the log line below: `result`, `added_books`,
                # the skeleton and the delta are all untouched.
                external_missing_vs_owned: list[int] = []
                external_gap_ratio: float | None = None
                new_volume_flags: list[dict] = []
                owned_books_total: int | None = None
                owned_books_with_numbers: int | None = None
                drop_explanations: list[dict] = []
                drop_explanations_total = 0
                drop_explanation_counts: dict[str, int] = {}

                # Grouped rather than split three ways: the two below it
                # both consume external_missing_vs_owned, so letting them
                # run on its [] fallback would log a confident
                # external_gap_ratio of 0.0 and is_new_volume: false for
                # every candidate -- i.e. "series is complete, nothing new"
                # -- when the truth is that the computation failed. Failing
                # all three together keeps the ratio None and the flags
                # empty, which reads as "no data".
                try:
                    external_missing_vs_owned = discovery_engine.compute_external_missing_vs_owned(
                        external_expected_total, owned_books_for_skeleton
                    )
                    external_gap_ratio = discovery_engine.compute_external_gap_ratio(
                        external_expected_total, external_missing_vs_owned
                    )
                    new_volume_flags = discovery_engine.compute_new_volume_flags(
                        candidates,
                        series.name,
                        external_missing_vs_owned,
                        belongs_indices,
                        known_indices,
                    )
                except Exception:
                    external_missing_vs_owned = []
                    external_gap_ratio = None
                    new_volume_flags = []
                    logger.exception("Phase 4 new-volume/gap-ratio computation failed for series_id=%s", series_id)

                try:
                    coverage = discovery_engine.compute_owned_number_coverage(owned_books_for_skeleton)
                    owned_books_total = coverage["owned_books_total"]
                    owned_books_with_numbers = coverage["owned_books_with_numbers"]
                except Exception:
                    owned_books_total = None
                    owned_books_with_numbers = None
                    logger.exception("Phase 4 owned-number-coverage computation failed for series_id=%s", series_id)

                try:
                    explained = discovery_engine.compute_drop_explanations(all_drop_diagnostics)
                    drop_explanations = explained["drop_explanations"]
                    drop_explanations_total = explained["drop_explanations_total"]
                    drop_explanation_counts = explained["drop_explanation_counts"]
                except Exception:
                    drop_explanations = []
                    drop_explanations_total = 0
                    drop_explanation_counts = {}
                    logger.exception("Phase 4 drop-explanation computation failed for series_id=%s", series_id)

                logger.info(
                    "series_external_reality: %s",
                    json.dumps(
                        {
                            "series_id": series_id,
                            "external_expected_total": external_expected_total,
                            "external_total_hint_count": external_total_hint_count,
                            "external_missing_numbers": external_missing_numbers,
                            "external_missing_vs_owned": external_missing_vs_owned,
                            "external_gap_ratio": external_gap_ratio,
                            "owned_books_total": owned_books_total,
                            "owned_books_with_numbers": owned_books_with_numbers,
                            "new_volume_flags": new_volume_flags,
                            # Phase 4 supersedes the raw drop_diagnostics
                            # list this entry used to carry: every field of
                            # it survives inside drop_explanations, plus the
                            # explanation text, so logging both would just
                            # double the payload.
                            "drop_explanations": drop_explanations,
                            "drop_explanations_total": drop_explanations_total,
                            "drop_explanation_counts": drop_explanation_counts,
                        },
                        default=str,
                    ),
                )
            except Exception:
                logger.exception("Phase 3.5 external-reality/drop-diagnostics logging failed for series_id=%s", series_id)

            found = bool(available_missing or upcoming_books)
            series.has_new_books = found
            series.last_checked = today
            db.commit()
            db.refresh(series)

            added_books = [_build_added_book_entry(canonical, status="available") for canonical in available_missing]
            added_books += [_build_added_book_entry(canonical, status="upcoming") for canonical in upcoming_books]

            _console_log(f"CHECK NOW completed successfully for series: {series.name}")

            result = {
                "series_id": series.id,
                "series_name": series.name,
                "highest_owned_book_number": highest_owned_book_number,
                "candidate_numbers": [],
                "added_count": len(added_books),
                "added_books": added_books,
                "found_books": added_books,
                "candidate_diagnostics": candidate_diagnostics,
                "complete": True,
                "status": "complete" if found else "no_hits",
                "no_new_books": not found,
                "reason": None if found else "no-hit-after-new-book-check",
                "has_new_books": series.has_new_books,
                "series_state": series.series_state,
                "last_checked": series.last_checked,
                "next_unread_book_number": series.next_unread_book_number,
                "next_upcoming_book_number": series.next_upcoming_book_number,
                "missing_books": available_missing,
                "available_missing": available_missing,
                "upcoming_books": upcoming_books,
                "needs_review": needs_review,
                "validated_candidates": [],
                "found": found,
                "candidate": (available_missing[0] if available_missing else (upcoming_books[0] if upcoming_books else None)),
                "provider_failures": provider_failures,
                "all_providers_failed": all_providers_failed,
                "asin_discovery": {
                    "discovered": len(candidates),
                    "processed": len(candidates),
                    "fetch_success": len(candidates),
                    "fetch_failed": 0,
                    "metadata_hits": len(added_books),
                },
                "provider_ledger": [],
                "discovery_engine": "official_api_v1",
                "agent_pipeline": True,
                "telemetry": telemetry.summary() if telemetry is not None else None,
                "cache": cache.summary() if cache is not None else None,
            }
            if emit_summary:
                log_discovery_summary(result=result)
            return result
        except Exception as exc:
            if emit_summary:
                log_discovery_summary(
                    result=_empty_result(series_id, getattr(series, "name", None), "error"),
                    terminal_error=f"{type(exc).__name__}: {exc}",
                )
            raise


def _candidate_richness_score(candidate: dict) -> int:
    """Used to pick which duplicate to keep when the same real book comes
    back as multiple raw candidates (see discover_more_by_author) -- prefer
    whichever copy carries the most useful information instead of an
    arbitrary "first one wins".
    """
    score = 0
    if candidate.get("matched_series_id"):
        score += 4
    if candidate.get("series_name"):
        score += 2
    if candidate.get("release_date"):
        score += 2
    if candidate.get("source_url"):
        score += 1
    return score


def _owned_book_indexes(db: Session, author: str, profile_id: str = "robbie") -> dict:
    """Shared by discover_more_by_author and discover_series_by_name --
    both need the exact same "what does this author's owned library already
    contain" picture to decide what's genuinely new, just starting from a
    different raw candidate list (author-wide vs. one targeted series).

    profile_id scopes both the owned-books and tracked-series lookups
    below -- without it, a book Robbie owns by this author would
    incorrectly suppress a genuinely-new discovery result for Daughter's
    copy of the same book. Defaults to "robbie" only so pre-existing
    single-profile callers (tests, scripts) don't all need updating --
    every real request path (routers/books.py) always passes it explicitly
    from get_current_profile_id.
    """
    owned_books = [
        book
        for book in db.query(Book).filter(Book.author.isnot(None), Book.profile_id == profile_id).all()
        if str(book.record_status or "") != "deleted" and _authors_match_exact(author, book.author)
    ]

    known_isbns = {str(book.isbn13 or "").strip() for book in owned_books if str(book.isbn13 or "").strip()}
    # Safe to compare globally (not per-series): this only matches on
    # actual title *text*, including any number folded in directly from
    # that text (see core_title_key) -- unlike a bare numeric position,
    # two unrelated series both happening to use identical title wording
    # is effectively impossible.
    known_title_keys = {discovery_engine.core_title_key(owned_title_for_identity(book)) for book in owned_books if book.title}
    bare_title_counts: dict[str, int] = {}
    for book in owned_books:
        bare_key = discovery_engine.bare_title_key(owned_title_for_identity(book))
        if bare_key:
            bare_title_counts[bare_key] = bare_title_counts.get(bare_key, 0) + 1
    # Only trusted when unique across this author's whole catalog -- a
    # bare, number-less candidate title ("Ruin") matching a single owned
    # book's bare title is strong evidence of the same book; if two owned
    # books share that bare title it's too ambiguous to use as a match.
    known_bare_titles = {key for key, count in bare_title_counts.items() if count == 1}

    # Owned book *numbers* are only meaningful when scoped to one series --
    # nearly every series has a "book 1", so comparing a candidate's number
    # against every tracked series' numbers combined would treat a
    # genuinely new book in an untracked series as already owned just
    # because some other series also has that position.
    owned_numbers_by_series_id: dict[int, set[str]] = {}
    for book in owned_books:
        if book.series_id is None or book.book_number is None:
            continue
        owned_numbers_by_series_id.setdefault(book.series_id, set()).add(_normalize_identity_number(book.book_number))

    # Lets the caller offer "add to this existing series" instead of
    # silently creating a new one from the LLM's guessed series name -- this
    # function only reports the match, it never creates or links anything.
    # Keyed on the branding-normalized name (strips generic words like
    # "Universe"/"Series") rather than exact text, since catalogs commonly
    # tack those onto a series name for one listing but not another
    # (regression: a candidate guessed series "Duchy of Terra Universe" for
    # a book already owned under the tracked series "Duchy of Terra" -- an
    # exact-text lookup missed that and reported it "not yet tracked").
    tracked_series_by_name = {
        discovery_engine.normalize_series_branding_name(series.name): series
        for series in db.query(Series).filter(Series.author.isnot(None), Series.profile_id == profile_id).all()
        if _authors_match_exact(author, series.author)
    }

    return {
        "known_isbns": known_isbns,
        "known_title_keys": known_title_keys,
        "known_bare_titles": known_bare_titles,
        "owned_numbers_by_series_id": owned_numbers_by_series_id,
        "tracked_series_by_name": tracked_series_by_name,
    }


def _build_discovery_candidate_entries(raw_candidates: list[dict], author: str, indexes: dict) -> list[dict]:
    """Turns raw discovery-engine candidates into the response shape both
    discover_more_by_author and discover_series_by_name return: owned-book
    exclusion, matched-tracked-series lookup, placeholder-date cleanup, and
    same-book deduplication within this one batch.

    Catalog APIs return editions (hardcover/paperback/ebook/audiobook, each
    with its own ISBN) and re-listings of already-owned books far more
    aggressively here than in a single-series Check Now, since there's no
    per-series lookahead query to focus on just the next unread number --
    so both "is this already owned" and "is this a duplicate of another
    candidate in this same batch" need to be checked primarily by title
    identity, not ISBN (regression: a live check surfaced ~20 rows that
    were almost entirely editions/re-listings of 12 already-owned books,
    because the original version of this function only matched
    already-owned titles by exact core_title_key and never collapsed
    same-book duplicates within a single batch at all).
    """
    known_isbns = indexes["known_isbns"]
    known_title_keys = indexes["known_title_keys"]
    known_bare_titles = indexes["known_bare_titles"]
    owned_numbers_by_series_id = indexes["owned_numbers_by_series_id"]
    tracked_series_by_name = indexes["tracked_series_by_name"]

    deduped: dict[str, dict] = {}
    for raw in raw_candidates:
        title = str(raw.get("title") or "").strip()
        if not title:
            continue

        isbn13 = str(raw.get("isbn13") or "").strip()
        title_key = discovery_engine.core_title_key(title)
        bare_key = discovery_engine.bare_title_key(title)

        series_name_guess = str(raw.get("series_name_hint") or "").strip() or None
        matched_series = (
            tracked_series_by_name.get(discovery_engine.normalize_series_branding_name(series_name_guess))
            if series_name_guess
            else None
        )
        inferred_number = raw.get("series_number_hint") or discovery_engine.infer_number_from_title(
            title, series_name_guess
        )
        normalized_number = _normalize_identity_number(inferred_number) if inferred_number else ""

        already_owned = bool(isbn13 and isbn13 in known_isbns) or bool(title_key and title_key in known_title_keys)
        if not already_owned and matched_series is not None and normalized_number:
            already_owned = normalized_number in owned_numbers_by_series_id.get(matched_series.id, set())
        # Bare-title matching is number-agnostic by construction (it's the
        # title text before any parenthetical/colon suffix), so it isn't
        # restricted to only-when-no-number-is-known -- the safety net
        # against false collisions is the uniqueness requirement on
        # known_bare_titles, not the presence/absence of a number
        # (regression: candidates carrying a number tied to an unmatched
        # alternate-branding series listing -- e.g. Glynn Stewart's
        # "Starship's Mage: UnArcana Rebellion" re-release of already-owned
        # main-series books -- have a number, so this check used to be
        # skipped entirely alongside the per-series check above, letting
        # several already-owned books through as "new").
        if not already_owned and bare_key and bare_key in known_bare_titles:
            already_owned = True
        if already_owned:
            continue

        parsed_date = discovery_engine.parse_flexible_date(raw.get("published_date"))
        release_date_iso = parsed_date.isoformat() if parsed_date else None
        if release_date_iso and discovery_engine.looks_like_placeholder_date(release_date_iso):
            # A literal Jan 1st is almost always a "year-only" stand-in, not
            # a confirmed exact date -- keep the candidate, just don't show
            # a fabricated day/month next to genuinely-dated releases.
            release_date_iso = None
        is_upcoming = discovery_engine.classify_upcoming(parsed_date, raw.get("upcoming_hint"))

        candidate_entry = {
            # Cleaned for *display* only -- title/title_key/bare_key/
            # inferred_number above all stay derived from the original raw
            # title, since e.g. the "#N" this strips off is also where
            # infer_number_from_title reads the book's position from.
            "title": discovery_engine.clean_display_title(title),
            "author": author,
            "series_name": series_name_guess,
            "matched_series_id": matched_series.id if matched_series else None,
            # The canonical tracked name, distinct from series_name (the raw
            # per-candidate guess, e.g. "Duchy of Terra Universe") -- lets
            # the caller show "you already track <this>" using the name you
            # actually gave the series, not whatever branding a given
            # catalog listing happened to use.
            "matched_series_name": matched_series.name if matched_series else None,
            # For a tracked series, its own is_finished/total_books (kept up
            # to date by the normal Check Now / recalculation flow) is a far
            # more authoritative maturity signal than anything this one
            # discovery batch could re-derive -- surfaced here so the
            # frontend never needs to guess for a series we already track.
            "matched_series_is_finished": bool(matched_series.is_finished) if matched_series else None,
            "matched_series_total_books": matched_series.total_books if matched_series else None,
            "series_number": inferred_number,
            "status": "upcoming" if is_upcoming else "available",
            "release_date": release_date_iso,
            "source_url": raw.get("source_url"),
            "isbn13": isbn13 or None,
            "provider": raw.get("source"),
            # Only used for the on-demand "Series Overview" LLM call and the
            # "found N books" maturity count for series NOT yet tracked --
            # never sent anywhere unless the user clicks that button.
            "description": raw.get("description"),
            "series_total_books": raw.get("series_total_hint"),
        }

        # Collapse the same real book showing up as separate raw candidates
        # within this one batch (different provider, different
        # edition/ISBN, or simply missing a number one other source did
        # provide) -- grouped primarily by bare title text, since the same
        # book is inconsistently number-tagged across providers far more
        # often than two genuinely different books share identical title
        # wording.
        group_key = bare_key or title_key or discovery_engine.normalize_text(title)
        if not group_key:
            continue

        existing = deduped.get(group_key)
        if (
            existing is not None
            and normalized_number
            and existing.get("series_number") is not None
            and _normalize_identity_number(existing["series_number"]) != normalized_number
        ):
            # Same bare title but a clearly different resolved number --
            # almost certainly two distinct books (e.g. two unrelated
            # series that happen to share a common one-word title) rather
            # than two editions of the same one, so don't merge them.
            group_key = f"{group_key}|{normalized_number}"
            existing = deduped.get(group_key)

        if existing is None or _candidate_richness_score(candidate_entry) > _candidate_richness_score(existing):
            deduped[group_key] = candidate_entry

    return sorted(
        deduped.values(),
        key=lambda c: (str(c.get("series_name") or "\uffff"), str(c.get("title") or "")),
    )


def discover_more_by_author(db: Session, author: str, profile_id: str = "robbie") -> dict:
    """"More by this author" -- a lightweight, on-demand discovery across
    ALL of an author's tracked series and standalone books at once.

    Unlike run_series_check, this is not scoped to one series, does not
    write anything to the database, and returns its candidate list directly
    -- the caller (the API layer) decides per-row whether/how to add a
    match via the normal create-book/create-series endpoints, per the "no
    auto-created series from a guessed name" rule.

    Deliberately a broad, shallow sweep (one query per catalog API, no
    per-series lookahead) -- see discover_series_by_name for the deeper,
    targeted counterpart used to fill in a specific series this pass didn't
    fully cover. profile_id scopes the "already owned" check to just this
    profile's library (see _owned_book_indexes).
    """
    author = str(author or "").strip()
    if not author:
        return {"author": author, "candidates": [], "provider_failures": [], "all_providers_failed": False}

    indexes = _owned_book_indexes(db, author, profile_id)
    discovery = discovery_engine.discover_candidates_for_author(author, exclude_title_keys=indexes["known_title_keys"])
    results = _build_discovery_candidate_entries(discovery["candidates"], author, indexes)

    return {
        "author": author,
        "candidates": results,
        "provider_failures": discovery["provider_failures"],
        "all_providers_failed": discovery["all_providers_failed"],
    }


def discover_series_by_name(db: Session, series_name: str, author: str, profile_id: str = "robbie") -> dict:
    """Deeper, targeted counterpart to discover_more_by_author's broad
    sweep -- used to fill in a specific series the broad pass only
    partially found.

    Live regression: "More by this author" for Glynn Stewart's "Scattered
    Stars" surfaced a books_count of 6 from Hardcover (see
    series_total_hint) but only ever found book 1 ("Conviction") -- the
    broad author-wide bibliography query simply doesn't return every book
    across every series for a prolific author. This reuses the same
    targeted "<series> <author>" search (with lookahead queries for an
    unannounced next book) that a tracked series' own Check Now uses, which
    is far more likely to surface the rest of a specific series than a
    single generic author-wide query ever would.
    """
    series_name = str(series_name or "").strip()
    author = str(author or "").strip()
    if not series_name or not author:
        return {"series_name": series_name, "author": author, "candidates": [], "provider_failures": [], "all_providers_failed": False}

    indexes = _owned_book_indexes(db, author, profile_id)
    discovery = discovery_engine.discover_candidates_for_series(
        series_name, author, exclude_title_keys=indexes["known_title_keys"], allow_author_fallback=False
    )
    results = _build_discovery_candidate_entries(discovery["candidates"], author, indexes)

    return {
        "series_name": series_name,
        "author": author,
        "candidates": results,
        "provider_failures": discovery["provider_failures"],
        "all_providers_failed": discovery["all_providers_failed"],
    }
