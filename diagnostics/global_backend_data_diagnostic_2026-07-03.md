# Global Backend Data Diagnostic (2026-07-03)

## Scope
- Series scanned: 318
- Books scanned: 2534
- Series with total_books != active_rows: 74
- Series with estimated missing rows (total_books > active_rows): 53
- Rows with record_status IS NULL: 0

## Artifacts
- Mismatch inventory: diagnostics/series_count_mismatch.csv
- Missing-row candidates: diagnostics/missing_backend_rows_candidates.csv
- Numbering gaps: diagnostics/series_numbering_gaps.csv
- Null-status rows: diagnostics/null_record_status_rows.csv

## Key Finding
The current database snapshot does not contain any rows with record_status IS NULL, so the originally suspected NULL-status drop path is not present in this dataset at the moment. Mismatches are still widespread and mostly explained by one of these patterns:
1. total_books metadata overstates current active rows.
2. series has explicit numbering gaps (missing integer sequence values).
3. series has low/no numbered rows but non-zero total_books.
4. series appears to contain merged/mis-imported numbering domains (high max integer with sparse occupancy).

## Top Missing-Row Candidates (by missing_row_count_estimate)
1. series_id 283, The Human Chronicles: total 13, active 2, estimated missing 11, missing numbers 1|2|3|4|5|6|7|8|10|11|12
2. series_id 9, The Survivors: total 28, active 18, estimated missing 10, missing numbers 3|5|6|8|11|14|15|16|17|18
3. series_id 244, Scot Harvath Series: total 16, active 7, estimated missing 9, missing numbers 2|3|4|6|7|9|11|12|14
4. series_id 114, Viridian Gate Archives: total 8, active 1, estimated missing 7, missing numbers 1|2|3|4|5|6|7
5. series_id 127, Nathan McBride: total 7, active 1, estimated missing 6, missing numbers 1|2|3|4|5|6
6. series_id 163, Life Reset - Neo: total 6, active 1, estimated missing 5, missing numbers 1|2|3|4|5
7. series_id 103, The Void Wraith Saga: total 9, active 5, estimated missing 4, missing numbers 2|3|7|8
8. series_id 207, Terra Nova Chronicles: total 8, active 4, estimated missing 4, missing numbers 2|3|6|7
9. series_id 120, Frontlines: total 11, active 8, estimated missing 3, missing numbers 2|9|10
10. series_id 173, Post-Human Series: total 6, active 3, estimated missing 3, missing numbers 2|3|4
11. series_id 231, Air Awakens: total 5, active 2, estimated missing 3, missing numbers 2|3|4
12. series_id 235, Heritage of power: total 4, active 1, estimated missing 3, missing numbers 1|2|3
13. series_id 253, Caverns and Creatures: total 5, active 2, estimated missing 3, missing numbers 2|3|4
14. series_id 286, Whiskey Tango Foxtrot: total 6, active 3, estimated missing 3, missing numbers 2|3|4
15. series_id 292, Scrapyard Ship series: total 10, active 7, estimated missing 3, missing numbers 3|6|8
16. series_id 30, Awaken Online: total 15, active 13, estimated missing 2, no integer gap signal

## Safe Row-Recreation Plan (Do Before Any Ghost Cleanup/Recalc)

### Phase 0: Freeze mutating workflows
1. Pause check-for-new background jobs.
2. Pause ghost cleanup endpoints.
3. Pause manual deletes/normalizations during repair window.

### Phase 1: Build candidate rows only (no writes yet)
For each series in diagnostics/missing_backend_rows_candidates.csv:
1. If missing_integer_numbers is populated:
- Generate one candidate per missing integer book_number.
- Set series_order = book_number.
2. If no integer-gap signal but missing_row_count_estimate > 0:
- Source candidates from canonical entries table for that series.
- If canonical entries are missing, derive candidate titles from import_raw_row/import_source evidence.

Candidate defaults:
- record_status = active
- is_missing = true
- is_upcoming_auto = false
- is_upcoming_final = false
- read_status = unread
- is_read = false
- title = "Missing Book {n}" only when no reliable title exists

### Phase 2: Human-reviewed patch set
1. Export a review sheet with columns:
- series_id, series_name, book_number, proposed_title, evidence_source, confidence
2. Approve rows with strong evidence first:
- canonical entry exact match
- import_raw_row exact number/title match
3. Flag low-confidence rows for manual curation.

### Phase 3: Idempotent write strategy
1. Upsert rule key: (series_id, book_number OR series_order).
2. Insert only if no active/deleted row already occupies that numeric slot.
3. Never resurrect deleted ghost rows automatically; require explicit approval.
4. Write in small batches (for example 20 rows/transaction) with full rollback on failure.

### Phase 4: Post-write verification gates
After each batch:
1. Re-run diagnostics/series_count_mismatch.csv query for touched series only.
2. Re-run diagnostics/series_numbering_gaps.csv query for touched series only.
3. Confirm no duplicate numeric slots within each touched series.
4. Only then run recalculate_intelligence for touched series.

### Phase 5: Global release gate
1. Re-run global mismatch report.
2. Re-run null-status report.
3. Re-enable ghost cleanup/check-for-new only after mismatch trend is acceptable and approved.

## Notes for Awaken Online (series_id 30)
- Current snapshot: total_books 15, active_rows 13, missing_row_count_estimate 2.
- No integer gap signal from current numbered rows (1..3 are present).
- Recovery for this series must use canonical/import evidence, not gap inference.
