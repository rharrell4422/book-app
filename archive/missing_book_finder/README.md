# Missing Book Finder Archive

Deprecated as of July 2026 - superseded by series_agent intelligence and ghost book management.

## What Was Removed
- Series View Missing Book Finder UI block in book-app-ui/app/series/[seriesId]/page.tsx.
- Frontend suggestion scan and cache/session logic that called /books/suggest.
- Frontend manual search buttons tied to missing slot suggestion flow (Goodreads and Google from Missing Book Finder paths).
- Backend suggest routes in main.py:
  - GET /books/suggest
  - GET /series/{series_id}/suggest
- Endpoint-specific regression tests in tests/test_series_discovery.py.
- Legacy endpoint note in COPILOT.md.

## Why
The legacy suggestion workflow duplicated newer series intelligence/discovery behavior and added extra external-call paths that are no longer part of the supported workflow.

## Supported Replacement
- Run discovery through series intelligence/check-for-new flow.
- Add or import confirmed books through existing create/import paths.
- Use ghost deletion cleanup plus intelligence recalculation for corrections.
