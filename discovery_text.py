"""Shared, provider-agnostic text/identity primitives for book discovery.

Split out of discovery_engine.py (RT-1a) as the one leaf module of that
split: title/series-name normalization, placeholder/non-new-release
detection, and number/date parsing that provider_io.py, deterministic_fusion
.py, and diagnostics.py all need. Kept dependency-free with respect to this
package's other discovery modules specifically so none of those three ever
needs to import from each other (or from discovery_engine.py itself) just to
reuse this text-processing plumbing -- if a function here ever needs
something from one of them, it no longer belongs in this module.

discovery_engine.py re-exports everything below, so existing external
callers (agents/series_agent.py, tests, etc.) are unaffected by this split.
"""
from __future__ import annotations

import re
from datetime import date

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

# Bare genre-category nouns that self-published/indie catalog listings
# routinely tack onto a series name as a back-cover tagline -- "A <Series>
# Thriller", "A <Series> Mystery", "A <Series> Romance" -- rather than as
# any part of a real, individually-titled book (regression: "Check Now" on
# Georgia Wagner's "Jonathan Hunt Thriller Series" admitted a candidate
# titled exactly "A Jonathan Hunt Thriller" -- no ISBN, no real subtitle,
# just the series name plus this exact tagline idiom -- as a new book,
# because "thriller" isn't _TITLE_VARIANT_FILLER_TOKENS' kind of filler and
# the tracked series name itself didn't happen to already contain the word
# "Thriller" to cancel it out). Unlike _TITLE_VARIANT_FILLER_TOKENS, these
# are ONLY treated as filler when the title's single remaining token beyond
# the series name is one of these -- two or more such tokens (or one of
# these alongside any other real word) is left alone as likely-genuine,
# more substantial descriptive content, not this narrow one-word tagline
# idiom.
_SOLO_GENRE_TAGLINE_TOKENS = {
    "thriller", "thrillers",
    "mystery", "mysteries",
    "romance", "romances",
    "saga", "sagas",
    "epic", "epics",
    "adventure", "adventures",
    "drama", "dramas",
    "chronicle", "chronicles",
    "tale", "tales",
    "story", "stories",
}


def _title_is_series_variant(
    title: str, series_name: str | None, isbn13: str | None, structured_number_hint
) -> bool:
    """True if `title` is effectively just the series name -- an exact
    match, or a trivial variant of it ("A <Series> Thriller", "<Series>
    Book 6", "<Series> Novel") -- rather than a real, distinctly-titled
    book. Complements looks_like_series_index_entry: that function catches
    the bare, unadorned series name; this one catches the same underlying
    non-book stub with a little filler text stapled on, which slips past
    looks_like_series_index_entry's exact-form comparison (regression:
    "Check Now" on Georgia Wagner's "Jonathan Hunt Thriller Series" admitted
    a candidate titled "A Jonathan Hunt Thriller" -- no ISBN, no real
    subtitle, nothing but the series' own name and a genre word -- as if it
    were a new, distinctly-titled book. This recurred even after an initial
    fix, because that fix only cancelled the genre word out when it was
    already part of the *tracked series name's own text* -- if the series
    is tracked under a shorter name that doesn't itself contain "Thriller",
    the word survived as if it were real content. See
    _SOLO_GENRE_TAGLINE_TOKENS: a single bare genre-category word is now
    filler in its own right, independent of how the series name happens to
    be spelled).

    structured_number_hint must come from a provider's own structured field
    (e.g. Hardcover's series_number_hint), NOT a number inferred from this
    same title's own text -- a "Book 6" parsed out of the very title being
    checked here isn't independent evidence of a real book on its own, since
    that's exactly the filler text this function exists to see through.
    It's still useful as corroboration for a title that also names its own
    "Book <N>" filler (see below), since a title/provider pair that agree on
    the same N is a real, self-consistent signal.

    Passing the ISBN through unconditionally short-circuits this (same
    guard looks_like_series_index_entry itself uses) -- an ISBN is tied to
    one specific real edition, so it's strong enough evidence on its own.

    For a title that's an EXACT match to the series name -- carrying zero
    content beyond the series' own branding -- a bare structured number is
    NOT enough cover unless it's plausibly the series' own eponymous first
    entry (position 1): a real book is essentially never titled exactly
    like its series except for that one legitimate case (e.g. "Mistborn"
    for "Mistborn"). Treating ANY structured number as sufficient cover
    for an exact-match title was itself a regression: Hardcover assigning
    a real series-position number (e.g. 6) to what was otherwise still
    just a bare/placeholder title let that exact malformed combination
    straight through. A partial/trivial-variant title (e.g. "<Series> Book
    7") is different -- there the "Book 7" is a common, legitimate
    self-published title convention (see _TITLE_SERIES_MARKER_PATTERN),
    and a structured number hint at all (any position, since it's the
    title's own restated number this time, not an unrelated one) still
    counts as real corroboration.
    """
    if isbn13:
        return False
    normalized_title = normalize_series_branding_name(str(title or ""))
    normalized_series = normalize_series_branding_name(str(series_name or ""))
    if not normalized_title or not normalized_series:
        return False
    if normalized_title == normalized_series:
        try:
            is_eponymous_first_entry = structured_number_hint is not None and float(structured_number_hint) == 1
        except (TypeError, ValueError):
            is_eponymous_first_entry = False
        return not is_eponymous_first_entry
    if structured_number_hint:
        return False

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
    # A single bare genre tagline word (see _SOLO_GENRE_TAGLINE_TOKENS) is
    # filler too, but ONLY when it's the one and only meaningful token left --
    # any second real word alongside it means there's genuine descriptive
    # content here, not just the series-name-plus-tagline idiom.
    if len(meaningful_unique_tokens) == 1 and meaningful_unique_tokens <= _SOLO_GENRE_TAGLINE_TOKENS:
        return True
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
        # Truncated to its integer part regardless of whether it's whole
        # or fractional, matching exactly what the old integer-only
        # implementation always did, so this key stays completely
        # unchanged now that infer_number_from_title can return a genuinely
        # fractional value (e.g. 3.5 for "Book 3.5") -- the
        # fractional-collision problem that preserves against is handled
        # at the persistence identity layer instead, not here. See
        # services/identity.py's _normalized_book_number_value /
        # _series_book_identity_key.
        return f"{normalized_core} {int(number)}"
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


