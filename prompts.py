"""Tier-specific LLM prompt builders (HTA Orchestrator Step 5).

Introduces the three prompt builders the Step 5 architecture review
specified, one per tier:

- `build_extraction_prompt` (Tier A): minimalist extraction, no reasoning.
  Used by `provider_io._structure_web_results_with_llm` to turn raw
  web-search snippets into structured candidate dicts.
- `build_reconciliation_prompt` (Tier B): light reasoning + reconciliation.
  Used by `provider_io._reconcile_candidates_with_llm` to merge/normalize
  an already-fused candidate list.
- `build_belongs_to_series_prompt` (Tier C): full deep reasoning, shadow-
  only in Step 5. Used by `agents/series_agent.py`'s classification loop,
  strictly behind the Tier C shadow predicate -- see that call site's own
  comment for the exact trigger condition. Never affects live routing.

Tier A/B builders are deliberately just the pre-existing `.format()` calls
moved out of `provider_io.py` and named -- per the Step 5 architecture
review item 4, Tier A/B call sites and prompt text are NOT changing
behavior in this step, only where the prompt-assembly code lives. Tier C
is genuinely new (no prior inline prompt existed for it).

This module has no dependency on `provider_io.py`/`agents/series_agent.py`
so both can import from here without a circular import; `provider_io.py`
re-exports the two prompt template constants below for
`discovery_engine.py`'s existing `from provider_io import
_WEB_SEARCH_STRUCTURING_PROMPT, _LLM_RECONCILIATION_PROMPT` re-export
chain, so no other module needs to change its imports.
"""
from __future__ import annotations

_WEB_SEARCH_STRUCTURING_PROMPT = """You are extracting structured book-release data from live web search results.

{scope_line}
Target author: "{author}"

Below are {count} web search results returned for this search. For EACH result that actually describes a specific book entry by {title_scope} (a released book, an upcoming/pre-order book, or a firm announcement of one) extract its data. Skip results that are: reviews or discussions of a book without any new release info,{skip_other_series} retailer category/search pages, fan wiki summaries of a whole series, news unrelated to a specific book, or fan speculation/discussion about a future book that has no confirmed title yet (e.g. only referred to as "the next book" or "an untitled sequel").

Domain signal guidance:
Use the domain of the URL as a reliability signal when deciding whether a result is likely to describe a real book entry.

High-signal domains (more likely to describe real books):
- amazon.com
- goodreads.com
- hardcover.app
- books.google.com

Treat results from these domains as more likely to be valid book entries, and be more willing to accept them when the snippet plausibly describes a specific book by {title_scope}.

Low-signal domains (more likely to contain noise or non-book content):
- reddit.com
- *.fandom.com
- wikipedia.org
- *.wordpress.com
- *.blogspot.com

Treat results from these domains with greater skepticism, but do NOT discard them outright. If the snippet clearly describes a specific book (for example, it gives a title, author, volume number, release information, or other strong book-identifying signals), you should still accept the result. This is a soft bias, not a hard filter: always prioritize correctness. If a low-signal domain contains the earliest or only available information about a book, accept it.

Genre-specific metadata guidance:
- Series names may appear in multiple forms (abbreviations, alternate titles, renamed editions). When multiple series names appear for the same book, prefer the most complete or explicit series name.
- Book numbering may be expressed in many formats: "Book N", "Volume N", "Part N", "Arc N", "Season N Episode M", or Roman numerals. Infer "book_number" whenever the numbering is explicit or clearly implied.
- Fractional numbering (e.g., "3.5", "0.5", "Book 2.5", "Interlude", "Side Story", "Novella") should be accepted as valid book entries. Treat these as legitimate positions within the series.
- Web-serial to book transitions are common. If a snippet describes a web-serial chapter bundle, arc, or season being released as a book, treat it as a valid book entry.
- If a snippet shows multiple possible titles for the same book (e.g., a renamed volume or rebranded edition), prefer the title that appears in the URL or retailer listing.
- Reject box sets or omnibus editions unless the snippet explicitly describes a new individual volume within the set.

Search results:
{snippets}

A retailer listing existing (e.g. a Kindle Store page) is NOT proof a book has already been released -- pre-order listings look identical to a snippet with no date. If the snippet/title does not explicitly confirm a release date or that the book is already out, set "is_upcoming" to true and "published_date" to null rather than guessing it's already available -- it's far more useful to flag a book as "coming soon, exact date unconfirmed" than to wrongly tell a reader something is ready to read.

Respond with ONLY a JSON array (no prose, no markdown code fences). Each element must have this shape:
{{"result_index": <int, the [N] index above>, "title": <string, the clean book title without the series name or a "Book N" suffix -- BUT if the book has no title of its own beyond its series name and position (i.e. the only title given for it IS "<Series Name> <N>", with no separate subtitle at all), output that full "<Series Name> <N>" text as-is instead of stripping it down to just the bare series name>, "series_name": <string or null, the name of the series this book belongs to, if any -- null if it's a standalone>, "book_number": <int or null, this book's position in its series if stated or clearly implied>, "author_names": [<string>, ...], "published_date": <string, "YYYY-MM-DD"/"YYYY-MM"/"YYYY" if EXPLICITLY stated in the snippet, else null>, "is_upcoming": <bool, see rule above>, "isbn13": <string or null>}}

If none of the results are genuine matches, respond with exactly: []"""


