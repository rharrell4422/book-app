"""Console logging for Check Now runs -- kept separate from the persistence
engine (services/series_check_engine.py) since it's a distinct concern
(formatting output) with no side effects on the database.
"""


def _console_log(message: str) -> None:
    print(f"[main] {message}", flush=True)


def _condense_provider_ledger(provider_ledger: list[dict]) -> list[str]:
    """Turn the detailed per-provider ledger (30+ fields each) into ONE short
    line per provider, so the total log size doesn't grow with how much raw
    scraping happened. Full detail is still available in the returned result
    dict for anything that needs it (e.g. an API response) -- this is just
    for what gets printed to the console.
    """
    lines: list[str] = []
    for entry in provider_ledger:
        parts = [f"provider={entry.get('provider_name')}", f"status={entry.get('status')}"]

        http_status = entry.get("http_status")
        if http_status:
            parts.append(f"http={http_status}")
        if entry.get("bot_blocked"):
            parts.append("bot_blocked=yes")
        if entry.get("cache_fallback"):
            parts.append("cached=yes")

        candidates = entry.get("canonical_candidates") or 0
        valid = entry.get("classification_valid") or 0
        invalid = entry.get("classification_invalid") or 0
        if candidates or valid or invalid:
            parts.append(f"candidates={candidates} valid={valid} invalid={invalid}")

        discovered_books = entry.get("author_discovered_books")
        if discovered_books:
            parts.append(f"discovered={discovered_books}")

        asin_seed_count = entry.get("asin_seed_count") or 0
        if asin_seed_count:
            parts.append(
                f"asin_seeds={asin_seed_count} "
                f"pages_ok={entry.get('asin_seed_pages_fetched') or 0} "
                f"pages_failed={entry.get('asin_seed_pages_failed') or 0}"
            )

        if entry.get("accepted_as_missing"):
            parts.append("ACCEPTED_AS_MISSING=YES")

        added = entry.get("added_books_count") or 0
        if added:
            parts.append(f"added={added}")

        error = entry.get("error")
        if error:
            parts.append(f"error={error}")

        lines.append(" | ".join(parts))
    return lines


def log_discovery_summary(*, result: dict, terminal_error: str | None = None) -> None:
    """Prints ONE short, bounded-size block summarizing a Check Now run --
    at most a few dozen lines, no matter how many candidates were scanned.
    Everything between the START and END markers is meant to be
    copy/pasted whole for debugging.
    """
    provider_ledger = result.get("provider_ledger") or []
    asin_discovery = result.get("asin_discovery") or {}
    provider_failures = result.get("provider_failures") or []
    validated_candidates = result.get("validated_candidates") or []
    missing_books = result.get("missing_books") or []
    upcoming_books = result.get("upcoming_books") or []

    _console_log("===== CHECK NOW DEBUG SUMMARY START =====")
    _console_log(f"series_id={result.get('series_id')} series_name={result.get('series_name')}")
    _console_log(f"status={result.get('status')} found={bool(result.get('found'))} added_count={int(result.get('added_count') or 0)}")
    _console_log(f"all_providers_failed={bool(result.get('all_providers_failed'))} provider_failures={len(provider_failures)}")
    _console_log(
        "asin_discovery: "
        f"discovered={int(asin_discovery.get('discovered') or 0)} "
        f"processed={int(asin_discovery.get('processed') or 0)} "
        f"fetch_success={int(asin_discovery.get('fetch_success') or 0)} "
        f"fetch_failed={int(asin_discovery.get('fetch_failed') or 0)} "
        f"metadata_hits={int(asin_discovery.get('metadata_hits') or 0)}"
    )

    _console_log(f"--- providers (one line each, {len(provider_ledger)} total) ---")
    for line in _condense_provider_ledger(provider_ledger):
        _console_log(line)

    _console_log(f"--- validated_candidates={len(validated_candidates)} ---")

    _console_log(f"--- missing_books (found, not yet owned) = {len(missing_books)} ---")
    for book in missing_books[:15]:
        _console_log(f"  MISSING: {book.get('title')} | asin={book.get('asin')} | number={book.get('series_number')}")

    _console_log(f"--- upcoming_books (pre-order / future release) = {len(upcoming_books)} ---")
    for book in upcoming_books[:15]:
        _console_log(f"  UPCOMING: {book.get('title')} | asin={book.get('asin')} | expected={book.get('publication_date')}")

    if provider_failures:
        _console_log(f"--- provider_failures (first 10 of {len(provider_failures)}) ---")
        for failure in provider_failures[:10]:
            _console_log(f"  FAILED: {failure.get('provider')} | {failure.get('error')}")

    if terminal_error:
        _console_log(f"terminal_error={terminal_error}")
    _console_log("===== CHECK NOW DEBUG SUMMARY END =====")
