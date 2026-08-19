"""Book discovery via official, public book-metadata APIs.

This replaces the old Amazon/Google HTML-scraping pipeline
(checker_core.py / checker_providers.py / checker_rules.py) as the primary
discovery data source. HTML scraping is inherently fragile -- sites detect
and block scrapers and change markup without notice -- while Google Books
and OpenLibrary are free, public JSON APIs intended for exactly this use
case, with no bot-blocking risk.

Strategy: query each API for "<series name> <author>" (a combined
free-text search, not a strict field filter) so the API's own relevance
ranking does the hard work of figuring out which of an author's books
belong to this series -- this is far more precise than pulling an author's
entire bibliography and guessing from title text alone, since many authors
(especially prolific indie/self-published authors) write multiple unrelated
series or standalone works. If that targeted search returns nothing, we
fall back to a plain author-bibliography sweep as a lower-confidence
last resort.

Note: Google Books' unauthenticated (no API key) quota is a small pool
shared globally across all callers without a key, so it may return 429s
even under light use. Set the GOOGLE_BOOKS_API_KEY environment variable
(free from Google Cloud Console) for reliable results -- OpenLibrary has
no such restriction and needs no key.
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Reused, not reimplemented -- these are the same edition-title-normalization
# and edition-priority-ranking heuristics services/series_check_engine.py
# already uses for its DB-write-path edition collapse (see
# _finalize_candidates), so a "which edition wins" decision means the same
# thing on both the discovery side and the persistence side.
from services.identity import _edition_priority, _normalize_title_for_identity

# Loaded here (rather than relying on the entry point having done it first)
# so this module reads the right API key regardless of import order.
load_dotenv()

GOOGLE_BOOKS_ENDPOINT = "https://www.googleapis.com/books/v1/volumes"
OPENLIBRARY_ENDPOINT = "https://openlibrary.org/search.json"
HARDCOVER_ENDPOINT = "https://api.hardcover.app/v1/graphql"
BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
REQUEST_TIMEOUT_SECONDS = 12.0
WEB_SEARCH_TIMEOUT_SECONDS = 20.0

# Structured APIs (Google Books/OpenLibrary/Hardcover) index catalog metadata,
# which lags behind for indie/self-published titles and pure announcements
# that only exist as blog posts, retailer pre-order pages, or author social
# posts. This provider fills that gap with a live web search whose raw,
# unstructured results are then normalized into the same candidate shape by
# a small LLM call, rather than trying to hand-write regexes/heuristics for
# every possible way a book announcement can be phrased across the web.
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
WEB_SEARCH_MAX_RESULTS = 8

# A generic "<series> <author>" search's relevance ranking skews heavily
# towards whichever entry has the most existing links/reviews -- almost
# always book 1 -- so a brand new release can fail to place in the top
# results even with a generous count (observed live: book 8 of a series
# appeared in a plain "<series> <author>" search, but book 9, an
# announced-but-not-yet-released preorder, did not, even at count=20).
# Explicitly searching "<series> book <N>" for the next few sequential
# numbers reliably surfaces it instead, so both query styles are combined.
WEB_SEARCH_LOOKAHEAD_BOOKS = 3

# When a candidate's first-pass query snippet doesn't include a release date,
# a second, title-specific "<title> release date" query surfaces
# date-focused pages (Goodreads, author sites, retailer detail pages, "new
# releases this week" roundups, etc.) far more often than the broader
# "<series> book N" query does -- observed live: a just-released book's
# generic listing had no date in its snippet and got wrongly defaulted to
# "upcoming" until this second look ran. Capped since it costs one extra
# Brave + Anthropic call per undated candidate.
WEB_SEARCH_DATE_REFINEMENT_MAX = 3

# Hardcover's own search index tags each hit with its position within a
# series (when it has one), which is a far more reliable source of a book's
# number than trying to parse it out of free-text title formatting -- so a
# result from this provider carries that as an explicit hint rather than
# leaving it to title-text inference.
_HARDCOVER_SEARCH_QUERY = """
query Search($query: String!, $perPage: Int!) {
  search(query: $query, query_type: "Book", per_page: $perPage) {
    results
  }
}
"""

# OpenLibrary (and, less aggressively, Google Books) apply basic
# bot-mitigation heuristics that can reject requests using generic HTTP
# client default user agents. A descriptive User-Agent identifying this
# app, per OpenLibrary's own guidance, avoids spurious 403s.
REQUEST_HEADERS = {"User-Agent": "BookAppSeriesTracker/1.0 (personal series-tracking tool)"}

# Titles that are almost never a new story entry in the series -- they
# bundle/repackage existing books rather than introduce a new one.
NON_NEW_RELEASE_TITLE_MARKERS = (
    "omnibus",
    "box set",
    "boxset",
    # "collection" deliberately excluded (regression, live bug): a brand
    # new companion release can legitimately be branded as a "collection"
    # of new short stories -- e.g. Rebecca Yarros's "Threshing Day (Wing
    # and Claw Collection)", a real September 2026 Empyrean release, not a
    # repackaging of already-published books. This marker was too broad;
    # a true bundle of existing content is already caught by the more
    # specific markers below (omnibus/box set/bundle/"complete series") or
    # by series_agent's is_compilation_of_owned_titles check, which flags a
    # candidate that spells out 2+ already-owned titles by name.
    "compilation",
    "anthology",
    "complete series",
    "bundle",
    "deluxe edition",
    "special edition",
    "collector's edition",
    "anniversary edition",
    "illustrated edition",
    "annotated edition",
    "extended edition",
    "author's cut",
    # Foreign-language editions -- the structured language field is often
    # missing on these records, so title text is the more reliable signal.
    "french edition",
    "spanish edition",
    "german edition",
    "italian edition",
    "portuguese edition",
    "dutch edition",
)

# Word-boundary patterns for non-new-release detection where a plain
# substring check would risk false positives (e.g. "tome" is also an
# ordinary English word meaning "a large book").
NON_NEW_RELEASE_TITLE_PATTERNS = (
    re.compile(r"\btome\s*\d*\b"),
    # "<Series Name> Series, Volume I" / "... Series Volume 1" -- a common
    # indie/legacy-publisher naming convention for a multi-book compilation
    # listing, distinct from "Volume 7" used as a standalone numbered entry.
    re.compile(r"\bseries,?\s+volume\b", re.IGNORECASE),
)

# A speculative "there will eventually be a book N, we just don't know its
# title yet" mention -- common in fan wikis/forums/roundups discussing an
# unannounced future release -- isn't a real, actionable book. Surfacing a
# literal "Untitled" entry as if it were a confirmed new release just adds
# noise with nothing a reader can act on (live regression: a web-search hit
# discussing fan speculation about a series' next book got structured by the
# LLM into a candidate literally titled "Untitled").
PLACEHOLDER_TITLE_MARKERS = (
    "untitled",
    "unannounced",
    "unnamed",
    "unconfirmed title",
    "tba",
    "tbd",
    "to be announced",
    "to be determined",
    "to be titled",
    "working title",
    "coming soon",
)


def looks_like_placeholder_title(title: str) -> bool:
    title_norm = normalize_text(title)
    if not title_norm:
        return True
    return any(marker in f" {title_norm} " for marker in (f" {m} " for m in PLACEHOLDER_TITLE_MARKERS))


# Generic, non-book suffixes publishers/cataloguers tack onto a bare series
# name for a series-level listing (an aggregation page, a boxed-set/imprint
# entity, an author-page grouping, etc.) rather than any single book --
# stripped before comparing a candidate's title (or a candidate's guessed
# series name) to a known series name so "<series> Universe"/"<series>
# Collection"/etc. are treated the same as the bare "<series>" name.
_SERIES_INDEX_SUFFIX_PATTERN = re.compile(r"\b(?:series|universe|collection|world|saga)\b")


def normalize_series_branding_name(name: str | None) -> str:
    """Strip generic branding words a cataloguer tacks onto a series name
    ("Universe", "Series", "Collection", ...) so two listings for the same
    tracked series don't fail to match over a single extra word (regression:
    an author-wide discovery pass guessed series name "Duchy of Terra
    Universe" for a book already owned under the tracked series "Duchy of
    Terra", and the exact-text comparison used elsewhere treated them as
    two different series, mislabeling an owned book "not yet tracked").

    Deliberately narrow: only strips *generic* words, never a distinctive
    proper-noun qualifier. "Starship's Mage: Red Falcon" and "Starship's
    Mage: UnArcana Rebellion" must NOT collapse to "Starship's Mage" here --
    those are real, distinct sub-series/rebranded editions, and conflating
    them was the exact cause of an earlier cross-series contamination bug.
    """
    stripped = _SERIES_INDEX_SUFFIX_PATTERN.sub("", normalize_text(name)).strip()
    return re.sub(r"\s+", " ", stripped).strip()


# Local duplicates of agents/series_agent.py's _token_set/_token_overlap_ratio
# rather than importing them -- series_agent.py imports this module, not the
# other way around, so importing back would be circular. Same semantics
# (normalize_text + split; overlap divided by the SMALLER set's size, not
# the union, so a short series name isn't unfairly penalized just for
# having fewer tokens than a longer, more descriptive variant).
def _token_set(value: str | None) -> set[str]:
    return {token for token in normalize_text(value).split() if token}


def _token_overlap_ratio(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, min(len(left), len(right)))


def _series_names_compatible(hint: str | None, target: str | None) -> bool:
    """True if `hint` and `target` plausibly name the same series, just
    branded/worded differently across providers (e.g. Hardcover's bare
    "Jonathan Hunt" vs. Google Books' "Jonathan Hunt Thriller Series") --
    as opposed to two genuinely different series that happen to share some
    text. Used anywhere a hint needs to be checked against a series
    identity, whether that's the target series being searched for
    (_is_cross_series_contamination, the Phase 2 scoring penalty) or another
    hint from the same candidate's own source_provenance
    (_candidate_has_provenance_disagreement).

    normalize_series_branding_name handles the common case (a generic
    suffix word like "Series"/"Universe" added or dropped). Token
    subset/overlap on top of that handles the harder case above, where the
    two names aren't a pure suffix difference -- one is just a shorter or
    more/less descriptive rendering of the other. Requiring at least 2
    overlapping tokens (not just a high ratio) guards against two short,
    unrelated series names being called "compatible" purely because they
    happen to share one common word (e.g. "Jonathan Hunt" vs. a
    hypothetical unrelated "Hunt for Red October" would otherwise clear a
    plain >=0.5 ratio on the single shared word "hunt" alone).
    """
    normalized_hint = normalize_series_branding_name(str(hint or ""))
    normalized_target = normalize_series_branding_name(str(target or ""))
    if not normalized_hint or not normalized_target:
        return False
    if normalized_hint == normalized_target:
        return True

    hint_tokens = _token_set(normalized_hint)
    target_tokens = _token_set(normalized_target)
    overlap = hint_tokens & target_tokens
    if len(overlap) < 2:
        return False

    if hint_tokens <= target_tokens or target_tokens <= hint_tokens:
        return True

    return _token_overlap_ratio(hint_tokens, target_tokens) >= 0.5


# Words that add no real distinguishing content to a title beyond restating
# "this is part of the series" -- a title that's just the series name plus
# some of these (e.g. "Jonathan Hunt Thriller Series Book 6") is exactly as
# much a non-book stub as the bare series name alone, just with extra filler
# stapled on. Deliberately does NOT include genre/descriptive words -- those
# are exactly the kind of real (if generic) content _title_is_series_variant
# must NOT treat as filler, or it would wrongly reject genuinely-titled books
# that happen to lead with the full series name (a common indie-catalog
# convention -- see _TITLE_SERIES_MARKER_PATTERN/_DASH_SERIES_MARKER_PATTERN
# elsewhere in this file for the same convention in reverse).
_TITLE_VARIANT_FILLER_TOKENS = {
    "a", "an", "the",
    "book", "books", "novel", "novella", "novellas", "novels",
    "vol", "vols", "volume", "volumes", "part", "parts", "no", "number", "numbers",
}


def _title_is_series_variant(
    title: str, series_name: str | None, isbn13: str | None, has_structured_number_hint: bool
) -> bool:
    """True if `title` is effectively just the series name -- an exact
    match, or a trivial variant of it ("A <Series> Thriller", "<Series>
    Book 6", "<Series> Novel") -- rather than a real, distinctly-titled
    book. Complements looks_like_series_index_entry: that function catches
    the bare, unadorned series name; this one catches the same underlying
    non-book stub with a little filler text stapled on, which slips past
    looks_like_series_index_entry's exact-form comparison (regression:
    "Check Now" on George Wagner's "Jonathan Hunt Thriller Series" admitted
    a candidate titled "A Jonathan Hunt Thriller" -- no ISBN, no real
    subtitle, nothing but the series' own name and a genre word -- as if it
    were a new, distinctly-titled book).

    has_structured_number_hint must come from a provider's own structured
    field (e.g. Hardcover's series_number_hint), NOT a number inferred from
    this same title's own text -- a "Book 6" parsed out of the very title
    being checked here isn't independent evidence of a real book, since
    that's exactly the filler text this function exists to see through. A
    real book's title-independent ISBN or structured series position is
    real corroborating evidence and should still short-circuit this (same
    guard looks_like_series_index_entry itself uses), so a legitimate,
    exactly-eponymous book 1 (e.g. "Mistborn" for "Mistborn", cataloged
    with an ISBN) is never caught here.
    """
    if isbn13 or has_structured_number_hint:
        return False
    normalized_title = normalize_series_branding_name(str(title or ""))
    normalized_series = normalize_series_branding_name(str(series_name or ""))
    if not normalized_title or not normalized_series:
        return False
    if normalized_title == normalized_series:
        return True

    title_tokens = _token_set(normalized_title)
    series_tokens = _token_set(normalized_series)
    overlap = title_tokens & series_tokens
    if len(overlap) < 2:
        return False

    # Tokens the title has beyond the series name itself -- if every one of
    # those is just generic filler (an article, "book"/"novel", a bare
    # number), the title carries no real distinguishing content at all and
    # is just as much a stub as an exact match. A title with even one real
    # word beyond the series name (a genuine subtitle, however short) is a
    # real, distinctly-titled book and must NOT be excluded here -- so this
    # is the sole determinant, not a secondary check alongside a raw
    # overlap-ratio threshold: a plain ratio would also flag a real title
    # that happens to fully restate the series name up front (e.g.
    # "<Series>: <Real Subtitle>"), since restating the whole series name
    # trivially maximizes overlap regardless of how substantial the rest of
    # the title is.
    unique_title_tokens = title_tokens - series_tokens
    meaningful_unique_tokens = {
        token for token in unique_title_tokens if token not in _TITLE_VARIANT_FILLER_TOKENS and not token.isdigit()
    }
    return not meaningful_unique_tokens


def looks_like_series_index_entry(
    title: str, series_name: str | None, isbn13: str | None, has_number_hint: bool
) -> bool:
    """Some catalog listings are for the series itself -- a Goodreads-style
    aggregation page, a boxed set cataloged under the bare series name, an
    author-page "series" entity, etc. -- rather than any single book in it
    (live regression: a search for "The Empyrean" by Rebecca Yarros returned
    separate records literally titled "The Empyrean" and "The Empyrean
    Series", both with no book number, that both passed through as if they
    were new, unread entries). Requiring the absence of *both* an ISBN and
    an explicit book-number hint keeps this from misfiring on a real,
    individually-cataloged eponymous book 1 (e.g. "Mistborn" for the
    "Mistborn" series), which will almost always carry at least one of those.
    """
    if isbn13 or has_number_hint:
        return False
    title_norm = normalize_text(title)
    series_norm = normalize_text(series_name)
    if not title_norm or not series_norm:
        return False
    stripped = normalize_series_branding_name(title)
    title_forms = {title_norm, stripped} if stripped else {title_norm}
    if series_norm in title_forms:
        return True
    # Second live regression on the same fix: a profile's tracked series was
    # auto-created from an imported spreadsheet's bare series column value
    # ("Empyrean"), while Google Books' own bare-series-listing records used
    # the full, article-carrying name ("The Empyrean" / "The Empyrean
    # Series") -- the same stub-listing pattern above, just missed because
    # the two sides disagreed on a leading "The". Comparing article-stripped
    # forms here is safe specifically because series_name is always the one
    # already-known series this check is evaluating a candidate against --
    # never a lookup across a profile's *other*, unrelated tracked series
    # (that broader ambiguity is why normalize_series_branding_name/
    # _strip_leading_article aren't merged for series matching in general;
    # see _strip_leading_article's docstring).
    series_no_article = _strip_leading_article(series_norm)
    title_forms_no_article = {_strip_leading_article(form) for form in title_forms}
    return bool(series_no_article) and series_no_article in title_forms_no_article


# Catches the common indie-publishing convention of naming a spin-off
# novella/short story after its parent series right in the subtitle (e.g.
# "Fae, Flames & Fedoras: A Changeling Blood Universe Novella") -- Google
# Books/OpenLibrary never populate a structured series field, so without
# this the book shows up as a bare standalone with no way to group it with
# the rest of its series.
_TITLE_SERIES_MARKER_PATTERN = re.compile(
    r":\s*a\s+(.+?)\s+(?:universe|series|saga|world)\s+(?:novella|short story|story|book|companion)\b",
    re.IGNORECASE,
)

# Another common cataloguer convention (seen heavily on Hardcover-style
# listings): "<Title> - <Series Name> #<N>", with no structured series
# field at all -- e.g. "A Little Too Close - Madigan Mountain #2",
# "Ignite - Legacy #0.7". Without reading this, a book whose *only*
# available listing uses this format never gets grouped with the rest of
# its series at all (regression: an author-wide discovery pass for Rebecca
# Yarros put every "Legacy" novella in "New standalone books" instead of
# forming a "Legacy" series group, since none of them had any other,
# more-structured listing to fall back on).
_DASH_SERIES_MARKER_PATTERN = re.compile(r"\s-\s+(.+?)\s*#\s*[\d.]+\s*$")


def infer_series_hint_from_title_text(title: str) -> str | None:
    raw = str(title or "")
    for pattern in (_TITLE_SERIES_MARKER_PATTERN, _DASH_SERIES_MARKER_PATTERN):
        match = pattern.search(raw)
        if match:
            guess = re.sub(r"\s+", " ", match.group(1)).strip()
            if guess:
                return guess
    return None


def clean_display_title(title: str) -> str:
    """Strips the redundant "- <Series Name> #<N>" suffix (see
    _DASH_SERIES_MARKER_PATTERN above) from a title meant for display.

    Without this, even after such a candidate is correctly grouped under
    its series and deduplicated against a cleaner-titled listing of the
    same book, whichever raw copy happened to win the dedup tie-break
    could still be the ugly "A Little Too Close - Madigan Mountain #2"
    version rather than the plain "A Little Too Close" a reader expects --
    the series name and number are already shown structurally elsewhere on
    the row, so repeating them in the title itself is pure clutter.
    """
    raw = str(title or "")
    match = _DASH_SERIES_MARKER_PATTERN.search(raw)
    if not match:
        return raw
    cleaned = raw[: match.start()].strip()
    return cleaned or raw


_PLACEHOLDER_DATE_PATTERN = re.compile(r"^\d{4}-01-01$")


def looks_like_placeholder_date(iso_date: str | None) -> bool:
    """A literal January 1st is the common "we only know the year" stand-in
    several catalogs use in place of a real, precise publication date --
    not evidence the book actually released on that exact day (live
    regression: an author-wide discovery pass showed several already-listed
    standalone titles with dates like 1/1/1900 and 1/1/2017 -- including the
    same 1/1/2017 on three unrelated titles -- displayed with the same
    confidence as a genuinely-dated release).
    """
    return bool(_PLACEHOLDER_DATE_PATTERN.match(str(iso_date or "").strip()))


def _log(message: str) -> None:
    print(f"[discovery_engine] {message}", flush=True)


def normalize_text(value: str | None) -> str:
    # "&" is treated as the word "and" (not just stripped to a space) so
    # "Muses & Melodies" and "Muses and Melodies" -- the same book, listed
    # under two spelling conventions by two different sources -- normalize
    # to identical text instead of silently differing by one word.
    with_and = re.sub(r"&", " and ", str(value or "").lower())
    cleaned = re.sub(r"[^a-z0-9\s]", " ", with_and)
    return re.sub(r"\s+", " ", cleaned).strip()


_LEADING_ARTICLE_PATTERN = re.compile(r"^(?:the|an?)\s+")


def _strip_leading_article(normalized_text: str) -> str:
    """Drops a leading "the"/"a"/"an" from an already-normalize_text'd
    title so "The Reality of Everything" and "Reality of Everything" --
    the same book, one source's title carrying the article and another's
    dropping it -- resolve to the same identity key.

    Deliberately NOT folded into normalize_text() itself: that function is
    also used for *series* names (see normalize_series_branding_name),
    where the article is part of the series' actual identity ("The
    Empyrean" needs to stay distinguishable from a hypothetical unrelated
    series just called "Empyrean") -- only book-title matching wants this.
    """
    return _LEADING_ARTICLE_PATTERN.sub("", normalized_text, count=1)


def _title_core_segment(raw: str) -> str:
    # ':', '(', ',' and a standalone " - " are the separators commonly used
    # to introduce a subtitle/series-suffix ("Title: subtitle", "Title
    # (Series Book N)", "Title, A Series Short Story", "Title - Series
    # #N"). Splitting on whichever comes first gives a stable "core title"
    # across differently-formatted sources -- e.g. Hardcover's "Havoc in
    # the Deathyards, A Completionist Chronicles Short Story" vs
    # OpenLibrary's bare "Havoc in the Deathyards" both reduce to "havoc in
    # the deathyards". The " - " split requires surrounding spaces
    # specifically so it never fires on a hyphenated word inside the title
    # itself (e.g. "Self-Made Superhero"). A few owned titles use a comma
    # as part of the actual title itself (e.g. "2 Lies, 2 Thrones",
    # "Arisen, Book Two - ..."), which this over-truncates -- but
    # core_title_key folds the book number (parsed from the *full* raw
    # title, not the truncated core) back in, which still keeps
    # same-series siblings distinct, and bare_title_key is only trusted
    # when its result is unique across the owned catalog. Both safeguards
    # absorb this.
    return re.split(r"[:,(]|\s+-\s+", raw, maxsplit=1)[0]


def core_title_key(title: str | None) -> str:
    """Titles in this app's library are often stored as
    "Core Title: (Series Name Book N)" while API results are usually just
    the bare "Core Title". Comparing on the text before the first subtitle
    separator gives a stable identity key across both shapes -- *except*
    for series that name every entry "<Series Name> (Volume N): <subtitle>"
    or similar, where the volume number itself lives inside that first
    separated segment. Truncating there would make every volume collapse
    to the exact same key (e.g. book 1 and book 4 both becoming just
    "1 lifesteal"), making it impossible to ever recognize a new volume as
    distinct from an owned one. To avoid that, fold any book/volume number
    found anywhere in the title into the key.
    """
    raw = str(title or "")
    normalized_core = _strip_leading_article(normalize_text(_title_core_segment(raw)))
    number = infer_number_from_title(raw)
    if number:
        return f"{normalized_core} {number}"
    return normalized_core


def bare_title_key(title: str | None) -> str:
    """Like core_title_key, but never folds in a book/volume number -- used
    as a fallback identity signal for candidates whose title carries no
    parseable number at all (e.g. a search result that comes back as just
    the bare book title "Crown" with no "(Series Book 9)" suffix and no
    other number hint). core_title_key can't recognize that as the already-
    owned "Crown: A LitRPG: (Unbound Book 9)" since one side folds in "9"
    and the other has no number to fold in. Callers should only trust this
    as an identity match when the candidate's own inferred number is empty
    (i.e. there's nothing more specific to compare) AND the bare key is
    unique across the owned catalog (so it can't conflate two different,
    numbered volumes that happen to share a one-word core title).
    """
    raw = str(title or "")
    return _strip_leading_article(normalize_text(_title_core_segment(raw)))


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}


def infer_number_from_title(title: str | None, series_name: str | None = None) -> int | None:
    # Checked against the raw (non-normalized) title first: normalize_text
    # strips punctuation like "#", so a "#7"-style pattern could never
    # actually match once run against the already-normalized text below.
    hash_match = re.search(r"#\s*(\d+)\b", str(title or ""))
    if hash_match:
        try:
            value = int(hash_match.group(1))
        except ValueError:
            value = 0
        if value > 0:
            return value

    cleaned = normalize_text(title)
    if not cleaned:
        return None
    patterns = (
        r"\bbook\s*(\d+)\b",
        r"\bvolume\s*(\d+)\b",
        r"\bvol\.?\s*(\d+)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if not match:
            continue
        try:
            value = int(match.group(1))
        except ValueError:
            continue
        if value > 0:
            return value

    # Some listings spell the number out ("Book One", "Volume Two") instead
    # of using a digit -- same intent, different formatting.
    word_pattern = r"\b(?:book|volume|vol\.?)\s+(" + "|".join(_WORD_NUMBERS) + r")\b"
    word_match = re.search(word_pattern, cleaned)
    if word_match:
        value = _WORD_NUMBERS.get(word_match.group(1))
        if value:
            return value

    # Many rapid-release indie/LitRPG series just number titles as
    # "<Series Name> <N>" with no "book"/"vol"/"#" keyword at all (e.g.
    # "All the Skills 5"). If the title starts with the series name
    # followed directly by a bare number, treat that as the entry number.
    series_norm = normalize_text(series_name)
    if series_norm and cleaned.startswith(series_norm):
        remainder = cleaned[len(series_norm):].strip()
        match = re.match(r"(\d+)\b", remainder)
        if match:
            try:
                value = int(match.group(1))
            except ValueError:
                value = 0
            if value > 0:
                return value

    # Same bare "<Series Name> <N>" pattern, but appearing anywhere in the
    # title rather than only as a strict prefix -- e.g. a reprint listing
    # titled "By Schism Rent Asunder (Safehold 2) Publisher: Tor..." embeds
    # the series-name-plus-number as a parenthetical rather than a prefix.
    if series_norm:
        anywhere_match = re.search(rf"\b{re.escape(series_norm)}\s+(\d+)\b", cleaned)
        if anywhere_match:
            try:
                value = int(anywhere_match.group(1))
            except ValueError:
                value = 0
            if value > 0:
                return value
    return None


_BUNDLE_TITLE_PATTERN = re.compile(r"\b\d+\s+books?\b")


def looks_like_non_new_release(title: str) -> bool:
    title_norm = normalize_text(title)
    if any(marker in title_norm for marker in NON_NEW_RELEASE_TITLE_MARKERS):
        return True
    if any(pattern.search(title_norm) for pattern in NON_NEW_RELEASE_TITLE_PATTERNS):
        return True
    return bool(_BUNDLE_TITLE_PATTERN.search(title_norm))


def is_english_or_unknown(language: str | None) -> bool:
    """This app's library is in English -- exclude editions we can
    positively identify as a different language (translations), but don't
    require the language field to be present since many entries lack one.
    """
    code = str(language or "").strip().lower()
    if not code:
        return True
    return code in {"en", "eng", "en-us", "en-gb"}


def parse_flexible_date(value: str | None) -> date | None:
    """Best-effort parse of Google Books / OpenLibrary date strings, which
    can be full dates, year-month, or just a year.
    """
    raw = str(value or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        try:
            return date.fromisoformat(f"{raw}-01")
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}", raw):
        try:
            return date(int(raw), 1, 1)
        except ValueError:
            return None
    return None


def classify_upcoming(parsed_date: date | None, upcoming_hint: bool | None) -> bool:
    """A candidate is treated as upcoming (not yet available to read) when
    either its known release date is still in the future, or -- when
    there's no date at all to compare -- a provider's own hint says it
    isn't out yet (e.g. Hardcover's `unreleased` flag, or the web-search
    safety net that defaults an undated result to "unconfirmed" rather than
    assuming it's already out). Extracted so both the single-series check
    (agents/series_agent.py) and author-wide discovery classify status the
    exact same way instead of each having their own copy of this rule.
    """
    if parsed_date:
        return parsed_date > date.today()
    return bool(upcoming_hint)


def split_author_names(value: str | None) -> list[str]:
    """Series in this app's library sometimes store multiple co-authors in
    one string (e.g. "J.N Chaney; Terry Maggert"). Split those apart so
    each name can be matched/queried individually -- APIs match one author
    name at a time and rarely list co-authors concatenated like that.
    """
    if not value:
        return []
    parts = re.split(r"\s*(?:;|,|&|\band\b)\s*", str(value), flags=re.IGNORECASE)
    return [p.strip() for p in parts if p and p.strip()]


def primary_author_name(value: str | None) -> str:
    names = split_author_names(value)
    return names[0] if names else str(value or "").strip()


def _author_matches(candidate_authors: list[str], target_author: str) -> bool:
    target_names = split_author_names(target_author) or [target_author]
    for target_name in target_names:
        target_tokens = [token for token in normalize_text(target_name).split() if len(token) > 1]
        if not target_tokens:
            continue
        for candidate in candidate_authors:
            candidate_norm = normalize_text(candidate)
            if all(token in candidate_norm for token in target_tokens):
                return True
    return False


def _fetch_google_books(query: str, max_results: int = 40) -> list[dict]:
    params: dict = {"q": query, "maxResults": max_results}
    api_key = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()
    if api_key:
        params["key"] = api_key
    response = httpx.get(GOOGLE_BOOKS_ENDPOINT, params=params, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    items = (response.json() or {}).get("items") or []
    results: list[dict] = []
    for item in items:
        info = item.get("volumeInfo") or {}
        title = str(info.get("title") or "").strip()
        if not title:
            continue
        subtitle = str(info.get("subtitle") or "").strip()
        full_title = f"{title}: {subtitle}" if subtitle else title
        identifiers = info.get("industryIdentifiers") or []
        isbn13 = next((i.get("identifier") for i in identifiers if i.get("type") == "ISBN_13"), None)
        results.append(
            {
                "source": "google_books",
                "source_id": item.get("id"),
                "title": full_title,
                "authors": info.get("authors") or [],
                "published_date": str(info.get("publishedDate") or "").strip(),
                "description": info.get("description"),
                "isbn13": str(isbn13 or "").strip() or None,
                "source_url": str(info.get("infoLink") or "").strip() or None,
                "language": str(info.get("language") or "").strip(),
            }
        )
    return results


def _fetch_openlibrary(query: str, max_results: int = 40) -> list[dict]:
    params = {"q": query, "limit": max_results}
    response = httpx.get(OPENLIBRARY_ENDPOINT, params=params, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    docs = (response.json() or {}).get("docs") or []
    results: list[dict] = []
    for doc in docs:
        title = str(doc.get("title") or "").strip()
        if not title:
            continue
        year = doc.get("first_publish_year")
        isbn_list = doc.get("isbn") or []
        languages = doc.get("language") or []
        results.append(
            {
                "source": "openlibrary",
                "source_id": doc.get("key"),
                "title": title,
                "authors": doc.get("author_name") or [],
                "published_date": str(year) if year else "",
                "description": None,
                "isbn13": next((i for i in isbn_list if len(str(i)) == 13), None),
                "source_url": f"https://openlibrary.org{doc.get('key')}" if doc.get("key") else None,
                "language": str(languages[0]) if languages else "",
            }
        )
    return results


def _fetch_hardcover(query: str, max_results: int = 25) -> list[dict]:
    api_key = os.environ.get("HARDCOVER_API_KEY", "").strip()
    if not api_key:
        return []

    headers = {**REQUEST_HEADERS, "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"query": _HARDCOVER_SEARCH_QUERY, "variables": {"query": query, "perPage": max_results}}
    response = httpx.post(HARDCOVER_ENDPOINT, json=payload, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    body = response.json() or {}
    if body.get("errors"):
        raise RuntimeError(str(body["errors"])[:300])

    hits = (((body.get("data") or {}).get("search") or {}).get("results") or {}).get("hits") or []
    results: list[dict] = []
    for hit in hits:
        doc = hit.get("document") or {}
        title = str(doc.get("title") or "").strip()
        if not title:
            continue

        isbns = doc.get("isbns") or []
        isbn13 = next((i for i in isbns if len(str(i)) == 13), None)

        featured_series = doc.get("featured_series") or {}
        series_position = None
        raw_position = featured_series.get("position")
        if raw_position is not None:
            try:
                # Rounding to the nearest int used to collapse Hardcover's
                # fractional positions (0.5-style companion novellas/side
                # stories, e.g. "Threshing Day" at position 3.5 in The
                # Empyrean) into a whole number -- and Python's round-half-
                # to-even rounds X.5 *up* whenever the next integer is even,
                # so position 3.5 became "Book 4", making a side story
                # indistinguishable from (and blocking/confusing detection
                # of) the real next numbered entry. Keep the float as-is so
                # downstream code can tell a companion book (non-integer
                # position) apart from a genuine next-in-sequence book.
                series_position = float(raw_position)
            except (TypeError, ValueError):
                series_position = None

        # Hardcover's own crowd-sourced series length -- a real answer to
        # "how many books are in this series" that doesn't depend on our own
        # search coverage. primary_books_count (numbered main entries) is
        # preferred over books_count (which also counts 0.5-style novellas/
        # side content) since it's the more intuitive "book N of M" figure;
        # falls back to books_count when that's the only one present.
        series_info = featured_series.get("series") or {}
        series_total_hint = series_info.get("primary_books_count") or series_info.get("books_count")
        try:
            series_total_hint = int(series_total_hint) if series_total_hint else None
        except (TypeError, ValueError):
            series_total_hint = None

        results.append(
            {
                "source": "hardcover",
                "source_id": doc.get("id"),
                "title": title,
                "authors": doc.get("author_names") or [],
                "published_date": str(doc.get("release_date") or "").strip(),
                "description": doc.get("description"),
                "isbn13": str(isbn13 or "").strip() or None,
                "source_url": f"https://hardcover.app/books/{doc.get('slug')}" if doc.get("slug") else None,
                "language": "",
                "series_number_hint": series_position,
                "upcoming_hint": bool(featured_series.get("unreleased")),
                # Only populated when Hardcover's own index actually ties
                # this book to a series -- used as a per-candidate series
                # name signal by discover_candidates_for_author, which (unlike
                # discover_candidates_for_series) has no single fixed series
                # name of its own to compare candidates against. The name
                # itself lives one level deeper than `position`/`unreleased`
                # (verified against a live API response) -- featured_series
                # is `{position, unreleased, series: {id, name, slug, ...}}`,
                # not a flat object with its own "name" key.
                "series_name_hint": str((featured_series.get("series") or {}).get("name") or "").strip() or None,
                "series_total_hint": series_total_hint,
            }
        )
    return results


def _fetch_brave_web_search(query: str, count: int = WEB_SEARCH_MAX_RESULTS) -> list[dict]:
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if not api_key:
        return []

    headers = {**REQUEST_HEADERS, "Accept": "application/json", "X-Subscription-Token": api_key}
    params = {"q": query, "count": count}
    response = httpx.get(BRAVE_SEARCH_ENDPOINT, params=params, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    hits = ((response.json() or {}).get("web") or {}).get("results") or []
    results: list[dict] = []
    for hit in hits:
        title = str(hit.get("title") or "").strip()
        url = str(hit.get("url") or "").strip()
        if not title or not url:
            continue
        results.append(
            {
                "title": title,
                "description": str(hit.get("description") or "").strip(),
                "url": url,
            }
        )
    return results


_WEB_SEARCH_STRUCTURING_PROMPT = """You are extracting structured book-release data from live web search results.

{scope_line}
Target author: "{author}"

Below are {count} web search results returned for this search. For EACH result that actually describes a specific book entry by {title_scope} (a released book, an upcoming/pre-order book, or a firm announcement of one) extract its data. Skip results that are: reviews or discussions of a book without any new release info,{skip_other_series} retailer category/search pages, fan wiki summaries of a whole series, news unrelated to a specific book, or fan speculation/discussion about a future book that has no confirmed title yet (e.g. only referred to as "the next book" or "an untitled sequel").

Search results:
{snippets}

A retailer listing existing (e.g. a Kindle Store page) is NOT proof a book has already been released -- pre-order listings look identical to a snippet with no date. If the snippet/title does not explicitly confirm a release date or that the book is already out, set "is_upcoming" to true and "published_date" to null rather than guessing it's already available -- it's far more useful to flag a book as "coming soon, exact date unconfirmed" than to wrongly tell a reader something is ready to read.

Respond with ONLY a JSON array (no prose, no markdown code fences). Each element must have this shape:
{{"result_index": <int, the [N] index above>, "title": <string, the clean book title without the series name or a "Book N" suffix>, "series_name": <string or null, the name of the series this book belongs to, if any -- null if it's a standalone>, "book_number": <int or null, this book's position in its series if stated or clearly implied>, "author_names": [<string>, ...], "published_date": <string, "YYYY-MM-DD"/"YYYY-MM"/"YYYY" if EXPLICITLY stated in the snippet, else null>, "is_upcoming": <bool, see rule above>, "isbn13": <string or null>}}

If none of the results are genuine matches, respond with exactly: []"""


def _structure_web_results_with_llm(series_name: str | None, author: str, raw_results: list[dict]) -> list[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or not raw_results:
        return []

    import anthropic

    snippets = "\n\n".join(
        f"[{i}] Title: {r['title']}\nSnippet: {r['description']}\nURL: {r['url']}"
        for i, r in enumerate(raw_results)
    )
    series_name = str(series_name or "").strip()
    if series_name:
        # Scoped to one specific series (the normal Check Now case) -- other
        # series by the same author are noise here, so explicitly excluded.
        scope_line = f'Target series: "{series_name}"'
        skip_other_series = " unrelated books by other authors, other series by the same author,"
        title_scope = "this series by this author"
    else:
        # Author-wide discovery has no single series to scope to -- every
        # series and standalone by this author is in scope, so the "other
        # series by the same author" exclusion from the scoped case would be
        # wrong here and is dropped.
        scope_line = "Target: ANY book by this author, across all of their series and standalone works."
        skip_other_series = " unrelated books by other authors,"
        title_scope = "this author"

    prompt = _WEB_SEARCH_STRUCTURING_PROMPT.format(
        scope_line=scope_line,
        author=author,
        count=len(raw_results),
        snippets=snippets,
        skip_other_series=skip_other_series,
        title_scope=title_scope,
    )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
        timeout=WEB_SEARCH_TIMEOUT_SECONDS,
    )
    text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text").strip()

    # The prompt asks for raw JSON, but strip markdown fences defensively in
    # case the model wraps its answer in one anyway.
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


_SERIES_OVERVIEW_PROMPT = """You are writing a short, spoiler-light overview of a book series for a reader deciding whether to start it.

Series: "{series_name}" by {author}

Below are the descriptions of books discovered in this series so far. Use ONLY this information -- do not invent plot details, characters, or facts not present in the text below.

{book_descriptions}

Write 2-4 sentences covering: the general premise/setting/genre, and what kind of reader would enjoy it. Do not describe it book-by-book or mention book numbers. Do not add a title or heading. Respond with ONLY the overview text, no prose before or after it."""


def generate_series_overview(series_name: str, author: str, books: list[dict]) -> str | None:
    """On-demand only (called from a "Series Overview" button click, never
    during discovery itself) -- synthesizes a short premise summary for a
    series the user doesn't own yet, from descriptions already fetched
    during discovery. Reuses the same Anthropic client/model as the
    web-search structuring pass rather than adding new LLM infrastructure.

    Deliberately does not re-fetch anything: the caller passes in whatever
    descriptions the discovery pass already returned, keeping this to a
    single LLM call with no additional catalog API cost.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None

    usable_books = [b for b in books if str(b.get("description") or "").strip()]
    if not usable_books:
        return None

    import anthropic

    book_descriptions = "\n\n".join(
        f"- {str(b.get('title') or 'Untitled').strip()}: {str(b.get('description') or '').strip()}"
        for b in usable_books
    )
    prompt = _SERIES_OVERVIEW_PROMPT.format(
        series_name=series_name or "this series",
        author=author or "this author",
        book_descriptions=book_descriptions,
    )

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
        timeout=WEB_SEARCH_TIMEOUT_SECONDS,
    )
    text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text").strip()
    return text or None


def _refine_undated_web_search_result(result: dict, series_name: str, author: str) -> dict:
    """Best-effort second look for a candidate the first pass couldn't date:
    re-searches specifically for "<title> release date" and re-runs the same
    LLM structuring on those results. If a date turns up, the downstream
    upcoming-vs-available classification (which compares the real date to
    today) takes over from the conservative "no date -> upcoming" guess.
    Any failure here just leaves the original candidate as-is.
    """
    title = str(result.get("title") or "").strip()
    if not title:
        return result

    try:
        # Live regression: quoting just the bare title as an exact phrase
        # (the previous version of this query) gets swamped for a common
        # title -- "Here We Go Again" is also a Demi Lovato song/album, a
        # movie, a TV series, etc., none of which are this book. Adding the
        # series name and author as unquoted extra terms (soft ranking
        # signals, not exact-phrase requirements) reliably surfaced the
        # actual author's release-announcement blog post instead.
        query = " ".join(part for part in (title, series_name, author, "release date") if part)
        raw = _fetch_brave_web_search(query)
    except Exception:
        return result
    if not raw:
        return result

    try:
        structured = _structure_web_results_with_llm(series_name, author, raw)
    except Exception:
        return result

    for item in structured:
        if not isinstance(item, dict):
            continue
        published_date = str(item.get("published_date") or "").strip()
        if published_date:
            refined = dict(result)
            refined["published_date"] = published_date
            refined["upcoming_hint"] = bool(item.get("is_upcoming"))
            return refined

    return result


def _fetch_web_search(queries: list[str], series_name: str | None, author: str) -> list[dict]:
    raw_results: list[dict] = []
    seen_urls: set[str] = set()
    query_errors: list[Exception] = []
    for query in queries:
        try:
            items = _fetch_brave_web_search(query)
        except Exception as exc:  # one query's transient failure shouldn't sink the others
            query_errors.append(exc)
            continue
        for item in items:
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            raw_results.append(item)

    if not raw_results:
        if query_errors and len(query_errors) == len(queries):
            raise query_errors[0]
        return []

    structured = _structure_web_results_with_llm(series_name, author, raw_results)

    results: list[dict] = []
    for item in structured:
        if not isinstance(item, dict):
            continue
        try:
            source = raw_results[int(item.get("result_index"))]
        except (TypeError, ValueError, IndexError):
            continue

        title = str(item.get("title") or "").strip()
        if not title:
            continue

        book_number = item.get("book_number")
        try:
            book_number = int(book_number) if book_number is not None else None
        except (TypeError, ValueError):
            book_number = None

        author_names = item.get("author_names")
        if not isinstance(author_names, list) or not author_names:
            author_names = [author]

        published_date = str(item.get("published_date") or "").strip()
        # Belt-and-suspenders on top of the prompt's own instruction: a
        # retailer/store listing existing is not proof a book is actually
        # out yet (pre-orders look identical), so if the model didn't
        # extract an explicit date, treat it as not-yet-confirmed-available
        # regardless of what it set "is_upcoming" to -- safer to under- than
        # over-claim availability here.
        upcoming_hint = bool(item.get("is_upcoming")) or not published_date

        results.append(
            {
                "source": "web_search",
                "source_id": source["url"],
                "title": title,
                "authors": [str(a) for a in author_names if str(a).strip()],
                "published_date": published_date,
                "description": source.get("description"),
                "isbn13": str(item.get("isbn13") or "").strip() or None,
                "source_url": source["url"],
                "language": "",
                "series_number_hint": book_number,
                "upcoming_hint": upcoming_hint,
                # The LLM's own guess at which series (if any) this result
                # belongs to -- used as a per-candidate series-name signal by
                # discover_candidates_for_author (see series_name_hint on the
                # Hardcover provider for the same purpose).
                "series_name_hint": str(item.get("series_name") or "").strip() or None,
            }
        )

    refinements_used = 0
    for index, entry in enumerate(results):
        if entry.get("published_date"):
            continue
        if refinements_used >= WEB_SEARCH_DATE_REFINEMENT_MAX:
            break
        refinements_used += 1
        results[index] = _refine_undated_web_search_result(entry, series_name, author)

    return results


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
        has_structured_number_hint = bool(raw.get("series_number_hint"))
        if looks_like_series_index_entry(
            title, effective_series_name, isbn13, has_number_hint
        ) or _title_is_series_variant(title, effective_series_name, isbn13, has_structured_number_hint):
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
                "confidence": confidence,
                "series_name_hint": series_name_hint,
                "series_total_hint": raw.get("series_total_hint"),
            }
        )
    return merged


