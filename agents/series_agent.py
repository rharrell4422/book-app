from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

import agentic_hooks
import confidence_engine
import delta_engine
import discovery_engine
import intelligence
import settings
from llm_client import TIER_MODEL_MAP, LLMCallError, call_llm
from models import Book, Series, SeriesSkeleton
from prompts import build_belongs_to_series_prompt
from services import candidate_notifications, tier_c_shadow_store
from services.discovery_cache import DiscoveryCache
from services.discovery_logging import log_discovery_summary
from services.discovery_telemetry import DiscoveryTelemetry, maybe_pass_scope
from services.fingerprint_store import build_fingerprint_observations, get_effective_fingerprint
from services.identity import owned_title_for_identity
from services.llm_pricing import get_price_per_million
from services.skeleton_store import backfill_skeleton_for_series


logger = logging.getLogger(__name__)


def _console_log(message: str) -> None:
    print(f"[series_agent] {message}", flush=True)


def _skeleton_entry_for_number(skeleton_entries: list[dict] | None, number) -> dict | None:
    """PB-11 diagnostic helper: looks up whatever SeriesSkeleton entry (if
    any) exists at `number` -- used only by the low/zero-confidence drop
    log line above to show whether a stale/mismatched skeleton entry (see
    confidence_engine._title_confidence's "low" grade) is what's actually
    dragging a candidate down, without needing DB access to check.
    """
    try:
        target = float(number)
    except (TypeError, ValueError):
        return None
    for entry in skeleton_entries or []:
        if not isinstance(entry, dict):
            continue
        try:
            if float(entry.get("book_number")) == target:
                return entry
        except (TypeError, ValueError):
            continue
    return None


def _normalize_author(value: str | None) -> str:
    return str(value or "").strip().lower()


def _agentic_safe_sort_key(value) -> float:
    """Phase 6 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
    phase1_evaluation.md`, not re-litigated here): a book_number that
    isn't a real `int`/`float` sorts to `+inf` -- last, deterministically
    -- rather than raising `TypeError` when compared against a genuine
    number or another malformed value of a different type.
    """
    return value if isinstance(value, (int, float)) else float("inf")


def _sorted_book_number_dict(raw: dict | None) -> dict:
    """Phase 6: deterministic normalization for a `{book_number: value}`
    mapping used by the Phase 3/4/5 promotion block below -- coerces
    every key to `float` (matching every other agentic book_number key
    in this codebase, so `1` and `1.0` collide as the same book) and
    returns a NEW dict with keys inserted in ascending order, so
    downstream `.items()`/`.keys()` iteration is stable regardless of
    `raw`'s own insertion order. Fail-soft: a key that can't be coerced
    to `float` (not a genuine book_number) is dropped rather than
    raising or corrupting the rest of the mapping; a non-dict `raw`
    yields `{}`.
    """
    if not isinstance(raw, dict):
        return {}
    coerced: dict[float, object] = {}
    for key, value in raw.items():
        try:
            coerced[float(key)] = value
        except (TypeError, ValueError):
            continue
    return {key: coerced[key] for key in sorted(coerced)}


def _sorted_agentic_trace_list(raw, *, book_number_key: str = "book_number") -> list[dict]:
    """Phase 6: deterministic normalization for one of `run_agentic_
    turn`'s trace lists (`agentic_trace["confidence_traces"]`/
    `["gate_traces"]`) before the Phase 3/4/5 promotion block below
    reads them -- sorts dict entries by `book_number_key` ascending so
    promotion evaluation/storage is stable regardless of the shadow
    loop's own internal (provider/candidate) iteration order.

    Fail-soft, per Phase 6 spec ("missing or malformed -> treat as
    empty list"): anything that isn't a `list` at all yields `[]`.
    Individual non-dict entries within an otherwise-valid list are
    dropped rather than raising -- the loop this feeds already skips
    any entry missing a usable `book_number_key`
    (`agents/series_agent.py`'s promotion block continues past those),
    so dropping a non-dict entry here changes nothing about which
    books end up promoted; it just avoids an `AttributeError` from
    calling `.get()` on something that isn't a dict.
    """
    if not isinstance(raw, list):
        return []
    try:
        entries = [entry for entry in raw if isinstance(entry, dict)]
        return sorted(
            entries,
            key=lambda entry: _agentic_safe_sort_key(entry.get(book_number_key)),
        )
    except Exception:
        return []


def _run_agentic_turn_guarded(run_agentic_turn_fn, series_id: int, context: dict, *, shared_state: dict | None = None):
    """Phase 8 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
    phase1_evaluation.md`, not re-litigated here): a per-turn guard
    ensuring `run_agentic_turn_fn` (always `agents.agentic_series_agent.
    run_agentic_turn` in practice, passed in rather than imported here
    to keep this helper trivially testable/free of a circular import) is
    invoked at most once per `run_series_check` call, no matter how many
    of this module's call sites ask for a trace during that call.

    `context`/`shared_state` are plain dicts (`run_agentic_turn`'s real,
    actual context shape below -- e.g. `promotion_context`/`dry_run_
    context` -- is a dict, never an object with attributes), so the
    guard is a dict key (`"_agentic_turn_ran"`/`"_agentic_turn_result"`),
    not `getattr`/`setattr` on an object.

    `shared_state`, when provided, is the guard/result-cache location
    instead of `context` itself -- this is what actually lets the two
    call sites below (the Phase 3/4/5 promotion block's own call, and
    the Phase 2 dry-run block's call, each building its own `context`
    dict for its own purposes -- see each block's own comment) share
    ONE underlying `run_agentic_turn` invocation for a given `run_series_
    check` call: both pass the *same* `shared_state` dict (created once,
    near the top of that section of `run_series_check`), so whichever of
    the two runs first performs the real call (using *its own* `context`
    -- e.g. the promotion block's, which passes this turn's live `db`
    session), and the second one reuses that exact trace unchanged,
    never building its own `context`'s session/trace at all. Omitting
    `shared_state` (the default) falls back to guarding against re-entry
    on `context` alone, matching this helper's pre-Phase-8-merge
    behavior for any other, unrelated caller.

    Given both call sites below only ever read from `db`/`SeriesSkeleton`
    state that is already fully committed by the time either one runs
    (see each block's own comment on session handling), reusing the
    first call's trace for the second is behaviorally equivalent to
    computing it twice -- the only observable difference is the shared
    trace's `turn_timestamp` field (informational only, never consulted
    by any routing/promotion decision), which now reflects whichever
    call happened first instead of a separately-stamped value per call
    site.

    If the one real invocation raises, that exception is cached too
    (not just a successful result) and re-raised, unmodified, to every
    subsequent guarded call for that same `shared_state` -- each call
    site's own surrounding try/except (each already existed before
    Phase 8) still independently observes and fail-softs on "run_
    agentic_turn raised" exactly as it always did; this guard only
    changes how many times the underlying call actually happens, never
    whether a given call site sees success or failure.

    Phase 9 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
    phase1_evaluation.md`, not re-litigated here): the one real
    invocation below (never a cache-hit reuse) bumps `services.
    discovery_telemetry`'s in-memory `agentic_turn_invocations` counter,
    and `agentic_turn_failures` too if it raises -- fail-soft, never
    raises itself, purely observational.
    """
    state = shared_state if shared_state is not None else context
    if state.get("_agentic_turn_ran"):
        cached_exception = state.get("_agentic_turn_exception")
        if cached_exception is not None:
            raise cached_exception
        return state.get("_agentic_turn_result")
    state["_agentic_turn_ran"] = True
    _record_agentic_turn_metric(invoked=True)
    try:
        result = run_agentic_turn_fn(series_id, context)
    except Exception as exc:
        state["_agentic_turn_exception"] = exc
        _record_agentic_turn_metric(failed=True)
        raise
    state["_agentic_turn_result"] = result
    return result


def _record_agentic_turn_metric(*, invoked: bool = False, failed: bool = False) -> None:
    """Fail-soft telemetry side-channel for `_run_agentic_turn_guarded`'s
    Phase 9 counters above -- never raises. Function-scoped import, same
    convention as every other telemetry call site in the agentic modules
    (avoids any risk of a circular import at module load time).
    """
    try:
        from services.discovery_telemetry import record_agentic_turn_failure, record_agentic_turn_invocation

        if invoked:
            record_agentic_turn_invocation()
        if failed:
            record_agentic_turn_failure()
    except Exception:
        logger.exception(
            "_record_agentic_turn_metric: failed to record metric (invoked=%s, failed=%s)", invoked, failed
        )


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


