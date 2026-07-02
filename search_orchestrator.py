import re
from dataclasses import dataclass
from typing import Callable, Any


SearchProvider = Callable[[str, str | None, int], list[dict]]


@dataclass
class SearchContext:
    cleaned_series_name: str
    series_name_norm: str
    base_forms: list[str]
    base_forms_norm: list[str]
    author_candidates: list[str]
    primary_author: str | None
    book_number: int | None


class SearchOrchestrator:
    def __init__(self, google_search: SearchProvider, openlibrary_search: SearchProvider, serp_search: SearchProvider | None = None):
        self.google_search = google_search
        self.openlibrary_search = openlibrary_search
        self.serp_search = serp_search

    def suggest_series(self, series_name: str, book_number: int | None = None, author: str | None = None) -> dict:
        if not series_name:
            return {
                "query": "",
                "results": [],
                "diagnostics": {
                    "selected_stage": None,
                    "provider_counts": {"google": 0, "openlibrary": 0, "serpapi": 0},
                    "stages": [],
                    "accepted_total": 0,
                    "rejection_counts": {},
                },
            }

        ctx = self._build_context(series_name, book_number, author)
        results: list[dict] = []
        seen: set[tuple[str, str | None]] = set()
        final_query = ""
        diagnostics = {
            "selected_stage": None,
            "provider_counts": {"google": 0, "openlibrary": 0, "serpapi": 0},
            "stages": [],
            "accepted_total": 0,
            "rejection_counts": {},
        }

        google_queries, candidate_queries = self._build_query_candidates(ctx)
        google_query_candidates = list(dict.fromkeys(google_queries + candidate_queries))

        for query in google_query_candidates[:4]:
            if not query.strip():
                continue
            final_query = query
            google_results = self.google_search(query, author, 5)
            diagnostics["provider_counts"]["google"] += len(google_results)
            results = self._collect_candidates(google_results, ctx, seen, author_hint=None, diagnostics=diagnostics)
            diagnostics["stages"].append({
                "stage": "google_candidate",
                "provider": "google_books",
                "query": query,
                "raw_count": len(google_results),
                "accepted_count": len(results),
            })
            if results:
                diagnostics["selected_stage"] = "google_candidate"
                diagnostics["accepted_total"] = len(results)
                return self._build_response(final_query, results, diagnostics)

        for candidate_author in ctx.author_candidates:
            final_query = f'inauthor:"{candidate_author}"'
            google_results = self.google_search(final_query, None, 8)
            diagnostics["provider_counts"]["google"] += len(google_results)
            results = self._collect_candidates(google_results, ctx, seen, author_hint=candidate_author, diagnostics=diagnostics)
            diagnostics["stages"].append({
                "stage": "google_author_fallback",
                "provider": "google_books",
                "query": final_query,
                "raw_count": len(google_results),
                "accepted_count": len(results),
            })
            if results:
                diagnostics["selected_stage"] = "google_author_fallback"
                diagnostics["accepted_total"] = len(results)
                return self._build_response(final_query, results, diagnostics)

        open_query_candidates = list(dict.fromkeys(ctx.base_forms + candidate_queries))
        for query in open_query_candidates[:5]:
            final_query = query
            open_results = self.openlibrary_search(query, ctx.primary_author, 5)
            diagnostics["provider_counts"]["openlibrary"] += len(open_results)
            results = self._collect_candidates(open_results, ctx, seen, author_hint=None, diagnostics=diagnostics)
            diagnostics["stages"].append({
                "stage": "openlibrary_candidate",
                "provider": "openlibrary",
                "query": query,
                "raw_count": len(open_results),
                "accepted_count": len(results),
            })
            if results:
                diagnostics["selected_stage"] = "openlibrary_candidate"
                diagnostics["accepted_total"] = len(results)
                return self._build_response(final_query, results, diagnostics)

        final_query = ctx.cleaned_series_name
        open_results = self.openlibrary_search(ctx.cleaned_series_name, ctx.primary_author, 5)
        diagnostics["provider_counts"]["openlibrary"] += len(open_results)
        results = self._collect_candidates(open_results, ctx, seen, author_hint=None, diagnostics=diagnostics)
        diagnostics["stages"].append({
            "stage": "openlibrary_series_direct",
            "provider": "openlibrary",
            "query": final_query,
            "raw_count": len(open_results),
            "accepted_count": len(results),
        })
        if results:
            diagnostics["selected_stage"] = "openlibrary_series_direct"
            diagnostics["accepted_total"] = len(results)
            return self._build_response(final_query, results, diagnostics)

        for candidate_author in ctx.author_candidates:
            final_query = f'author:"{candidate_author}"'
            author_results = self.openlibrary_search(final_query, candidate_author, 8)
            diagnostics["provider_counts"]["openlibrary"] += len(author_results)
            results = self._collect_candidates(author_results, ctx, seen, author_hint=candidate_author, diagnostics=diagnostics)
            diagnostics["stages"].append({
                "stage": "openlibrary_author_fallback",
                "provider": "openlibrary",
                "query": final_query,
                "raw_count": len(author_results),
                "accepted_count": len(results),
            })
            if results:
                diagnostics["selected_stage"] = "openlibrary_author_fallback"
                diagnostics["accepted_total"] = len(results)
                return self._build_response(final_query, results, diagnostics)

        for candidate_author in ctx.author_candidates:
            final_query = f'intitle:"{ctx.cleaned_series_name}" inauthor:"{candidate_author}"'
            strict_results = self.google_search(final_query, None, 8)
            diagnostics["provider_counts"]["google"] += len(strict_results)
            strict = []
            for result in strict_results:
                accepted, rejection_reason = self._accept_result_with_reason(result, ctx, author_hint=candidate_author, strict_title_match=True)
                if accepted:
                    key = self._result_key(result)
                    if key in seen:
                        continue
                    seen.add(key)
                    strict.append(result)
                elif rejection_reason:
                    diagnostics["rejection_counts"][rejection_reason] = diagnostics["rejection_counts"].get(rejection_reason, 0) + 1
            diagnostics["stages"].append({
                "stage": "google_strict_title_fallback",
                "provider": "google_books",
                "query": final_query,
                "raw_count": len(strict_results),
                "accepted_count": len(strict),
            })
            if strict:
                diagnostics["selected_stage"] = "google_strict_title_fallback"
                diagnostics["accepted_total"] = len(strict)
                return self._build_response(final_query, strict, diagnostics)

        if self.serp_search:
            serp_queries = self._build_serp_queries(ctx)
            for query in serp_queries[:4]:
                final_query = query
                serp_results = self.serp_search(query, ctx.primary_author, 10)
                diagnostics["provider_counts"]["serpapi"] += len(serp_results)
                results = self._collect_candidates(serp_results, ctx, seen, author_hint=ctx.primary_author, diagnostics=diagnostics)
                diagnostics["stages"].append({
                    "stage": "serpapi_fallback",
                    "provider": "serpapi",
                    "query": query,
                    "raw_count": len(serp_results),
                    "accepted_count": len(results),
                })
                if results:
                    diagnostics["selected_stage"] = "serpapi_fallback"
                    diagnostics["accepted_total"] = len(results)
                    return self._build_response(final_query, results, diagnostics)

        diagnostics["selected_stage"] = "none"
        diagnostics["accepted_total"] = 0
        return self._build_response(final_query, [], diagnostics)

    def _build_serp_queries(self, ctx: SearchContext) -> list[str]:
        queries: list[str] = []
        for base in ctx.base_forms[:3]:
            if ctx.book_number is not None and ctx.primary_author:
                queries.append(f'{base} book {ctx.book_number} {ctx.primary_author}')
            elif ctx.book_number is not None:
                queries.append(f'{base} book {ctx.book_number}')
            elif ctx.primary_author:
                queries.append(f'{base} {ctx.primary_author}')
            else:
                queries.append(base)

        if ctx.primary_author:
            queries.append(f'{ctx.cleaned_series_name} {ctx.primary_author} order')

        # Deduplicate while preserving order.
        return list(dict.fromkeys(queries))

    def _collect_candidates(self, raw_results: list[dict], ctx: SearchContext, seen: set[tuple[str, str | None]], author_hint: str | None, diagnostics: dict | None = None) -> list[dict]:
        collected: list[dict] = []
        for result in raw_results:
            accepted, rejection_reason = self._accept_result_with_reason(result, ctx, author_hint=author_hint)
            if not accepted:
                if diagnostics is not None and rejection_reason:
                    diagnostics["rejection_counts"][rejection_reason] = diagnostics["rejection_counts"].get(rejection_reason, 0) + 1
                continue
            key = self._result_key(result)
            if key in seen:
                continue
            seen.add(key)
            collected.append(result)
        return collected

    def _build_response(self, query: str, results: list[dict], diagnostics: dict) -> dict:
        ranked_results = self._rank_results(results, query)
        canonical_results = [self._canonicalize_result(item) for item in ranked_results]
        return {
            "query": query,
            "results": canonical_results,
            "diagnostics": diagnostics,
        }

    def _rank_results(self, results: list[dict], query: str) -> list[dict]:
        if not results:
            return []

        query_norm = self._normalize(query)

        def score(result: dict) -> int:
            s = 0
            title_norm = self._normalize(result.get("title") or "")
            url_norm = self._normalize(result.get("source_url") or "")

            # Prefer explicit book-number/title style matches in top slots.
            if re.search(r"\bbook\s+\d+\b", title_norm):
                s += 50
            if re.search(r"#\s*\d+", title_norm):
                s += 35
            if re.search(r"\(.*book\s+\d+.*\)", title_norm):
                s += 25

            # Prefer candidates with meaningful title words over generic series-only titles.
            if len(title_norm.split()) >= 2:
                s += 15

            # Boost trusted storefront/catalog sources for concrete title data.
            if any(domain in url_norm for domain in [
                "amazon.",
                "audible.",
                "goodreads.",
                "kindle",
                "openlibrary.org",
            ]):
                s += 25

            # Keep author-site pages but below explicit store/catalog entries.
            if any(domain in url_norm for domain in ["nicoligonnella.com", "/books"]):
                s += 12

            # If the query text appears in title, it's likely closer to user intent.
            query_tokens = [t for t in query_norm.split() if len(t) > 2]
            overlap = sum(1 for token in query_tokens if token in title_norm)
            s += min(20, overlap * 4)

            # Penalize generic or meta pages.
            if any(marker in title_norm for marker in ["audiobooks", "author of", "home"]):
                s -= 8

            return s

        return sorted(results, key=score, reverse=True)

    def _canonicalize_result(self, result: dict) -> dict:
        year_value: Any = result.get("year")
        normalized_year: str | int | None = None
        if isinstance(year_value, int):
            normalized_year = year_value
        elif isinstance(year_value, str):
            match = re.search(r"\d{4}", year_value)
            normalized_year = match.group(0) if match else year_value

        series_name = result.get("series_name")
        if isinstance(series_name, list):
            series_name = [str(item).strip() for item in series_name if str(item).strip()]
            if not series_name:
                series_name = None

        return {
            "title": result.get("title") or "",
            "author": result.get("author"),
            "year": normalized_year,
            "description": result.get("description"),
            "source_url": result.get("source_url"),
            "series_name": series_name,
            "series_position": result.get("series_position"),
            "source": result.get("source"),
        }

    def _accept_result(self, result: dict, ctx: SearchContext, author_hint: str | None, strict_title_match: bool = False) -> bool:
        accepted, _ = self._accept_result_with_reason(result, ctx, author_hint, strict_title_match)
        return accepted

    def _accept_result_with_reason(self, result: dict, ctx: SearchContext, author_hint: str | None, strict_title_match: bool = False) -> tuple[bool, str | None]:
        title = result.get("title")
        if not title:
            return False, "missing_title"

        if not self._passes_author_filter(result.get("author"), ctx):
            return False, "author_mismatch"

        if author_hint and not self._author_matches_result(result.get("author"), author_hint):
            return False, "author_mismatch"

        if ctx.author_candidates and not result.get("author"):
            return False, "missing_author"

        title_norm = self._normalize(title)
        if self._looks_like_discussion_result(title_norm, result.get("source_url")):
            return False, "discussion_result"

        if self._looks_like_collection_title(title_norm, ctx.book_number):
            return False, "collection_result"

        if strict_title_match:
            if not any(self._phrase_matches(title_norm, base) for base in ctx.base_forms_norm):
                return False, "strict_title_mismatch"
        else:
            if not self._matches_series(result, title_norm, ctx):
                return False, "series_mismatch"

        if not self._matches_number(result, title_norm, ctx.book_number):
            return False, "number_mismatch"

        return True, None

    def _build_context(self, series_name: str, book_number: int | None, author: str | None) -> SearchContext:
        cleaned_series_name = self._clean_series_label(series_name)
        base_forms = self._build_base_forms(cleaned_series_name)
        author_candidates = self._split_author_names(author)
        primary_author = author_candidates[0] if author_candidates else None

        return SearchContext(
            cleaned_series_name=cleaned_series_name,
            series_name_norm=self._normalize(cleaned_series_name),
            base_forms=base_forms,
            base_forms_norm=[self._normalize(base) for base in base_forms],
            author_candidates=author_candidates,
            primary_author=primary_author,
            book_number=book_number,
        )

    def _build_query_candidates(self, ctx: SearchContext) -> tuple[list[str], list[str]]:
        google_queries: list[str] = []
        candidate_queries: list[str] = []

        for base in ctx.base_forms:
            base_quoted = f'"{base}"'
            if ctx.book_number is not None:
                google_queries.extend([
                    f'intitle:{base_quoted} inauthor:"{ctx.primary_author}"' if ctx.primary_author else f'intitle:{base_quoted} {ctx.book_number}',
                    f'intitle:{base_quoted} book {ctx.book_number} inauthor:"{ctx.primary_author}"' if ctx.primary_author else f'intitle:{base_quoted} book {ctx.book_number}',
                    f'intitle:{base_quoted} volume {ctx.book_number} inauthor:"{ctx.primary_author}"' if ctx.primary_author else f'intitle:{base_quoted} volume {ctx.book_number}',
                    f'{base_quoted} {ctx.book_number} inauthor:"{ctx.primary_author}"' if ctx.primary_author else f'{base_quoted} {ctx.book_number}',
                ])
            google_queries.extend([
                f'intitle:{base_quoted} inauthor:"{ctx.primary_author}"' if ctx.primary_author else f'intitle:{base_quoted}',
                f'{base_quoted} inauthor:"{ctx.primary_author}"' if ctx.primary_author else base,
            ])

            if ctx.primary_author:
                google_queries.extend([
                    f'intitle:{base_quoted} OR "LitRPG" inauthor:"{ctx.primary_author}"',
                    f'intitle:{base_quoted} OR "Lit RPG" inauthor:"{ctx.primary_author}"',
                ])

            if ctx.book_number is not None:
                candidate_queries.extend([
                    f'"{base}" {ctx.book_number}',
                    f'"{base}" book {ctx.book_number}',
                    f'"{base}" volume {ctx.book_number}',
                    f'{base} {ctx.book_number}',
                ])

            candidate_queries.append(base)
            if ctx.primary_author:
                candidate_queries.append(f'{base} {ctx.primary_author}')
                candidate_queries.append(f'{base} author:{ctx.primary_author}')

        return google_queries, candidate_queries

    def _clean_series_label(self, name: str) -> str:
        cleaned = name.strip()
        cleaned = re.sub(r"[,\s]*book\s*[)\]]*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -,:;")
        return cleaned or name.strip()

    def _split_author_names(self, value: str | None) -> list[str]:
        if not value:
            return []
        cleaned_value = value.strip()
        # Ignore placeholder values so we do not over-filter valid results.
        if cleaned_value.lower() in {"unknown", "unknown author", "n/a", "na", "none"}:
            return []
        parts = re.split(r"\s*(?:,|&|\band\b)\s*", value, flags=re.IGNORECASE)
        authors = [p.strip() for p in parts if p and p.strip()]

        seen_authors = set()
        ordered = []
        for item in authors:
            key = item.lower()
            if key in seen_authors:
                continue
            seen_authors.add(key)
            ordered.append(item)
        return ordered

    def _build_base_forms(self, name: str) -> list[str]:
        canonical = self._clean_series_label(name)
        forms = [canonical]
        if canonical != name.strip():
            forms.append(name.strip())

        expanded = list(forms)
        for form in list(expanded):
            if form.lower().startswith("the "):
                expanded.append(form[4:].strip())

            for marker in ["novels", "series", "trilogy", "saga", "cycle", "files", "chronicles"]:
                if marker in form.lower():
                    prefix = re.split(rf"\b{marker}\b", form, flags=re.IGNORECASE)[0].strip(" -,:;")
                    if prefix:
                        expanded.append(prefix)
                    if prefix.lower().startswith("the "):
                        expanded.append(prefix[4:].strip())

        cleaned_forms = []
        seen_forms = set()
        for entry in expanded:
            value = re.sub(r"\s+", " ", entry).strip(" -,:;")
            if not value:
                continue
            key = value.lower()
            if key in seen_forms:
                continue
            seen_forms.add(key)
            cleaned_forms.append(value)
        return cleaned_forms

    def _extract_series_names(self, result: dict) -> list[str]:
        series_names = result.get("series_name") or []
        if isinstance(series_names, str):
            series_names = [series_names]
        return [self._normalize(str(name)) for name in series_names if name]

    def _matches_series(self, result: dict, title_norm: str, ctx: SearchContext) -> bool:
        series_names = self._extract_series_names(result)
        series_matches = any(
            ctx.series_name_norm == name or ctx.series_name_norm in name or name in ctx.series_name_norm
            for name in series_names
        )
        title_matches = self._phrase_matches(title_norm, ctx.series_name_norm)
        base_title_matches = any(
            (base and self._phrase_matches(title_norm, base))
            for base in ctx.base_forms_norm
        )
        # If we have no reliable series metadata, allow strong title phrase matches.
        return series_matches or title_matches or base_title_matches

    def _matches_number(self, result: dict, title_norm: str, book_number: int | None) -> bool:
        if book_number is None:
            return True

        position = result.get("series_position")
        if position == book_number or position == str(book_number):
            return True

        if book_number == 1:
            if re.search(r"\b(?:book|books|volume|vol\.?)\s*(?:[2-9]\d*|1\d+)\b", title_norm):
                return False
            if re.search(r"\b(?:[2-9]\d*|1\d+)\b", title_norm):
                return False
            return True

        number_text = str(book_number)
        patterns = [
            rf"\b{re.escape(number_text)}\b",
            rf"\bbook\s+{re.escape(number_text)}\b",
            rf"\bvolume\s+{re.escape(number_text)}\b",
            rf"#\s*{re.escape(number_text)}\b",
            rf"\({re.escape(number_text)}\)",
            rf"{re.escape(number_text)}:",
        ]
        return any(re.search(pattern, title_norm) for pattern in patterns)

    def _looks_like_collection_title(self, title_norm: str, book_number: int | None) -> bool:
        collection_markers = [
            "omnibus",
            "box set",
            "boxset",
            "collection",
            "compilation",
            "anthology",
            "series",
        ]
        if any(marker in title_norm for marker in collection_markers):
            return True

        if book_number is None:
            return False

        target = re.escape(str(book_number))
        multi_book_patterns = [
            rf"\b(?:book|books|volume|vol\.?)\s+{target}\s*(?:,|and|&|/|-|to)\s*\d",
            rf"\b{target}\s*(?:,|and|&|/|-|to)\s*\d",
        ]
        return any(re.search(pattern, title_norm) for pattern in multi_book_patterns)

    def _looks_like_discussion_result(self, title_norm: str, source_url: str | None) -> bool:
        url_norm = self._normalize(source_url or "")
        blocked_domains = [
            "reddit.com",
            "reddit.",
            "facebook.com",
            "x.com",
            "twitter.com",
        ]
        if any(domain in url_norm for domain in blocked_domains):
            return True

        discussion_markers = [
            "opinion",
            "review",
            "discussion",
            "thoughts on",
            "what do you think",
        ]
        return any(marker in title_norm for marker in discussion_markers)

    def _normalize_author_text(self, value: str | None) -> str:
        if not value:
            return ""
        lowered = value.lower()
        lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
        return re.sub(r"\s+", " ", lowered).strip()

    def _author_matches_result(self, result_author: str | None, expected_author: str | None) -> bool:
        if not expected_author:
            return True
        result_norm = self._normalize_author_text(result_author)
        expected_norm = self._normalize_author_text(expected_author)
        if not result_norm or not expected_norm:
            return False

        expected_tokens = [token for token in expected_norm.split() if len(token) > 1]
        if not expected_tokens:
            return expected_norm in result_norm
        return all(token in result_norm for token in expected_tokens)

    def _passes_author_filter(self, result_author: str | None, ctx: SearchContext) -> bool:
        if not ctx.author_candidates:
            return True
        for candidate in ctx.author_candidates[:2]:
            if self._author_matches_result(result_author, candidate):
                return True
        return False

    def _result_key(self, result: dict) -> tuple[str, str | None]:
        title_norm = self._normalize(result.get("title", ""))
        author = result.get("author")
        return title_norm, author

    def _normalize(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.lower().strip())

    def _phrase_matches(self, text: str, phrase: str) -> bool:
        if not text or not phrase:
            return False
        return re.search(rf"\b{re.escape(phrase)}\b", text) is not None
