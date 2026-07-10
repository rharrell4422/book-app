from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote_plus, unquote

import httpx
from dom_pattern_harvester import harvest_dom_patterns
from provider_core.html_attribute_extractor import extract_html_attribute_metadata_with_metrics
from provider_core.json_extractor import extract_json_objects_from_html_with_metrics
from provider_core.json_parser import parse_json_objects_to_candidates_debug
from providers.amazon.asin_series_provider import discover_series_candidates_from_seed_asins
from providers.amazon.html_adapter import extract_amazon_asins_from_search_html, extract_amazon_candidates_from_html
from providers.amazon.json_adapter import extract_amazon_candidates_from_json

from checker_rules import _extract_author_from_text, _extract_book_number, _extract_series_number_pattern, _is_clearly_non_book_candidate, _normalize_match_text, _strip_tags


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

AMAZON_RETRY_ATTEMPTS = 3
AMAZON_MOBILE_HOST = "m.amazon.com"
AMAZON_DELAY_RANGE_SECONDS = (0.35, 1.15)

AMAZON_HEADER_PROFILES: tuple[dict[str, str], ...] = (
    {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        "Sec-Ch-Ua-Mobile": "?1",
        "Sec-Ch-Ua-Platform": '"iOS"',
    },
    {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.6478.122 Mobile Safari/537.36",
        "Sec-Ch-Ua-Mobile": "?1",
        "Sec-Ch-Ua-Platform": '"Android"',
    },
    {
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; SAMSUNG SM-S918U) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/121.0.6167.164 Mobile Safari/537.36",
        "Sec-Ch-Ua-Mobile": "?1",
        "Sec-Ch-Ua-Platform": '"Android"',
    },
)

AMAZON_HTML_FALLBACK_CACHE: dict[str, dict] = {}


def _log(message: str) -> None:
    print(f"[new_book_checker] {message}", flush=True)


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


def _amazon_series_page_url(series_name: str, author_name: str, next_number: int) -> str:
    del next_number
    clean_series = re.sub(r"\s+", " ", str(series_name or "")).strip()
    clean_author = re.sub(r"\s+", " ", str(author_name or "")).strip()
    query = f"{clean_series} {clean_author}".strip()
    encoded_query = quote_plus(query)
    url = f"https://m.amazon.com/s?k={encoded_query}&i=stripbooks"
    _log(f"amazon_series_page url generated: {url}")
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
    query = f'"{series_name}" ("book {next_number}" OR "#{next_number}")'
    scoped = f"{query} site:penguinrandomhouse.com OR site:tor.com OR site:orbitbooks.net OR site:baen.com"
    return _google_html_url(scoped)


def _author_site_url(series_name: str, author_name: str, next_number: int) -> str:
    normalized_author = re.sub(r"[^a-z0-9]", "", author_name.lower())
    domain_guess = f"{normalized_author}.com" if normalized_author else ""
    query = f'"{series_name}" ("book {next_number}" OR "#{next_number}")'
    scoped = f"{query} site:{domain_guess}" if domain_guess else query
    return _google_html_url(scoped)


def _google_organic_url(series_name: str, author_name: str, next_number: int) -> str:
    query = f'"{series_name}" "{author_name}" "book {next_number}"'.strip()
    return _google_html_url(query)


def _asin_series_seed_url(series_name: str, author_name: str, next_number: int) -> str:
    del author_name, next_number
    return f"asin-series://{quote_plus(series_name)}"


def _amazon_author_discovery_url(series_name: str, author_name: str, next_number: int) -> str:
    del series_name, next_number
    clean_author = re.sub(r"\s+", " ", str(author_name or "")).strip()
    encoded_rh = quote_plus(f"p_27:{clean_author}") if clean_author else ""
    url = f"https://m.amazon.com/s?i=stripbooks&rh={encoded_rh}&s=date-desc-rank"
    _log(f"author_discovery_amazon url generated: {url}")
    return url


