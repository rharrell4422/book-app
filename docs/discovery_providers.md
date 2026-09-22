# Discovery providers — operational notes

Static documentation for humans operating ReaderPro in production. If this
file and the code disagree, the code wins — update this file when deployment
choices change.

## Production status (Railway backend)

| Provider | Env var | Status (Sep 2026) | Notes |
|----------|---------|-------------------|-------|
| Google Books | `GOOGLE_BOOKS_API_KEY` | On | Primary catalog source |
| Hardcover | `HARDCOVER_API_KEY` | On | Series position, indie coverage |
| OpenLibrary | *(none)* | On | No key required |
| Serper | `SERPER_API_KEY` | On | Web search (see below) |
| Anthropic | `ANTHROPIC_API_KEY` | On | Structures Serper hits + Goodreads canonical pages |
| **Apify** | **`APIFY_API_TOKEN`** | **Off (intentionally unset)** | See below |

### Apify is turned off on purpose

**`APIFY_API_TOKEN` is not set on the ReaderPro Backend Railway service.**

This was a deliberate, reversible choice on **2026-09-03**, not a broken
integration:

- Apify had been fixed in August 2026 (token format + `junglee/free-amazon-product-scraper`
  actor), but Jonathan Hunt-style series burns through free-tier budget fast
  (~$1+ per full Check Now).
- Discovery dev switched Jonathan Hunt to **Goodreads canonical** discovery
  (httpx → trafilatura → LLM) and removed the Apify token from Railway to get
  **zero-cost** Check Now runs while tuning discovery.
- **Sealing** a variable in Railway does *not* disable it — the app still reads
  sealed values at runtime. To turn Apify off, delete the variable or set it to
  empty.

**What does not work while Apify is off:**

- Guided Discovery with **Source = Amazon / Kindle Unlimited** (uses Apify only)
- Amazon product enrichment during the Serper web-search sub-flow
- Retail-search gap recovery (`apify_retail_search_provider`) after missing-volume lookahead

**What still works without Apify:**

- Check Now on catalog-backed series (Google Books, OpenLibrary, Hardcover)
- Serper + Anthropic web search when the catalog-sufficiency gate does *not* skip it
- Guided Discovery with **Goodreads** (or other non-KU sources): page fetch + LLM, no Apify
- Manual Add Book

**To re-enable Apify temporarily** (e.g. one Amazon product URL for Tracy Crosswhite #13):

1. Copy token from [Apify → Settings → Integrations](https://console.apify.com/account/integrations)
   (must start with `apify_api_`).
2. Set `APIFY_API_TOKEN` on **ReaderPro Backend** in Railway.
3. Redeploy the backend.
4. Run Check Now or **Save & Run Canonical Discovery Now** with KU source.
5. Optional: remove or empty the token again to stop spend.

Apify free tier was at **~$5.75 / $8.00** usage before removal (mostly Jonathan Hunt).
A single product-page scrape is typically a few cents, not another full-series run.

### Do you need to turn off Serper?

**No — Serper and Apify are separate.**

| | Serper + Anthropic | Apify |
|--|-------------------|-------|
| **Purpose** | Google-style web search → LLM extracts book title/number/date from snippets | Amazon/KU product scraping (structured ASIN, “Book N of M”, release date) |
| **Typical cost** | Per-query Serper + small LLM call | Per Amazon actor run (~$12/1k results list price; one product ≈ pennies) |
| **Guided Discovery Goodreads** | Uses Anthropic only (no Serper) | Not used |
| **Guided Discovery KU** | Not used | **Required** |

Keep **Serper on** if you want Check Now to find books that catalog APIs have not
indexed yet (fan sites, early announcements). The catalog-sufficiency gate often
*skips* web search when books 1–N already look complete in catalogs — that is a
separate issue from Apify being off.

Turn Serper off only if you want **zero** web-search discovery and are fine with
catalog APIs + manual adds only.

### Series notes

- **Jonathan Hunt Thriller Series** — treated as out-of-scope for automatic Amazon/KU
  discovery; use Goodreads canonical URL if continuing to test that series.
- **Tracy Crosswhite** — book 13 (*Graves Tell Lies*) is on Amazon/KU but not in
  major catalogs yet; KU Guided Discovery needs Apify re-enabled, or add the book manually.

## Related files

- `.env.example` — all provider env vars and comments
- `apify_provider.py` — Amazon actor (`junglee/free-amazon-product-scraper`)
- `discovery_engine.py` — `_attempt_canonical_source_recovery` (KU vs Goodreads routing)
- `provider_io.py` — catalog-sufficiency gate (may skip Serper web search)