def build_extraction_prompt(
    *,
    scope_line: str,
    author: str,
    count: int,
    snippets: str,
    skip_other_series: str,
    title_scope: str,
) -> str:
    """Tier A prompt builder: extract raw metadata fields from provider
    (web-search) snippets, no reasoning/inference beyond what the prompt
    text itself already asked for pre-Step-5. Zero behavior change from
    the inline `.format()` call this replaces -- see
    `provider_io._structure_web_results_with_llm` for how each parameter
    is derived.
    """
    return _WEB_SEARCH_STRUCTURING_PROMPT.format(
        scope_line=scope_line,
        author=author,
        count=count,
        snippets=snippets,
        skip_other_series=skip_other_series,
        title_scope=title_scope,
    )


_CANONICAL_PAGE_STRUCTURING_PROMPT = """You are extracting a full list of books from a canonical series-listing page.

{scope_line}
Target author: "{author}"

Below is the full text content of {source_label}, a page the user has identified as the authoritative source listing every book in this series. Unlike a short search-result snippet, this page is EXPECTED to describe MANY books at once -- extract EVERY book you can identify on it, however many there are. Do not skip it for being a "whole series summary" or a listing/catalog page -- that IS the intended content here, not noise to filter out.

For each book you identify, extract its data using the fields below. If the page mixes in books from a different series, by a different author, or unrelated content (navigation text, ads, unrelated recommendations), skip those specific entries -- but do not reject the page as a whole just because it also contains that kind of noise.

Genre-specific metadata guidance:
- Book numbering may be expressed in many formats: "Book N", "Volume N", "Part N", "Arc N", "Season N Episode M", or Roman numerals. Infer "book_number" whenever the numbering is explicit or clearly implied by list order.
- Fractional numbering (e.g., "3.5", "0.5", "Book 2.5", "Interlude", "Side Story", "Novella") should be accepted as valid book entries. Treat these as legitimate positions within the series.
- Reject box sets or omnibus editions unless the page explicitly describes a new individual volume within the set.

Page content:
{page_text}

If the page does not explicitly confirm a book has already been released, set "is_upcoming" to true and "published_date" to null rather than guessing it's already available.

Respond with ONLY a JSON array (no prose, no markdown code fences). Each element must have this shape:
{{"title": <string, the clean book title without the series name or a "Book N" suffix -- BUT if the book has no title of its own beyond its series name and position (i.e. the only title given for it IS "<Series Name> <N>", with no separate subtitle at all), output that full "<Series Name> <N>" text as-is instead of stripping it down to just the bare series name>, "series_name": <string or null, the name of the series this book belongs to, if any -- null if it's a standalone>, "book_number": <int or null, this book's position in its series if stated or clearly implied>, "author_names": [<string>, ...], "published_date": <string, "YYYY-MM-DD"/"YYYY-MM"/"YYYY" if EXPLICITLY stated, else null>, "is_upcoming": <bool, see rule above>, "isbn13": <string or null>}}

If the page describes zero books belonging to this series, respond with exactly: []"""