def _fetch_all_providers_parallel(
    query_author: str,
    series_name: str | None,
    targeted_query_text: str,
    highest_owned_book_number: int | None,
    *,
    author: str,
    openlibrary_query: str | None = None,
    web_search_queries: list[str] | None = None,
    enable_web_search: bool = True,
) -> dict:
    """Fetch Google Books, OpenLibrary, Hardcover, and (optionally) the
    Brave+LLM web-search provider concurrently instead of one after another.

    This only changes *how* the four provider calls are issued (threaded
    instead of sequential) -- it does not change what gets sent to each
    provider, nor how each provider's own errors/timeouts are handled.
    Every _fetch_* function keeps its own REQUEST_TIMEOUT_SECONDS/
    WEB_SEARCH_TIMEOUT_SECONDS and lets its own exceptions propagate exactly
    as before; this function just catches whichever one propagates from
    each thread so a single slow/failing provider can't block -- or crash --
    the others, mirroring the previous per-provider try/except blocks.

    Default query construction below reproduces
    discover_candidates_for_series's targeted (primary) pass exactly:
    Google gets `"<series>" inauthor:"<author>"` (or plain `inauthor:` with
    no series name), OpenLibrary/Hardcover both get the bare
    `targeted_query_text`, and the web-search query list is the
    lookahead-aware "<series> book <N>" set gated on highest_owned_book_number.
    Callers whose query shape genuinely differs -- the author-bibliography
    fallback pass and discover_candidates_for_author's plain author-wide
    sweep, both of which use OpenLibrary's `author:"<name>"` field query and
    (for the latter) a different web-search query -- pass
    openlibrary_query/web_search_queries explicitly to reproduce their own
    existing query text unchanged; the fallback pass also passes
    enable_web_search=False since it never queries web search at all.

    Returns {"google": [...], "openlibrary": [...], "hardcover": [...],
    "web": [...]} exactly like the four raw lists the sequential calls used
    to produce, plus "_failures": {provider_key: exception} for any provider
    whose call raised -- callers use that to build the same
    provider_failures/any_provider_succeeded bookkeeping they always have,
    unchanged.
    """
    google_query = f'"{series_name}" inauthor:"{query_author}"' if series_name else f'inauthor:"{query_author}"'
    resolved_openlibrary_query = openlibrary_query if openlibrary_query is not None else targeted_query_text
    hardcover_query = targeted_query_text

    if web_search_queries is not None:
        resolved_web_queries = web_search_queries
    else:
        resolved_web_queries = [targeted_query_text] if targeted_query_text else []
        if series_name and highest_owned_book_number:
            lookahead_author = f" {query_author}" if query_author else ""
            resolved_web_queries += [
                f'"{series_name}"{lookahead_author} book {number}'
                for number in range(
                    highest_owned_book_number + 1, highest_owned_book_number + 1 + WEB_SEARCH_LOOKAHEAD_BOOKS
                )
            ]

    run_web_search = bool(
        enable_web_search
        and resolved_web_queries
        and os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
        and os.environ.get("ANTHROPIC_API_KEY", "").strip()
    )

    tasks: dict[str, tuple] = {
        "google": (_fetch_google_books, (google_query,)),
        "openlibrary": (_fetch_openlibrary, (resolved_openlibrary_query,)),
        "hardcover": (_fetch_hardcover, (hardcover_query,)),
    }
    if run_web_search:
        tasks["web"] = (_fetch_web_search, (resolved_web_queries, series_name, author))

    results: dict[str, list[dict]] = {"google": [], "openlibrary": [], "hardcover": [], "web": []}
    failures: dict[str, Exception] = {}

    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_to_provider = {executor.submit(func, *args): provider for provider, (func, args) in tasks.items()}
        for future, provider in future_to_provider.items():
            try:
                results[provider] = future.result()
            except Exception as exc:  # one provider's failure shouldn't sink the others
                failures[provider] = exc

    results["_failures"] = failures
    return results


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
    """
    ordered_raw: list[dict] = [
        *(provider_results.get("hardcover") or []),
        *(provider_results.get("google") or []),
        *(provider_results.get("openlibrary") or []),
        *(provider_results.get("web") or []),
    ]

    groups: dict[str, list[dict]] = {}
    group_order: list[str] = []
    for raw in ordered_raw:
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        isbn13 = str(raw.get("isbn13") or "").strip()
        identity_key = isbn13 or core_title_key(title) or normalize_text(title)
        if identity_key not in groups:
            groups[identity_key] = []
            group_order.append(identity_key)
        groups[identity_key].append(raw)

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

        completeness_values = {
            "title": primary.get("title"),
            "authors": merged_authors,
            "series_name_hint": merged_series_name_hint,
            "series_number_hint": merged_series_number_hint,
            "isbn13": merged_isbn13,
            "published_date": merged_published_date,
            "description": merged_description,
        }
        present_count = sum(
            1
            for field in _METADATA_COMPLETENESS_FIELDS
            for value in (completeness_values.get(field),)
            if (value if not isinstance(value, list) else bool(value)) not in (None, "", False)
        )
        metadata_completeness_score = round(present_count / len(_METADATA_COMPLETENESS_FIELDS), 4)

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


# Bounds how many gap numbers _reconstruct_series_skeleton will fire a
# targeted Brave lookahead query for in one call -- a series with a large
# number of gaps (e.g. only book 1 and book 12 are owned/found) would
# otherwise turn into a double-digit number of live web searches in a
# single Check Now run. All still batched into one _fetch_web_search call
# (one shared Brave loop + one LLM structuring pass), same as the existing
# highest-owned-number lookahead in _fetch_all_providers_parallel.
MAX_MISSING_VOLUME_LOOKAHEAD_QUERIES = 6


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


def _reconstruct_series_skeleton(
    unified_candidates: list["UnifiedCandidate"],
    owned_books: list[dict],
    *,
    series_name: str | None = None,
    author: str | None = None,
) -> dict:
    """Infers how many volumes a series is expected to have -- the highest
    integer book number seen anywhere, across owned_books' book_number,
    each unified_candidate's own fused series_number, and (for whichever
    candidates have neither) a title-text inference pass -- builds the full
    1..N "skeleton" of expected volume numbers, and identifies which of
    those numbers has no owned book AND no discovered candidate at all.

    For each such gap (up to MAX_MISSING_VOLUME_LOOKAHEAD_QUERIES), fires a
    targeted Brave+LLM lookahead query ("<series> <author> book <N>")
    specifically for that missing number. This is more surgical than the
    generic targeted "<series> <author>" query or the highest-owned-number-
    only lookahead _fetch_all_providers_parallel already does: it can
    recover a gap buried in the *middle* of an otherwise-complete series
    (e.g. owns/found 1-4 and 6-9 but never found 5), not just a gap at the
    very end.

    Only integer-numbered volumes count toward the skeleton -- a companion
    0.5/3.5-style novella is real content but isn't a "volume" in the
    sequential numbering sense a skeleton like this reconstructs (mirrors
    _fetch_hardcover's own reasoning for keeping series_position as a float
    instead of rounding it).

    Any newly-recovered candidates are fused back into unified_candidates
    via _fuse_and_score_candidates (existing candidates given top priority,
    so a lookahead hit can only *backfill* an already-found candidate, not
    override it) rather than replacing it outright.

    series_name/author are optional and, when omitted, inferred from
    unified_candidates' own series_name/authors fields -- series_agent.py,
    the real caller, always has both on hand directly (series.name/
    series_author) and passes them explicitly instead of relying on this
    fallback, which exists mainly so this function still does something
    sensible if unified_candidates is empty of any usable hint.

    Returns {"candidates": [...], "expected_total": int | None,
    "missing_numbers": [...], "recovered_numbers": [...]}. "candidates" is
    unified_candidates with any newly-recovered volumes fused in; when
    there's nothing missing, no resolvable series_name/author, no resolvable
    expected total at all, or web search isn't configured
    (BRAVE_SEARCH_API_KEY/ANTHROPIC_API_KEY), it's returned unchanged.
    """
    resolved_series_name = series_name or next((c.series_name for c in unified_candidates if c.series_name), None)
    resolved_author = author or next(
        (candidate_author for c in unified_candidates for candidate_author in c.authors if candidate_author), None
    )

    known_numbers: set[int] = set()
    for book in owned_books:
        number = book.get("book_number")
        try:
            if number is not None and float(number).is_integer():
                known_numbers.add(int(float(number)))
        except (TypeError, ValueError):
            continue
    for candidate in unified_candidates:
        number = _resolve_candidate_number(candidate, resolved_series_name)
        if number is not None and float(number).is_integer():
            known_numbers.add(int(number))

    def _result(missing_numbers: list[int], recovered_numbers: list[int], candidates: list["UnifiedCandidate"]) -> dict:
        return {
            "candidates": candidates,
            "expected_total": max(known_numbers) if known_numbers else None,
            "missing_numbers": missing_numbers,
            "recovered_numbers": recovered_numbers,
        }

    if not known_numbers:
        return _result([], [], unified_candidates)

    expected_total = max(known_numbers)
    missing_numbers = sorted(set(range(1, expected_total + 1)) - known_numbers)

    if (
        not missing_numbers
        or not resolved_series_name
        or not resolved_author
        or not (os.environ.get("BRAVE_SEARCH_API_KEY", "").strip() and os.environ.get("ANTHROPIC_API_KEY", "").strip())
    ):
        return _result(missing_numbers, [], unified_candidates)

    targeted_missing = missing_numbers[:MAX_MISSING_VOLUME_LOOKAHEAD_QUERIES]
    lookahead_queries = [f'"{resolved_series_name}" {resolved_author} book {number}' for number in targeted_missing]

    try:
        lookahead_raw = _fetch_web_search(lookahead_queries, resolved_series_name, resolved_author)
    except Exception as exc:  # a lookahead failure should never sink the candidates already found
        _log(f"missing-volume lookahead failed: {exc}")
        return _result(missing_numbers, [], unified_candidates)

    if not lookahead_raw:
        return _result(missing_numbers, [], unified_candidates)

    # Existing candidates fed in under the highest-priority bucket key so
    # fusion's own backfill logic treats a lookahead hit as a supplement to
    # an already-found candidate (when it matches one by identity) rather
    # than a competing, potentially-lower-quality duplicate -- see
    # _fuse_and_score_candidates. Each entry's own "source" field (already
    # baked in via _unified_candidate_to_raw_dict) is untouched by which
    # bucket key it's passed under here.
    existing_raw = [_unified_candidate_to_raw_dict(candidate) for candidate in unified_candidates]
    refreshed = _fuse_and_score_candidates(
        {"hardcover": existing_raw, "google": [], "openlibrary": [], "web": lookahead_raw},
        resolved_author,
        resolved_series_name,
    )

    recovered_numbers = sorted(
        {
            int(candidate.series_number)
            for candidate in refreshed
            if candidate.series_number is not None
            and float(candidate.series_number).is_integer()
            and int(candidate.series_number) in targeted_missing
        }
    )

    return {
        "candidates": refreshed,
        "expected_total": expected_total,
        "missing_numbers": missing_numbers,
        "recovered_numbers": recovered_numbers,
    }


# Thresholds gating _reconcile_candidates_with_llm -- deliberately
# conservative so the (comparatively expensive, latency-adding) LLM pass
# only runs when the cheap, deterministic fusion above actually left
# something messy behind, not on every Check Now run.
RECONCILIATION_SERIES_COMPLETENESS_THRESHOLD = 0.8
RECONCILIATION_METADATA_COMPLETENESS_THRESHOLD = 0.5
RECONCILIATION_DISAGREEMENT_RATIO_THRESHOLD = 0.2

# Bounds how many candidates get sent into one reconciliation prompt --
# keeps the call's cost/latency bounded for an unusually noisy fetch.
# Anything beyond this is passed through untouched rather than dropped.
RECONCILIATION_MAX_CANDIDATES = 40


def _candidate_has_provenance_disagreement(candidate: "UnifiedCandidate") -> bool:
    """True if the raw members _fuse_and_score_candidates grouped into this
    one candidate don't actually agree with each other on book number,
    ISBN, or series name -- i.e. fusion picked *a* value (the first
    non-null one it found), but the providers disagreed about what that
    value should be. That's exactly the kind of conflict a deterministic
    "first non-null wins" backfill can't adjudicate well, and is one of the
    signals _needs_llm_reconciliation uses to decide the fused set is worth
    a second, LLM-driven look.
    """
    if len(candidate.source_provenance) < 2:
        return False
    numbers = {
        round(float(member["series_number_hint"]), 2)
        for member in candidate.source_provenance
        if member.get("series_number_hint") is not None
    }
    if len(numbers) > 1:
        return True
    isbns = {
        str(member["isbn13"]).strip() for member in candidate.source_provenance if str(member.get("isbn13") or "").strip()
    }
    if len(isbns) > 1:
        return True
    # Providers disagreeing on which series a book belongs to (e.g. one
    # source tagging a candidate under a different Wagner thriller series
    # than another) is just as real a fusion conflict as disagreeing on
    # number/ISBN, and the same "first non-null wins" backfill can't
    # adjudicate it either. Compared via _series_names_compatible rather
    # than strict equality, so two members merely branding the SAME series
    # differently (e.g. "Jonathan Hunt" vs. "Jonathan Hunt Thriller
    # Series") don't register as a disagreement -- only genuinely
    # different series names do.
    series_names = [
        str(member.get("series_name_hint") or "")
        for member in candidate.source_provenance
        if member.get("series_name_hint")
    ]
    return any(
        not _series_names_compatible(a, b)
        for i, a in enumerate(series_names)
        for b in series_names[i + 1 :]
    )


def _needs_llm_reconciliation(unified_candidates: list["UnifiedCandidate"], series_name: str | None) -> bool:
    """Gates _reconcile_candidates_with_llm. Only worth the extra LLM call
    when the deterministic fusion pass actually left something messy: the
    series looks incomplete (few distinct book numbers relative to the
    highest one seen), providers actively disagreed with each other on some
    candidates, or the fused metadata is thin across the board. A clean,
    complete, well-agreed-upon result skips this entirely -- fusion alone
    is enough.
    """
    if len(unified_candidates) < 2:
        return False

    numbers = sorted(
        {
            int(number)
            for candidate in unified_candidates
            for number in (_resolve_candidate_number(candidate, series_name),)
            if number is not None and float(number).is_integer()
        }
    )
    if numbers:
        expected_total = numbers[-1]
        series_completeness = len(numbers) / expected_total
        if series_completeness < RECONCILIATION_SERIES_COMPLETENESS_THRESHOLD:
            _log(f"LLM reconciliation triggered: series completeness {series_completeness:.0%} below threshold")
            return True

    disagreement_count = sum(1 for candidate in unified_candidates if _candidate_has_provenance_disagreement(candidate))
    if disagreement_count / len(unified_candidates) > RECONCILIATION_DISAGREEMENT_RATIO_THRESHOLD:
        _log(
            f"LLM reconciliation triggered: provider disagreement on "
            f"{disagreement_count}/{len(unified_candidates)} candidates"
        )
        return True

    avg_completeness = sum(candidate.metadata_completeness_score for candidate in unified_candidates) / len(
        unified_candidates
    )
    if avg_completeness < RECONCILIATION_METADATA_COMPLETENESS_THRESHOLD:
        _log(f"LLM reconciliation triggered: average metadata completeness {avg_completeness:.0%} below threshold")
        return True

    return False


def _format_candidate_for_reconciliation(index: int, candidate: "UnifiedCandidate") -> str:
    sources = list(dict.fromkeys(str(member.get("source") or "unknown") for member in candidate.source_provenance))
    return (
        f"[{index}] title={candidate.title!r} authors={candidate.authors!r} "
        f"series_name={candidate.series_name!r} "
        f"series_number={candidate.series_number} isbn13={candidate.isbn13 or 'null'} "
        f"published_date={candidate.published_date or 'null'!r} sources={sources} "
        f"metadata_completeness={candidate.metadata_completeness_score}"
    )


def _coerce_reconciled_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _coerce_reconciled_str(value) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _reconciled_completeness_score(
    title: str, authors: list[str], series_name: str | None, series_number: float | None, isbn13: str | None,
    published_date: str | None, description,
) -> float:
    # Mirrors _fuse_and_score_candidates' own present-field-count approach
    # (same _METADATA_COMPLETENESS_FIELDS) so a reconciled candidate's score
    # stays comparable to one that only ever went through plain fusion.
    completeness_values = {
        "title": title,
        "authors": authors,
        "series_name_hint": series_name,
        "series_number_hint": series_number,
        "isbn13": isbn13,
        "published_date": published_date,
        "description": description,
    }
    present_count = sum(
        1
        for field in _METADATA_COMPLETENESS_FIELDS
        for value in (completeness_values.get(field),)
        if (value if not isinstance(value, list) else bool(value)) not in (None, "", False)
    )
    return round(present_count / len(_METADATA_COMPLETENESS_FIELDS), 4)


def _apply_reconciliation_entry(entry: dict, members: list["UnifiedCandidate"]) -> "UnifiedCandidate":
    """Turns one entry of _reconcile_candidates_with_llm's response (plus
    the UnifiedCandidate(s) it references) into a single resolved
    UnifiedCandidate.

    A single-member entry (the model found nothing to merge it with) only
    ever *backfills* a field that candidate was missing -- it never
    overwrites a value the candidate already had, so one LLM misread can't
    quietly corrupt a candidate that didn't actually need reconciling. A
    multi-member entry is a real merge: the model's own resolved values are
    trusted first (that's the point of asking it to pick the best value
    across disagreeing sources), falling back to whichever merged member
    has a non-null value if the model left a field null.
    """
    primary = members[0]

    if len(members) == 1:
        title = primary.title
        authors = primary.authors
        series_name = primary.series_name
        series_number = primary.series_number
        isbn13 = primary.isbn13
        published_date = primary.published_date

        if series_number is None:
            series_number = _coerce_reconciled_float(entry.get("series_number"))
        if series_name is None:
            series_name = _coerce_reconciled_str(entry.get("series_name"))
        if not isbn13:
            candidate_isbn = _coerce_reconciled_str(entry.get("isbn13"))
            if candidate_isbn and len(candidate_isbn) == 13 and candidate_isbn.isdigit():
                isbn13 = candidate_isbn
        if not published_date:
            published_date = _coerce_reconciled_str(entry.get("published_date"))

        provenance = [dict(item) for item in primary.source_provenance] or [{}]
        provenance[0] = {
            **provenance[0],
            "series_number_hint": series_number if series_number is not None else provenance[0].get("series_number_hint"),
            "series_name_hint": series_name or provenance[0].get("series_name_hint"),
            "isbn13": isbn13 or provenance[0].get("isbn13"),
            "published_date": published_date or provenance[0].get("published_date"),
        }

        completeness = _reconciled_completeness_score(
            title, authors, series_name, series_number, isbn13, published_date, provenance[0].get("description")
        )

        return UnifiedCandidate(
            title=title,
            authors=authors,
            series_name=series_name,
            series_number=series_number,
            isbn13=isbn13,
            edition_type="bundle" if entry.get("is_bundle") else primary.edition_type,
            published_date=published_date,
            source_provenance=provenance,
            confidence_score=primary.confidence_score,
            metadata_completeness_score=max(primary.metadata_completeness_score, completeness),
            upcoming_hint=primary.upcoming_hint,
        )

    title = _coerce_reconciled_str(entry.get("title")) or primary.title
    author_names = entry.get("author_names")
    authors = list(author_names) if isinstance(author_names, list) and author_names else None
    if not authors:
        authors = next((member.authors for member in members if member.authors), [])
    series_name = _coerce_reconciled_str(entry.get("series_name")) or next(
        (member.series_name for member in members if member.series_name), None
    )
    series_number = _coerce_reconciled_float(entry.get("series_number"))
    if series_number is None:
        series_number = next((member.series_number for member in members if member.series_number is not None), None)
    isbn13_candidate = _coerce_reconciled_str(entry.get("isbn13"))
    if isbn13_candidate and len(isbn13_candidate) == 13 and isbn13_candidate.isdigit():
        isbn13 = isbn13_candidate
    else:
        isbn13 = next((member.isbn13 for member in members if member.isbn13), None)
    published_date = _coerce_reconciled_str(entry.get("published_date")) or next(
        (member.published_date for member in members if member.published_date), None
    )

    provenance: list[dict] = []
    for member in members:
        provenance.extend(dict(item) for item in member.source_provenance)
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
        min(max(member.confidence_score for member in members) + (0.1 if len(unique_sources) > 1 else 0.0), 1.0), 4
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
        edition_type="bundle" if entry.get("is_bundle") else "unknown",
        published_date=published_date,
        source_provenance=provenance,
        confidence_score=confidence_score,
        metadata_completeness_score=max(completeness, max(member.metadata_completeness_score for member in members)),
        upcoming_hint=next((member.upcoming_hint for member in members if member.upcoming_hint is not None), None),
    )


# Deliberately a completely separate prompt from _WEB_SEARCH_STRUCTURING_PROMPT
# -- that one extracts book data from raw web-search snippets; this one takes
# already-structured UnifiedCandidates and reconciles disagreements between
# them. Changing one should never risk affecting the other.
_LLM_RECONCILIATION_PROMPT = """You are reconciling a messy, possibly-duplicated list of book candidates for one series, assembled from several different data providers (catalog APIs and web search) that don't always agree with each other.