def _emit_provider_html_capture(provider_name: str, html: str) -> None:
    del provider_name, html
    return None


def _amazon_cache_key(url: str, amazon_mode: str) -> str:
    return f"{amazon_mode}|{str(url or '').strip()}"


def _rewrite_to_mobile_amazon_url(url: str) -> str:
    raw_url = str(url or "").strip()
    if not raw_url:
        return raw_url
    return re.sub(r"^https://(?:www\.)?amazon\.com", f"https://{AMAZON_MOBILE_HOST}", raw_url, flags=re.IGNORECASE)


def _build_amazon_headers(profile_index: int) -> dict[str, str]:
    profile = AMAZON_HEADER_PROFILES[profile_index % len(AMAZON_HEADER_PROFILES)]
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Pragma": "no-cache",
        "Referer": "https://m.amazon.com/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-User": "?1",
        **profile,
    }


def _is_amazon_bot_or_error_page(html: str, status_code: int | None) -> bool:
    lowered = str(html or "").lower()
    blocked_tokens = (
        "enter the characters you see below",
        "to discuss automated access to amazon data",
        "sorry, we just need to make sure you're not a robot",
        "api-services-support@amazon.com",
        "captcha",
        "robot check",
        "automated access",
        "503 service unavailable",
    )
    if any(token in lowered for token in blocked_tokens):
        return True
    return status_code in {429, 503}


def _apply_amazon_request_delay(attempt: int) -> None:
    jitter_floor, jitter_ceiling = AMAZON_DELAY_RANGE_SECONDS
    jitter = random.uniform(jitter_floor, jitter_ceiling)
    backoff = min(0.8, attempt * 0.2)
    time.sleep(jitter + backoff)


def fetch_provider_html(provider_name: str, url: str, amazon_mode: str = "search") -> dict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html",
        "Accept-Language": "en-US,en",
        "Connection": "keep-alive",
    }
    is_amazon_provider = provider_name in {"amazon_books", "amazon_series_page", "author_discovery_amazon"}
    if provider_name == "fantasticfiction":
        headers.update(
            {
                "Referer": "https://www.fantasticfiction.com/",
                "Accept-Encoding": "gzip, deflate, br",
                "Cache-Control": "no-cache",
                "Upgrade-Insecure-Requests": "1",
            }
        )
    final_url = _rewrite_to_mobile_amazon_url(url) if is_amazon_provider else url
    if not str(url or "").strip().lower().startswith("https://"):
        reason = "invalid-url"
        return {
            "ok": False,
            "provider": provider_name,
            "url": url,
            "html": None,
            "content_length": 0,
            "status_code": None,
            "error": reason,
        }

    max_attempts = AMAZON_RETRY_ATTEMPTS if is_amazon_provider else 1
    cache_key = _amazon_cache_key(final_url, amazon_mode)
    last_error = "request-error"
    last_status_code: int | None = None
    last_blocked = False

    for attempt in range(1, max_attempts + 1):
        request_headers = headers
        header_profile_id: str | None = None
        if is_amazon_provider:
            profile_index = random.randrange(len(AMAZON_HEADER_PROFILES))
            request_headers = _build_amazon_headers(profile_index)
            header_profile_id = f"amazon_mobile_profile_{profile_index + 1}"
            _apply_amazon_request_delay(attempt)

        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS, headers=request_headers, trust_env=False, follow_redirects=True) as client:
                response = client.get(final_url)
            try:
                html = response.content.decode("utf-8", errors="replace")
            except Exception:
                html = str(response.text or "")
        except httpx.TimeoutException:
            last_error = "timeout"
            continue
        except httpx.ConnectError:
            last_error = "connection-error"
            continue
        except httpx.RequestError:
            last_error = "request-error"
            continue
        except Exception as exc:
            last_error = f"provider-exception:{type(exc).__name__}"
            continue

        content_length = len(html or "")
        last_status_code = response.status_code
        _log(f"Provider {provider_name} GET completed status={response.status_code} url={final_url} attempt={attempt}")

        if response.status_code != 200:
            last_error = f"http-{response.status_code}"
            continue
        if html is None:
            last_error = "html-none"
            continue
        if not str(html).strip():
            last_error = "html-empty"
            continue

        blocked = is_amazon_provider and _is_amazon_bot_or_error_page(html, response.status_code)
        if blocked:
            last_error = "amazon-bot-blocked"
            last_blocked = True
            continue

        if is_amazon_provider:
            AMAZON_HTML_FALLBACK_CACHE[cache_key] = {
                "html": html,
                "status_code": response.status_code,
                "header_profile": header_profile_id,
            }

        _emit_provider_html_capture(provider_name, html)
        return {
            "ok": True,
            "provider": provider_name,
            "url": final_url,
            "raw_html": html,
            "html": html,
            "content_length": content_length,
            "status_code": response.status_code,
            "error": None,
            "fetch_attempts": attempt,
            "header_profile": header_profile_id,
            "cache_fallback": False,
            "bot_blocked": False,
        }

    if is_amazon_provider:
        cached = AMAZON_HTML_FALLBACK_CACHE.get(cache_key)
        if isinstance(cached, dict):
            cached_html = str(cached.get("html") or "")
            if cached_html:
                _log(f"Provider {provider_name} using cached HTML fallback url={final_url}")
                return {
                    "ok": True,
                    "provider": provider_name,
                    "url": final_url,
                    "raw_html": cached_html,
                    "html": cached_html,
                    "content_length": len(cached_html),
                    "status_code": int(cached.get("status_code") or 200),
                    "error": f"{last_error}-cached-fallback",
                    "fetch_attempts": max_attempts,
                    "header_profile": cached.get("header_profile"),
                    "cache_fallback": True,
                    "bot_blocked": last_blocked,
                }

    return {
        "ok": False,
        "provider": provider_name,
        "url": final_url,
        "html": None,
        "content_length": 0,
        "status_code": last_status_code,
        "error": last_error,
        "fetch_attempts": max_attempts,
        "header_profile": None,
        "cache_fallback": False,
        "bot_blocked": last_blocked,
    }