def _needs_review_to_skeleton_updates(needs_review: list[dict]) -> list[dict]:
    """PB-1: the only bucket of "found this round but not persisted"
    evidence the deterministic (Phase 0) pipeline already computes is
    `needs_review` -- ambiguous/low-confidence candidates a human still has
    to triage. Mapping it onto `skeleton_store.apply_skeleton_updates`'s
    input shape gives that unconfirmed finding a durable trace
    (`source_class: "discovered"`) instead of it vanishing the moment this
    request ends, so a later run's confidence/gap computations have a
    memory of "already surfaced, still unresolved" for this book_number.

    `available_missing`/`upcoming_books` are deliberately NOT a source
    here: both get persisted as real `Book` rows this same round (see
    `services/series_check_engine.py`'s `added_books` handling) and so
    become `library`-class skeleton entries on the very next backfill --
    routing them through here too would just be an immediately-stale
    duplicate of that.

    Conservative by design: `status` is "unconfirmed", never "confirmed"
    -- unlike the recommendation doc's example of an actual Phase 1 agent
    finding, nothing here has verified this book exists, only that a
    low-confidence candidate surfaced it. `apply_skeleton_updates` fills
    in `source_class`/`first_seen_at`/`last_confirmed_at` itself, so those
    are intentionally omitted here.
    """
    updates: list[dict] = []
    for entry in needs_review:
        if not isinstance(entry, dict):
            continue
        try:
            book_number = float(entry.get("series_number"))
        except (TypeError, ValueError):
            continue
        updates.append(
            {
                "book_number": book_number,
                "title": entry.get("title"),
                "status": "unconfirmed",
                "confidence": entry.get("overall_confidence"),
                "release_date": entry.get("date_iso"),
                # LitRPG-discovery-plan addition: threads canonical's isbn13
                # (None when the candidate had none) through to the durable
                # skeleton entry -- see services/skeleton_store.py's
                # SeriesSkeleton doc comment for the schema addition.
                "isbn13": entry.get("isbn13"),
                "sources": [
                    {
                        "provider": entry.get("provider"),
                        "url": entry.get("url"),
                    }
                ],
            }
        )
    return updates


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


def evaluate_belongs_to_series_gate(
    *,
    title: str,
    inferred_number,
    candidate_confidence: str | None,
    series_name: str,
    known_series_titles: set[str],
    owned_core_title_texts: set[str],
    highest_owned_book_number: int | None,
) -> dict:
    """Pure extraction of `run_series_check`'s belongs-to-series gate --
    exact same computation the live loop used inline before this
    extraction, just named and callable independently (zero behavior
    change to the live loop, which now just calls this and unpacks the
    result -- see the loop body for the call site). Callers pass in
    `title`/`inferred_number` already resolved (see the loop's own
    Hardcover-position-vs-title-inference comment) rather than this
    function re-deriving them, so there's exactly one place that ever
    computes those two values.

    Extracted specifically so PB-5's shadow gate trace and Phase 1's
    `agents/agentic_series_agent.py` (deterministic shadow loop, see that
    module) can evaluate the identical, unmodified gate logic without
    duplicating it -- two independently-maintained copies of this much
    branching logic is exactly the kind of drift risk
    `discovery_agentic_phase1_evaluation.md` flags elsewhere (see its
    author-mismatch-reconciliation discussion) for a different gate.

    Targeted-search results are relevance-ranked by the API against
    "<series name> <author>", but that ranking isn't a strict filter -- a
    prolific author's unrelated books (e.g. a different series, an
    anthology, a companion volume) can still come back as "targeted"
    hits with zero textual tie to the series being checked (regression:
    searching "Safehold David Weber" surfaced "Bolo!", "Worlds Of Honor",
    and "At All Costs" -- unrelated Weber titles from other series).
    Trusting confidence=="targeted" alone is only safe when the source
    also gave a real series-position number for it (structured data,
    e.g. Hardcover's series_number_hint, or a "Book N" pattern in the
    title itself) -- a same-author hit with no number and no textual
    series reference is too weak a signal on its own to add to the
    library as a new book. "missing_volume_recovery" is trusted the same
    way: it's tagged only when the candidate came from a lookahead query
    built for that exact missing number, at least as specific as the
    plain targeted pass's "<series> <author>" query.

    continues_numbering alone is too weak a signal for a prolific
    multi-series author: a same-author book that simply has a higher
    inferred number than the highest owned volume says nothing about
    which of the author's several series it belongs to (regression:
    "Check Now" on George Wagner's "Jonathan Hunt Thriller Series" pulled
    in higher-numbered books from his other, unrelated thriller series
    purely because they continued the numbering). Requiring it to be
    corroborated by an actual textual tie to *this* series -- explicit or
    partial title match -- keeps continues_numbering useful for genuine
    continuations while closing that cross-series contamination path.

    A self-identified "<Flagship Series> universe" tie-in novel (see
    _looks_like_universe_tie_in) can textually match the series name via
    explicit_series_match/partial_match while actually belonging to a
    different spin-off series -- for those, downgrade to requiring an
    actual series-position number tying it to *this* series, same bar as
    any other same-author-but-unrelated book.

    A candidate that spells out two or more already-owned book titles by
    name (rather than using a "Books 1-3"/"Boxed Set"/"Omnibus" label) is
    a compilation of existing content, not a new entry -- regardless of
    how it otherwise matched (regression: "The Safehold Series, Volume I:
    Off Armageddon Reef, By Schism Rent Asunder, By Heresies Distressed,
    A Mighty Fortress, How Firm a Foundation" strings together five owned
    titles with no number and no bundle keyword, so it passed as a new
    "available" book).
    """
    came_from_targeted_search = candidate_confidence in ("targeted", "missing_volume_recovery")
    explicit_series_match = _title_pattern_match(title, series_name, known_series_titles)
    partial_match = _partial_series_match(title, series_name)
    # inferred_number is deliberately left as whatever type its source
    # gave it (int from infer_number_from_title, but a *string* from a
    # provider's series_number_hint, e.g. Apify's/Hardcover's "9") --
    # _to_int_or_none gives both sides a real numeric type to compare
    # regardless of which source inferred_number came from (see the live
    # loop's own comment for the crash this fixed).
    inferred_number_int = discovery_engine._to_int_or_none(inferred_number)
    continues_numbering = bool(
        inferred_number_int is not None
        and highest_owned_book_number
        and inferred_number_int > highest_owned_book_number
    )
    targeted_with_number = bool(came_from_targeted_search and inferred_number)
    continues_numbering_valid = continues_numbering and (explicit_series_match or partial_match)
    belongs_to_series = bool(
        targeted_with_number or explicit_series_match or partial_match or continues_numbering_valid
    )

    is_universe_tie_in = _looks_like_universe_tie_in(title)
    if is_universe_tie_in and not (targeted_with_number or continues_numbering):
        belongs_to_series = False

    referenced_owned_titles = _count_referenced_owned_titles(title, owned_core_title_texts)
    is_compilation_of_owned_titles = referenced_owned_titles >= 2
    if is_compilation_of_owned_titles:
        belongs_to_series = False

    return {
        "explicit_series_match": explicit_series_match,
        "partial_match": partial_match,
        "inferred_number_int": inferred_number_int,
        "continues_numbering": continues_numbering,
        "targeted_with_number": targeted_with_number,
        "is_universe_tie_in": is_universe_tie_in,
        "referenced_owned_titles": referenced_owned_titles,
        "is_compilation_of_owned_titles": is_compilation_of_owned_titles,
        "belongs_to_series": belongs_to_series,
    }