def build_canonical_page_extraction_prompt(
    *,
    scope_line: str,
    author: str,
    source_label: str,
    page_text: str,
) -> str:
    """Tier A prompt builder, canonical-page variant (Guided Discovery,
    Option A fix, 2026-09-03 Goodreads/Jonathan Hunt validation test).

    A dedicated prompt for provider_io.fetch_canonical_page_candidates,
    deliberately NOT sharing build_extraction_prompt's snippet-oriented
    prompt/schema, even though both ultimately feed the same
    _parse_web_search_structured_items downstream. Reusing the snippet
    prompt unmodified against a real canonical series page (that
    validation test) produced ZERO extracted candidates, root-caused to
    two things that prompt does on purpose for the snippet case but which
    are exactly backwards for a canonical page:

    1. build_extraction_prompt explicitly instructs the model to SKIP
       "fan wiki summaries of a whole series" and "retailer category/
       search pages" -- a canonical series-listing page (e.g. a Goodreads
       series page) IS structurally that exact shape. An LLM applying
       that exclusion by analogy has every reason to reject the entire
       page as out-of-scope noise, which is exactly what happened.
    2. Its schema is framed one-book-per-result ("for EACH result...
       extract its data", keyed by a single result_index per input) --
       nothing in that prompt text tells the model it may/should emit
       many array elements describing many different books all found
       within one single input. fetch_canonical_page_candidates' own
       calling code is ready for that (every parsed item is paired with
       the same one source dict, no result_index-based lookup needed --
       see its own docstring), but the model was never told to behave
       that way.

    This prompt inverts both: states outright that the page is EXPECTED
    to describe many books and instructs extracting all of them, and
    drops the whole-series-summary/catalog-page exclusion entirely --
    that's the intended input here, not noise. No result_index in the
    output schema, unlike build_extraction_prompt's -- fetch_canonical_
    page_candidates always pairs every returned item with the same
    single source dict regardless of index (there's only one page ever
    passed in), so result_index would serve no purpose and risks
    confusing the model into artificially deduplicating entries by index.
    """
    return _CANONICAL_PAGE_STRUCTURING_PROMPT.format(
        scope_line=scope_line,
        author=author,
        source_label=source_label,
        page_text=page_text,
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


def build_reconciliation_prompt(
    *,
    series_name: str | None,
    count: int,
    candidate_listing: str,
    max_index: int,
) -> str:
    """Tier B prompt builder: resolve inconsistencies between providers,
    light semantic reasoning, normalize titles/numbers/series names. Zero
    behavior change from the inline `.format()` call this replaces -- see
    `provider_io._reconcile_candidates_with_llm` for how each parameter is
    derived.
    """
    return _LLM_RECONCILIATION_PROMPT.format(
        series_name=series_name or "unknown",
        count=count,
        candidate_listing=candidate_listing,
        max_index=max_index,
    )


def build_belongs_to_series_prompt(
    *,
    title: str,
    series_name: str | None,
    inferred_number,
    provider_metadata: list[dict] | None = None,
    known_series_titles=None,
    owned_core_title_texts=None,
    highest_owned_book_number=None,
    candidate_confidence: str | None = None,
    reason_flags: dict | None = None,
    description: str | None = None,
    sibling_candidates: list[dict] | None = None,
) -> str:
    """Tier C prompt builder: deep reasoning over rich context to determine
    `belongs_to_series` in ambiguous cases, infer latent ordering, detect
    alternate titles, resolve contradictory metadata. Shadow-only -- see
    this call's one call site (`agents/series_agent.py`'s classification
    loop, inside the `"belongs_to_series_shadow_check"` pass_scope) for
    the exact trigger predicate and why its output is recorded via
    `record_shadow_llm_call` rather than ever feeding live routing.

    Deliberately takes richer inputs than the deterministic gate
    (`evaluate_belongs_to_series_gate`) sees -- raw per-provider metadata,
    sibling candidates from the same batch, and the book description when
    available -- per the Step 5 architecture review item 3: a purely
    textual/numeric gate can't do latent-series inference or alternate-
    title detection, which is exactly what this prompt asks the model to
    attempt. `description` may legitimately be `None` (many providers,
    e.g. OpenLibrary, never populate it) -- this degrades gracefully by
    saying so in the prompt rather than omitting the field.

    HTA Orchestrator Step 6: refines only the instructional wording below
    (explicit deterministic-reasoning bullets, an explicit no-chain-of-
    thought/no-intermediate-steps instruction) -- every interpolated value,
    every graceful-degradation fallback string, the builder's own
    signature, and the output schema (including `is_alternate_title_of_
    known_book` staying a bool) are unchanged from Step 5. This call site
    still isn't consumed by anything beyond `record_shadow_llm_call`'s
    token/cost accounting, so this step only changes what's sent to the
    model and what's available for future shadow-data review -- it cannot
    change live behavior.
    """
    provider_metadata = provider_metadata or []
    sibling_candidates = sibling_candidates or []
    known_series_titles = known_series_titles or set()
    owned_core_title_texts = owned_core_title_texts or set()
    reason_flags = reason_flags or {}

    provider_metadata_lines = (
        "\n".join(
            f"- source={entry.get('source') or 'unknown'}: "
            f"title={entry.get('title')!r}, "
            f"series_number_hint={entry.get('series_number_hint')!r}, "
            f"isbn13={entry.get('isbn13')!r}"
            for entry in provider_metadata
        )
        or "(no additional provider metadata available)"
    )

    sibling_lines = (
        "\n".join(
            f"- {sibling.get('title')!r} (number hint: {sibling.get('number')!r})"
            for sibling in sibling_candidates
        )
        or "(no other candidates in this batch)"
    )

    known_titles_line = ", ".join(sorted(known_series_titles)) if known_series_titles else "(none known)"
    owned_core_titles_line = (
        ", ".join(sorted(owned_core_title_texts)) if owned_core_title_texts else "(none known)"
    )
    reason_flags_line = ", ".join(f"{key}={value}" for key, value in reason_flags.items()) or "(none)"

    return f"""You are resolving an AMBIGUOUS book-series-membership case in shadow mode. This is an evaluation-only pass -- your answer is recorded for later comparison and does NOT change any live decision about this candidate.

Candidate title: {title!r}
Target series: {series_name!r}
Candidate's inferred series position: {inferred_number!r}
Candidate confidence label from the discovery pipeline: {candidate_confidence!r}
Book description (may be unavailable -- reason from title/metadata alone if so): {description or "(no description available)"}

Deterministic-gate signals already computed for this candidate: {reason_flags_line}

Highest book number already owned in this series: {highest_owned_book_number!r}
Known (normalized) titles already recognized as part of this series: {known_titles_line}
Known (normalized) owned book titles: {owned_core_titles_line}

Raw metadata from every provider that returned this candidate:
{provider_metadata_lines}

Other candidates discovered in this same batch (titles/number hints only -- context for possible latent ordering or alternate/duplicate titles):
{sibling_lines}

Perform deep but deterministic reasoning over the context above:
- Compare the candidate's title against the known series titles and owned book titles above for alternate, rebranded, or variant-title matches.
- Compare the candidate's inferred/highest-owned numbering against the sibling candidates to detect latent ordering the deterministic gate couldn't confirm.
- Use the book description, when available, to judge narrative continuity with the series; if it is unavailable, degrade gracefully and rely on titles, numbering, and sibling candidates alone instead.
- Weigh the deterministic-gate signals listed above as inputs to your judgment rather than re-deriving them from scratch.
- Decide whether the candidate is likely part of the series despite missing explicit metadata, or likely NOT part of it due to a narrative or structural mismatch.

Do NOT:
- invent metadata that isn't present above
- hallucinate plot details beyond what the description states
- use chain-of-thought or step-by-step reasoning
- output intermediate reasoning steps

Respond with ONLY a JSON object (no prose, no markdown code fences) of this exact shape:
{{"belongs_to_series": <bool>, "confidence": "low"|"medium"|"high", "inferred_number": <number or null>, "is_alternate_title_of_known_book": <bool>, "reasoning": <short string>}}"""