def _normalize_number_context(value: str | None) -> str:
    """Like normalize_text, but protects a decimal point sitting between two
    digits (e.g. the ".5" in "Book 3.5") before stripping punctuation, so a
    fractional book-number pattern matched against the result can still see
    it. Every other punctuation character (colons, parens, hyphens, etc.)
    collapses to a space exactly as normalize_text already does -- this is
    strictly a superset, not a behavior change, for any title with no
    digit.digit sequence at all.
    """
    text = str(value or "").lower()
    text = re.sub(r"(?<=\d)\.(?=\d)", "\uE000", text)
    text = re.sub(r"[^a-z0-9\uE000\s]", " ", text)
    text = text.replace("\uE000", ".")
    return re.sub(r"\s+", " ", text).strip()


def _parse_positive_number(raw_value: str | None) -> float | None:
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def infer_number_from_title(title: str | None, series_name: str | None = None) -> float | None:
    """Returns the inferred series position for `title`, preferring (in
    order): a "#N" marker, a "book/volume/vol N" marker, a spelled-out
    "book/volume/vol <word>" marker, then (if `series_name` is given) a bare
    "<series name> N" prefix or mid-title occurrence.

    Fractional positions (e.g. "Book 3.5" for a companion/novella slotted
    between two numbered entries) are preserved as a float rather than
    truncated to their integer part -- see services/identity.py's
    _normalized_book_number_value docstring for why truncating a genuinely
    fractional position is dangerous (it collapses a companion book's
    identity onto the numbered entry beside it). Fractional support only
    extends to the "#"/"book"/"volume"/"vol" keyword patterns below; the
    bare "<series name> N" positional patterns remain integer-only exactly
    as before, since a fractional position is essentially never expressed
    that way in practice.

    core_title_key (below) intentionally truncates this back to its integer
    part before folding it into a discovery matching key -- that key needs
    to stay stable across whole-number titles regardless of this function's
    own precision, and the fractional-collision problem this preserves
    against belongs at the persistence identity layer, not the discovery
    matching key. See that function's own note.
    """
    # Checked against the raw (non-normalized) title first: normalize_text
    # strips punctuation like "#", so a "#7"-style pattern could never
    # actually match once run against the already-normalized text below.
    hash_match = re.search(r"#\s*(\d+(?:\.\d+)?)\b", str(title or ""))
    if hash_match:
        value = _parse_positive_number(hash_match.group(1))
        if value is not None:
            return value

    # A separately (lightly) normalized pass that preserves a digit.digit
    # decimal point -- see _normalize_number_context -- so "Book 3.5" isn't
    # silently truncated to "Book 3" the way plain normalize_text would
    # force it to be (it strips "." unconditionally).
    number_context = _normalize_number_context(title)
    if number_context:
        keyword_patterns = (
            r"\bbook\s*(\d+(?:\.\d+)?)\b",
            r"\bvolume\s*(\d+(?:\.\d+)?)\b",
            r"\bvol\.?\s*(\d+(?:\.\d+)?)\b",
        )
        for pattern in keyword_patterns:
            match = re.search(pattern, number_context)
            if not match:
                continue
            value = _parse_positive_number(match.group(1))
            if value is not None:
                return value

    cleaned = normalize_text(title)
    if not cleaned:
        return None

    # Some listings spell the number out ("Book One", "Volume Two") instead
    # of using a digit -- same intent, different formatting. No fractional
    # form exists for a spelled-out number.
    word_pattern = r"\b(?:book|volume|vol\.?)\s+(" + "|".join(_WORD_NUMBERS) + r")\b"
    word_match = re.search(word_pattern, cleaned)
    if word_match:
        value = _WORD_NUMBERS.get(word_match.group(1))
        if value:
            return float(value)

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
                return float(value)

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
                return float(value)
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




def _to_int_or_none(value) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _integral_or_none(value) -> int | None:
    """Deliberately NOT _to_int_or_none: that one *truncates* (3.5 -> 3),
    this one rejects anything non-integral (3.5 -> None). A 0.5/3.5-style
    companion novella is real content but is not volume 3, and treating it
    as one is exactly the confusion _fetch_hardcover keeps series_position
    as a float to avoid (see its own comment). Numeric strings parse
    ("3" and "3.0" both -> 3) so a provider that ever hands back a string
    hint degrades gracefully rather than being silently skipped.
    """
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return int(number) if number.is_integer() else None


