"""Identity/dedup matching used when persisting discovered candidates against
existing library rows (Check Now write path).

NOTE: agents/series_agent.py has its own, deliberately different, identity
helpers (e.g. its own `_authors_match_exact`). That module answers a
different question -- "does this raw search result plausibly belong to this
series at all?" -- and is intentionally more lenient. This module answers a
stricter question -- "is this candidate the same row as one already in the
database?" -- for safe insert/update/skip decisions. They are not
interchangeable, so they are kept as separate implementations rather than
merged.
"""

import re
from datetime import date

import models


def _normalize_discovered_title(value: str | None) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return re.sub(r"[^a-z0-9]+", "", cleaned)


def _normalize_identity_text(value: str | None) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return re.sub(r"[^a-z0-9]+", " ", cleaned).strip()


def _normalize_author_for_identity(value: str | None) -> str:
    text = _normalize_identity_text(value)
    text = re.sub(r"\band\s+\d+\s+more\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(author|narrator|editor)\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def _authors_match_exact(series_author: str | None, candidate_author: str | None) -> bool:
    series_norm = _normalize_author_for_identity(series_author)
    candidate_norm = _normalize_author_for_identity(candidate_author)
    if not series_norm or not candidate_norm:
        return False
    return series_norm == candidate_norm


# Placeholder author values that write paths have historically substituted
# for a genuinely missing author (e.g. "Unknown author" prefilled on the Add
# Book form for an authorless locked series -- see use-add-book-form.ts's
# history). Listed in their *normalized* form since that's what callers
# should compare against, not their raw display spelling.
#
# Normalization makes these placeholders more dangerous than an empty
# string, not less: _normalize_author_for_identity strips the literal word
# "author" as a role descriptor, so "Unknown author" and "Unknown" both
# collapse to the same non-empty token "unknown" -- which then compares
# equal to any *other* placeholder-tainted row. Two otherwise-unrelated
# series can end up looking like the same author's work (see
# _authors_match_exact above, which returns False for a genuinely empty
# value but would return True for two placeholder values), silently fusing
# their identities in every author-keyed lookup (known-sibling-series sets,
# the author-wide tracked-series map, discovery's author-match gate).
_PLACEHOLDER_AUTHOR_DENYLIST = {"unknown", "n a", "none", "various"}


def is_placeholder_author(value: str | None) -> bool:
    """True if `value` normalizes to one of the placeholder author values
    above. Callers adopting a value into Series.author/Book.author from a
    fallback or backfill path (never from direct user input, which is free
    to contain any string) should check this and treat a placeholder the
    same as an empty value -- i.e. don't adopt it, leave the field NULL/
    empty instead so it stays visibly unresolved rather than looking like
    real data.
    """
    normalized = _normalize_author_for_identity(value)
    return bool(normalized) and normalized in _PLACEHOLDER_AUTHOR_DENYLIST


def _normalize_series_name_for_identity(value: str | None) -> str:
    text = _normalize_identity_text(value)
    text = re.sub(r"\b(series|book series)\b", "", text).strip()
    return re.sub(r"\s+", " ", text).strip()


# A trailing "(<series name> Book N[.0])"-style annotation -- e.g. "The
# Chalice of the Gods: (Percy Jackson and the Olympians Book 6.0)" --
# commonly embedded directly in Amazon/Apify-sourced listing titles. Left
# in place, this is just as unstable as Series.name itself: two Check Now
# runs of the same series minutes apart returned this same book with the
# series name formatted two different ways ("Percy Jackson and the
# Olympians" vs. "Percy Jackson & The Olympians"), which would otherwise
# make the *title* component of an identity key differ between runs even
# after series_name was removed from the key itself (see
# _series_book_identity_key's docstring for the full incident). Stripped
# before any other title normalization below so neither this nor
# _canonical_title_identity_key can be destabilized by it.
_TRAILING_SERIES_ANNOTATION_PATTERN = re.compile(
    r"\s*[:\-]?\s*\([^()]*\bbook\s+\d+(?:\.\d+)?\)\s*$",
    re.IGNORECASE,
)


def _normalize_title_for_identity(value: str | None) -> str:
    # NS-3: the persistence-time sibling of discovery_text.py's core_title_
    # key/bare_title_key (discovery-time identity matching) -- see that
    # module's docstring for the full three-way split, including
    # services/title_normalization.py's separate UI-reformatting concern.
    text = str(value or "").strip()
    text = _TRAILING_SERIES_ANNOTATION_PATTERN.sub("", text)
    text = re.sub(
        r"\((?:audible|audible audio|audio cd|kindle|kindle edition|paperback|hardcover|mass market paperback)[^)]*\)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*[:\-]\s*(audible|kindle|paperback|hardcover)\b.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+,\s+book\s+\d+\b", "", text, flags=re.IGNORECASE)
    # A signed/autographed copy is a printing variant, not a different book --
    # same live regression class as the format markers above (live bug: a
    # discovered "Iron Flame SIGNED" listing carried its own real ISBN, so it
    # skipped every other dedupe path and got persisted as a brand new book
    # alongside the already-owned "Iron Flame" instead of being recognized as
    # the same title). Only stripped as a bracketed or trailing qualifier
    # (never mid-title) so a title that genuinely contains the word --
    # e.g. "The Signed Confession" -- is left untouched.
    text = re.sub(r"\((?:signed|autographed)(?:\s+(?:edition|copy))?\)", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\s*[:\-]?\s*(?:signed|autographed)(?:\s+(?:edition|copy|by\s+the\s+author))?\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return _normalize_identity_text(text)


def _normalized_book_number_value(value) -> float | None:
    # Deliberately keeps fractional precision (used to be int(float(value)),
    # which truncates -- NOT rounds -- so book_number 3.5 collapsed to the
    # same value as 3. That made a companion/side-story book (e.g. "Threshing
    # Day" at position 3.5 in The Empyrean) share an identity key with the
    # real book 3 ("Onyx Storm"), so every matching/dedup pass below treated
    # them as the same row -- merging the companion book's fields into (or
    # collapsing away) the real numbered entry it shares a truncated number
    # with. Book numbers must stay exact here since this key answers "is
    # this the same book as one already in the database?", not "which whole
    # number is this closest to?".
    try:
        if value is None or str(value).strip() == "":
            return None
        parsed = float(value)
        return parsed if parsed > 0 else None
    except (TypeError, ValueError):
        return None


def _series_book_identity_key(
    series_id: "int | None",
    title: str | None,
    author: str | None,
    book_number,
) -> str | None:
    """Identity key for "book #N of series S", used to match a discovered
    Check Now candidate against an existing library row.

    Keyed on the immutable `series_id` -- never `Series.name` or a
    candidate's own `series_name`/`series_name_hint` text, both of which are
    display strings that can and do change between runs (a live incident:
    two Check Now runs of the same series 8 minutes apart resolved its name
    to "Percy Jackson and the Olympians" and then "Percy Jackson & The
    Olympians" -- same series_id throughout. The old series_name-keyed
    version of this function produced two different key values for the
    exact same book across those two runs, so the second run's "Stolen
    Chariot"/"Wrath of the Triple Goddess" candidates didn't match their own
    already-persisted rows from the first run and got inserted a second
    time as literal duplicates instead of being recognized as updates).
    `series_name` must stay display-only from here on -- it is deliberately
    not even accepted as a parameter any more, so no future call site can
    reintroduce the same instability.

    `title`/`author` are normalized the same way _canonical_title_
    identity_key/_authors_match_exact do (including stripping a trailing
    "(<series> Book N)"-style annotation some listing titles embed directly
    -- see _TRAILING_SERIES_ANNOTATION_PATTERN -- so that source of the same
    instability can't leak back in through the title component either) and
    folded into the key alongside series_id/number for extra precision, but
    -- like the series_name-keyed version before it -- only `series_id` and
    `book_number` are required for a key to be produced at all; missing
    title/author simply normalize to an empty key segment rather than
    blocking the match, exactly as a missing series_name used to.
    """
    if series_id is None:
        return None
    normalized_book_number = _normalized_book_number_value(book_number)
    if normalized_book_number is None:
        return None
    normalized_title = _normalize_title_for_identity(title)
    normalized_author = _normalize_author_for_identity(author)
    number_text = (
        str(int(normalized_book_number))
        if normalized_book_number.is_integer()
        else str(normalized_book_number)
    )
    return f"{series_id}|{normalized_title}|{normalized_author}|{number_text}"


def _series_number_slot_key(series_id: "int | None", book_number) -> str | None:
    """Lenient sibling of _series_book_identity_key, keyed ONLY on
    series_id + book_number -- deliberately ignores title/author entirely.

    Used exclusively by the *existing-row* dedupe-collapse passes in
    services/series_check_engine.py, whose job is to merge two rows that
    already share the same series+number slot but drifted apart on title
    (e.g. "Quest Academy: Scavengers" vs. "Scavengers: Quest Academy, Book
    2" -- a real production case). Folding title/author into that
    collapse's key would defeat its entire purpose: it exists specifically
    to catch same-slot duplicates *despite* a title (or missing-ASIN)
    mismatch. _series_book_identity_key's title/author components are
    correct and desirable for *candidate-vs-existing* matching (deciding
    whether a freshly discovered candidate is an update to an existing row
    or a new insert), which is a different question with a different
    tolerance for false negatives -- so the two keys are kept deliberately
    separate rather than one being reused for both jobs.
    """
    if series_id is None:
        return None
    normalized_book_number = _normalized_book_number_value(book_number)
    if normalized_book_number is None:
        return None
    number_text = (
        str(int(normalized_book_number))
        if normalized_book_number.is_integer()
        else str(normalized_book_number)
    )
    return f"{series_id}|{number_text}"


def _canonical_title_identity_key(title: str | None) -> str | None:
    normalized_title = _normalize_title_for_identity(title)
    return normalized_title or None


def owned_title_for_identity(book: "models.Book") -> str:
    """The title to use for identity/discovery matching against an existing
    owned book -- Book.canonical_title (provider-resolved) when present,
    falling back to Book.title (the user's original entry). FIND bind,
    bulk re-resolution, and Check Now persistence now all populate
    canonical_title, so this is a real fallback (not a permanent no-op) --
    callers that build title-key exclusion sets or dedupe against
    *existing* rows should use this instead of reading `.title` directly,
    so a resolved title (e.g. a corrected "Volume 4" -> "Book 4") is
    recognized under its canonical identity rather than staying keyed off
    whatever the user originally typed. Never used for the *incoming
    candidate* side of a comparison -- a fresh FIND/discovery result has no
    canonical_title of its own yet; that's exactly what Bind assigns.
    """
    canonical = str(getattr(book, "canonical_title", None) or "").strip()
    return canonical or str(getattr(book, "title", None) or "")


def _edition_priority(value: str | None) -> int:
    edition = str(value or "").strip().lower()
    priorities = {
        "hardcover": 5,
        "paperback": 4,
        "ebook": 3,
        "audio": 2,
        "unknown": 1,
        "": 1,
    }
    return priorities.get(edition, 1)


def _is_upcoming_future_book(book: "models.Book", *, today: date) -> bool:
    status = str(getattr(book, "read_status", "") or "").strip().lower()
    publication_date = getattr(book, "publication_date", None)
    if status != "upcoming" or not isinstance(publication_date, date):
        return False
    return publication_date > today