Series: "{series_name}"

Below are {count} candidates. Each may be missing information, and two or more entries may actually describe the SAME real book (e.g. one provider has "Book Three" as the title with no ISBN, another has the real subtitle and an ISBN but no book number). Some candidates may also not actually belong to this series at all -- a prolific author often has several different series, and a same-author candidate can slip in here even though it's really from one of those other series.

Candidates:
{candidate_listing}

For EACH candidate above, first decide whether it actually belongs to the series named above, "{series_name}". If a candidate clearly belongs to a different, distinct series by the same author (or to a different series entirely), put its index in "excluded_indices" instead of a resolved entry -- do not guess an exclusion just because a field is missing or a series name is slightly differently worded/branded; only exclude when the candidate's own title/series_name clearly point to a genuinely different series.

For every remaining candidate (the ones that do belong to this series), decide which other such candidates (if any) describe the same real book, and merge them into one resolved entry. Every candidate index 0-{max_index} must appear in EXACTLY ONE of: a resolved entry's "source_indices", or "excluded_indices" -- never both, and never omitted entirely. A candidate that belongs to the series but doesn't match any other is still its own resolved entry with just its own index. For each resolved entry, normalize the book number to a plain number (e.g. "Three"/"Vol. 3"/"#3" -> 3) and pick the most complete/likely-correct value for each field across whichever candidates you merged into it, resolving any disagreement (e.g. two different book numbers) by picking the value supported by more of the merged candidates, or the more specific/authoritative-looking one if it's a tie. If a candidate appears to be a bundle/omnibus of multiple existing volumes rather than a single new one, set "is_bundle" to true.

