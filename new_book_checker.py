from __future__ import annotations

import logging
import re
from datetime import date, datetime
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote_plus, unquote, urlparse

import httpx
from playwright.sync_api import sync_playwright
from dom_pattern_harvester import harvest_dom_patterns
from provider_core.html_attribute_extractor import extract_html_attribute_metadata
from provider_core.json_extractor import extract_json_objects_from_html
from provider_core.json_parser import parse_json_objects_to_candidates_debug
from providers.amazon.html_adapter import extract_amazon_asins_from_search_html, extract_amazon_candidates_from_html
from providers.amazon.json_adapter import extract_amazon_candidates_from_json
from providers.amazon.product_page_extractor import extract_amazon_product_metadata_from_html

from models import Series


@dataclass
class ProviderSpec:
    name: str
    source_type: str
    url_builder: Callable[[str, str, int], str]
    parser: Callable[[str, str], list[dict]]


USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
REQUEST_TIMEOUT_SECONDS = 10.0
ENABLE_CHECKER_LOGS = False
logger = logging.getLogger(__name__)

RETAIL_DOMAINS = {
    "amazon.com",
    "www.amazon.com",
    "fantasticfiction.com",
    "www.fantasticfiction.com",
}
PUBLISHER_DOMAINS = {
    "penguinrandomhouse.com",
    "www.penguinrandomhouse.com",
    "tor.com",
    "www.tor.com",
    "orbitbooks.net",
    "www.orbitbooks.net",
    "baen.com",
    "www.baen.com",
    "harpercollins.com",
    "www.harpercollins.com",
}
METADATA_WAREHOUSE_TOKENS = {
    "openlibrary",
    "goodreads",
    "isbn",
    "worldcat",
    "librarything",
    "bookfinder",
    "isfdb",
    "wikipedia",
}

PROVIDER_PRIORITY = {
    "publisher_site": 5,
    "author_site": 4,
    "fantasticfiction": 3,
    "amazon_books": 2,
    "google_html_search": 1,
}


def _log(message: str) -> None:
    print(f"[new_book_checker] {message}", flush=True)


def _to_int(value) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number.is_integer():
        return int(number)
    return None


def determine_next_book_number(series: Series) -> int:
    candidates: list[int] = []

    next_upcoming = _to_int(getattr(series, "next_upcoming_book_number", None))
    if next_upcoming:
        candidates.append(next_upcoming)

    next_unread = _to_int(getattr(series, "next_unread_book_number", None))
    if next_unread:
        candidates.append(next_unread)

    missing = getattr(series, "missing_books", None)
    if isinstance(missing, list):
        missing_numbers = sorted(number for number in (_to_int(item) for item in missing) if number)
        if missing_numbers:
            candidates.append(missing_numbers[0])

    highest_owned = _to_int(getattr(series, "highest_owned_book_number", None))
    if highest_owned:
        candidates.append(highest_owned + 1)

    total_books = _to_int(getattr(series, "total_books", None))
    if total_books:
        candidates.append(total_books + 1)

    if candidates:
        return min(candidates)
    return 1


def _google_html_url(query: str) -> str:
    return f"https://www.google.com/search?q={quote_plus(query)}&num=10"


def _amazon_url(series_name: str, author_name: str, next_number: int) -> str:
    clean_series = re.sub(r"\s+", " ", str(series_name or "")).strip()
    clean_author = re.sub(r"\s+", " ", str(author_name or "")).strip()

    if not clean_author:
        query = f"{clean_series} book series".strip()
    elif isinstance(next_number, int) and next_number > 1:
        query = f"{clean_series} {clean_author} book {next_number}".strip()
    else:
        query = f"{clean_series} {clean_author} book series".strip()

    encoded_query = quote_plus(query)
    url = f"https://m.amazon.com/s?k={encoded_query}&ref=nb_sb_noss"
    _log(f"amazon_books url generated: {url}")
    return url


def _fantastic_fiction_url(series_name: str, author_name: str, next_number: int) -> str:
    clean_series = re.sub(r"\s+", " ", str(series_name or "")).strip()
    clean_author = re.sub(r"\s+", " ", str(author_name or "")).strip()
    query = f"{clean_series} {clean_author}".strip()
    encoded_query = quote_plus(query)
    url = f"https://www.fantasticfiction.com/search/?searchfor=book&keywords={encoded_query}"
    _log(f"fantasticfiction url generated: {url}")
    return url


def _publisher_site_url(series_name: str, author_name: str, next_number: int) -> str:
    query = f"\"{series_name}\" (\"book {next_number}\" OR \"#{next_number}\")"
    scoped = f"{query} site:penguinrandomhouse.com OR site:tor.com OR site:orbitbooks.net OR site:baen.com"
    return _google_html_url(scoped)


def _author_site_url(series_name: str, author_name: str, next_number: int) -> str:
    normalized_author = re.sub(r"[^a-z0-9]", "", author_name.lower())
    domain_guess = f"{normalized_author}.com" if normalized_author else ""
    query = f"\"{series_name}\" (\"book {next_number}\" OR \"#{next_number}\")"
    scoped = f"{query} site:{domain_guess}" if domain_guess else query
    return _google_html_url(scoped)


def _google_organic_url(series_name: str, author_name: str, next_number: int) -> str:
    query = f'"{series_name}" "{author_name}" "book {next_number}"'.strip()
    return _google_html_url(query)


def _strip_tags(text: str) -> str:
    collapsed = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", collapsed).strip()


def _safe_html_preview(html: str, max_chars: int = 200) -> str:
    snippet = re.sub(r"\s+", " ", str(html or "")).strip()
    if len(snippet) <= max_chars:
        return snippet
    return f"{snippet[:max_chars]}…(truncated)"


def _emit_provider_html_capture(provider_name: str, html: str) -> None:
    _log(f"{provider_name} HTML snippet captured")
    preview = _safe_html_preview(html, max_chars=200)
    if preview:
        _log(f"{provider_name} HTML preview: {preview}")
    return None


