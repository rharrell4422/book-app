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
        if looks_like_series_index_entry(title, effective_series_name, isbn13, has_number_hint):
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


def discover_candidates_for_series(
    series_name: str,
    author: str,
    *,
    exclude_title_keys: set[str] | None = None,
    allow_author_fallback: bool = True,
    progress_callback=None,
    highest_owned_book_number: int | None = None,
) -> dict:
    """Find candidate books for a specific series by a specific author.

    Primary pass: a targeted "<series name> <author>" search on both APIs,
    which leans on each API's own relevance ranking to associate books with
    the series (via title/description text), rather than trying to infer
    series membership purely from title patterns.

    Fallback pass (only if the primary pass finds nothing, and only when
    the caller says it's safe -- i.e. this author has no other tracked
    series in the library): a plain author-bibliography sweep, so a brand
    new release whose indexed text doesn't yet mention the series name can
    still surface.
    """
    exclude_title_keys = exclude_title_keys or set()
    series_name = str(series_name or "").strip()
    author = str(author or "").strip()
    provider_failures: list[dict] = []

    if not author:
        return {"candidates": [], "provider_failures": [], "all_providers_failed": False, "used_author_fallback": False}

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

    # Hardcover listed first: when multiple sources return the same book,
    # dedup keeps whichever copy appears first, and Hardcover's explicit
    # series-position/release-status fields are more trustworthy than
    # Google Books/OpenLibrary free-text for indie/self-published LitRPG,
    # which both of those APIs tend to index/cover poorly. Web search is
    # listed last since it's the least structured of the four sources.
    combined = _filter_and_merge(
        [*hardcover_raw, *google_raw, *openlibrary_raw, *web_search_raw],
        author,
        exclude_title_keys,
        confidence="targeted",
        series_name=series_name,
    )

    used_author_fallback = False
    if not combined and allow_author_fallback:
        used_author_fallback = True
        if progress_callback:
            progress_callback({"current_pass": f"Broadening search to all books by {author}"})

        # No web search on this fallback pass (never has been) -- pass
        # enable_web_search=False rather than omitting web entirely so the
        # helper's return shape stays consistent regardless of caller.
        fallback_results = _fetch_all_providers_parallel(
            query_author,
            None,
            query_author,
            None,
            author=author,
            openlibrary_query=f'author:"{query_author}"',
            enable_web_search=False,
        )
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

        combined = _filter_and_merge(
            [*hardcover_fallback, *google_fallback, *openlibrary_fallback],
            author,
            exclude_title_keys,
            confidence="author_fallback",
            series_name=series_name,
        )

    # "All providers failed" should mean we got no usable data at all (every
    # call raised), not just that filtering left zero new candidates -- a
    # provider that successfully returned data (even if it was all already
    # owned, or simply had no coverage) is a normal, successful outcome.
    all_providers_failed = bool(provider_failures) and not any_provider_succeeded

    if progress_callback:
        progress_callback({"current_pass": "Done", "total": 1, "completed": 1})

    return {
        "candidates": combined,
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

    combined = _filter_and_merge(
        [*hardcover_raw, *google_raw, *openlibrary_raw, *web_search_raw],
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
            if not looks_like_series_index_entry(
                str(c.get("title") or ""),
                c.get("series_name_hint"),
                str(c.get("isbn13") or "").strip(),
                bool(c.get("series_number_hint"))
                or bool(infer_number_from_title(str(c.get("title") or ""), c.get("series_name_hint"))),
            )
        ]

    all_providers_failed = bool(provider_failures) and not any_provider_succeeded

    if progress_callback:
        progress_callback({"current_pass": "Done", "total": 1, "completed": 1})

    return {
        "candidates": combined,
        "provider_failures": provider_failures,
        "all_providers_failed": all_providers_failed,
    }