def fetch_amazon_search_html(url: str) -> dict:
    return fetch_provider_html("amazon_books", url, amazon_mode="search")


def fetch_amazon_product_html(url: str) -> dict:
    return fetch_provider_html("amazon_books", url, amazon_mode="product")


def fetch_amazon_html(url: str) -> dict:
    return fetch_amazon_search_html(url)


def fetch_fantasticfiction_html(url: str) -> dict:
    return fetch_provider_html("fantasticfiction", url)


def fetch_provider_html_by_name(provider_name: str, url: str, amazon_mode: str = "search") -> dict:
    if provider_name in {"amazon_books", "amazon_series_page", "author_discovery_amazon"}:
        if amazon_mode == "product":
            return fetch_amazon_product_html(url)
        return fetch_amazon_search_html(url)
    if provider_name == "fantasticfiction":
        return fetch_fantasticfiction_html(url)
    return fetch_provider_html(provider_name, url)


def parse_amazon_candidates(html: str, series_name: str) -> list[dict]:
    candidates = extract_amazon_candidates_from_html(html, series_name) or []
    return [candidate for candidate in candidates if not _is_garbage_candidate(candidate)]


def parse_author_discovery_amazon_candidates(html: str, series_name: str) -> list[dict]:
    del series_name
    candidates: list[dict] = []
    seen_asins: set[str] = set()

    link_pattern = re.compile(
        r'<a[^>]+href="([^"]*/dp/([A-Z0-9]{10})[^"]*)"[^>]*>(.*?)</a>',
        flags=re.IGNORECASE | re.DOTALL,
    )
    title_pattern = re.compile(r'<span[^>]+class="[^"]*a-size-medium[^"]*"[^>]*>(.*?)</span>', flags=re.IGNORECASE | re.DOTALL)
    image_pattern = re.compile(r'<img[^>]+src="([^"]+)"', flags=re.IGNORECASE)
    author_pattern = re.compile(
        r'(?:<span[^>]+class="[^"]*(?:a-size-base|a-color-secondary|a-size-small)[^"]*"[^>]*>\s*)?(?:by\s+)([^<\n\r]+)',
        flags=re.IGNORECASE,
    )
    pub_pattern = re.compile(
        r'(?:Publication date|Published|Release date)\s*</span>\s*<span[^>]*>(.*?)</span>',
        flags=re.IGNORECASE | re.DOTALL,
    )

    def _looks_like_non_book_artifact(title_text: str, card_text: str) -> bool:
        blob = f"{title_text} {card_text}".strip().lower()
        artifact_tokens = (
            "sponsored",
            "ad feedback",
            "shop all",
            "see all",
            "customer reviews",
            "best sellers",
            "results",
            "collections",
            "collection",
            "author page",
            "storefront",
            "discover more",
        )
        return any(token in blob for token in artifact_tokens)

    for match in link_pattern.finditer(html):
        asin = str(match.group(2) or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{10}", asin) or asin in seen_asins:
            continue

        href = str(match.group(1) or "").strip()
        anchor_body = str(match.group(3) or "")
        window_start = max(0, match.start() - 2000)
        window_end = min(len(html), match.end() + 2000)
        card_html = html[window_start:window_end]

        title_match = title_pattern.search(card_html)
        title = _strip_tags(title_match.group(1)) if title_match else _strip_tags(anchor_body)
        title = str(title or "").strip()
        if not title:
            continue

        card_text = _strip_tags(card_html)
        if _looks_like_non_book_artifact(title, card_text):
            continue

        author = ""
        author_match = author_pattern.search(card_html)
        if author_match:
            author = _strip_tags(author_match.group(1)).strip()
        if not author:
            byline_match = re.search(r'\bby\s+([^\|\n\r]+)', card_text, flags=re.IGNORECASE)
            if byline_match:
                author = str(byline_match.group(1) or "").strip(" -:|,\t ")
        if not author:
            continue

        image_match = image_pattern.search(card_html)
        cover_url = str(image_match.group(1) or "").strip() if image_match else ""

        publication_date = None
        pub_match = pub_pattern.search(card_html)
        if pub_match:
            publication_date = _strip_tags(pub_match.group(1)).strip() or None

        if href.startswith("/"):
            url = f"https://m.amazon.com{href}"
        else:
            url = href

        seen_asins.add(asin)
        candidates.append(
            {
                "title": title,
                "author": author,
                "asin": asin,
                "asin_or_id": asin,
                "retailer_id": asin,
                "cover_url": cover_url,
                "publication_date": publication_date,
                "url": url,
                "provider": "author_discovery_amazon",
                "source_layer": "author_discovery",
            }
        )

    return candidates


def _extract_visible_retailer_id(value: str) -> str:
    text = str(value or "")
    asin_match = re.search(r"\b([A-Z0-9]{10})\b", text)
    if asin_match:
        return asin_match.group(1).strip().upper()
    isbn_match = re.search(r"\b(97[89][0-9]{10})\b", text)
    if isbn_match:
        return isbn_match.group(1).strip()
    return ""


def _extract_visible_series_text(value: str) -> str:
    text = _strip_tags(str(value or ""))
    patterns = (
        r"\(([^)]*(?:series|book|volume|vol\.?|part)\s*\d*[^)]*)\)",
        r"\b((?:series|book|volume|vol\.?|part)\s*\d+)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return re.sub(r"\s+", " ", str(match.group(1) or "")).strip()
    return ""


def _parse_google_search_result_links(html: str) -> list[dict]:
    links: list[dict] = []
    seen: set[str] = set()
    blocks = list(
        re.finditer(
            r'<div[^>]+class="[^"]*g[^"]*"[^>]*>(.*?)</div>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    if not blocks:
        blocks = list(
            re.finditer(
                r'(<a href="/url\?q=[^"]+"[^>]*>.*?</a>)(.*?)(?=<a href="/url\?q=|$)',
                html,
                flags=re.IGNORECASE | re.DOTALL,
            )
        )

    for block in blocks[:8]:
        body = block.group(1)
        link_match = re.search(r'href="/url\?q=([^"&]+)', body, flags=re.IGNORECASE)
        if not link_match:
            continue
        url = unquote(link_match.group(1)).strip()
        if not url or url in seen or not url.lower().startswith("http"):
            continue
        seen.add(url)

        title_match = re.search(r"<h3[^>]*>(.*?)</h3>", body, flags=re.IGNORECASE | re.DOTALL)
        if title_match:
            title = _strip_tags(title_match.group(1))
        else:
            anchor_match = re.search(r"<a[^>]*>(.*?)</a>", body, flags=re.IGNORECASE | re.DOTALL)
            title = _strip_tags(anchor_match.group(1)) if anchor_match else ""

        snippet_match = re.search(
            r'<div[^>]+class="[^"]*(?:VwiC3b|IsZvec)[^"]*"[^>]*>(.*?)</div>',
            body,
            flags=re.IGNORECASE | re.DOTALL,
        )
        snippet = _strip_tags(snippet_match.group(1)) if snippet_match else _strip_tags(body)
        links.append(
            {
                "url": url,
                "title": str(title or "").strip(),
                "snippet": str(snippet or "").strip(),
            }
        )
    return links


def parse_author_discovery_google_html_candidates(
    search_html: str,
    author_name: str,
    linked_pages: list[dict] | None = None,
) -> list[dict]:
    candidates: list[dict] = []
    seen_keys: set[str] = set()

    links = _parse_google_search_result_links(search_html)
    for link in links:
        title = str(link.get("title") or "").strip()
        snippet = str(link.get("snippet") or "").strip()
        source_blob = f"{title} {snippet}"
        asin_or_id = _extract_visible_retailer_id(source_blob)
        series_text = _extract_visible_series_text(source_blob)
        dedupe_key = f"{asin_or_id}|{_normalize_match_text(title)}|search"
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        candidates.append(
            {
                "title": title,
                "asin_or_id": asin_or_id,
                "series_text": series_text,
                "url": str(link.get("url") or "").strip(),
                "snippet": snippet,
                "author": str(author_name or "").strip(),
                "provider": "author_discovery_google_html",
                "source_layer": "author_discovery",
            }
        )

    for page in (linked_pages or []):
        page_url = str(page.get("url") or "").strip()
        page_html = str(page.get("html") or "")
        if not page_html:
            continue

        title_match = re.search(r"<title>(.*?)</title>", page_html, flags=re.IGNORECASE | re.DOTALL)
        page_title = _strip_tags(title_match.group(1)) if title_match else ""
        plain_text = _strip_tags(page_html)
        merged_blob = f"{page_title} {plain_text[:4000]}"
        asin_or_id = _extract_visible_retailer_id(merged_blob)
        series_text = _extract_visible_series_text(merged_blob)
        dedupe_key = f"{asin_or_id}|{_normalize_match_text(page_title)}|page"
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        candidates.append(
            {
                "title": str(page_title or "").strip(),
                "asin_or_id": asin_or_id,
                "series_text": series_text,
                "url": page_url,
                "snippet": plain_text[:500],
                "author": str(author_name or "").strip(),
                "provider": "author_discovery_google_html",
                "source_layer": "author_discovery",
            }
        )

    return [item for item in candidates if str(item.get("title") or "").strip()]


AUTHOR_DISCOVERY_PROVIDER = ProviderSpec(
    name="author_discovery_amazon",
    source_type="discovery",
    url_builder=_amazon_author_discovery_url,
    parser=parse_author_discovery_amazon_candidates,
)

AUTHOR_DISCOVERY_GOOGLE_PROVIDER = ProviderSpec(
    name="author_discovery_google_html",
    source_type="discovery",
    url_builder=lambda series_name, author_name, next_number: _google_html_url(
        f'"{str(author_name or "").strip()}" ({str(series_name or "").strip()} OR books OR bibliography)'
    ),
    parser=parse_author_discovery_amazon_candidates,
)


def run_author_discovery_amazon(author_name: str) -> dict:
    provider = AUTHOR_DISCOVERY_PROVIDER

    query_url = provider.url_builder("", author_name, 0)
    fetch_result = fetch_provider_html_by_name(provider.name, query_url, amazon_mode="search")
    if not fetch_result.get("ok"):
        return {
            "ok": False,
            "provider": provider.name,
            "url": query_url,
            "books": [],
            "error": str(fetch_result.get("error") or "author-discovery-fetch-failed"),
            "http_status": fetch_result.get("status_code"),
            "fetch_attempts": int(fetch_result.get("fetch_attempts") or 0),
            "header_profile": fetch_result.get("header_profile"),
            "cache_fallback": bool(fetch_result.get("cache_fallback")),
            "bot_blocked": bool(fetch_result.get("bot_blocked")),
        }

    html = str(fetch_result.get("raw_html") or fetch_result.get("html") or "")
    books = provider.parser(html, "") if html else []
    return {
        "ok": True,
        "provider": provider.name,
        "url": query_url,
        "books": books if isinstance(books, list) else [],
        "error": None,
        "http_status": fetch_result.get("status_code"),
        "fetch_attempts": int(fetch_result.get("fetch_attempts") or 0),
        "header_profile": fetch_result.get("header_profile"),
        "cache_fallback": bool(fetch_result.get("cache_fallback")),
        "bot_blocked": bool(fetch_result.get("bot_blocked")),
    }


def run_author_discovery_google_html(author_name: str, series_phrase: str = "", query_url: str = "") -> dict:
    provider = AUTHOR_DISCOVERY_GOOGLE_PROVIDER
    if query_url is not None and str(query_url).strip():
        resolved_query_url = str(query_url).strip()
    else:
        resolved_query_url = ""

    if not resolved_query_url:
        resolved_query_url = provider.url_builder(series_phrase, author_name, 0)
    if not resolved_query_url:
        resolved_query_url = _google_html_url('"Honour Rae" "All The Skills" book novel release series')

    search_result = fetch_provider_html_by_name(provider.name, resolved_query_url)
    if not search_result.get("ok"):
        return {
            "ok": False,
            "provider": provider.name,
            "url": resolved_query_url,
            "books": [],
            "error": str(search_result.get("error") or "author-discovery-google-fetch-failed"),
            "http_status": search_result.get("status_code"),
            "fetch_attempts": int(search_result.get("fetch_attempts") or 0),
            "linked_pages_fetched": 0,
        }

    search_html = str(search_result.get("raw_html") or search_result.get("html") or "")
    search_links = _parse_google_search_result_links(search_html)
    linked_pages: list[dict] = []
    for link in search_links[:3]:
        link_url = str(link.get("url") or "").strip()
        if not link_url:
            continue
        linked_fetch = fetch_provider_html_by_name(provider.name, link_url)
        if not linked_fetch.get("ok"):
            continue
        linked_pages.append(
            {
                "url": link_url,
                "html": str(linked_fetch.get("raw_html") or linked_fetch.get("html") or ""),
            }
        )

    books = parse_author_discovery_google_html_candidates(search_html, author_name, linked_pages)
    return {
        "ok": True,
        "provider": provider.name,
        "url": resolved_query_url,
        "books": books,
        "error": None,
        "http_status": search_result.get("status_code"),
        "fetch_attempts": int(search_result.get("fetch_attempts") or 0),
        "linked_pages_fetched": len(linked_pages),
    }


def _candidate_text_blob(candidate: dict) -> str:
    fields = [
        candidate.get("title"),
        candidate.get("name"),
        candidate.get("snippet"),
        candidate.get("url"),
        candidate.get("category"),
        candidate.get("product_category"),
        candidate.get("department"),
        candidate.get("series_name"),
        candidate.get("series"),
        candidate.get("author"),
        candidate.get("availability"),
        candidate.get("status_hint"),
    ]
    return " ".join(str(value or "") for value in fields).strip().lower()


def _has_book_metadata(candidate: dict) -> bool:
    title = str(candidate.get("title") or candidate.get("name") or "").strip()
    has_identity = bool(
        str(candidate.get("asin") or candidate.get("asin_or_id") or "").strip()
        or str(candidate.get("isbn") or "").strip()
    )
    has_book_context = bool(
        str(candidate.get("author") or "").strip()
        or candidate.get("book_number") is not None
        or str(candidate.get("series_name") or candidate.get("series") or "").strip()
        or str(candidate.get("publication_date") or candidate.get("release_date") or candidate.get("publish_date") or "").strip()
    )
    return bool(title) and (has_identity or has_book_context)


def _is_garbage_candidate(candidate: dict) -> bool:
    if not isinstance(candidate, dict):
        return True

    blob = _candidate_text_blob(candidate)
    asin = str(candidate.get("asin") or candidate.get("asin_or_id") or "").strip().upper()

    amazon_ui_tokens = (
        "a-section",
        "a-link-normal",
        "a-size-base",
        "celwidget",
        "s-result-item",
        "data-csa",
        "widget",
        "nav-",
    )
    amazon_internal_tokens = (
        "amazon internal",
        "internal symbol",
        "slotid",
        "rhf",
        "octopus",
        "search-alias",
        "p13n",
    )
    ad_tokens = (
        "sponsored",
        "ad feedback",
        "advertisement",
        "promoted",
    )

    if any(token in blob for token in amazon_ui_tokens):
        return True
    if any(token in blob for token in amazon_internal_tokens):
        return True
    if any(token in blob for token in ad_tokens):
        return True

    if asin and not re.fullmatch(r"[A-Z0-9]{10}", asin):
        return True

    if _is_clearly_non_book_candidate(candidate):
        return True

    if not _has_book_metadata(candidate):
        return True

    return False


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
    title_match = re.search(r"<title>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    heading_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, flags=re.IGNORECASE | re.DOTALL)
    text = _strip_tags(html)
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
        blocks = list(re.finditer(r'(<a href="/url\?q=[^"]+"[^>]*>.*?</a>)(.*?)(?=<a href="/url\?q=|$)', html, flags=re.IGNORECASE | re.DOTALL))

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
    if provider_name in {"amazon_books", "amazon_series_page"}:
        return "amazon"
    return provider_name


def _build_provider_output(provider: ProviderSpec, raw_html: str, series_name: str) -> dict:
    strict_candidates = provider.parser(raw_html, series_name) or []
    if provider.name in {"amazon_books", "amazon_series_page"}:
        strict_candidates = extract_amazon_candidates_from_html(raw_html, series_name) or []
    strict_candidates = [candidate for candidate in strict_candidates if not _is_garbage_candidate(candidate)]

    fallback_candidates = parse_publisher_or_author_candidates(raw_html, series_name) or []
    if provider.name in {"amazon_books", "amazon_series_page"}:
        fallback_candidates = (provider.parser(raw_html, series_name) or []) + fallback_candidates
    fallback_candidates = [candidate for candidate in fallback_candidates if not _is_garbage_candidate(candidate)]

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


def _run_dom_harvester_layers(provider_output: dict, series_name: str) -> tuple[list[dict], dict[str, int]]:
    provider_name = str(provider_output.get("provider_name") or "").strip()
    raw_html = str(provider_output.get("raw_html") or "")
    base_url = ""

    json_candidates: list[dict] = []
    decoded_json_blobs, json_metrics = extract_json_objects_from_html_with_metrics(raw_html)
    json_book_blobs = 0
    if provider_name == "amazon":
        adapter_result = extract_amazon_candidates_from_json(raw_html) or {}
        adapter_candidates = adapter_result.get("book_candidates") if isinstance(adapter_result, dict) else []
        if not isinstance(adapter_candidates, list):
            adapter_candidates = []
        json_book_blobs = int(adapter_result.get("json_blobs_valid") or 0) if isinstance(adapter_result, dict) else 0
        parsed_candidates, _ = parse_json_objects_to_candidates_debug(decoded_json_blobs)
        json_candidates = adapter_candidates + parsed_candidates
    else:
        generic_json_candidates, _ = parse_json_objects_to_candidates_debug(decoded_json_blobs)
        json_candidates = generic_json_candidates
        json_book_blobs = len(generic_json_candidates)
    json_candidates = [candidate for candidate in json_candidates if not _is_garbage_candidate(candidate)]

    html_attribute_candidates: list[dict] = []
    html_attribute_metadata, html_metrics = extract_html_attribute_metadata_with_metrics(raw_html)
    for item in html_attribute_metadata:
        asin = str(item.get("asin") or "").strip().upper()
        url = f"https://m.amazon.com/dp/{asin}" if provider_name == "amazon" and asin else base_url
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
    html_attribute_candidates = [candidate for candidate in html_attribute_candidates if not _is_garbage_candidate(candidate)]

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
                    "url": f"https://m.amazon.com/dp/{str(recovered_asin or '').strip().upper()}",
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
    pattern_candidates = [candidate for candidate in pattern_candidates if not _is_garbage_candidate(candidate)]

    dom_candidates = []
    if provider_name == "amazon":
        dom_candidates.extend(html_attribute_candidates)
        dom_candidates.extend(json_candidates)
        dom_candidates.extend(pattern_candidates)
    else:
        dom_candidates.extend(json_candidates)
        dom_candidates.extend(html_attribute_candidates)
        dom_candidates.extend(pattern_candidates)

    return dom_candidates, {
        "dom_elements_scanned": int(html_metrics.get("elements_scanned") or 0),
        "metadata_candidates": int(html_metrics.get("metadata_candidates") or 0),
        "asin_groups": int(html_metrics.get("asin_groups") or 0),
        "json_blobs_extracted": int(json_metrics.get("json_blobs_extracted") or 0),
        "json_book_blobs": int(json_book_blobs or 0),
        "dom_primary_candidates": int(len(html_attribute_candidates)),
        "json_secondary_candidates": int(len(json_candidates)),
        "asin_tertiary_candidates": int(len(pattern_candidates)),
    }


def _extract_candidates_three_layer(provider_output: dict, series_name: str) -> dict:
    provider_name = str(provider_output.get("provider_name") or "")
    dom_candidates, dom_metrics = _run_dom_harvester_layers(provider_output, series_name)
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

    return {
        "strict": normalized_strict,
        "fallback": normalized_fallback,
        "dom": normalized_dom,
        "merged": merged,
        "metrics": dom_metrics,
    }


PROVIDERS: list[ProviderSpec] = [
    ProviderSpec(name="amazon_asin_series", source_type="retail", url_builder=_asin_series_seed_url, parser=parse_publisher_or_author_candidates),
    ProviderSpec(name="amazon_series_page", source_type="retail", url_builder=_amazon_series_page_url, parser=parse_publisher_or_author_candidates),
    ProviderSpec(name="amazon_books", source_type="retail", url_builder=_amazon_url, parser=parse_publisher_or_author_candidates),
    ProviderSpec(name="fantasticfiction", source_type="retail", url_builder=_fantastic_fiction_url, parser=parse_fantastic_fiction_candidates),
    ProviderSpec(name="publisher_site", source_type="publisher", url_builder=_publisher_site_url, parser=parse_publisher_or_author_candidates),
    ProviderSpec(name="author_site", source_type="author", url_builder=_author_site_url, parser=parse_publisher_or_author_candidates),
    ProviderSpec(name="google_html_search", source_type="search", url_builder=_google_organic_url, parser=parse_google_organic_candidates),
]