def fetch_provider_html(provider_name: str, url: str, amazon_mode: str = "search") -> dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html",
        "Accept-Language": "en-US,en",
        "Connection": "keep-alive",
    }
    if provider_name == "amazon_books":
        headers.update(
            {
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
                "Referer": "https://m.amazon.com/",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Accept-Language": "en-US,en",
                "Cache-Control": "no-cache",
                "Upgrade-Insecure-Requests": "1",
            }
        )
    if provider_name == "fantasticfiction":
        headers.update(
            {
                "Referer": "https://www.fantasticfiction.com/",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "no-cache",
                "Upgrade-Insecure-Requests": "1",
            }
        )
    final_url = url
    final_headers = headers
    if provider_name == "amazon_books":
        _log(f"Amazon query URL: {url}")
    else:
        _log(f"Provider query URL for {provider_name}: {url}")
    if not str(url or "").strip().lower().startswith("https://"):
        reason = "invalid-url"
        _log(f"Provider {provider_name} failed: {reason}")
        return {
            "ok": False,
            "provider": provider_name,
            "url": url,
            "html": None,
            "content_length": 0,
            "status_code": None,
            "error": reason,
        }

    try:
        if provider_name == "amazon_books":
            with sync_playwright() as p:
                context = p.chromium.launch_persistent_context(
                    user_data_dir="/Users/robbieharrell/Documents/AgenticAI Projects/Book App/.amazon-playwright-profile",
                    headless=False,
                    user_agent=final_headers.get("User-Agent"),
                    viewport={"width": 1366, "height": 768},
                    locale="en-US",
                    timezone_id="America/New_York",
                    java_script_enabled=True,
                    extra_http_headers={
                        **final_headers,
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept-Encoding": "gzip, deflate, br",
                        "Sec-Ch-Ua": '"Chromium";v="126", "Google Chrome";v="126", ";Not A Brand";v="99"',
                        "Sec-Ch-Ua-Mobile": "?0",
                        "Sec-Ch-Ua-Platform": '"macOS"',
                        "Sec-Fetch-Site": "none",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-User": "?1",
                    },
                )
                page = context.new_page()
                goto_response = page.goto(final_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector("body", timeout=15000)

                def _extract_asin_from_url(value: str) -> str | None:
                    if not value:
                        return None
                    match = re.search(r"/dp/([A-Z0-9]{10})", value, flags=re.IGNORECASE)
                    if match:
                        return match.group(1).upper()
                    match = re.search(r"/gp/product/([A-Z0-9]{10})", value, flags=re.IGNORECASE)
                    if match:
                        return match.group(1).upper()
                    return None

                if amazon_mode == "product":
                    target_asin = _extract_asin_from_url(final_url)
                    if not target_asin:
                        target_asin = _extract_asin_from_url(str(page.url or ""))
                    canonical_product_url = f"https://www.amazon.com/dp/{target_asin}" if target_asin else None

                    def _is_product_detail_page() -> bool:
                        current_url = str(page.url or "")
                        looks_like_product_url = "/dp/" in current_url or "/gp/product/" in current_url
                        product_signals = (
                            "#productTitle",
                            "div#dp",
                        )
                        for selector in product_signals:
                            try:
                                if page.locator(selector).count() > 0:
                                    return True
                            except Exception:
                                continue

                        detail_text_signals = (
                            "ASIN",
                            "Publication date",
                        )
                        for text_signal in detail_text_signals:
                            try:
                                if page.locator(f"text={text_signal}").count() > 0:
                                    return True
                            except Exception:
                                continue

                        if looks_like_product_url:
                            try:
                                if page.locator("[data-asin]").count() > 0:
                                    return True
                            except Exception:
                                pass

                        return False

                    max_navigation_attempts = 4
                    navigation_attempt = 0
                    while navigation_attempt < max_navigation_attempts and not _is_product_detail_page():
                        if not canonical_product_url:
                            break
                        goto_response = page.goto(canonical_product_url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(2000)
                        if _is_product_detail_page():
                            break
                        page.wait_for_timeout(1500)
                        navigation_attempt += 1

                html = page.content()
                page.wait_for_timeout(1500)

                class _PlaywrightResponse:
                    def __init__(self, status_code: int):
                        self.status_code = status_code

                response = _PlaywrightResponse(goto_response.status if goto_response else 200)
                context.close()

            _emit_provider_html_capture(provider_name, html)
        else:
            with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, headers=headers, trust_env=False, follow_redirects=True) as client:
                response = client.get(url)
                try:
                    html = response.content.decode("utf-8", errors="replace")
                except Exception:
                    html = str(response.text or "")

            _emit_provider_html_capture(provider_name, html)

        content_length = len(html or "")
        _log(
            f"Provider {provider_name} GET completed "
            f"status={response.status_code} url={url}"
        )
        if response.status_code != 200:
            reason = f"http-{response.status_code}"
            logger.info("Provider %s failed: %s", provider_name, reason)
            return {
                "ok": False,
                "provider": provider_name,
                "url": url,
                "html": None,
                "content_length": content_length,
                "status_code": response.status_code,
                "error": reason,
            }

        if html is None:
            reason = "html-none"
            logger.info("Provider %s failed: %s", provider_name, reason)
            return {
                "ok": False,
                "provider": provider_name,
                "url": url,
                "html": None,
                "content_length": 0,
                "status_code": response.status_code,
                "error": reason,
            }

        if not str(html).strip():
            reason = "html-empty"
            logger.info("Provider %s failed: %s", provider_name, reason)
            return {
                "ok": False,
                "provider": provider_name,
                "url": url,
                "html": None,
                "content_length": content_length,
                "status_code": response.status_code,
                "error": reason,
            }

        return {
            "ok": True,
            "provider": provider_name,
            "url": url,
            "html": html,
            "content_length": content_length,
            "status_code": response.status_code,
            "error": None,
        }
    except httpx.TimeoutException:
        reason = "timeout"
        logger.info("Provider %s failed: %s", provider_name, reason)
        return {
            "ok": False,
            "provider": provider_name,
            "url": url,
            "html": None,
            "content_length": 0,
            "status_code": None,
            "error": reason,
        }
    except httpx.ConnectError:
        reason = "connection-error"
        logger.info("Provider %s failed: %s", provider_name, reason)
        return {
            "ok": False,
            "provider": provider_name,
            "url": url,
            "html": None,
            "content_length": 0,
            "status_code": None,
            "error": reason,
        }
    except httpx.RequestError:
        reason = "request-error"
        logger.info("Provider %s failed: %s", provider_name, reason)
        return {
            "ok": False,
            "provider": provider_name,
            "url": url,
            "html": None,
            "content_length": 0,
            "status_code": None,
            "error": reason,
        }
    except Exception as exc:
        reason = f"provider-exception:{type(exc).__name__}"
        logger.info("Provider %s failed: %s", provider_name, reason)
        return {
            "ok": False,
            "provider": provider_name,
            "url": url,
            "html": None,
            "content_length": 0,
            "status_code": None,
            "error": reason,
        }


def fetch_amazon_search_html(url: str) -> dict:
    _log("Using amazon_books search-page fetch path")
    return fetch_provider_html("amazon_books", url, amazon_mode="search")


def fetch_amazon_product_html(url: str) -> dict:
    _log("Using amazon_books product-page fetch path")
    return fetch_provider_html("amazon_books", url, amazon_mode="product")


def fetch_amazon_html(url: str) -> dict:
    _log("Using amazon_books provider-specific fetch path")
    return fetch_amazon_search_html(url)


def fetch_fantasticfiction_html(url: str) -> dict:
    _log("Using fantasticfiction provider-specific fetch path")
    return fetch_provider_html("fantasticfiction", url)


def fetch_provider_html_by_name(provider_name: str, url: str, amazon_mode: str = "search") -> dict:
    if provider_name == "amazon_books":
        if amazon_mode == "product":
            return fetch_amazon_product_html(url)
        return fetch_amazon_search_html(url)
    if provider_name == "fantasticfiction":
        return fetch_fantasticfiction_html(url)
    return fetch_provider_html(provider_name, url)


def _extract_book_number(text: str) -> int | None:
    match = re.search(r"\b(?:book|volume|vol\.?|#)\s*(\d+)\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _normalize_domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().strip()


def _extract_author_from_text(text: str) -> str | None:
    match = re.search(r"\bby\s+([A-Z][A-Za-z\-\'\s\.]{2,80})", text)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip(" .,-")


def _extract_series_number_pattern(text: str, series_name: str) -> int | None:
    escaped_series = re.escape(series_name)
    patterns = [
        rf"book\s*#?\s*(\d+)\s+in\s+the\s+{escaped_series}\s+series",
        rf"{escaped_series}\s+series\s*[\-|:]?\s*book\s*#?\s*(\d+)",
        rf"{escaped_series}\s*[\-|:]?\s*#\s*(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                continue
    return _extract_book_number(text)


def _extract_publication_date_from_text(text: str) -> str | None:
    if not text:
        return None
    patterns = [
        r"\b(?:on|published\s+on|publication\s+date\s*[:\-]?)\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})\b",
        r"\b([A-Za-z]+\s+\d{1,2},\s+\d{4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = str(match.group(1) or "").strip()
        try:
            parsed = datetime.strptime(candidate, "%B %d, %Y").date()
            return parsed.isoformat()
        except ValueError:
            continue
    return None


def _status_hint_for_amazon(text: str, publication_date_iso: str | None) -> str:
    lowered = str(text or "").lower()
    if "pre-order" in lowered or "preorder" in lowered or "upcoming" in lowered:
        return "upcoming"

    if publication_date_iso:
        try:
            parsed = date.fromisoformat(publication_date_iso)
            if parsed > date.today():
                return "upcoming"
            return "available"
        except ValueError:
            pass
    return "unknown"


def parse_fantastic_fiction_candidates(html: str, series_name: str) -> list[dict]:
    candidates: list[dict] = []

    for card in re.finditer(r'<div[^>]+class="[^"]*(?:booklist|seriesbook|bookitem)[^"]*"[^>]*>(.*?)</div>', html, flags=re.IGNORECASE | re.DOTALL):
        body = card.group(1)
        text = _strip_tags(body)
        link_match = re.search(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body, flags=re.IGNORECASE | re.DOTALL)
        if not link_match:
            continue
        url = link_match.group(1).strip()
        title = _strip_tags(link_match.group(2))
        candidates.append(
            {
                "title": title,
                "author": _extract_author_from_text(text),
                "book_number": _extract_series_number_pattern(text, series_name),
                "url": url,
                "snippet": text,
            }
        )

    if candidates:
        return candidates

    text = _strip_tags(html)
    title_match = re.search(r"<title>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        title = _strip_tags(title_match.group(1))
        return [
            {
                "title": title,
                "author": _extract_author_from_text(text),
                "book_number": _extract_series_number_pattern(text, series_name),
                "url": "",
                "snippet": text,
            }
        ]
    return []


def parse_publisher_or_author_candidates(html: str, series_name: str) -> list[dict]:
    text = _strip_tags(html)
    title_match = re.search(r"<title>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    heading_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, flags=re.IGNORECASE | re.DOTALL)
    title = ""
    if heading_match:
        title = _strip_tags(heading_match.group(1))
    elif title_match:
        title = _strip_tags(title_match.group(1))

    number = _extract_series_number_pattern(text, series_name)
    if not title and number is None:
        return []

    return [
        {
            "title": title or series_name,
            "author": _extract_author_from_text(text),
            "book_number": number,
            "url": "",
            "snippet": text,
        }
    ]


def parse_google_organic_candidates(html: str, series_name: str) -> list[dict]:
    candidates: list[dict] = []
    blocks = list(re.finditer(r'<div[^>]+class="[^"]*g[^"]*"[^>]*>(.*?)</div>', html, flags=re.IGNORECASE | re.DOTALL))
    if not blocks:
        blocks = list(re.finditer(r"(<a href=\"/url\?q=[^\"]+\"[^>]*>.*?</a>)(.*?)(?=<a href=\"/url\?q=|$)", html, flags=re.IGNORECASE | re.DOTALL))

    for block in blocks[:5]:
        body = block.group(1)
        link_match = re.search(r'href="/url\?q=([^"&]+)', body, flags=re.IGNORECASE)
        if not link_match:
            continue
        url = unquote(link_match.group(1)).strip()
        title_match = re.search(r"<h3[^>]*>(.*?)</h3>", body, flags=re.IGNORECASE | re.DOTALL)
        if title_match:
            title = _strip_tags(title_match.group(1))
        else:
            anchor_match = re.search(r"<a[^>]*>(.*?)</a>", body, flags=re.IGNORECASE | re.DOTALL)
            title = _strip_tags(anchor_match.group(1)) if anchor_match else ""

        snippet_match = re.search(r'<div[^>]+class="[^"]*(?:VwiC3b|IsZvec)[^"]*"[^>]*>(.*?)</div>', body, flags=re.IGNORECASE | re.DOTALL)
        snippet = _strip_tags(snippet_match.group(1)) if snippet_match else ""
        merged = f"{title} {snippet}".strip()
        if not title or not url:
            continue
        candidates.append(
            {
                "title": title,
                "author": _extract_author_from_text(merged),
                "book_number": _extract_series_number_pattern(merged, series_name),
                "url": url,
                "snippet": snippet,
            }
        )
    return candidates


def _provider_output_name(provider_name: str) -> str:
    if provider_name == "amazon_books":
        return "amazon"
    return provider_name


def _build_provider_output(provider: ProviderSpec, raw_html: str, series_name: str) -> dict:
    strict_candidates = provider.parser(raw_html, series_name) or []
    if provider.name == "amazon_books":
        strict_candidates = extract_amazon_candidates_from_html(raw_html, series_name) or []

    fallback_candidates = parse_publisher_or_author_candidates(raw_html, series_name) or []
    if provider.name == "amazon_books":
        fallback_candidates = (provider.parser(raw_html, series_name) or []) + fallback_candidates

    return {
        "provider_name": _provider_output_name(provider.name),
        "raw_html": raw_html,
        "strict_candidates": strict_candidates if isinstance(strict_candidates, list) else [],
        "fallback_candidates": fallback_candidates if isinstance(fallback_candidates, list) else [],
    }


def _normalize_candidate_schema(candidate: dict, provider_name: str, source_layer: str, series_name: str) -> dict:
    title = str(candidate.get("title") or "").strip()
    series_value = str(candidate.get("series") or candidate.get("series_name") or series_name).strip()
    asin = str(candidate.get("asin") or candidate.get("asin_or_id") or "").strip().upper()
    isbn = str(candidate.get("isbn") or "").strip()
    release_date = candidate.get("release_date") or candidate.get("publication_date") or candidate.get("publish_date")
    snippet = str(candidate.get("snippet") or "").strip()
    book_number = candidate.get("book_number")
    if book_number is None:
        book_number = _extract_series_number_pattern(f"{title} {snippet}".strip(), series_value or series_name)

    status_signal_text = f"{title} {snippet} {candidate.get('status_hint') or ''} {candidate.get('availability') or ''}".strip().lower()
    if any(token in status_signal_text for token in ("coming soon", "pre-order", "preorder", "releases on")):
        status = "upcoming"
    else:
        status = "published"

    return {
        "title": title,
        "series": series_value,
        "series_name": series_value,
        "book_number": book_number,
        "asin": asin or None,
        "asin_or_id": asin,
        "isbn": isbn or None,
        "release_date": release_date,
        "publication_date": candidate.get("publication_date") or candidate.get("publish_date") or release_date,
        "expected_date": candidate.get("expected_date") or candidate.get("upcoming_date") or release_date,
        "status": status,
        "provider_name": provider_name,
        "provider": provider_name,
        "source_layer": source_layer,
        "extraction_layer": source_layer,
        "author": str(candidate.get("author") or "").strip(),
        "url": str(candidate.get("url") or "").strip(),
        "snippet": snippet,
        "status_hint": str(candidate.get("status_hint") or status).strip().lower() or status,
    }


def _run_dom_harvester_layers(provider_output: dict, series_name: str) -> list[dict]:
    provider_name = str(provider_output.get("provider_name") or "").strip()
    raw_html = str(provider_output.get("raw_html") or "")
    base_url = ""

    json_candidates: list[dict] = []
    decoded_json_blobs = extract_json_objects_from_html(raw_html)
    if provider_name == "amazon":
        adapter_candidates = extract_amazon_candidates_from_json(raw_html) or []
        parsed_candidates, _ = parse_json_objects_to_candidates_debug(decoded_json_blobs)
        json_candidates = adapter_candidates + parsed_candidates
    else:
        generic_json_candidates, _ = parse_json_objects_to_candidates_debug(decoded_json_blobs)
        json_candidates = generic_json_candidates

    html_attribute_candidates: list[dict] = []
    for item in extract_html_attribute_metadata(raw_html):
        asin = str(item.get("asin") or "").strip().upper()
        url = f"https://www.amazon.com/dp/{asin}" if provider_name == "amazon" and asin else base_url
        html_attribute_candidates.append(
            {
                "title": str(item.get("title") or "").strip(),
                "author": str(item.get("author") or "").strip(),
                "series_name": str(item.get("series_name") or "").strip(),
                "book_number": _extract_series_number_pattern(
                    f"{item.get('title') or ''} {item.get('author') or ''}".strip(),
                    series_name,
                ),
                "asin_or_id": asin,
                "url": url,
                "snippet": "dom-html-harvester",
            }
        )

    pattern_candidates: list[dict] = []
    pattern_report = harvest_dom_patterns(raw_html)
    title_match = re.search(r"<title>(.*?)</title>", raw_html, flags=re.IGNORECASE | re.DOTALL)
    title_text = _strip_tags(title_match.group(1)) if title_match else ""
    plain_text = _strip_tags(raw_html)
    if provider_name == "amazon":
        recovered_asins = extract_amazon_asins_from_search_html(raw_html, series_name)
        for recovered_asin in recovered_asins:
            pattern_candidates.append(
                {
                    "title": title_text or series_name,
                    "author": _extract_author_from_text(plain_text) or "",
                    "series_name": series_name,
                    "book_number": _extract_series_number_pattern(plain_text, series_name),
                    "asin_or_id": str(recovered_asin or "").strip().upper(),
                    "url": f"https://www.amazon.com/dp/{str(recovered_asin or '').strip().upper()}",
                    "snippet": "dom-pattern-harvester-amazon",
                }
            )
    else:
        if title_text:
            pattern_candidates.append(
                {
                    "title": title_text,
                    "author": _extract_author_from_text(plain_text) or "",
                    "series_name": series_name,
                    "book_number": _extract_series_number_pattern(plain_text, series_name),
                    "asin_or_id": "",
                    "url": "",
                    "snippet": "dom-pattern-harvester-generic",
                }
            )

    if pattern_report and "pre-order" in pattern_report.lower() and pattern_candidates:
        for item in pattern_candidates:
            item["status_hint"] = "upcoming"

    dom_candidates = []
    dom_candidates.extend(json_candidates)
    dom_candidates.extend(html_attribute_candidates)
    dom_candidates.extend(pattern_candidates)
    return dom_candidates


def _extract_candidates_three_layer(provider_output: dict, series_name: str) -> dict:
    provider_name = str(provider_output.get("provider_name") or "")
    dom_candidates = _run_dom_harvester_layers(provider_output, series_name)
    strict_candidates = provider_output.get("strict_candidates") if isinstance(provider_output.get("strict_candidates"), list) else []
    fallback_candidates = provider_output.get("fallback_candidates") if isinstance(provider_output.get("fallback_candidates"), list) else []

    normalized_strict = [_normalize_candidate_schema(item, provider_name, "strict", series_name) for item in strict_candidates]
    normalized_fallback = [_normalize_candidate_schema(item, provider_name, "fallback", series_name) for item in fallback_candidates]
    normalized_dom = [_normalize_candidate_schema(item, provider_name, "dom", series_name) for item in dom_candidates]

    merged: list[dict] = []
    seen_keys: set[str] = set()
    for bucket in (normalized_strict, normalized_fallback, normalized_dom):
        for candidate in bucket:
            title = str(candidate.get("title") or "").strip()
            if not title:
                continue
            key = "|".join(
                [
                    str(candidate.get("asin") or candidate.get("asin_or_id") or "").strip().upper(),
                    str(candidate.get("isbn") or "").strip().upper(),
                    _normalize_match_text(title),
                    str(candidate.get("book_number") if candidate.get("book_number") is not None else ""),
                ]
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged.append(candidate)

    _log(
        f"Candidate extraction layers for {provider_name}: "
        f"strict={len(normalized_strict)} fallback={len(normalized_fallback)} dom={len(normalized_dom)} merged={len(merged)}"
    )
    return {
        "strict": normalized_strict,
        "fallback": normalized_fallback,
        "dom": normalized_dom,
        "merged": merged,
    }


PROVIDERS: list[ProviderSpec] = [
    ProviderSpec(name="amazon_books", source_type="retail", url_builder=_amazon_url, parser=parse_publisher_or_author_candidates),
    ProviderSpec(name="fantasticfiction", source_type="retail", url_builder=_fantastic_fiction_url, parser=parse_fantastic_fiction_candidates),
    ProviderSpec(name="publisher_site", source_type="publisher", url_builder=_publisher_site_url, parser=parse_publisher_or_author_candidates),
    ProviderSpec(name="author_site", source_type="author", url_builder=_author_site_url, parser=parse_publisher_or_author_candidates),
    ProviderSpec(name="google_html_search", source_type="search", url_builder=_google_organic_url, parser=parse_google_organic_candidates),
]


def _passes_micro_filters(candidate: dict, series_name: str, source_type: str) -> bool:
    url = str(candidate.get("url") or "").strip()
    title = str(candidate.get("title") or "").strip()
    domain = _normalize_domain(url)

    if source_type not in {"retail", "publisher", "author"}:
        if any(token in domain for token in METADATA_WAREHOUSE_TOKENS):
            return False
        if not (domain in RETAIL_DOMAINS or domain in PUBLISHER_DOMAINS):
            return False

    if candidate.get("book_number") is None:
        return False

    candidate_series_name = str(candidate.get("series_name") or "").strip()
    if series_name.lower() not in title.lower() and series_name.lower() not in candidate_series_name.lower():
        return False

    return True


def _micro_filter_reasons(candidate: dict, series_name: str, source_type: str) -> list[str]:
    reasons: list[str] = []
    url = str(candidate.get("url") or "").strip()
    title = str(candidate.get("title") or "").strip()
    domain = _normalize_domain(url)

    if source_type not in {"retail", "publisher", "author"}:
        if any(token in domain for token in METADATA_WAREHOUSE_TOKENS):
            reasons.append("metadata-domain")
        if not (domain in RETAIL_DOMAINS or domain in PUBLISHER_DOMAINS):
            reasons.append("unsupported-domain")

    if candidate.get("book_number") is None:
        reasons.append("missing-book-number")

    candidate_series_name = str(candidate.get("series_name") or "").strip()
    if series_name.lower() not in title.lower() and series_name.lower() not in candidate_series_name.lower():
        reasons.append("series-not-in-title")

    return reasons


def _classify_candidate_signal(candidate: dict, series_name: str, author_name: str) -> str:
    title = str(candidate.get("title") or "").strip()
    candidate_series = str(candidate.get("series") or candidate.get("series_name") or "").strip()
    candidate_author = str(candidate.get("author") or "").strip()
    asin = str(candidate.get("asin") or candidate.get("asin_or_id") or "").strip().upper()
    isbn = str(candidate.get("isbn") or "").strip().upper()
    availability = str(candidate.get("availability") or candidate.get("status_hint") or candidate.get("status") or "").strip().lower()
    snippet = str(candidate.get("snippet") or "").strip().lower()
    url = str(candidate.get("url") or "").strip().lower()
    has_series_signal = bool(series_name and (series_name.lower() in title.lower() or series_name.lower() in candidate_series.lower()))
    if not has_series_signal:
        return "invalid"
    if not _author_matches(author_name, candidate_author):
        return "invalid"
    if any(token in f"{snippet} {url}" for token in METADATA_WAREHOUSE_TOKENS):
        return "invalid"

    if asin or isbn or any(token in availability for token in ("available", "in stock", "published", "released")):
        return "published"

    has_upcoming_signal = any(
        token in f"{title.lower()} {snippet} {availability}"
        for token in ("coming soon", "pre-order", "preorder", "releases on")
    )
    if has_upcoming_signal:
        return "upcoming"

    if title and candidate.get("book_number") is not None and not asin and not isbn:
        return "upcoming"

    return "invalid"


def _passes_minimal_scoring(candidate: dict, series_name: str, author_name: str, expected_number: int) -> bool:
    title = str(candidate.get("title") or "")
    candidate_series_name = str(candidate.get("series_name") or "")
    author = str(candidate.get("author") or "")
    number = candidate.get("book_number")

    title_ok = series_name.lower() in title.lower() or series_name.lower() in candidate_series_name.lower()
    author_ok = _author_matches(author_name, author)
    number_ok = number == expected_number

    return title_ok and author_ok and number_ok


def _rank_candidate(candidate: dict, series_name: str, author_name: str, expected_number: int, provider_name: str) -> int:
    title = str(candidate.get("title") or "")
    candidate_series_name = str(candidate.get("series_name") or "")
    author = str(candidate.get("author") or "")
    number = candidate.get("book_number")

    title_score = 1 if (series_name.lower() in title.lower() or series_name.lower() in candidate_series_name.lower()) else 0
    author_score = 1 if _author_matches(author_name, author) else 0
    number_score = 1 if number == expected_number else 0
    provider_score = PROVIDER_PRIORITY.get(provider_name, 0)
    return title_score + author_score + number_score + provider_score


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_series_name(value: str) -> str:
    cleaned = _normalize_whitespace(value)
    cleaned = re.sub(r"\b(series|book series)\b", "", cleaned, flags=re.IGNORECASE)
    return _normalize_whitespace(cleaned)


def _normalize_author_name(value: str) -> str:
    cleaned = _normalize_whitespace(value)
    cleaned = re.sub(r"\band\s+\d+\s+more\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(author|narrator|editor)\b", "", cleaned, flags=re.IGNORECASE)
    return _normalize_whitespace(cleaned).strip(",-")


def _normalize_title_text(value: str) -> str:
    cleaned = _normalize_whitespace(value)
    # Remove common Amazon edition suffixes from title.
    cleaned = re.sub(
        r"\((?:audible|audible audio|audio cd|kindle|kindle edition|paperback|hardcover|mass market paperback)[^)]*\)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\s*[:\-]\s*(audible|kindle|paperback|hardcover)\b.*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+,\s+book\s+\d+\b", "", cleaned, flags=re.IGNORECASE)
    return _normalize_whitespace(cleaned)


def _extract_book_number_from_text(value: str) -> int | None:
    text = _normalize_whitespace(value)
    patterns = (
        r"\bbook\s*(\d+)\b",
        r"\b#\s*(\d+)\b",
        r"\((?:[^)]*?)book\s*(\d+)(?:[^)]*?)\)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            number = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _parse_date_flexible(value: str | None) -> str | None:
    raw = _normalize_whitespace(str(value or ""))
    if not raw:
        return None

    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            return None

    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _derive_edition_type(value: str) -> str:
    lowered = _normalize_whitespace(value).lower()
    if "audible" in lowered or "audio" in lowered:
        return "audio"
    if "hardcover" in lowered:
        return "hardcover"
    if "paperback" in lowered:
        return "paperback"
    if "kindle" in lowered or "ebook" in lowered:
        return "ebook"
    return "unknown"


def _edition_priority(edition_type: str) -> int:
    priorities = {
        "hardcover": 5,
        "paperback": 4,
        "ebook": 3,
        "audio": 2,
        "unknown": 1,
    }
    return priorities.get(edition_type, 0)


def _build_canonical_amazon_metadata(
    *,
    target_series_name: str,
    metadata_candidate: dict,
) -> tuple[dict | None, list[str]]:
    reasons: list[str] = []

    raw_title = str(metadata_candidate.get("title") or "").strip()
    if not raw_title:
        return None, ["missing-title"]

    title = _normalize_title_text(raw_title)
    if not title:
        return None, ["empty-normalized-title"]

    extracted_series_name = _normalize_series_name(str(metadata_candidate.get("series_name") or ""))
    target_series_normalized = _normalize_series_name(target_series_name)

    # Prefer explicit extracted series name; fallback to target only when title clearly references it.
    if not extracted_series_name and target_series_normalized and target_series_normalized.lower() in raw_title.lower():
        extracted_series_name = target_series_normalized

    raw_author = str(metadata_candidate.get("author") or "").strip()
    author = _normalize_author_name(raw_author)

    raw_book_number = metadata_candidate.get("book_number")
    book_number: int | None = None
    try:
        if raw_book_number is not None and str(raw_book_number).strip() != "":
            parsed = int(float(raw_book_number))
            if parsed > 0:
                book_number = parsed
    except (TypeError, ValueError):
        book_number = None
    if book_number is None:
        book_number = _extract_book_number_from_text(raw_title)

    publish_date = (
        _parse_date_flexible(metadata_candidate.get("publish_date"))
        or _parse_date_flexible(metadata_candidate.get("release_date"))
        or _parse_date_flexible(metadata_candidate.get("publication_date"))
    )
    upcoming_date = _parse_date_flexible(metadata_candidate.get("upcoming_date"))

    availability = str(metadata_candidate.get("availability") or "").strip().lower()
    if availability not in {"available", "upcoming", "unknown"}:
        availability = "unknown"
    if upcoming_date:
        availability = "upcoming"
    elif publish_date and availability == "unknown":
        availability = "available"

    # Reject ambiguous books that fail to carry reliable series info.
    if not extracted_series_name:
        reasons.append("ambiguous-missing-series")
    elif target_series_normalized and not _series_names_match(target_series_normalized, extracted_series_name):
        reasons.append("ambiguous-series-mismatch")

    canonical = {
        "title": title,
        "title_raw": raw_title,
        "author": author,
        "author_raw": raw_author,
        "asin_or_id": str(metadata_candidate.get("asin_or_id") or "").strip().upper(),
        "series_name": extracted_series_name,
        "book_number": book_number,
        "publish_date": publish_date,
        "upcoming_date": upcoming_date,
        "availability": availability,
        "release_date": publish_date,
        "url": str(metadata_candidate.get("url") or "").strip(),
        "title_selector": metadata_candidate.get("title_selector"),
        "edition_type": _derive_edition_type(raw_title),
        "canonical_key": f"{_normalize_match_text(extracted_series_name)}|{book_number if book_number is not None else _normalize_match_text(title)}",
    }
    return canonical, reasons


def _normalize_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _tokenize(value: str) -> set[str]:
    return {token for token in _normalize_match_text(value).split() if token}


def _series_names_match(target_series: str, observed_series: str) -> bool:
    target_norm = _normalize_match_text(target_series)
    observed_norm = _normalize_match_text(observed_series)
    if not target_norm or not observed_norm:
        return False
    if target_norm == observed_norm:
        return True
    if target_norm in observed_norm or observed_norm in target_norm:
        return True

    target_tokens = _tokenize(target_series)
    observed_tokens = _tokenize(observed_series)
    if not target_tokens or not observed_tokens:
        return False

    overlap = len(target_tokens & observed_tokens)
    # Require strong overlap to avoid admitting similarly named but different series.
    return overlap >= max(2, int(len(target_tokens) * 0.75))


def _author_matches(target_author: str, observed_author: str) -> bool:
    target_norm = _normalize_match_text(_normalize_author_name(target_author))
    observed_norm = _normalize_match_text(_normalize_author_name(observed_author))
    if not target_norm or not observed_norm:
        return False
    return target_norm == observed_norm


def _passes_early_author_gate(target_author: str, candidate_author: str) -> bool:
    return _author_matches(target_author, candidate_author)


def _passes_title_series_fallback(
    *,
    target_series_name: str,
    candidate_title: str,
    candidate_series_name: str,
) -> bool:
    target_series_norm = _normalize_match_text(target_series_name)
    title_norm = _normalize_match_text(candidate_title)
    candidate_series_norm = _normalize_match_text(candidate_series_name)
    if not target_series_norm:
        return False
    return bool(
        (title_norm and target_series_norm in title_norm)
        or (candidate_series_norm and target_series_norm in candidate_series_norm)
    )


def _evaluate_hybrid_author_gate(
    *,
    target_author: str,
    candidate_author: str,
    target_series_name: str,
    candidate_title: str,
    candidate_series_name: str,
) -> tuple[bool, bool, str]:
    # Rule order: strict author match is always the first gate.
    if _passes_early_author_gate(target_author, candidate_author):
        return True, False, "strict-author-match"

    # Fallback is allowed only when strict matching cannot operate (missing author data).
    target_author_norm = _normalize_match_text(_normalize_author_name(target_author))
    candidate_author_norm = _normalize_match_text(_normalize_author_name(candidate_author))
    strict_inoperable = not target_author_norm or not candidate_author_norm
    if not strict_inoperable:
        return False, False, "author-mismatch"

    if _passes_title_series_fallback(
        target_series_name=target_series_name,
        candidate_title=candidate_title,
        candidate_series_name=candidate_series_name,
    ):
        return True, True, "fallback-title-series-match"

    return False, False, "fallback-title-series-mismatch"


def _extract_asin_from_value(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    direct = raw.upper()
    if re.fullmatch(r"[A-Z0-9]{10}", direct):
        return direct
    match = re.search(r"/dp/([A-Z0-9]{10})", raw, flags=re.IGNORECASE)
    if match:
        return str(match.group(1) or "").strip().upper()
    match = re.search(r"/gp/product/([A-Z0-9]{10})", raw, flags=re.IGNORECASE)
    if match:
        return str(match.group(1) or "").strip().upper()
    return ""


def _passes_amazon_membership_reconciliation(
    *,
    target_series_name: str,
    target_author_name: str,
    expected_next_number: int,
    title: str,
    extracted_series_name: str,
    extracted_author: str,
    extracted_book_number: int | None,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    if not extracted_series_name:
        reasons.append("missing-series-name")
    elif not _series_names_match(target_series_name, extracted_series_name):
        reasons.append("series-name-mismatch")

    if not target_author_name:
        reasons.append("missing-target-author")
    elif not extracted_author:
        reasons.append("missing-author")
    elif not _author_matches(target_author_name, extracted_author):
        reasons.append("author-mismatch")

    title_norm = _normalize_match_text(title)
    target_series_norm = _normalize_match_text(target_series_name)
    extracted_series_norm = _normalize_match_text(extracted_series_name)
    if not title_norm:
        reasons.append("title-empty")
    else:
        has_target_series_in_title = bool(target_series_norm and target_series_norm in title_norm)
        has_extracted_series_in_title = bool(extracted_series_norm and extracted_series_norm in title_norm)
        if not has_target_series_in_title and not has_extracted_series_in_title:
            reasons.append("title-series-guard-failed")

        # Ambiguous titles that mention a series, but not the expected one.
        if "series" in title_norm and not has_target_series_in_title:
            reasons.append("ambiguous-series-title")

    if extracted_book_number is not None:
        if extracted_book_number <= 0:
            reasons.append("invalid-book-number")
        elif extracted_book_number > max(200, expected_next_number + 40):
            reasons.append("book-number-out-of-range")

    return len(reasons) == 0, reasons


def _candidate_summary(candidate: dict, score: int) -> str:
    return (
        f"title={str(candidate.get('title') or '').strip()!r} "
        f"number={candidate.get('book_number')} "
        f"author={str(candidate.get('author') or '').strip()!r} "
        f"url={str(candidate.get('url') or '').strip()!r} "
        f"score={score}"
    )


def _print_candidate_extraction(provider_name: str, candidate: dict) -> None:
    title = str(candidate.get("title") or "").strip() or "Unknown title"
    asin = str(candidate.get("asin_or_id") or candidate.get("asin") or "").strip().upper() or "NO-ASIN"
    _log(f"Candidate: {title} {asin}")
    return None


def check_for_new_book(series: Series, progress_callback: Callable[[dict], None] | None = None) -> dict:
    series_id = getattr(series, "id", None)
    series_name = str(getattr(series, "name", "") or "").strip()
    author_name = str(getattr(series, "author", "") or "").strip()
    _log(f"CHECK NOW triggered for series: {series_name}")

    next_number = determine_next_book_number(series)

    if not series_name:
        return {"found": False, "candidate": None}

    ranked: list[tuple[int, dict, str]] = []
    provider_failures: list[dict] = []
    successful_html_count = 0
    amazon_book_candidates: list[dict] = []
    amazon_asin_candidates: list[dict] = []
    seen_amazon_asins: set[str] = set()
    amazon_product_fetch_success = 0
    amazon_product_fetch_failed = 0
    amazon_product_metadata_hits = 0
    first_extracted_product_metadata: dict | None = None
    first_product_extraction_failure: dict | None = None

    for provider in PROVIDERS:
        query_url = provider.url_builder(series_name, author_name, next_number)
        if provider.name == "amazon_books":
            _log(f"Amazon query URL: {query_url}")
        else:
            _log(f"Checking provider {provider.name}")

        if provider.name == "amazon_books" and not query_url.startswith("https://m.amazon.com/s?k="):
            _log("Classification result: INVALID")
        if provider.name == "fantasticfiction" and not query_url.startswith("https://www.fantasticfiction.com/search/"):
            _log("Classification result: INVALID")

        fetch_result = fetch_provider_html_by_name(provider.name, query_url)
        if not fetch_result.get("ok"):
            error_message = str(fetch_result.get("error") or "no-html")
            _log(f"Provider {provider.name} returned no usable results: {error_message}")
            provider_failures.append(
                {
                    "provider": provider.name,
                    "query": query_url,
                    "error": error_message,
                }
            )
            continue
        html = str(fetch_result.get("html") or "")
        _log(f"Provider {provider.name} returned HTML")
        successful_html_count += 1

        provider_output = _build_provider_output(provider, html, series_name)
        extraction_result = _extract_candidates_three_layer(provider_output, series_name)

        if provider.name == "amazon_books":
            asin_hits = extract_amazon_asins_from_search_html(html, series_name)
            search_page_candidates = extraction_result.get("merged") or []
            parsed_candidates = []
            amazon_book_candidates = []
            amazon_asin_candidates = []

            search_page_author_by_asin: dict[str, str] = {}
            search_page_title_by_asin: dict[str, str] = {}
            search_page_series_by_asin: dict[str, str] = {}
            for search_candidate in search_page_candidates:
                search_asin = _extract_asin_from_value(
                    str(search_candidate.get("asin_or_id") or search_candidate.get("asin") or search_candidate.get("url") or "")
                )
                if not search_asin:
                    continue
                search_author = str(search_candidate.get("author") or "").strip()
                search_title = str(search_candidate.get("title") or "").strip()
                search_series = str(search_candidate.get("series_name") or "").strip()
                if search_asin not in search_page_author_by_asin:
                    search_page_author_by_asin[search_asin] = search_author
                if search_asin not in search_page_title_by_asin:
                    search_page_title_by_asin[search_asin] = search_title
                if search_asin not in search_page_series_by_asin:
                    search_page_series_by_asin[search_asin] = search_series

            for asin_hit in asin_hits:
                asin_value = str(asin_hit or "").strip().upper()
                if not asin_value or asin_value in seen_amazon_asins:
                    continue

                predicted_author = str(search_page_author_by_asin.get(asin_value) or "").strip()
                predicted_title = str(search_page_title_by_asin.get(asin_value) or "").strip()
                predicted_series_name = str(search_page_series_by_asin.get(asin_value) or "").strip()

                allow_candidate, used_fallback, gate_reason = _evaluate_hybrid_author_gate(
                    target_author=author_name,
                    candidate_author=predicted_author,
                    target_series_name=series_name,
                    candidate_title=predicted_title,
                    candidate_series_name=predicted_series_name,
                )
                if not allow_candidate:
                    _log("Classification result: INVALID")
                    if progress_callback is not None:
                        progress_callback(
                            {
                                "total": len(asin_hits),
                                "completed": 0,
                                "current_book_number": None,
                                "current_pass": "amazon-early-author-gate",
                                "current_asin": asin_value,
                                "asins_discovered": len(asin_hits),
                                "asins_processed": 0,
                                "asin_fetch_success": amazon_product_fetch_success,
                                "asin_fetch_failed": amazon_product_fetch_failed,
                            }
                        )
                    continue

                seen_amazon_asins.add(asin_value)
                amazon_asin_candidates.append(
                    {
                        "asin": asin_value,
                        "title": predicted_title,
                        "author": predicted_author,
                        "series_name": predicted_series_name,
                        "used_title_fallback": used_fallback,
                        "early_gate_reason": gate_reason,
                        "url": f"https://www.amazon.com/dp/{asin_value}",
                    }
                )

            _log(f"ASINs extracted: {[item.get('asin') for item in amazon_asin_candidates]}")
            _log(f"Candidates found: {len(amazon_asin_candidates)}")

            if progress_callback is not None:
                progress_callback(
                    {
                        "total": len(amazon_asin_candidates),
                        "completed": 0,
                        "current_book_number": None,
                        "current_pass": "amazon-product-fetch",
                        "current_asin": None,
                        "asins_discovered": len(amazon_asin_candidates),
                        "asins_processed": 0,
                        "asin_fetch_success": 0,
                        "asin_fetch_failed": 0,
                    }
                )

            seen_amazon_book_keys: set[tuple[str, str]] = set()
            pending_canonical_candidates: list[dict] = []
            for index, asin_hit in enumerate(amazon_asin_candidates, start=1):
                asin_value = str(asin_hit.get("asin") or "").strip().upper()
                product_url = f"https://www.amazon.com/dp/{asin_value}"

                product_fetch_result = fetch_provider_html_by_name("amazon_books", product_url, amazon_mode="product")
                if not product_fetch_result.get("ok"):
                    amazon_product_fetch_failed += 1
                    provider_failures.append(
                        {
                            "provider": "amazon_books_product",
                            "query": product_url,
                            "error": str(product_fetch_result.get("error") or "no-html"),
                        }
                    )
                    if progress_callback is not None:
                        progress_callback(
                            {
                                "total": len(amazon_asin_candidates),
                                "completed": index,
                                "current_book_number": None,
                                "current_pass": "amazon-product-fetch",
                                "current_asin": asin_value,
                                "asins_discovered": len(amazon_asin_candidates),
                                "asins_processed": index,
                                "asin_fetch_success": amazon_product_fetch_success,
                                "asin_fetch_failed": amazon_product_fetch_failed,
                            }
                        )
                    continue

                amazon_product_fetch_success += 1
                product_html = str(product_fetch_result.get("html") or "")
                metadata_candidate = extract_amazon_product_metadata_from_html(
                    product_html,
                    product_url,
                    expected_asin=asin_value,
                )

                failure_reason = str(metadata_candidate.get("failure_reason") or "").strip()
                if failure_reason:
                    if first_product_extraction_failure is None:
                        first_product_extraction_failure = {
                            "failure_reason": failure_reason,
                            "asin_or_id": str(metadata_candidate.get("asin_or_id") or asin_value).strip().upper(),
                            "expected_asin": str(metadata_candidate.get("expected_asin") or asin_value).strip().upper(),
                            "title_selector": metadata_candidate.get("title_selector"),
                            "url": str(metadata_candidate.get("url") or product_url).strip() or product_url,
                        }
                else:
                    if first_extracted_product_metadata is None:
                        first_extracted_product_metadata = {
                            "title": metadata_candidate.get("title"),
                            "author": metadata_candidate.get("author"),
                            "asin_or_id": metadata_candidate.get("asin_or_id"),
                            "series_name": metadata_candidate.get("series_name"),
                            "book_number": metadata_candidate.get("book_number"),
                            "publish_date": metadata_candidate.get("publish_date"),
                            "upcoming_date": metadata_candidate.get("upcoming_date"),
                            "availability": metadata_candidate.get("availability"),
                            "title_selector": metadata_candidate.get("title_selector"),
                            "url": metadata_candidate.get("url"),
                        }

                    canonical, normalization_reasons = _build_canonical_amazon_metadata(
                        target_series_name=series_name,
                        metadata_candidate={
                            **metadata_candidate,
                            "asin_or_id": str(metadata_candidate.get("asin_or_id") or asin_value).strip().upper(),
                            "url": str(metadata_candidate.get("url") or product_url).strip() or product_url,
                        },
                    )
                    if canonical is None:
                        _log("Classification result: INVALID")
                        continue
                    if normalization_reasons:
                        _log("Classification result: INVALID")
                        continue

                    candidate_asin = str(canonical.get("asin_or_id") or asin_value).strip().upper()
                    title = str(canonical.get("title") or "").strip()
                    author = str(canonical.get("author") or "").strip()
                    extracted_series_name = str(canonical.get("series_name") or "").strip()
                    resolved_book_number = canonical.get("book_number")
                    publish_date = canonical.get("publish_date")
                    upcoming_date = canonical.get("upcoming_date")
                    availability = str(canonical.get("availability") or "").strip().lower()
                    release_date = canonical.get("release_date")
                    candidate_url = str(canonical.get("url") or product_url).strip() or product_url

                    # Fallback path always requires strict author validation after product fetch.
                    if not _passes_early_author_gate(author_name, author):
                        _log("Classification result: INVALID")
                        continue

                    is_member_match, membership_reasons = _passes_amazon_membership_reconciliation(
                        target_series_name=series_name,
                        target_author_name=author_name,
                        expected_next_number=next_number,
                        title=title,
                        extracted_series_name=extracted_series_name,
                        extracted_author=author,
                        extracted_book_number=resolved_book_number,
                    )
                    if not is_member_match:
                        _log("Classification result: INVALID")
                        continue

                    dedupe_key = (candidate_asin or asin_value, title.lower())
                    if dedupe_key in seen_amazon_book_keys:
                        continue
                    seen_amazon_book_keys.add(dedupe_key)
                    pending_canonical_candidates.append(canonical)

                if progress_callback is not None:
                    progress_callback(
                        {
                            "total": len(amazon_asin_candidates),
                            "completed": index,
                            "current_book_number": None,
                            "current_pass": "amazon-product-fetch",
                            "current_asin": asin_value,
                            "asins_discovered": len(amazon_asin_candidates),
                            "asins_processed": index,
                            "asin_fetch_success": amazon_product_fetch_success,
                            "asin_fetch_failed": amazon_product_fetch_failed,
                        }
                    )

            merged_by_key: dict[str, dict] = {}
            for canonical in pending_canonical_candidates:
                key = str(canonical.get("canonical_key") or "").strip()
                if not key:
                    key = f"asin:{canonical.get('asin_or_id') or ''}"

                current = merged_by_key.get(key)
                if current is None:
                    merged_by_key[key] = canonical
                    continue

                current_priority = _edition_priority(str(current.get("edition_type") or "unknown"))
                candidate_priority = _edition_priority(str(canonical.get("edition_type") or "unknown"))
                current_date_score = 1 if current.get("publish_date") else 0
                candidate_date_score = 1 if canonical.get("publish_date") else 0

                if (candidate_priority, candidate_date_score) > (current_priority, current_date_score):
                    merged_by_key[key] = canonical

            for canonical in merged_by_key.values():
                amazon_product_metadata_hits += 1

                normalized_book_candidate = {
                    "title": canonical.get("title"),
                    "author": canonical.get("author"),
                    "asin_or_id": canonical.get("asin_or_id"),
                    "release_date": canonical.get("release_date"),
                    "series_name": canonical.get("series_name"),
                    "book_number": canonical.get("book_number"),
                    "publish_date": canonical.get("publish_date"),
                    "upcoming_date": canonical.get("upcoming_date"),
                    "availability": canonical.get("availability") or "unknown",
                    "url": canonical.get("url"),
                    "edition_type": canonical.get("edition_type"),
                    "title_selector": canonical.get("title_selector"),
                }
                amazon_book_candidates.append(normalized_book_candidate)

                availability = str(canonical.get("availability") or "").strip().lower()
                if availability == "upcoming":
                    status_hint = "upcoming"
                elif availability == "available":
                    status_hint = "available"
                else:
                    status_hint = _status_hint_for_amazon(
                        f"{canonical.get('title') or ''} {canonical.get('author') or ''}".strip(),
                        canonical.get("release_date"),
                    )

                parsed_candidates.append(
                    {
                        "title": canonical.get("title"),
                        "author": canonical.get("author"),
                        "series_name": canonical.get("series_name"),
                        "book_number": canonical.get("book_number") if canonical.get("book_number") is not None else _extract_book_number(str(canonical.get("title") or "")),
                        "url": canonical.get("url"),
                        "snippet": "",
                        "publication_date": canonical.get("publish_date"),
                        "expected_date": canonical.get("upcoming_date"),
                        "status_hint": status_hint,
                        "asin_or_id": canonical.get("asin_or_id"),
                        "canonical_metadata": {
                            "title_normalized": canonical.get("title"),
                            "series_name_normalized": canonical.get("series_name"),
                            "book_number_normalized": canonical.get("book_number"),
                            "publish_date_normalized": canonical.get("publish_date"),
                            "upcoming_date_normalized": canonical.get("upcoming_date"),
                            "availability": canonical.get("availability"),
                            "edition_type": canonical.get("edition_type"),
                            "title_selector": canonical.get("title_selector"),
                        },
                    }
                )
        else:
            parsed_candidates = extraction_result.get("merged") or []
        _log(f"Candidates found: {len(parsed_candidates)}")
        if provider.name in {"amazon_books", "fantasticfiction"}:
            for parsed_candidate in parsed_candidates:
                _print_candidate_extraction(provider.name, parsed_candidate)

        for candidate in parsed_candidates:
            candidate_author = str(candidate.get("author") or "").strip()
            candidate_title = str(candidate.get("title") or "").strip()
            candidate_series_name = str(candidate.get("series_name") or "").strip()
            allow_candidate, used_fallback, gate_reason = _evaluate_hybrid_author_gate(
                target_author=author_name,
                candidate_author=candidate_author,
                target_series_name=series_name,
                candidate_title=candidate_title,
                candidate_series_name=candidate_series_name,
            )
            if not allow_candidate:
                _log("Classification result: INVALID")
                continue

            # Shared-loop fallback requires post-fetch strict validation capability.
            # Amazon fallback candidates are re-validated with strict author matching after product fetch.
            if used_fallback and provider.name != "amazon_books":
                _log("Classification result: INVALID")
                continue

            classification = _classify_candidate_signal(candidate, series_name, author_name)
            if classification == "invalid":
                _log("Classification result: INVALID")
                continue
            candidate["status"] = classification
            candidate["status_hint"] = classification

            reasons = _micro_filter_reasons(candidate, series_name, provider.source_type)
            if reasons:
                _log("Classification result: INVALID")
                continue

            score = _rank_candidate(candidate, series_name, author_name, next_number, provider.name)
            ranked.append((score, candidate, provider.name))

    if ranked:
        ranked.sort(
            key=lambda item: (
                item[0],
                1 if _passes_minimal_scoring(item[1], series_name, author_name, next_number) else 0,
            ),
            reverse=True,
        )
        best_score, best_candidate, best_provider = ranked[0]
        final_classification = str(best_candidate.get("status") or "published").strip().lower()
        _log(f"Classification result: {final_classification.upper()}")
        _log(f"CHECK NOW completed successfully for series: {series_name}")
        return {
            "found": True,
            "candidate": {
                "title": str(best_candidate.get("title") or "").strip(),
                "author": str(best_candidate.get("author") or author_name).strip(),
                "number": str(best_candidate.get("book_number") or "").strip(),
                "url": str(best_candidate.get("url") or "").strip(),
                "provider": best_provider,
                "publication_date": best_candidate.get("publication_date"),
                "expected_date": best_candidate.get("expected_date"),
                "status_hint": best_candidate.get("status_hint"),
                "asin_or_id": best_candidate.get("asin_or_id"),
            },
            "provider_failures": provider_failures,
            "all_providers_failed": False,
            "amazon_book_candidates": amazon_book_candidates,
            "amazon_asin_candidates": amazon_asin_candidates,
            "asin_discovery": {
                "discovered": len(amazon_asin_candidates),
                "processed": len(amazon_asin_candidates),
                "fetch_success": amazon_product_fetch_success,
                "fetch_failed": amazon_product_fetch_failed,
                "metadata_hits": amazon_product_metadata_hits,
            },
            "first_extracted_product_metadata": first_extracted_product_metadata,
            "first_product_extraction_failure": first_product_extraction_failure,
        }

    return {
        "found": False,
        "candidate": None,
        "provider_failures": provider_failures,
        "all_providers_failed": successful_html_count == 0,
        "amazon_book_candidates": amazon_book_candidates,
        "amazon_asin_candidates": amazon_asin_candidates,
        "asin_discovery": {
            "discovered": len(amazon_asin_candidates),
            "processed": len(amazon_asin_candidates),
            "fetch_success": amazon_product_fetch_success,
            "fetch_failed": amazon_product_fetch_failed,
            "metadata_hits": amazon_product_metadata_hits,
        },
        "first_extracted_product_metadata": first_extracted_product_metadata,
        "first_product_extraction_failure": first_product_extraction_failure,
    }