Respond with ONLY a JSON object (no prose, no markdown code fences) of this exact shape:
{{"resolved_candidates": [{{"source_indices": [<int>, ...], "title": <string>, "series_name": <string or null>, "series_number": <number or null>, "isbn13": <string or null>, "author_names": [<string>, ...], "published_date": <string or null>, "is_bundle": <bool>, "notes": <short string explaining what changed, or "" if nothing did>}}, ...], "excluded_indices": [<int, index of a candidate that does NOT belong to this series>, ...], "missing_volume_suggestions": [<int, a book number you suspect exists but isn't in the candidate list above, based on the candidates' own text>, ...]}}"""


def _reconcile_candidates_with_llm(
    unified_candidates: list["UnifiedCandidate"],
    series_name: str | None,
) -> list["UnifiedCandidate"]:
    """Single conditional LLM pass over an already-fused candidate set for
    one series -- normalizes series names/book numbers, merges candidates
    that plain identity-key fusion couldn't recognize as the same book
    (different title formatting, one has an ISBN the other lacks, etc.),
    excludes candidates it judges to actually belong to a different series
    by the same author, flags suspected bundles, and surfaces (but does not
    itself act on -- see _reconstruct_series_skeleton for actually
    searching for them) suspected missing volume numbers. Gated by
    _needs_llm_reconciliation; only called when fusion alone left the set
    looking incomplete, internally disagreeing, or thin on metadata.

    Deliberately conservative on failure: a missing API key, empty input,
    a network/parse error, or a response that doesn't cleanly partition
    every input candidate exactly once between a resolved entry's
    source_indices and excluded_indices (no gaps, no overlaps, nothing
    claimed by both) all fall back to returning unified_candidates
    completely unchanged -- this is an enrichment step, never allowed to
    lose or corrupt a candidate fusion already produced. An excluded
    candidate is simply dropped from the returned list -- there is no
    separate "other books by this author" output for it to land in
    instead.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key or len(unified_candidates) < 2:
        return unified_candidates

    # Anything beyond the cap was never sent to the model and is passed
    # through untouched below, rather than silently dropped.
    candidates = unified_candidates[:RECONCILIATION_MAX_CANDIDATES]

    import anthropic

    candidate_listing = "\n".join(
        _format_candidate_for_reconciliation(index, candidate) for index, candidate in enumerate(candidates)
    )
    prompt = _LLM_RECONCILIATION_PROMPT.format(
        series_name=series_name or "unknown",
        count=len(candidates),
        candidate_listing=candidate_listing,
        max_index=len(candidates) - 1,
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
            timeout=WEB_SEARCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # a reconciliation failure should never sink the candidates fusion already found
        _log(f"LLM reconciliation call failed: {exc}")
        return unified_candidates

    text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return unified_candidates

    if not isinstance(parsed, dict):
        return unified_candidates

    resolved_entries = parsed.get("resolved_candidates")
    if not isinstance(resolved_entries, list) or not resolved_entries:
        return unified_candidates

    missing_volume_suggestions = parsed.get("missing_volume_suggestions")
    if isinstance(missing_volume_suggestions, list) and missing_volume_suggestions:
        _log(f"LLM reconciliation suspects missing volume(s): {missing_volume_suggestions}")

    # Every original index must be claimed by exactly one entry, OR listed
    # in excluded_indices, and never both -- anything else means the model
    # didn't cleanly partition the input (overlap, gap, garbage index, or
    # an index silently missing from both), and trusting a
    # partial/overlapping response risks silently dropping or duplicating a
    # candidate fusion already handled correctly. Excluding a candidate
    # (because it belongs to a different series) is the one allowed way for
    # an index to leave "resolved_candidates" -- it must still be accounted
    # for, just via excluded_indices instead of a resolved entry.
    all_indices = set(range(len(candidates)))
    seen_indices: set[int] = set()
    valid_entries: list[dict] = []
    for entry in resolved_entries:
        if not isinstance(entry, dict):
            return unified_candidates
        indices = entry.get("source_indices")
        if not isinstance(indices, list) or not indices:
            return unified_candidates
        try:
            indices = [int(index) for index in indices]
        except (TypeError, ValueError):
            return unified_candidates
        if any(index < 0 or index >= len(candidates) or index in seen_indices for index in indices):
            return unified_candidates
        seen_indices.update(indices)
        valid_entries.append({**entry, "source_indices": indices})

    raw_excluded = parsed.get("excluded_indices", [])
    if raw_excluded and not isinstance(raw_excluded, list):
        return unified_candidates
    try:
        excluded_indices = {int(index) for index in (raw_excluded or [])}
    except (TypeError, ValueError):
        return unified_candidates
    if not excluded_indices.issubset(all_indices):
        return unified_candidates
    if seen_indices & excluded_indices:
        return unified_candidates
    if excluded_indices:
        _log(f"LLM reconciliation excluded {len(excluded_indices)} candidate(s) as belonging to a different series")

    if seen_indices | excluded_indices != all_indices:
        return unified_candidates

    reconciled = [_apply_reconciliation_entry(entry, [candidates[i] for i in entry["source_indices"]]) for entry in valid_entries]

    return reconciled + unified_candidates[len(candidates):]


# Complements _normalize_title_for_identity (services/identity.py), which
# *strips* these same bracketed/trailing format qualifiers when comparing
# titles for identity -- this is the extractive counterpart, used only to
# rank same-book candidates by _edition_priority once _finalize_candidates
# has already decided two of them are the same underlying book. Order
# matters: the more specific "mass market paperback" is checked before the
# more general "paperback" it contains.
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


def _is_cross_series_contamination(
    raw: dict, target_series_name: str | None, other_known_series_names: set[str] | None
) -> bool:
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

    other_known_series_names is accepted for call-site compatibility but no
    longer gates or narrows this check (regression: an author tracked under
    only ONE series -- e.g. George Wagner's "Jonathan Hunt Thriller
    Series" -- had contamination detection effectively disabled entirely,
    since there were no "other tracked series" to compare against, even
    though the hint on a contaminating candidate was plainly a different
    series). Any explicit, incompatible series_name_hint is contamination
    regardless of whether the user happens to track that other series too.
    """
    hint = str(raw.get("series_name_hint") or "").strip()
    if not hint:
        return False
    if _series_names_compatible(hint, target_series_name):
        return False
    return True


def _filter_cross_series_contamination(
    fetch_results: dict, target_series_name: str | None, other_known_series_names: set[str] | None
) -> dict:
    filtered = dict(fetch_results)
    for provider in ("google", "openlibrary", "hardcover", "web"):
        filtered[provider] = [
            raw
            for raw in (fetch_results.get(provider) or [])
            if not _is_cross_series_contamination(raw, target_series_name, other_known_series_names)
        ]
    return filtered


# Explicit provider trust ranking, as an ordinal rather than a float weight
# -- mirrors _PROVIDER_CONFIDENCE_WEIGHT's own hardcover > google_books >
# openlibrary > web_search ordering, but only used here to break a sort
# tie between two candidates that share the exact same resolved series
# number and title (which _filter_and_merge's own dedupe should mostly
# already prevent, but isn't guaranteed to for every code path feeding
# finalize_discovery_output -- e.g. a targeted-pass hit and a fallback-pass
# hit that plain title-key dedupe didn't recognize as the same book). Any
# other/unrecognized source sorts last.
_PROVIDER_SORT_RANK = {"hardcover": 0, "google_books": 1, "openlibrary": 2, "web_search": 3}

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


def discover_candidates_for_series(
    series_name: str,
    author: str,
    *,
    exclude_title_keys: set[str] | None = None,
    allow_author_fallback: bool = True,
    other_known_series_names: set[str] | None = None,
    enable_fallback_web_search: bool = False,
    progress_callback=None,
    highest_owned_book_number: int | None = None,
) -> dict:
    """Find candidate books for a specific series by a specific author.

    Primary pass: a targeted "<series name> <author>" search on both APIs,
    which leans on each API's own relevance ranking to associate books with
    the series (via title/description text), rather than trying to infer
    series membership purely from title patterns.

    Fallback pass: a second, series-scoped sweep (query text/OpenLibrary
    query/web-search queries all built around "<series name> <author>", same
    shape as the primary pass) rather than a bare author-bibliography sweep,
    so a brand new release whose indexed text doesn't yet mention the series
    name can still surface without pulling in this author's other, unrelated
    series. Triggered by _should_trigger_author_fallback -- the targeted
    pass looking seriously incomplete or low-confidence, not just literally
    empty -- rather than the old "only if the targeted pass found nothing at
    all" rule (still covered, as the most extreme case of "incomplete").
    allow_author_fallback remains a hard, caller-controlled kill switch
    (e.g. discover_series_by_name deliberately always sets it False --
    it's already running the targeted search on demand specifically to fill
    in one series, so a broad author sweep on top would be redundant).

    Having other tracked series by this author no longer disables the
    fallback pass outright -- pass their names as other_known_series_names
    and any fallback hit EXPLICITLY tagged as one of those (not simply
    unlabeled) is dropped before it ever reaches fusion; see
    _is_cross_series_contamination. The fallback's own results are additive
    to the targeted pass's, not a replacement for them, since the targeted
    pass may well have already found real, legitimate matches even while
    still triggering fallback for being incomplete.

    The fallback pass has never queried web search by default, since
    Brave+LLM structuring here is a noisier, costlier signal than the
    catalog APIs already provide -- enable_fallback_web_search opts into it
    anyway for a caller that wants the extra coverage.
    """
    exclude_title_keys = exclude_title_keys or set()
    series_name = str(series_name or "").strip()
    author = str(author or "").strip()
    provider_failures: list[dict] = []

    if not author:
        return {
            "candidates": [],
            "unified_candidates": [],
            "provider_failures": [],
            "all_providers_failed": False,
            "used_author_fallback": False,
        }

    if progress_callback:
        progress_callback({"current_pass": f"Searching for {series_name or author}"})

    # Query APIs with just the first co-author's name (structured author
    # fields rarely contain multiple concatenated names), but keep
    # matching/filtering against the full original string so legitimate
    # co-authored results still pass.
    query_author = primary_author_name(author)
    targeted_query_text = f"{series_name} {query_author}".strip()
    any_provider_succeeded = False

    # Google/OpenLibrary/Hardcover/web-search fetched concurrently (see
    # _fetch_all_providers_parallel) instead of one after another -- query
    # construction and per-provider error handling are unchanged, only the
    # scheduling is. Default query formulas there match this targeted pass
    # exactly (Google gets "<series>" inauthor:"<author>", OpenLibrary/
    # Hardcover both get the bare targeted_query_text, and the web-search
    # query list is the lookahead-aware "<series> book <N>" set below).
    fetch_results = _fetch_all_providers_parallel(
        query_author,
        series_name,
        targeted_query_text,
        highest_owned_book_number,
        author=author,
    )
    failures = fetch_results["_failures"]

    google_raw = fetch_results["google"]
    if "google" in failures:
        provider_failures.append({"provider": "google_books", "error": str(failures["google"])})
    else:
        any_provider_succeeded = True

    openlibrary_raw = fetch_results["openlibrary"]
    if "openlibrary" in failures:
        provider_failures.append({"provider": "openlibrary", "error": str(failures["openlibrary"])})
    else:
        any_provider_succeeded = True

    hardcover_raw = fetch_results["hardcover"]
    if "hardcover" in failures:
        provider_failures.append({"provider": "hardcover", "error": str(failures["hardcover"])})
    elif hardcover_raw or os.environ.get("HARDCOVER_API_KEY", "").strip():
        any_provider_succeeded = True

    # Live web search fills the coverage gap the catalog APIs above have for
    # indie/self-published titles and pure announcements -- only runs when
    # both a Brave key and an Anthropic key are configured, since it needs
    # both to search and to structure the results.
    web_search_raw = fetch_results["web"]
    if os.environ.get("BRAVE_SEARCH_API_KEY", "").strip() and os.environ.get("ANTHROPIC_API_KEY", "").strip():
        if "web" in failures:
            provider_failures.append({"provider": "web_search", "error": str(failures["web"])})
        else:
            any_provider_succeeded = True

    # Fuse each real book's raw hits (across all four providers) into one
    # enriched candidate before merging/filtering -- see
    # _fuse_and_score_candidates. Hardcover listed first inside it (and
    # inside fetch_results itself): when multiple sources return the same
    # book, fusion's own backfill keeps whichever copy appears first as the
    # base, and Hardcover's explicit series-position/release-status fields
    # are more trustworthy than Google Books/OpenLibrary free-text for
    # indie/self-published LitRPG, which both of those APIs tend to index/
    # cover poorly. Web search is listed last since it's the least
    # structured of the four sources. _filter_and_merge itself is
    # unchanged -- it still receives the same flat dict shape it always
    # has, just pre-fused/enriched instead of four raw lists concatenated.
    fused_candidates = _fuse_and_score_candidates(fetch_results, author, series_name)
    # Conditional LLM reconciliation -- only when fusion alone left the set
    # looking incomplete, internally disagreeing, or thin on metadata (see
    # _needs_llm_reconciliation). Runs before _filter_and_merge so any
    # normalized/merged candidate it produces still goes through the exact
    # same filtering every other candidate does.
    if _needs_llm_reconciliation(fused_candidates, series_name):
        fused_candidates = _reconcile_candidates_with_llm(fused_candidates, series_name)
    # Edition-aware collapse -- see _finalize_candidates -- runs last,
    # immediately before candidates are converted to the dict shape
    # returned to series_agent.py, so it sees whatever fusion/reconciliation
    # above already produced.
    fused_candidates = _finalize_candidates(fused_candidates)
    combined = _filter_and_merge(
        [_unified_candidate_to_raw_dict(candidate) for candidate in fused_candidates],
        author,
        exclude_title_keys,
        confidence="targeted",
        series_name=series_name,
    )
    # Tracks whichever fused candidate set actually produced `combined` --
    # reassigned below if the fallback pass runs -- so callers that want the
    # pre-filter UnifiedCandidate objects themselves (e.g. series_agent.py's
    # missing-volume skeleton reconstruction) get the right one either way.
    final_fused_candidates = fused_candidates

    used_author_fallback = False
    if allow_author_fallback and _should_trigger_author_fallback(fused_candidates, series_name, highest_owned_book_number):
        used_author_fallback = True
        if progress_callback:
            progress_callback({"current_pass": f"Broadening search to all books by {author}"})

        # Scoped by series_name, not a bare author sweep: a plain
        # author-wide query has no way to tell this series' books apart from
        # a prolific author's other, unrelated series (regression: falling
        # back to plain "George Wagner" pulled in higher-numbered books from
        # his other thriller series alongside the Jonathan Hunt books).
        # Passing series_name through -- and building the OpenLibrary/
        # web-search queries around it the same way the targeted (primary)
        # pass already does -- keeps the fallback pass able to trigger on
        # low completeness/confidence without it being author-wide in scope.
        # Web search still stays off by default (enable_fallback_web_search
        # opts in) since Brave+LLM structuring here is a noisier, costlier
        # signal than the catalog APIs already provide.
        fallback_results = _fetch_all_providers_parallel(
            query_author,
            series_name,
            f"{series_name} {query_author}",
            highest_owned_book_number,
            author=author,
            openlibrary_query=f'"{series_name}" "{query_author}"',
            web_search_queries=[
                f"{series_name} {query_author} books",
                f"{series_name} {query_author} series",
            ],
            enable_web_search=enable_fallback_web_search,
        )
        # Explicit cross-series contamination -- a fallback hit tagged with
        # one of this author's OTHER tracked series' names -- is dropped
        # before fusion ever sees it, rather than disabling the whole pass
        # just because other series exist (see this function's docstring
        # and _is_cross_series_contamination).
        fallback_results = _filter_cross_series_contamination(fallback_results, series_name, other_known_series_names)
        fallback_failures = fallback_results["_failures"]

        google_fallback = fallback_results["google"]
        if "google" in fallback_failures:
            provider_failures.append({"provider": "google_books_fallback", "error": str(fallback_failures["google"])})
        else:
            any_provider_succeeded = True

        openlibrary_fallback = fallback_results["openlibrary"]
        if "openlibrary" in fallback_failures:
            provider_failures.append({"provider": "openlibrary_fallback", "error": str(fallback_failures["openlibrary"])})
        else:
            any_provider_succeeded = True

        hardcover_fallback = fallback_results["hardcover"]
        if "hardcover" in fallback_failures:
            provider_failures.append({"provider": "hardcover_fallback", "error": str(fallback_failures["hardcover"])})
        elif hardcover_fallback or os.environ.get("HARDCOVER_API_KEY", "").strip():
            any_provider_succeeded = True

        if enable_fallback_web_search:
            web_fallback = fallback_results["web"]
            if os.environ.get("BRAVE_SEARCH_API_KEY", "").strip() and os.environ.get("ANTHROPIC_API_KEY", "").strip():
                if "web" in fallback_failures:
                    provider_failures.append({"provider": "web_search_fallback", "error": str(fallback_failures["web"])})
                else:
                    any_provider_succeeded = True

        fused_fallback_candidates = _fuse_and_score_candidates(fallback_results, author, series_name)
        if _needs_llm_reconciliation(fused_fallback_candidates, series_name):
            fused_fallback_candidates = _reconcile_candidates_with_llm(fused_fallback_candidates, series_name)
        fused_fallback_candidates = _finalize_candidates(fused_fallback_candidates)
        # Additive, not a replacement: the targeted pass above may have
        # already found real matches even while still triggering fallback
        # for looking incomplete, so the targeted pass's own title keys are
        # excluded here too -- the fallback only ever contributes *new*
        # candidates the targeted pass didn't already surface, each still
        # correctly tagged confidence="author_fallback" (weaker trust than
        # "targeted" -- series_agent.py's belongs_to_series leans on that
        # distinction) rather than the two passes' results being conflated.
        already_found_title_keys = {core_title_key(str(candidate.get("title") or "")) for candidate in combined}
        fallback_combined = _filter_and_merge(
            [_unified_candidate_to_raw_dict(candidate) for candidate in fused_fallback_candidates],
            author,
            exclude_title_keys | already_found_title_keys,
            confidence="author_fallback",
            series_name=series_name,
        )
        combined = combined + fallback_combined
        final_fused_candidates = fused_candidates + fused_fallback_candidates

    # "All providers failed" should mean we got no usable data at all (every
    # call raised), not just that filtering left zero new candidates -- a
    # provider that successfully returned data (even if it was all already
    # owned, or simply had no coverage) is a normal, successful outcome.
    all_providers_failed = bool(provider_failures) and not any_provider_succeeded

    if progress_callback:
        progress_callback({"current_pass": "Done", "total": 1, "completed": 1})

    return {
        # Deterministically sorted and stripped of transient fusion-internal
        # fields -- see finalize_discovery_output. Runs after both the
        # targeted and (if triggered) fallback passes have already been
        # merged together above, so it's the true last step on this list.
        "candidates": finalize_discovery_output(combined),
        # Pre-filter fused candidates -- additive, existing "candidates" key
        # unchanged -- so a caller that needs the richer UnifiedCandidate
        # objects (confidence_score, metadata_completeness_score,
        # source_provenance, resolved series_number) doesn't have to redo
        # the fetch+fuse work itself. See _reconstruct_series_skeleton.
        # Deliberately NOT passed through finalize_discovery_output --
        # that strips exactly the fields _reconstruct_series_skeleton needs.
        "unified_candidates": final_fused_candidates,
        "provider_failures": provider_failures,
        "all_providers_failed": all_providers_failed,
        "used_author_fallback": used_author_fallback,
    }


# Bounds the number of extra per-candidate lookups _enrich_missing_series_hints
# performs -- keeps a "More by this author" run from ballooning into dozens
# of sequential API calls for a very prolific author.
MAX_SERIES_HINT_LOOKUPS = 25


def _enrich_missing_series_hints(candidates: list[dict], author: str) -> list[dict]:
    """Recover series membership for candidates the initial author-wide
    catalog sweep couldn't tag with a series name.

    Live regression: an author-bibliography search on Hardcover for "Glynn
    Stewart" returned "Refuge", "Crusade" and "Ashen Stars" with no series
    info at all, even though Hardcover's own per-title search --
    "Refuge Glynn Stewart" -- correctly identifies it as Exile #2. The
    author-wide bibliography query and a title-specific query apparently hit
    different index paths on Hardcover's side; only the latter reliably
    carries series data for every title. Re-querying per-title is more
    expensive, so it's only done for candidates that still have nothing
    after the cheap author-wide pass, and capped (MAX_SERIES_HINT_LOOKUPS).

    Also recovers the inverse case: a title that IS the bare series name and
    was never itself published as a book (e.g. "ONSET" -- the real books are
    "To Serve and Protect", "My Enemy's Enemy", etc.). If every sibling
    result from the per-title search shares one series name that matches the
    candidate's own title, the candidate is tagged with that series name so
    looks_like_series_index_entry can drop it downstream as a stub listing.
    """
    query_author = primary_author_name(author)
    enriched: list[dict] = []
    lookups_used = 0
    for candidate in candidates:
        if candidate.get("series_name_hint") or lookups_used >= MAX_SERIES_HINT_LOOKUPS:
            enriched.append(candidate)
            continue
        title = str(candidate.get("title") or "").strip()
        if not title:
            enriched.append(candidate)
            continue

        lookups_used += 1
        try:
            siblings = _fetch_hardcover(f"{title} {query_author}", max_results=8)
        except Exception:
            enriched.append(candidate)
            continue

        target_key = bare_title_key(title)
        self_match = next(
            (
                r
                for r in siblings
                if bare_title_key(r.get("title")) == target_key and r.get("series_name_hint")
            ),
            None,
        )
        if self_match:
            enriched.append(
                {
                    **candidate,
                    "series_name_hint": self_match.get("series_name_hint"),
                    "series_number_hint": candidate.get("series_number_hint")
                    or self_match.get("series_number_hint"),
                    "series_total_hint": candidate.get("series_total_hint")
                    or self_match.get("series_total_hint"),
                }
            )
            continue

        candidate_as_series = normalize_series_branding_name(title) or normalize_text(title)
        sibling_series_names = {
            normalize_text(r.get("series_name_hint")) for r in siblings if r.get("series_name_hint")
        }
        if siblings and candidate_as_series and sibling_series_names == {candidate_as_series}:
            enriched.append({**candidate, "series_name_hint": title})
            continue

        enriched.append(candidate)
    return enriched


def discover_candidates_for_author(
    author: str,
    *,
    exclude_title_keys: set[str] | None = None,
    progress_callback=None,
) -> dict:
    """Find candidate books by a specific author, across ALL of their series
    and standalone works -- the "More by this author" feature.

    Deliberately much lighter than discover_candidates_for_series: one plain
    author-bibliography query per catalog API (no series name to search
    against) plus at most one Brave web-search query -- no lookahead
    queries, since there's no single "next book number" to look ahead from
    when the results can span several different series at once. Each
    candidate carries its own guessed series name (see series_name_hint on
    the Hardcover/web-search providers) rather than being checked against
    one fixed series name.
    """
    exclude_title_keys = exclude_title_keys or set()
    author = str(author or "").strip()
    provider_failures: list[dict] = []

    if not author:
        return {"candidates": [], "provider_failures": [], "all_providers_failed": False}

    if progress_callback:
        progress_callback({"current_pass": f"Searching all books by {author}"})

    query_author = primary_author_name(author)
    any_provider_succeeded = False

    # No single "next book number" to look ahead from here (results can
    # span several different series at once), so pass the plain "<author>
    # new books" query explicitly rather than the lookahead-aware default --
    # see _fetch_all_providers_parallel's docstring.
    fetch_results = _fetch_all_providers_parallel(
        query_author,
        None,
        query_author,
        None,
        author=author,
        openlibrary_query=f'author:"{query_author}"',
        web_search_queries=[f"{query_author} new books"],
    )
    failures = fetch_results["_failures"]

    google_raw = fetch_results["google"]
    if "google" in failures:
        provider_failures.append({"provider": "google_books", "error": str(failures["google"])})
    else:
        any_provider_succeeded = True

    openlibrary_raw = fetch_results["openlibrary"]
    if "openlibrary" in failures:
        provider_failures.append({"provider": "openlibrary", "error": str(failures["openlibrary"])})
    else:
        any_provider_succeeded = True

    hardcover_raw = fetch_results["hardcover"]
    if "hardcover" in failures:
        provider_failures.append({"provider": "hardcover", "error": str(failures["hardcover"])})
    elif hardcover_raw or os.environ.get("HARDCOVER_API_KEY", "").strip():
        any_provider_succeeded = True

    web_search_raw = fetch_results["web"]
    if os.environ.get("BRAVE_SEARCH_API_KEY", "").strip() and os.environ.get("ANTHROPIC_API_KEY", "").strip():
        if "web" in failures:
            provider_failures.append({"provider": "web_search", "error": str(failures["web"])})
        else:
            any_provider_succeeded = True

    fused_candidates = _fuse_and_score_candidates(fetch_results, author, None)
    # Edition-aware collapse -- see _finalize_candidates -- same as
    # discover_candidates_for_series: keeps different-ISBN editions of the
    # same real book (e.g. hardcover vs. audiobook) from being reported as
    # two separate "new by this author" candidates.
    fused_candidates = _finalize_candidates(fused_candidates)
    combined = _filter_and_merge(
        [_unified_candidate_to_raw_dict(candidate) for candidate in fused_candidates],
        author,
        exclude_title_keys,
        confidence="author_wide",
    )

    # Only worth the extra per-title lookups when Hardcover is actually
    # configured -- without a key it would just be a guaranteed-empty call
    # per candidate.
    if os.environ.get("HARDCOVER_API_KEY", "").strip():
        if progress_callback:
            progress_callback({"current_pass": "Filling in series details"})
        combined = _enrich_missing_series_hints(combined, author)
        # Enrichment can newly reveal that a candidate IS the bare series
        # name itself (the "ONSET" case) -- re-run the same stub-listing
        # check the initial merge already applies so those get dropped too.
        combined = [
            c
            for c in combined
            if not (
                looks_like_series_index_entry(
                    str(c.get("title") or ""),
                    c.get("series_name_hint"),
                    str(c.get("isbn13") or "").strip(),
                    bool(c.get("series_number_hint"))
                    or bool(infer_number_from_title(str(c.get("title") or ""), c.get("series_name_hint"))),
                )
                or _title_is_series_variant(
                    str(c.get("title") or ""),
                    c.get("series_name_hint"),
                    str(c.get("isbn13") or "").strip(),
                    bool(c.get("series_number_hint")),
                )
            )
        ]

    all_providers_failed = bool(provider_failures) and not any_provider_succeeded

    if progress_callback:
        progress_callback({"current_pass": "Done", "total": 1, "completed": 1})

    return {
        # See finalize_discovery_output -- deterministic order, transient
        # fusion-internal fields stripped.
        "candidates": finalize_discovery_output(combined),
        "provider_failures": provider_failures,
        "all_providers_failed": all_providers_failed,
    }