def _score_tier_c_shadow_response(
    response_text: str,
    *,
    gate_belongs_to_series: bool,
    gate_inferred_number_int: int | None,
) -> dict:
    """HTA Orchestrator Step 7: parses one Tier C shadow LLM response
    (`build_belongs_to_series_prompt`'s documented JSON shape -- see
    prompts.py) and scores it against the deterministic gate's
    already-computed decision for the same candidate. Per-run, in-memory
    only -- the caller (the shadow call site below) hands this straight
    to `DiscoveryTelemetry.record_tier_c_shadow_score`, which never
    persists across runs (see Step 7's architectural diff, section 5, for
    why cross-run/per-series accuracy is explicit future work, not this
    one).

    Fail-soft on a malformed/unparseable response, same convention as
    every other LLM response parse site in this codebase (see
    provider_io.py's CR-2 comment): returns `{"parsed_ok": False, ...}`
    with every other field `None` rather than raising, so a single bad
    shadow response can never sink the classification loop it's shadowing.

    Field-by-field scoring rationale (Step 7 architectural diff,
    clarification 1):
      - `belongs_to_series`/`inferred_number` both have a deterministic-
        gate counterpart, so both get a real agreement/disagreement bool.
        `inferred_number` agreement is exact-match (both `None` counts as
        agreement; one `None` and the other not counts as disagreement) --
        these are discrete series-position numbers, not a continuous
        quantity a fuzzy tolerance would make sense for.
      - `confidence` is scored relative to disagreement, not compared to
        anything on the gate (the gate has no confidence-grade concept of
        its own at this layer -- see `confidence_engine` for the
        separate, unrelated overall_grade this doesn't touch):
        `confidence_aligned` is only meaningful when Tier C disagreed
        with the gate on `belongs_to_series`, and asks whether Tier C's
        own self-reported confidence was "medium"/"high" for that
        disagreement (as opposed to "low", which would mean Tier C
        wasn't even confident in its own dissent). `None` when Tier C
        agreed (the question doesn't apply).
      - `is_alternate_title_of_known_book` is recorded only -- the gate
        has no alternate-title concept to compare against, so this is
        never folded into the agreement/disagreement metrics above.

    Step 8 ("Tier C Shadow Scoring Persistence + Promotion Path", section
    1.2) addition: also returns Tier C's own raw, normalized
    `belongs_to_series`/`inferred_number` values (`tier_c_belongs_to_
    series`/`tier_c_inferred_number`), not just the agreement booleans
    above. Needed so the shadow call site's persistence write and "live"
    promotion-state override can use Tier C's actual decision without
    re-parsing `response_text` a second time -- this function stays the
    single source of truth for everything derived from one shadow
    response (section 1.2's "do not re-implement scoring" rule extends to
    "do not re-parse the response", not just the comparison booleans).
    """
    text = (response_text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if not isinstance(parsed, dict):
        return {
            "parsed_ok": False,
            "belongs_to_series_agreement": None,
            "inferred_number_agreement": None,
            "tier_c_confidence": None,
            "confidence_aligned": None,
            "tier_c_alternate_title_flag": None,
            "tier_c_belongs_to_series": None,
            "tier_c_inferred_number": None,
        }

    tier_c_belongs_to_series = parsed.get("belongs_to_series")
    if not isinstance(tier_c_belongs_to_series, bool):
        tier_c_belongs_to_series = None
    belongs_to_series_agreement = (
        bool(tier_c_belongs_to_series) == bool(gate_belongs_to_series)
        if tier_c_belongs_to_series is not None
        else None
    )

    tier_c_inferred_number_int = discovery_engine._to_int_or_none(parsed.get("inferred_number"))
    inferred_number_agreement = tier_c_inferred_number_int == gate_inferred_number_int

    tier_c_confidence = parsed.get("confidence") if isinstance(parsed.get("confidence"), str) else None
    confidence_aligned = None
    if belongs_to_series_agreement is False and tier_c_confidence is not None:
        confidence_aligned = tier_c_confidence in ("medium", "high")

    tier_c_alternate_title_flag = parsed.get("is_alternate_title_of_known_book")
    if not isinstance(tier_c_alternate_title_flag, bool):
        tier_c_alternate_title_flag = None

    return {
        "parsed_ok": True,
        "belongs_to_series_agreement": belongs_to_series_agreement,
        "inferred_number_agreement": inferred_number_agreement,
        "tier_c_confidence": tier_c_confidence,
        "confidence_aligned": confidence_aligned,
        "tier_c_alternate_title_flag": tier_c_alternate_title_flag,
        "tier_c_belongs_to_series": tier_c_belongs_to_series,
        "tier_c_inferred_number": tier_c_inferred_number_int,
    }


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
        # PB-1: always present (never absent/None) so
        # services/series_check_engine.py's apply_skeleton_updates call has
        # a real, empty list to reason about instead of relying on
        # `result.get(...)` silently defaulting -- see
        # _needs_review_to_skeleton_updates for the populated case.
        "skeleton_updates": [],
        "probes": [],
        # Series Fingerprint system (see discovery_agentic_fingerprint_
        # recommendation.md): mirrors skeleton_updates' shape/rationale
        # above -- always present so services/series_check_engine.py's
        # apply_fingerprint_updates call has a real, empty-safe payload to
        # reason about rather than relying on result.get(...) to default.
        "fingerprint_updates": None,
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
        run_id: str | None = None,
        tier_c_shadow_allowed: bool = True,
    ) -> dict:
        """`run_id`/`tier_c_shadow_allowed` (Step 8, "Tier C Shadow
        Scoring Persistence + Promotion Path"): both optional and default
        to values that reproduce pre-Step-8 behavior exactly for any
        caller that doesn't pass them (same convention as `telemetry`/
        `cache` above).

        `run_id` is minted ONCE per Check Now job in `services.series_
        check_engine.run_series_check_job_full` (alongside `telemetry`),
        not regenerated per round -- see `models.ShadowLLMCall`'s
        docstring for why a job's multiple catch-up rounds must share one
        durable identifier. `None` here just means "no persisted
        shadow_llm_calls row will carry a real run_id" (falls back to a
        literal `"unknown"` at the persistence call site), which is fine
        for tests/call sites that don't care about cross-round grouping.

        `tier_c_shadow_allowed` is Mechanism B's cached per-job budget
        decision (`services.tier_c_shadow_store.check_tier_c_shadow_
        budget`), computed once at job start and passed in unchanged for
        every round -- defaults to `True` (allowed) so omitting it never
        silently disables Tier C shadow.
        """
        series = db.query(Series).filter(Series.id == series_id).first()
        if not series:
            result = _empty_result(None, None, "series-not-found")
            if emit_summary:
                log_discovery_summary(result=result, terminal_error="series-not-found")
            return result

        _console_log(f"CHECK NOW triggered for series: {series.name}")

        # RT-1b (Phase 1 agentic substrate, see agentic_hooks.py): a
        # turn-scoped trace covering this whole run_series_check call.
        # Side-channel only -- agentic_context is threaded through this
        # function purely for logging/telemetry; nothing below ever
        # branches on anything read back out of it.
        agentic_context = agentic_hooks.begin_turn(
            {
                "series_id": series.id,
                "series_name": series.name,
                "user_id": getattr(series, "profile_id", None),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "telemetry": telemetry,
            }
        )

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

            # DiscoveryType classification (Discovery Rules): derived from
            # owned-book count rather than the durable/in-memory skeleton --
            # neither is populated yet at this point in the run. A series
            # with 0 or 1 genuinely-owned books (excluding is_missing
            # placeholders) is still being built out, so interior-gap
            # missing-volume lookahead (see _reconstruct_series_skeleton
            # below) should run as it does today. Once the user owns 2+
            # books, this is an established series and Check Now should
            # behave as a new-release check -- targeted/author-fallback
            # passes still run as-is, but the aggressive gap-chasing
            # lookahead is skipped so it doesn't fire extra web-search
            # queries chasing old interior gaps on every routine check.
            owned_book_count = sum(1 for book in active_series_books if not bool(book.is_missing))
            discovery_type = "FULL_SERIES" if owned_book_count <= 1 else "NEW_RELEASE"
            enable_missing_volume_lookahead = discovery_type == "FULL_SERIES"
            _console_log(
                f"DiscoveryType={discovery_type} (owned_book_count={owned_book_count}) for series: {series.name}"
            )

            if not series_author:
                result = _empty_result(series.id, series.name, "series-missing-author")
                result["highest_owned_book_number"] = highest_owned_book_number
                agentic_hooks.record_reasoning_step(
                    agentic_context,
                    {"phase": "precheck", "decision": "stop", "reason": "series-missing-author"},
                )
                agentic_hooks.end_turn(agentic_context)
                if emit_summary:
                    log_discovery_summary(result=result, terminal_error="series-missing-author")
                return result

            known_series_titles, known_series_numbers, known_bare_titles = _build_series_identity_sets(active_series_books)
            known_title_number_keys = _build_known_title_number_keys(active_series_books)
            known_series_isbns = {
                str(book.isbn13 or "").strip() for book in active_series_books if str(book.isbn13 or "").strip()
            }
            owned_core_title_texts = _build_owned_core_title_texts(active_series_books)

            # Step 8 ("Tier C Shadow Scoring Persistence + Promotion
            # Path", section 2.2): read ONCE per run, not per candidate --
            # routing code only ever *reads* this state; the state itself
            # is written by a separate policy/analysis layer (explicit
            # Phase 8b future work), never computed inline here. Defaults
            # to "shadow_only" (see get_tier_c_promotion_state's
            # docstring), which reproduces pre-Step-8 behavior exactly.
            tier_c_state = tier_c_shadow_store.get_tier_c_promotion_state(db, series_id)["tier_c_state"]

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
            # explicitly tagged (via its own series_name_hint) as belonging
            # to a different series than this one, unconditionally (see
            # _is_cross_series_contamination there) -- while still allowing
            # it to surface anything new for *this* series.

            discovery = discovery_engine.discover_candidates_for_series(
                series.name,
                series_author,
                exclude_title_keys=author_owned_titles,
                progress_callback=progress_callback,
                highest_owned_book_number=highest_owned_book_number,
                telemetry=telemetry,
                cache=cache,
            )
            candidates = discovery["candidates"]
            provider_failures = discovery["provider_failures"]
            all_providers_failed = discovery["all_providers_failed"]

            # RT-1b: from this function's vantage point, discover_candidates_
            # for_series *is* the call to the provider stack -- it fans out
            # to the individual Serper/Apify/catalog adapters itself (see
            # provider_protocol.py), so this records one aggregate tool call
            # here rather than reaching into that module's internals, which
            # is out of scope for this instrumentation-only change.
            agentic_hooks.record_tool_call(
                agentic_context,
                "discovery_stack",
                f"{series.name} | {series_author}",
                {
                    "candidate_count": len(candidates),
                    "provider_failures": provider_failures,
                    "all_providers_failed": all_providers_failed,
                },
            )
            # PB-5: same scoping decision as RT-1b's record_tool_call just
            # above -- this is the one point in this file where "a provider
            # call" is observable at all (the individual Serper/Apify/
            # catalog adapter calls happen deep inside provider_protocol.py,
            # threaded across worker threads with no context parameter to
            # extend without a much larger structural change than a
            # diagnostics-only pass warrants). Tagged "shadow:" in
            # DiscoveryTelemetry so it's never conflated with RT-1b's own
            # tool_calls entry for the same invocation.
            agentic_hooks.shadow_probe(
                agentic_context,
                "discovery_stack",
                f"{series.name} | {series_author}",
                {
                    "candidate_count": len(candidates),
                    "provider_failures": provider_failures,
                    "all_providers_failed": all_providers_failed,
                },
            )
            agentic_hooks.record_reasoning_step(
                agentic_context,
                {
                    "phase": "provider_selection",
                    "chosen": "author_fallback" if discovery.get("used_author_fallback") else "targeted",
                    "reason": "primary_search_strategy",
                },
            )
            if telemetry is not None:
                # PB-9: how often a whole run comes back with genuinely no
                # usable provider data at all, for cost/quality comparison
                # against confidence-grade distribution and gate outcomes
                # recorded further down this function.
                telemetry.record_gate_outcome("all_providers_failed", "true" if all_providers_failed else "false")
                telemetry.record_gate_outcome(
                    "author_fallback", "triggered" if discovery.get("used_author_fallback") else "not_triggered"
                )

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
            fingerprint_updates_this_round: dict | None = None
            try:
                # Rebuilt here rather than just read, so a stale/missing
                # SeriesSkeleton row (e.g. between boot and the first Check
                # Now, or after this run's own persistence changed the
                # owned Book rows) never starves title_confidence of a
                # skeleton entry to compare against -- see
                # confidence_engine's own docstring on why "unverified"
                # caps rather than fails, and _title_confidence for the
                # "high" grade this starves without a populated skeleton.
                # This is now an asymmetric merge, not a destructive
                # rebuild (see skeleton_store.py's module docstring for the
                # full rule) -- library-sourced entries are rebuilt fresh
                # from Book rows every call, but any `discovered` entries
                # a future agentic pass writes survive it. Commits
                # internally (see `_upsert_skeleton_row`) rather than
                # sharing this function's own single db.commit() below --
                # required for its upsert-with-retry concurrency
                # protection to actually work, and safe here since nothing
                # above this point in the function has written anything
                # else to `db` yet.
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

                # Series Fingerprint system (discovery_agentic_fingerprint_
                # recommendation.md): resolved exactly once, here -- the
                # two-tier activation gate (settings.
                # FINGERPRINT_INFLUENCE_ENABLED + is_fingerprint_activated)
                # is checked only inside get_effective_fingerprint, never
                # by confidence_engine.py itself. None (gate closed, or no
                # fingerprint row yet) reproduces compute_confidence's
                # exact pre-fingerprint behavior.
                effective_fingerprint = get_effective_fingerprint(db, series_id)

                series_confidence = confidence_engine.compute_confidence(
                    series_id,
                    skeleton_entries,
                    unified_candidate_dicts,
                    series_delta,
                    series_name=series.name,
                    series_author=series_author,
                    # PB-5: shares RT-1b's same per-turn context so both
                    # tickets' traces land under one turn_id -- see that
                    # module's docstring for why shadow_context defaulting
                    # to None elsewhere changes nothing about this call.
                    shadow_context=agentic_context,
                    fingerprint=effective_fingerprint,
                )
                logger.info("series_confidence: %s", json.dumps(series_confidence, default=str))

                # Fingerprint Builder: pure, no DB access, computed from
                # this round's already-computed skeleton/delta/confidence
                # output alone (zero additional cost) -- threaded through
                # result["fingerprint_updates"] below to
                # services/series_check_engine.py, which is the only
                # caller of fingerprint_store.apply_fingerprint_updates
                # (post-persistence, same call site as
                # skeleton_store.apply_skeleton_updates). Computed
                # unconditionally, regardless of the activation gate above
                # -- "shadow-first": the fingerprint is always built, only
                # its *influence* on scoring is gated.
                fingerprint_updates_this_round = build_fingerprint_observations(
                    skeleton_entries,
                    series_delta,
                    series_confidence,
                    series_author=series_author,
                )
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
                enable_missing_volume_lookahead=enable_missing_volume_lookahead,
            )
            agentic_hooks.record_reasoning_step(
                agentic_context,
                {
                    "phase": "re_query",
                    "decision": "recovered_missing_volumes" if skeleton["recovered_numbers"] else "stop",
                    "missing_numbers": skeleton["missing_numbers"],
                    "recovered_numbers": skeleton["recovered_numbers"],
                },
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

            # missing_volume_recovery candidates (tagged just above, in the
            # skeleton re-merge) are the one case backfill above can't help
            # with: they typically already have *a* date -- just one an LLM
            # read off a raw web-search snippet for a single targeted "book
            # N" query, this pipeline's least reliable source for a hard
            # fact like a release date. Live regression (2026-08-24):
            # "Jonathan Hunt Thriller Series" Book 9 was recovered this way
            # with published_date misread as a full year after its real
            # release, wrongly landing it in upcoming_books instead of
            # available_missing below. See verify_missing_volume_recovery_
            # dates' own docstring for why this needs to actually override a
            # present-but-wrong date rather than only filling a blank one.
            try:
                discovery_engine.verify_missing_volume_recovery_dates(candidates, series_author)
            except Exception:
                logger.exception("missing-volume-recovery date verification failed for series_id=%s", series_id)

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

                # Belongs-to-series gate: extracted to evaluate_belongs_to_
                # series_gate (see that function's docstring for the full
                # rationale/regressions behind every branch below) so PB-5's
                # shadow trace and agents/agentic_series_agent.py's
                # deterministic shadow loop can call the exact same,
                # unmodified logic instead of duplicating it. Zero behavior
                # change from before this extraction -- same inputs, same
                # branching, just named and callable.
                # HTA Orchestrator Step 4: tier="C" -- belongs_to_series'
                # static tier binding. Telemetry-only for now: this scope
                # exists so per-tier call/duration data can eventually be
                # measured (see pass_scope()'s docstring), but no LLM call
                # is made inside it here -- evaluate_belongs_to_series_gate
                # below is the same deterministic, non-LLM logic as before
                # this scope was added. An actual LLM call inside this
                # boundary is explicit future work (Step 5/6), not this one.
                with maybe_pass_scope(telemetry, "belongs_to_series", tier="C"):
                    gate_result = evaluate_belongs_to_series_gate(
                        title=title,
                        inferred_number=inferred_number,
                        candidate_confidence=raw.get("confidence"),
                        series_name=series.name,
                        known_series_titles=known_series_titles,
                        owned_core_title_texts=owned_core_title_texts,
                        highest_owned_book_number=highest_owned_book_number,
                    )
                explicit_series_match = gate_result["explicit_series_match"]
                partial_match = gate_result["partial_match"]
                inferred_number_int = gate_result["inferred_number_int"]
                continues_numbering = gate_result["continues_numbering"]
                targeted_with_number = gate_result["targeted_with_number"]
                is_universe_tie_in = gate_result["is_universe_tie_in"]
                referenced_owned_titles = gate_result["referenced_owned_titles"]
                is_compilation_of_owned_titles = gate_result["is_compilation_of_owned_titles"]
                belongs_to_series = gate_result["belongs_to_series"]

                # PB-5: this is the belongs-to-series gate the Phase-1 plan
                # calls out -- computed here (and only here; no such gate
                # exists in services/series_check_engine.py), so the shadow
                # trace is wired at the real decision site rather than a
                # file the plan named that doesn't contain this logic. Both
                # dicts are built purely from values already computed above
                # -- this call cannot change belongs_to_series itself.
                agentic_hooks.shadow_gate_trace(
                    agentic_context,
                    inferred_number_int,
                    {
                        "title": title,
                        "explicit_series_match": explicit_series_match,
                        "partial_match": partial_match,
                        "continues_numbering": continues_numbering,
                        "targeted_with_number": targeted_with_number,
                        "is_universe_tie_in": is_universe_tie_in,
                        "is_compilation_of_owned_titles": is_compilation_of_owned_titles,
                    },
                    {"belongs_to_series": belongs_to_series},
                )

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
                    # LitRPG-discovery-plan addition: the real isbn13 (None,
                    # not the "source:source_id" fallback "identifier" above)
                    # so a needs_review candidate that reaches
                    # _needs_review_to_skeleton_updates can carry it through
                    # to skeleton_json -- see that function and
                    # services/skeleton_store.py's matching addition.
                    "isbn13": isbn13 or None,
                    # LitRPG Enhanced Discovery addition: real ASIN (not the
                    # isbn13-or-synthetic "identifier" above), captured by
                    # apify_provider.py and backfilled through multi-source
                    # fusion (see deterministic_fusion._fuse_and_score_
                    # candidates) -- threaded through so a candidate
                    # notification's Review action can offer an optional
                    # direct ASIN lookup.
                    "asin": raw.get("asin"),
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
                # "medium" only routes to needs_review when
                # low_confidence_ambiguous is True, i.e. belongs_to_series
                # itself couldn't confirm series membership. `overall_grade`
                # itself is never literally "unverified" -- that's only a
                # per-dimension title_confidence value that _overall_confidence
                # folds into an overall "medium" ceiling (see that function's
                # docstring) -- but the title dimension being "unverified" is
                # exactly why so many genuinely new books land here as
                # "medium" rather than "high" in the first place. For a
                # candidate that already passed belongs_to_series cleanly
                # (explicit title match, targeted-with-number, etc.), that's
                # not a red flag -- it is the *expected* state for
                # title_confidence on every genuinely new book: SeriesSkeleton
                # entries persist independent of ownership (a prior round's
                # discovered-but-not-yet-owned prediction can already be in
                # there), but a book nobody -- owner or agent -- has ever
                # seen before this run still has no skeleton entry at its
                # number to corroborate against (see
                # confidence_engine._title_confidence). Gating on it
                # unconditionally was verified live (Jonathan Hunt/Georgia
                # Wagner, 2026-08-22) to silently route every legitimate new
                # discovery to needs_review -- which at the time nothing
                # outside this function read (now also feeds
                # skeleton_updates via _needs_review_to_skeleton_updates,
                # PB-1) -- making "Check Now" report "no books found" even
                # when
                # discovery worked correctly. So a clean belongs_to_series
                # pass auto-accepts on medium/missing-confidence the same as
                # it always did before this feature existed; only the
                # genuinely ambiguous (belongs_to_series=False) case is
                # gated by confidence at all.
                confidence_entry = confidence_lookup.get(confidence_engine.correlation_key(raw))
                overall_grade = confidence_entry.get("overall") if confidence_entry else None
                if telemetry is not None:
                    telemetry.record_gate_outcome("confidence_grade", str(overall_grade or "none"))

                # HTA Orchestrator Step 5: Tier C shadow LLM call. Fires
                # ONLY when all four are true -- mirrors the live routing
                # predicate just below (low_confidence_ambiguous and
                # overall_grade in {"medium", None}) plus excluding the two
                # downgrade flags already computed by the gate above, so
                # this never fires on a candidate the gate already
                # confidently rejected as a universe tie-in or a
                # compilation of owned titles. A second, distinct
                # pass_scope from the "belongs_to_series" one that wraps
                # only the deterministic gate (see that scope's own
                # comment) -- kept separate so summary()["by_pass"] can
                # measure Tier C shadow cost in isolation from gate
                # evaluation, per the Step 5 architecture review's item 2/
                # Round 3-4 resolution. Placed here, not immediately after
                # the gate call, because the predicate needs overall_grade,
                # which isn't resolved until the confidence_lookup access
                # just above.
                #
                # Step 8 addition: `tier_c_shadow_allowed` is Mechanism
                # B's cached per-job budget decision (see this method's
                # own docstring) -- when the job started over budget, this
                # predicate can never be satisfied for the whole job,
                # exactly like the two downgrade flags already do per
                # candidate.
                #
                # In every state EXCEPT "live", this call still cannot
                # change belongs_to_series, overall_grade, or routing --
                # its result is only ever recorded (telemetry, and now
                # persisted via tier_c_shadow_store). In "live" state, see
                # the override below: it CAN change `low_confidence_
                # ambiguous` for this candidate, which is the only lever
                # that actually drives auto-accept vs needs_review/drop.
                tier_c_shadow_predicate = (
                    low_confidence_ambiguous
                    and (overall_grade == "medium" or overall_grade is None)
                    and not is_universe_tie_in
                    and not is_compilation_of_owned_titles
                    and tier_c_shadow_allowed
                )
                tier_c_score: dict | None = None
                tier_c_disagreement_payload: dict | None = None
                if tier_c_shadow_predicate:
                    with maybe_pass_scope(telemetry, "belongs_to_series_shadow_check", tier="C"):
                        candidate_id = isbn13 or f"{raw.get('source')}:{raw.get('source_id')}"
                        _console_log(
                            f"Tier C shadow triggered for candidate_id={candidate_id!r} (reason=ambiguity)"
                        )
                        # Provider metadata bundle: reuses the same
                        # correlation_key lookup already built above
                        # (confidence_entry) rather than a new lookup --
                        # confidence_entry["candidate"] is the PRE-filter
                        # UnifiedCandidate.model_dump(), which still has
                        # source_provenance intact (unlike `raw`, which
                        # finalize_discovery_output has already stripped
                        # it from by this point).
                        provider_metadata = (
                            (confidence_entry.get("candidate") or {}).get("source_provenance") or []
                            if confidence_entry
                            else []
                        )
                        sibling_candidates = [
                            {"title": other.get("title"), "number": other.get("series_number_hint")}
                            for other_index, other in enumerate(candidates)
                            if other_index != index
                        ]
                        tier_c_prompt = build_belongs_to_series_prompt(
                            title=title,
                            series_name=series.name,
                            inferred_number=inferred_number,
                            provider_metadata=provider_metadata,
                            known_series_titles=known_series_titles,
                            owned_core_title_texts=owned_core_title_texts,
                            highest_owned_book_number=highest_owned_book_number,
                            candidate_confidence=raw.get("confidence"),
                            reason_flags={
                                "explicit_series_match": explicit_series_match,
                                "partial_match": partial_match,
                                "continues_numbering": continues_numbering,
                                "is_universe_tie_in": is_universe_tie_in,
                                "is_compilation_of_owned_titles": is_compilation_of_owned_titles,
                            },
                            description=raw.get("description"),
                            sibling_candidates=sibling_candidates,
                        )
                        # Step 8, section 5.1: only "live" state gets an
                        # explicit timeout -- every other state keeps
                        # today's best-effort, no-timeout behavior
                        # unchanged (a slow shadow call in shadow_only/
                        # shadow_advisory costs latency, not correctness,
                        # since nothing downstream waits on its result to
                        # make a decision). "live" state's Tier C call sits
                        # on the actual decision path for this candidate,
                        # so it needs the same explicit bound every other
                        # live, user-visible outbound call in this
                        # codebase gets (see settings.TIER_C_LIVE_TIMEOUT_
                        # SECONDS's docstring) -- a timeout here raises
                        # LLMCallError exactly like any other provider
                        # failure, so it's handled by the same except
                        # clause below with no special-casing.
                        tier_c_call_timeout = (
                            settings.TIER_C_LIVE_TIMEOUT_SECONDS if tier_c_state == "live" else None
                        )
                        started_tier_c = time.monotonic()
                        tier_c_response = None
                        try:
                            tier_c_response = call_llm(
                                tier="C",
                                prompt=tier_c_prompt,
                                shadow=True,
                                max_tokens=500,
                                temperature=0,
                                timeout=tier_c_call_timeout,
                            )
                        except LLMCallError as exc:
                            # Fail-soft, same convention as every other LLM
                            # call site in this codebase (see provider_io.py's
                            # CR-2 comment) -- a shadow-only call must never
                            # sink a real Check Now run. In "live" state this
                            # is also exactly "Tier C unavailable" (Step 8,
                            # section 5.1) -- the override below only ever
                            # fires when tier_c_response is not None, so a
                            # timeout/failure here always falls back to the
                            # deterministic gate's own belongs_to_series,
                            # with no separate handling needed.
                            _console_log(f"Tier C shadow LLM call failed: {exc}")
                        finally:
                            if telemetry is not None:
                                telemetry.record_shadow_llm_call(
                                    duration_s=time.monotonic() - started_tier_c,
                                    tokens_in=tier_c_response.tokens_in if tier_c_response is not None else 0,
                                    tokens_out=tier_c_response.tokens_out if tier_c_response is not None else 0,
                                    # HTA Orchestrator Step 7: TIER_MODEL_MAP["C"]
                                    # is now a {"provider", "model_id"} dict, not
                                    # a bare model_id string -- prefer the
                                    # response's own resolved model_id when the
                                    # call succeeded, falling back to the tier's
                                    # mapped model_id on failure (mirrors every
                                    # other call site's "record a zero-token
                                    # entry attributed to what would have been
                                    # called" convention).
                                    model_id=(
                                        tier_c_response.model_id
                                        if tier_c_response is not None
                                        else TIER_MODEL_MAP["C"]["model_id"]
                                    ),
                                )
                        # HTA Orchestrator Step 7 / Step 8: scores the shadow
                        # response against the deterministic gate's already-
                        # computed belongs_to_series/inferred_number_int for
                        # this same candidate. Only attempted when the call
                        # actually succeeded; a failed call has nothing to
                        # score beyond the zero-token entry already recorded
                        # above.
                        #
                        # Deliberately NOT gated on `telemetry is not None`
                        # (Step 7's original gate) -- `maybe_pass_scope`'s own
                        # docstring guarantees "a caller that doesn't pass a
                        # DiscoveryTelemetry instance ... changes no
                        # behavior", and the "live" override below is real
                        # behavior, not observability. Scoring itself must
                        # not depend on whether telemetry happens to be
                        # attached; only *recording* it (below) does.
                        if tier_c_response is not None:
                            tier_c_score = _score_tier_c_shadow_response(
                                tier_c_response.text,
                                gate_belongs_to_series=belongs_to_series,
                                gate_inferred_number_int=inferred_number_int,
                            )
                            if telemetry is not None:
                                telemetry.record_tier_c_shadow_score(
                                    parsed_ok=tier_c_score["parsed_ok"],
                                    belongs_to_series_agreement=tier_c_score["belongs_to_series_agreement"],
                                    inferred_number_agreement=tier_c_score["inferred_number_agreement"],
                                    tier_c_confidence=tier_c_score["tier_c_confidence"],
                                    confidence_aligned=tier_c_score["confidence_aligned"],
                                    tier_c_alternate_title_flag=tier_c_score["tier_c_alternate_title_flag"],
                                )
                            # Step 8, section 1: persist this shadow call --
                            # own independent DB session (see tier_c_shadow_
                            # store's module docstring for why), fail-soft,
                            # never raises back into this loop. Cost is
                            # computed the same way record_shadow_llm_call
                            # already computes it (services.llm_pricing),
                            # just also captured here since that function
                            # doesn't return its own cost_usd.
                            tier_c_cost_usd = 0.0
                            tier_c_pricing = get_price_per_million(tier_c_response.model_id)
                            if tier_c_pricing is not None:
                                price_in, price_out = tier_c_pricing
                                tier_c_cost_usd = (
                                    tier_c_response.tokens_in * price_in
                                    + tier_c_response.tokens_out * price_out
                                ) / 1_000_000
                            tier_c_shadow_store.persist_tier_c_shadow_call(
                                series_id=series.id,
                                run_id=run_id or "unknown",
                                gate_belongs_to_series=belongs_to_series,
                                gate_inferred_number=inferred_number_int,
                                gate_confidence=overall_grade,
                                shadow_provider=TIER_MODEL_MAP["C"]["provider"],
                                shadow_model_id=tier_c_response.model_id,
                                shadow_belongs_to_series=tier_c_score["tier_c_belongs_to_series"],
                                shadow_inferred_number=tier_c_score["tier_c_inferred_number"],
                                shadow_confidence=tier_c_score["tier_c_confidence"],
                                shadow_is_alternate_title_of_known_book=tier_c_score[
                                    "tier_c_alternate_title_flag"
                                ],
                                parsed_ok=tier_c_score["parsed_ok"],
                                belongs_to_series_agreement=tier_c_score["belongs_to_series_agreement"],
                                inferred_number_agreement=tier_c_score["inferred_number_agreement"],
                                confidence_aligned=tier_c_score["confidence_aligned"],
                                prompt_tokens=tier_c_response.tokens_in,
                                completion_tokens=tier_c_response.tokens_out,
                                total_cost_usd=tier_c_cost_usd,
                            )

                # Step 8, section 4.3: "live" state makes Tier C the
                # primary decision path for the same ambiguous candidates
                # it already shadows in every other state -- it can never
                # fire for a candidate the predicate above wouldn't have
                # already selected. Only overrides when Tier C actually
                # produced a usable, parsed boolean; a failed call or an
                # unparseable response leaves low_confidence_ambiguous
                # exactly as the deterministic gate computed it (the
                # required fallback -- Step 8, section 4.3/5.1). This can
                # only ever flip an ambiguous candidate toward acceptance
                # (Tier C saying "yes it belongs"), never the reverse --
                # the predicate above only ever fires when the gate already
                # said belongs_to_series=False, mirroring the deterministic
                # gate's own one-directional design (see the "low_
                # confidence_ambiguous = not belongs_to_series" comment
                # above).
                if (
                    tier_c_state == "live"
                    and tier_c_score is not None
                    and tier_c_score["parsed_ok"]
                    and tier_c_score["tier_c_belongs_to_series"] is not None
                ):
                    low_confidence_ambiguous = not tier_c_score["tier_c_belongs_to_series"]

                # Step 8, section 4.2: "shadow_advisory" state never
                # changes routing -- it only annotates the candidate
                # notification a human is about to review (if this
                # candidate still reaches that branch below) with "Tier C
                # would have disagreed". Only meaningful on an actual
                # disagreement; agreement or an unscoreable/failed call
                # leaves this None, same as every prior state.
                if (
                    tier_c_state == "shadow_advisory"
                    and tier_c_score is not None
                    and tier_c_score["belongs_to_series_agreement"] is False
                ):
                    tier_c_disagreement_payload = {
                        "tier_c_belongs_to_series": tier_c_score["tier_c_belongs_to_series"],
                        "tier_c_confidence": tier_c_score["tier_c_confidence"],
                    }

                if overall_grade in ("low", "zero"):
                    # PB-11 diagnostic (Percy Jackson books 4/5 investigation,
                    # 2026-08-25): prints exactly which dimension(s) caused
                    # the drop and whether a skeleton entry exists at this
                    # number at all -- see confidence_engine.compute_confidence
                    # for what each dimension means. Only on the drop path
                    # itself so this stays silent for the common case.
                    skeleton_entry_here = _skeleton_entry_for_number(skeleton_entries, resolved_number)
                    entry_for_log = confidence_entry or {}
                    _console_log(
                        f"DROP low/zero-confidence candidate: title={title!r} number={resolved_number!r} "
                        f"overall={overall_grade!r} provider={entry_for_log.get('provider_confidence')!r} "
                        f"title_conf={entry_for_log.get('title_confidence')!r} "
                        f"number_conf={entry_for_log.get('number_confidence')!r} "
                        f"alignment={entry_for_log.get('series_alignment_confidence')!r} "
                        f"skeleton_entry_at_number={skeleton_entry_here!r}"
                    )
                    agentic_hooks.record_reasoning_step(
                        agentic_context,
                        {"phase": "routing", "decision": "drop", "confidence": overall_grade, "title": title},
                    )
                    continue

                if low_confidence_ambiguous and (overall_grade == "medium" or overall_grade is None):
                    # Same-author/different-series candidates with valid
                    # numbering can land here permanently -- confidence_engine
                    # has no series-identity dimension, so it can score
                    # "medium" on numbering/title alone even when the
                    # candidate is actually an unrelated book by the same
                    # author. series_name_hint (below) is the cheap,
                    # deliberate mitigation: it lets a human dismiss those
                    # at a glance without this module needing new scoring
                    # logic. This is an expected noise floor, not a bug.
                    #
                    # LitRPG Enhanced Discovery ("Review Candidate Book")
                    # replacement: this branch used to append to
                    # `needs_review`, which fed _needs_review_to_skeleton_
                    # updates and wrote an "unconfirmed" SeriesSkeleton
                    # entry every round until a human happened to notice it
                    # in a Check Now response. It now creates (or refreshes,
                    # on rediscovery) a durable series_candidate_notifications
                    # row instead -- see services/candidate_notifications.py
                    # -- and deliberately does NOT append to `needs_review`
                    # or write a skeleton entry: SeriesSkeleton stays
                    # unaware of this candidate until "Add to Series" is
                    # explicitly chosen (services/candidate_notifications.
                    # resolve_add_to_series calls backfill_skeleton_for_
                    # series itself at that point).
                    reason_flags: list[str] = []
                    # "Ambiguous number extraction": the number came from
                    # infer_number_from_title's text-parsing fallback, not a
                    # structured provider field -- book_number_source can't
                    # be used here (it's Book-row-only, hardcoded to
                    # "provider" for every discovery-persisted book; see
                    # services/series_check_engine.py).
                    if not raw.get("series_number_hint") and inferred_number:
                        reason_flags.append("number_inferred_from_title")
                    if not resolved_number:
                        reason_flags.append("missing_series_number")

                    candidate_notifications.create_or_refresh_candidate_notification(
                        db,
                        profile_id=series.profile_id,
                        series_id=series.id,
                        series_name=series.name,
                        # Not `canonical` as-is: canonical["title"] is
                        # `display_title`, which has a "(Series Name Book N)"
                        # suffix appended above for the auto-accept path's
                        # benefit (a persisted Book's title). The notification
                        # already carries series_name/candidate_number as
                        # their own fields (see CandidateNotificationItem),
                        # so baking that same suffix into candidate_title
                        # would both duplicate it in the UI and drift
                        # title_key/bare_title_key dedupe away from the raw
                        # provider title this candidate is actually keyed on
                        # everywhere else in this loop (already_known,
                        # known_series_titles, etc.).
                        canonical={**canonical, "title": title},
                        overall_confidence=overall_grade,
                        provider_confidence=(
                            confidence_entry.get("provider_confidence") if confidence_entry else None
                        ),
                        series_name_hint=raw.get("series_name_hint"),
                        reason_flags=reason_flags,
                        tier_c_disagreement=tier_c_disagreement_payload,
                    )
                    agentic_hooks.record_reasoning_step(
                        agentic_context,
                        {"phase": "routing", "decision": "needs_review", "confidence": overall_grade, "title": title},
                    )
                    continue

                # PB-11 fix (Percy Jackson books 4/5 investigation,
                # 2026-08-25): these sets must only grow on genuine
                # acceptance, not merely on "reached this point without
                # already being known" -- growing them earlier (right
                # after the already_known check, before confidence
                # grading) let a noisy/low-confidence duplicate for the
                # same real book (e.g. a title-only "Book Four" hit with
                # no structured series_number, scored number_confidence=
                # "low" and dropped below) poison known_series_numbers
                # *before* it was dropped, causing the very next
                # candidate -- the real, well-corroborated "The Battle of
                # the Labyrinth" -- to be wrongly treated as already_known
                # and silently swallowed before it ever reached confidence
                # grading. Different providers can still return the same
                # real book under differently-formatted titles within a
                # *single* check run (e.g. Hardcover's "Havoc in the
                # Deathyards, A Completionist Chronicles Short Story" vs
                # OpenLibrary's bare "Havoc in the Deathyards") -- growing
                # these sets only as candidates get accepted (here) still
                # lets the identity check above catch that on the very
                # next candidate, the same way it catches matches against
                # pre-existing owned books, without punishing a good
                # candidate for a bad one that arrived first.
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

                if is_upcoming:
                    upcoming_books.append(canonical)
                else:
                    available_missing.append(canonical)
                agentic_hooks.record_reasoning_step(
                    agentic_context,
                    {
                        "phase": "routing",
                        "decision": "accept",
                        "confidence": overall_grade,
                        "status": "upcoming" if is_upcoming else "available",
                        "title": title,
                    },
                )

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

            # PB-1: computed once here (rather than inline in `result`
            # below) purely so RT-1b's world-model-update trace and the
            # actual `skeleton_updates` result field are guaranteed to
            # describe the exact same list -- _needs_review_to_skeleton_
            # updates is a pure function of `needs_review`, so calling it
            # once vs. twice changes nothing about its output.
            skeleton_updates_this_round = _needs_review_to_skeleton_updates(needs_review)
            agentic_hooks.record_world_model_update(
                agentic_context,
                {
                    "series_id": series.id,
                    "books_changed": len(added_books),
                    "numbers_changed": sorted(
                        {
                            entry.get("book_number")
                            for entry in skeleton_updates_this_round
                            if entry.get("book_number") is not None
                        }
                    ),
                    "confidence_changes": [
                        {"book_number": entry.get("book_number"), "confidence": entry.get("confidence")}
                        for entry in skeleton_updates_this_round
                    ],
                },
            )

            # Phase 3 candidate promotion, extended in Phase 4 with
            # per-series activation and Phase 5 with a centralized
            # resolution layer (discovery_agentic_phase1_plan.md/
            # discovery_agentic_phase1_evaluation.md, not re-litigated
            # here): feature-flagged (settings.AGENTIC_ROUTING_ENABLED),
            # fail-soft, conditional use of the Phase 1 shadow loop's
            # per-book confidence/gate decisions in place of the live
            # skeleton's own -- for already-tracked books only (see
            # agents/agentic_series_agent.run_agentic_turn's own
            # docstring: its candidate_numbers come from the skeleton,
            # not from this round's newly-discovered candidates the loop
            # above just routed). Additive only: read-only inputs
            # (skeleton_entries, already loaded above), a purely local
            # promotion_outcome computation, one shadow-table write per
            # traced book (agentic.promotion_evaluator.
            # store_promotion_decision), and one new result key below --
            # never a write to SeriesSkeleton.skeleton_json/probes_json,
            # and never a change to available_missing/needs_review/
            # added_books above, regardless of outcome or activation.
            # With AGENTIC_ROUTING_ENABLED off, this block is not entered
            # at all: zero extra queries, byte-for-byte identical to
            # before this feature existed.
            #
            # Phase 5 note: evaluate_promotion + store_promotion_decision
            # (the decision authority + its shadow-table write) are
            # unchanged from Phase 3/4 -- still called once per traced
            # book, still happen whenever AGENTIC_ROUTING_ENABLED is on,
            # activated or not. What moved is *only* the "which side
            # wins" resolution step: instead of an inline per-book
            # `if series_is_activated and outcome == "use_agentic":
            # ... else: ...`, all traced books' decisions are collected
            # into `promotion_decisions` and resolved in one call to
            # agentic.resolution.resolve_routing_decisions,
            # which encapsulates both gates (AGENTIC_ROUTING_ENABLED,
            # settings.is_agentic_activated) that used to be checked by
            # hand here. Behavior is identical to Phase 4 -- not
            # activated: "record, don't apply" (resolved_* always live);
            # activated: "record AND apply" (resolved_* becomes agentic
            # exactly when the outcome was "use_agentic").
            #
            # Passes this call's own `db` through (unlike the Phase 2
            # dry-run block below, which deliberately does not -- see
            # its own comment) so `run_agentic_turn` reads the exact
            # same in-flight transaction's skeleton state rather than a
            # second, independent session.
            # Phase 8: one shared guard/result slot for this whole
            # run_series_check call, so the Phase 3/4/5 promotion block
            # below and the Phase 2 dry-run block further down (see each
            # block's own comment) invoke agents.agentic_series_agent.
            # run_agentic_turn at most once between them, whichever runs
            # first -- see _run_agentic_turn_guarded's own docstring.
            agentic_turn_state: dict = {}

            agentic_promotion_payload: dict = {"enabled": False, "activated": False, "promotions": []}
            if settings.AGENTIC_ROUTING_ENABLED:
                try:
                    from agentic.promotion_evaluator import evaluate_promotion, store_promotion_decision
                    from agentic.resolution import resolve_routing_decisions
                    from agents.agentic_series_agent import run_agentic_turn

                    series_is_activated = settings.is_agentic_activated(series_id)

                    promotion_context = {
                        "series_id": series_id,
                        "timestamp": datetime.utcnow().isoformat(),
                        "db": db,
                    }
                    promotion_trace = _run_agentic_turn_guarded(
                        run_agentic_turn, series_id, promotion_context, shared_state=agentic_turn_state
                    )

                    # Phase 6: normalize both trace lists into
                    # deterministic, book_number-ascending order before
                    # anything below reads them, so the decision-
                    # authority pass and its shadow-table writes are
                    # stable regardless of run_agentic_turn's own
                    # internal (provider/candidate) iteration order.
                    # Missing/malformed input fails soft to [] (see
                    # _sorted_agentic_trace_list's own docstring).
                    normalized_confidence_traces = _sorted_agentic_trace_list(
                        promotion_trace.get("confidence_traces")
                    )
                    normalized_gate_traces = _sorted_agentic_trace_list(promotion_trace.get("gate_traces"))

                    # Same live snapshot shape services/agentic_
                    # confidence_gate_store.py's dry-run wiring already
                    # pairs against (services/agentic_evaluation_
                    # harness.py's _observe_live_pipeline) -- reused here
                    # by hand rather than via that function's own DB
                    # query, since skeleton_entries above already read
                    # the exact same row this same call/transaction, and
                    # nothing has changed it since. Phase 6: wrapped in
                    # _sorted_book_number_dict so both snapshots are
                    # keyed by float book_number and iterate in
                    # ascending order regardless of skeleton_entries'
                    # own order.
                    live_confidence_snapshot = _sorted_book_number_dict(
                        {
                            entry.get("book_number"): {
                                "confidence": entry.get("confidence"),
                                "status": entry.get("status"),
                            }
                            for entry in (skeleton_entries or [])
                            if isinstance(entry, dict) and entry.get("book_number") is not None
                        }
                    )
                    live_gate_snapshot = _sorted_book_number_dict(
                        {
                            entry.get("book_number"): {
                                "belongs_to_series": True,
                                "source_class": entry.get("source_class", "library"),
                            }
                            for entry in (skeleton_entries or [])
                            if isinstance(entry, dict) and entry.get("book_number") is not None
                        }
                    )
                    agentic_gate_by_number = {
                        trace_entry["book_number"]: trace_entry
                        for trace_entry in normalized_gate_traces
                        if trace_entry.get("book_number") is not None
                    }

                    # Decision authority pass: unchanged from Phase 3/4
                    # -- one evaluate_promotion + store_promotion_
                    # decision call per traced book. Only the outcome
                    # (plus the live/agentic pair it was computed from)
                    # is kept here now, keyed by book_number, for the
                    # single resolve_routing_decisions call below.
                    # Phase 6: iterates normalized_confidence_traces (
                    # book_number-ascending), so promotion_decisions is
                    # itself built in ascending book_number order.
                    promotion_decisions: dict = {}
                    for confidence_trace_entry in normalized_confidence_traces:
                        promo_book_number = confidence_trace_entry.get("book_number")
                        if promo_book_number is None:
                            continue
                        promo_live_conf = live_confidence_snapshot.get(promo_book_number, {})
                        promo_agentic_conf = confidence_trace_entry.get("after", {})
                        promo_live_gate = live_gate_snapshot.get(promo_book_number, {})
                        promo_agentic_gate = agentic_gate_by_number.get(promo_book_number, {})

                        promo_outcome = evaluate_promotion(
                            promo_live_conf,
                            promo_agentic_conf,
                            promo_live_gate,
                            promo_agentic_gate,
                            series_id=series_id,
                            book_number=promo_book_number,
                        )
                        store_promotion_decision(
                            series_id,
                            promo_book_number,
                            promo_live_conf,
                            promo_agentic_conf,
                            promo_live_gate,
                            promo_agentic_gate,
                            promo_outcome,
                            db_session=db,
                        )

                        promotion_decisions[promo_book_number] = {
                            "outcome": promo_outcome,
                            "live_confidence": promo_live_conf,
                            "agentic_confidence": promo_agentic_conf,
                            "live_gate": promo_live_gate,
                            "agentic_gate": promo_agentic_gate,
                        }

                    # Phase 5's centralized resolution layer. Wrapped in
                    # its own try/except (belt-and-suspenders on top of
                    # resolve_routing_decisions' own internal fail-soft
                    # fallback) so that even an unexpected error *inside*
                    # that defensive fallback can't escape this block --
                    # reverting to the live snapshots verbatim either way.
                    try:
                        resolved_confidence, resolved_gate = resolve_routing_decisions(
                            series_id,
                            live_confidence_snapshot,
                            live_gate_snapshot,
                            promotion_decisions,
                        )
                    except Exception:
                        resolved_confidence = live_confidence_snapshot
                        resolved_gate = live_gate_snapshot

                    # Phase 6: promotion_decisions is already built in
                    # ascending book_number order (normalized_
                    # confidence_traces above), so this comprehension
                    # would already be sorted -- the explicit sorted()
                    # here is defense-in-depth, making the "promotions
                    # is book_number-ascending" guarantee hold even if
                    # that upstream ordering assumption ever changes.
                    promotions: list[dict] = sorted(
                        (
                            {
                                "book_number": promo_book_number,
                                "outcome": decision["outcome"],
                                "resolved_confidence": resolved_confidence.get(promo_book_number),
                                "resolved_gate": resolved_gate.get(promo_book_number),
                            }
                            for promo_book_number, decision in promotion_decisions.items()
                        ),
                        key=lambda entry: _agentic_safe_sort_key(entry["book_number"]),
                    )

                    agentic_promotion_payload = {
                        "enabled": True,
                        "activated": series_is_activated,
                        "promotions": promotions,
                    }
                except Exception:
                    logger.exception(
                        "run_series_check: Phase 3/4/5 candidate-promotion layer failed for series_id=%s; "
                        "continuing with live routing unaffected",
                        series_id,
                    )
                    agentic_promotion_payload = {"enabled": True, "activated": False, "promotions": [], "error": True}

            result = {
                "series_id": series.id,
                "series_name": series.name,
                "highest_owned_book_number": highest_owned_book_number,
                "agentic_promotion": agentic_promotion_payload,
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
                # PB-1: wire this round's needs_review evidence through to
                # skeleton_store.apply_skeleton_updates (called by
                # services/series_check_engine.py after persistence, once
                # per round). `probes` stays [] -- no probe schema exists
                # yet (Phase 1, see apply_skeleton_updates' docstring), so
                # nothing invents one here.
                "skeleton_updates": skeleton_updates_this_round,
                "probes": [],
                # Series Fingerprint system: wired through to
                # services/series_check_engine.py's post-persistence
                # apply_fingerprint_updates call the exact same way
                # skeleton_updates above already flows to
                # apply_skeleton_updates. None when the try/except above
                # failed (see the empty default set at this function's
                # top) -- fingerprint_store.apply_fingerprint_updates
                # treats None the same as an empty payload.
                "fingerprint_updates": fingerprint_updates_this_round,
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
            agentic_hooks.end_turn(agentic_context)

            # Phase 2 kickoff, dual execution mode (`discovery_agentic_
            # phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`,
            # not re-litigated here): run the Phase 1 shadow loop
            # (`agents/agentic_series_agent.run_agentic_turn`) once more,
            # in parallel with -- never in place of -- the live result
            # this function is about to return, and log the comparison
            # for later inspection (`/admin/agentic/dry-run/{series_id}`).
            # This does NOT change what `result` above already is; it
            # runs strictly after `result` is fully built and after
            # RT-1b's own `end_turn` above, purely as a side observation.
            #
            # Local imports (not module-level) are required, not just
            # stylistic: `agents/agentic_series_agent.py` and `services/
            # agentic_evaluation_harness.py` both import from this module
            # (`agents.series_agent`) already (see their own module
            # docstrings) -- a top-level import here would be a circular
            # import. Deferring to call time, well after both modules
            # have already finished loading, avoids that entirely.
            #
            # Same no-write, fail-soft guarantees as every other Phase 1/2
            # diagnostic: `run_agentic_turn` never writes skeleton_json/
            # probes_json or touches confidence/gate logic (see that
            # module's own docstring), `_observe_live_pipeline` is a pure
            # read, and any exception here -- from either call, or from
            # logging itself -- is caught and logged via `record_agentic_
            # dry_run` instead of ever propagating to this function's
            # caller or affecting `result`.
            #
            # Phase 8: `_run_agentic_turn_guarded` below shares
            # `agentic_turn_state` with the Phase 3/4/5 promotion block
            # above -- when `AGENTIC_ROUTING_ENABLED` is on, that block
            # already ran `run_agentic_turn` for this exact series_id/
            # turn, so this call reuses its trace instead of invoking
            # `run_agentic_turn` (and re-running its provider-adjacent
            # deterministic replay) a second time. Everything below this
            # point (the skeleton-preview/confidence/gate shadow writes,
            # `_observe_live_pipeline`, `record_agentic_dry_run`) still
            # runs exactly as before -- only the trace's origin changed.
            # When the flag is off (the promotion block never ran), this
            # is unaffected: `run_agentic_turn` still runs here, exactly
            # once, exactly as before Phase 8.
            from services.discovery_telemetry import record_agentic_dry_run

            try:
                from agents.agentic_series_agent import run_agentic_turn
                from services.agentic_evaluation_harness import _observe_live_pipeline
                from services.agentic_skeleton_preview_store import store_agentic_skeleton_preview

                # CI hermeticity fix: this used to omit "db", so whenever
                # AGENTIC_ROUTING_ENABLED is off (the default -- see the
                # promotion block above, which DOES pass its own `db`)
                # this call is the one that actually executes
                # run_agentic_turn, and it fell back to opening a brand
                # new SessionLocal() -- connecting to whatever real
                # DATABASE_PATH file happens to exist on disk, completely
                # unrelated to this run's own in-flight `db` transaction
                # (or, in a test, to the test's own isolated session).
                # That's harmless-by-luck on a dev machine whose local
                # books.db happens to have a row at the same id the
                # caller cares about, but produces a real, environment-
                # dependent divergence anywhere that coincidence doesn't
                # hold (a fresh CI checkout with no books.db at all: the
                # stray session's query returns no matching series, so
                # run_agentic_turn takes its early "series-not-found" exit
                # instead of ever finishing its shadow pass --
                # tests/test_agentic_hooks.py's
                # test_hooks_are_actually_invoked_during_a_real_run_
                # without_changing_the_result only ever caught this in CI,
                # never locally, for exactly that reason). run_agentic_turn
                # never writes through `db` (see its own docstring), so
                # sharing this call's live session is exactly as safe as
                # the promotion block already assumes it is.
                dry_run_context = {"series_id": series_id, "timestamp": datetime.utcnow().isoformat(), "db": db}
                agentic_trace = _run_agentic_turn_guarded(
                    run_agentic_turn, series_id, dry_run_context, shared_state=agentic_turn_state
                )

                # Phase 2 dual-write (services/agentic_skeleton_preview_
                # store.py): persist this turn's preview to the dedicated
                # shadow table, entirely separate from -- and never
                # touching -- SeriesSkeleton.skeleton_json. Guarded
                # separately from the rest of this block so a shadow-
                # write failure can't prevent the dry-run trace itself
                # from being logged below.
                try:
                    preview = agentic_trace.get("skeleton_merge_previews", {})
                    store_agentic_skeleton_preview(series_id, preview, db_session=db)
                except Exception:
                    logger.exception(
                        "run_series_check: storing agentic skeleton preview failed for series_id=%s; continuing",
                        series_id,
                    )

                live_snapshot = _observe_live_pipeline(series_id, db)

                # Phase 2 dual-write, final Phase 2 scaffolding block
                # (agentic/confidence_gate_store.py): persist
                # each traced book's live-vs-agentic confidence/gate pair
                # to their own dedicated shadow tables, entirely separate
                # from -- and never touching -- confidence_engine.py or
                # the live evaluate_belongs_to_series_gate logic. Guarded
                # separately from the rest of this block, same reasoning
                # as the skeleton-preview write above: a shadow-write
                # failure here can't prevent the dry-run trace itself
                # from being logged below.
                try:
                    from agentic.confidence_gate_store import store_agentic_confidence, store_agentic_gate

                    confidence_snapshot = live_snapshot.get("confidence_snapshot") or {}
                    for entry in agentic_trace.get("confidence_traces", []):
                        book_number = entry["book_number"]
                        live_conf = confidence_snapshot.get(str(book_number), {})
                        agentic_conf = entry
                        store_agentic_confidence(series_id, book_number, live_conf, agentic_conf, db_session=db)

                    gate_snapshot = live_snapshot.get("gate_snapshot") or {}
                    for entry in agentic_trace.get("gate_traces", []):
                        book_number = entry["book_number"]
                        live_gate = gate_snapshot.get(str(book_number), {})
                        agentic_gate = entry
                        store_agentic_gate(series_id, book_number, live_gate, agentic_gate, db_session=db)
                except Exception:
                    logger.exception(
                        "run_series_check: storing agentic confidence/gate decisions failed for "
                        "series_id=%s; continuing",
                        series_id,
                    )

                record_agentic_dry_run(
                    series_id,
                    {
                        "live_snapshot": live_snapshot,
                        "agentic_trace": agentic_trace,
                        "timestamp": dry_run_context["timestamp"],
                    },
                )
            except Exception as dry_run_exc:
                try:
                    record_agentic_dry_run(
                        series_id, {"error": str(dry_run_exc), "timestamp": datetime.utcnow().isoformat()}
                    )
                except Exception:
                    logger.exception(
                        "run_series_check: Phase 2 dry-run logging itself failed for series_id=%s; "
                        "continuing (live result is unaffected)",
                        series_id,
                    )

            if emit_summary:
                log_discovery_summary(result=result)
            return result
        except Exception as exc:
            agentic_hooks.record_reasoning_step(
                agentic_context,
                {"phase": "error", "decision": "stop", "reason": type(exc).__name__},
            )
            agentic_hooks.end_turn(agentic_context)
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
