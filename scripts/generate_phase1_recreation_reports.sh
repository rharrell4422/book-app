#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

DB_PATH="books.db"
OUT_PROPOSALS="diagnostics/phase1_row_recreation_proposals.csv"
OUT_SUMMARY="diagnostics/phase1_series_recreation_summary.csv"

sqlite3 -header -csv "$DB_PATH" "
WITH book_rollup AS (
  SELECT
    s.id AS series_id,
    s.name AS series_name,
    s.total_books,
    SUM(
      CASE
        WHEN (b.record_status IS NULL OR b.record_status <> 'deleted')
          AND COALESCE(b.is_upcoming_final, 0) = 0
        THEN 1
        ELSE 0
      END
    ) AS active_rows
  FROM series s
  LEFT JOIN books b ON b.series_id = s.id
  GROUP BY s.id, s.name, s.total_books
),
active_book_rows AS (
  SELECT
    b.series_id,
    b.id AS book_id,
    b.title,
    LOWER(REPLACE(COALESCE(b.title, ''), '–', '-')) AS normalized_title,
    COALESCE(b.book_number, b.series_order) AS inferred_number
  FROM books b
  WHERE b.series_id IS NOT NULL
    AND (b.record_status IS NULL OR b.record_status <> 'deleted')
    AND COALESCE(b.is_upcoming_final, 0) = 0
),
active_titles AS (
  SELECT
    abr.series_id,
    abr.normalized_title
  FROM active_book_rows abr
),
omnibus_series AS (
  SELECT DISTINCT series_id
  FROM active_titles
  WHERE normalized_title LIKE '%books %-%'
),
single_book_series AS (
  SELECT
    abr.series_id,
    MIN(abr.book_id) AS only_book_id,
    MIN(abr.normalized_title) AS only_book_title,
    MAX(abr.inferred_number) AS inferred_number
  FROM active_book_rows abr
  GROUP BY abr.series_id
  HAVING COUNT(*) = 1
),
standalone_series AS (
  SELECT
    br.series_id
  FROM book_rollup br
  JOIN single_book_series sbs ON sbs.series_id = br.series_id
  WHERE COALESCE(br.active_rows, 0) = 1
    AND COALESCE(br.total_books, 0) <= 2
    AND sbs.inferred_number IS NULL
    AND sbs.only_book_title NOT LIKE '%book %'
    AND sbs.only_book_title NOT LIKE '%books %-%'
    AND LOWER(REPLACE(COALESCE(br.series_name, ''), '–', '-')) = RTRIM(sbs.only_book_title, ':')
),
active_books AS (
  SELECT
    b.series_id,
    COALESCE(b.book_number, b.series_order) AS num
  FROM books b
  WHERE b.series_id IS NOT NULL
    AND (b.record_status IS NULL OR b.record_status <> 'deleted')
    AND COALESCE(b.is_upcoming_final, 0) = 0
),
int_nums AS (
  SELECT
    series_id,
    CAST(num AS INTEGER) AS n
  FROM active_books
  WHERE num IS NOT NULL
    AND num > 0
    AND CAST(num AS REAL) = CAST(CAST(num AS INTEGER) AS REAL)
),
bounds AS (
  SELECT
    i.series_id,
    CASE
      WHEN COALESCE(br.total_books, 0) > 0 AND MAX(i.n) > COALESCE(br.total_books, 0)
        THEN COALESCE(br.total_books, 0)
      ELSE MAX(i.n)
    END AS max_n
  FROM int_nums i
  LEFT JOIN book_rollup br ON br.series_id = i.series_id
  GROUP BY i.series_id, br.total_books
),
seq(series_id, n, max_n) AS (
  SELECT series_id, 1 AS n, max_n
  FROM bounds
  UNION ALL
  SELECT series_id, n + 1, max_n
  FROM seq
  WHERE n < max_n
),
missing AS (
  SELECT seq.series_id, seq.n
  FROM seq
  LEFT JOIN int_nums i ON i.series_id = seq.series_id AND i.n = seq.n
  WHERE i.n IS NULL
),
missing_ranked AS (
  SELECT
    m.series_id,
    m.n,
    ROW_NUMBER() OVER (PARTITION BY m.series_id ORDER BY m.n) AS rn
  FROM missing m
),
affected AS (
  SELECT
    br.series_id,
    br.series_name,
    COALESCE(br.total_books, 0) AS total_books,
    COALESCE(br.active_rows, 0) AS active_rows,
    (COALESCE(br.total_books, 0) - COALESCE(br.active_rows, 0)) AS missing_row_count_estimate
  FROM book_rollup br
  LEFT JOIN omnibus_series os ON os.series_id = br.series_id
  LEFT JOIN standalone_series ss ON ss.series_id = br.series_id
  WHERE COALESCE(br.total_books, 0) > COALESCE(br.active_rows, 0)
    AND os.series_id IS NULL
    AND ss.series_id IS NULL
),
gap_candidates AS (
  SELECT
    a.series_id,
    a.series_name,
    m.n AS proposed_book_number,
    sce.canonical_title,
    db.title AS deleted_title,
    a.missing_row_count_estimate
  FROM affected a
  JOIN missing_ranked m ON m.series_id = a.series_id
    AND m.rn <= a.missing_row_count_estimate
  LEFT JOIN series_canonical_entries sce
    ON sce.series_id = a.series_id
    AND CAST(sce.book_number AS INTEGER) = m.n
    AND CAST(sce.book_number AS REAL) = CAST(CAST(sce.book_number AS INTEGER) AS REAL)
  LEFT JOIN books db
    ON db.series_id = a.series_id
    AND (db.book_number = m.n OR db.series_order = m.n)
    AND db.record_status = 'deleted'
),
no_gap_series AS (
  SELECT
    a.series_id,
    a.series_name,
    a.missing_row_count_estimate
  FROM affected a
  LEFT JOIN (SELECT DISTINCT series_id FROM missing) mg ON mg.series_id = a.series_id
  WHERE mg.series_id IS NULL
),
generated_proposals AS (
  SELECT
    gc.series_id,
    gc.series_name,
    gc.proposed_book_number,
    COALESCE(gc.canonical_title, gc.deleted_title, 'Missing Book ' || gc.proposed_book_number) AS proposed_title,
    CASE
      WHEN gc.canonical_title IS NOT NULL THEN 'canonical_entry'
      WHEN gc.deleted_title IS NOT NULL THEN 'deleted_tombstone_row'
      ELSE 'integer_gap_inference'
    END AS evidence_source,
    CASE
      WHEN gc.canonical_title IS NOT NULL THEN 'high'
      WHEN gc.deleted_title IS NOT NULL THEN 'medium'
      ELSE 'low'
    END AS confidence,
    CASE
      WHEN gc.canonical_title IS NOT NULL OR gc.deleted_title IS NOT NULL THEN 'auto_recreate_candidate'
      ELSE 'manual_review_required'
    END AS recommended_recreation_action,
    CASE
      WHEN gc.canonical_title IS NOT NULL THEN 'Canonical title present for missing number.'
      WHEN gc.deleted_title IS NOT NULL THEN 'Deleted tombstone row exists for this number.'
      ELSE 'No canonical/deleted evidence for this numbered gap; manual verification needed.'
    END AS notes,
    gc.missing_row_count_estimate
  FROM gap_candidates gc
),
resolved_omnibus AS (
  SELECT
    br.series_id,
    br.series_name,
    NULL AS proposed_book_number,
    'Omnibus coverage validated (Books X-Y ranges)' AS proposed_title,
    'omnibus_range_coverage' AS evidence_source,
    'high' AS confidence,
    'no_action_needed' AS recommended_recreation_action,
    'Series uses omnibus range titles; count mismatch is expected and not a missing-row condition.' AS notes,
    0 AS missing_row_count_estimate
  FROM book_rollup br
  JOIN omnibus_series os ON os.series_id = br.series_id
  WHERE COALESCE(br.total_books, 0) > COALESCE(br.active_rows, 0)
),
resolved_standalone AS (
  SELECT
    br.series_id,
    br.series_name,
    NULL AS proposed_book_number,
    'Standalone title misclassified as series' AS proposed_title,
    'standalone_misclassification' AS evidence_source,
    'high' AS confidence,
    'no_action_needed' AS recommended_recreation_action,
    'Single-book title should be treated as standalone; no missing-row action required.' AS notes,
    0 AS missing_row_count_estimate
  FROM book_rollup br
  JOIN standalone_series ss ON ss.series_id = br.series_id
  WHERE COALESCE(br.total_books, 0) > COALESCE(br.active_rows, 0)
)
SELECT
  series_id,
  series_name,
  proposed_book_number,
  proposed_title,
  evidence_source,
  confidence,
  recommended_recreation_action,
  notes,
  missing_row_count_estimate
FROM (
  SELECT * FROM generated_proposals
  UNION ALL
  SELECT * FROM resolved_omnibus
  UNION ALL
  SELECT * FROM resolved_standalone
)
ORDER BY
  series_id,
  CASE WHEN proposed_book_number IS NULL THEN 1000000 ELSE proposed_book_number END,
  proposed_title;
" > "$OUT_PROPOSALS"

sqlite3 -header -csv "$DB_PATH" "
WITH book_rollup AS (
  SELECT
    s.id AS series_id,
    s.name AS series_name,
    s.total_books,
    SUM(
      CASE
        WHEN (b.record_status IS NULL OR b.record_status <> 'deleted')
          AND COALESCE(b.is_upcoming_final, 0) = 0
        THEN 1
        ELSE 0
      END
    ) AS active_rows
  FROM series s
  LEFT JOIN books b ON b.series_id = s.id
  GROUP BY s.id, s.name, s.total_books
),
active_book_rows AS (
  SELECT
    b.series_id,
    b.id AS book_id,
    LOWER(REPLACE(COALESCE(b.title, ''), '–', '-')) AS normalized_title,
    COALESCE(b.book_number, b.series_order) AS inferred_number
  FROM books b
  WHERE b.series_id IS NOT NULL
    AND (b.record_status IS NULL OR b.record_status <> 'deleted')
    AND COALESCE(b.is_upcoming_final, 0) = 0
),
active_titles AS (
  SELECT
    abr.series_id,
    abr.normalized_title
  FROM active_book_rows abr
),
omnibus_series AS (
  SELECT DISTINCT series_id
  FROM active_titles
  WHERE normalized_title LIKE '%books %-%'
),
single_book_series AS (
  SELECT
    abr.series_id,
    MIN(abr.book_id) AS only_book_id,
    MIN(abr.normalized_title) AS only_book_title,
    MAX(abr.inferred_number) AS inferred_number
  FROM active_book_rows abr
  GROUP BY abr.series_id
  HAVING COUNT(*) = 1
),
standalone_series AS (
  SELECT
    br.series_id
  FROM book_rollup br
  JOIN single_book_series sbs ON sbs.series_id = br.series_id
  WHERE COALESCE(br.active_rows, 0) = 1
    AND COALESCE(br.total_books, 0) <= 2
    AND sbs.inferred_number IS NULL
    AND sbs.only_book_title NOT LIKE '%book %'
    AND sbs.only_book_title NOT LIKE '%books %-%'
    AND LOWER(REPLACE(COALESCE(br.series_name, ''), '–', '-')) = RTRIM(sbs.only_book_title, ':')
),
active_books AS (
  SELECT
    b.series_id,
    COALESCE(b.book_number, b.series_order) AS num
  FROM books b
  WHERE b.series_id IS NOT NULL
    AND (b.record_status IS NULL OR b.record_status <> 'deleted')
    AND COALESCE(b.is_upcoming_final, 0) = 0
),
int_nums AS (
  SELECT
    series_id,
    CAST(num AS INTEGER) AS n
  FROM active_books
  WHERE num IS NOT NULL
    AND num > 0
    AND CAST(num AS REAL) = CAST(CAST(num AS INTEGER) AS REAL)
),
bounds AS (
  SELECT
    i.series_id,
    CASE
      WHEN COALESCE(br.total_books, 0) > 0 AND MAX(i.n) > COALESCE(br.total_books, 0)
        THEN COALESCE(br.total_books, 0)
      ELSE MAX(i.n)
    END AS max_n
  FROM int_nums i
  LEFT JOIN book_rollup br ON br.series_id = i.series_id
  GROUP BY i.series_id, br.total_books
),
seq(series_id, n, max_n) AS (
  SELECT series_id, 1 AS n, max_n
  FROM bounds
  UNION ALL
  SELECT series_id, n + 1, max_n
  FROM seq
  WHERE n < max_n
),
missing AS (
  SELECT seq.series_id, seq.n
  FROM seq
  LEFT JOIN int_nums i ON i.series_id = seq.series_id AND i.n = seq.n
  WHERE i.n IS NULL
),
missing_ranked AS (
  SELECT
    m.series_id,
    m.n,
    ROW_NUMBER() OVER (PARTITION BY m.series_id ORDER BY m.n) AS rn
  FROM missing m
),
affected AS (
  SELECT
    br.series_id,
    br.series_name,
    (COALESCE(br.total_books, 0) - COALESCE(br.active_rows, 0)) AS missing_row_count_estimate
  FROM book_rollup br
  LEFT JOIN omnibus_series os ON os.series_id = br.series_id
  LEFT JOIN standalone_series ss ON ss.series_id = br.series_id
  WHERE COALESCE(br.total_books, 0) > COALESCE(br.active_rows, 0)
    AND os.series_id IS NULL
    AND ss.series_id IS NULL
),
gap_candidates AS (
  SELECT
    a.series_id,
    a.series_name,
    m.n AS proposed_book_number,
    sce.canonical_title,
    db.title AS deleted_title,
    a.missing_row_count_estimate
  FROM affected a
  JOIN missing_ranked m ON m.series_id = a.series_id
    AND m.rn <= a.missing_row_count_estimate
  LEFT JOIN series_canonical_entries sce
    ON sce.series_id = a.series_id
    AND CAST(sce.book_number AS INTEGER) = m.n
    AND CAST(sce.book_number AS REAL) = CAST(CAST(sce.book_number AS INTEGER) AS REAL)
  LEFT JOIN books db
    ON db.series_id = a.series_id
    AND (db.book_number = m.n OR db.series_order = m.n)
    AND db.record_status = 'deleted'
),
no_gap_series AS (
  SELECT
    a.series_id,
    a.series_name,
    a.missing_row_count_estimate
  FROM affected a
  LEFT JOIN (SELECT DISTINCT series_id FROM missing) mg ON mg.series_id = a.series_id
  WHERE mg.series_id IS NULL
),
generated_proposals AS (
  SELECT
    gc.series_id,
    gc.series_name,
    CASE
      WHEN gc.canonical_title IS NOT NULL THEN 'high'
      WHEN gc.deleted_title IS NOT NULL THEN 'medium'
      ELSE 'low'
    END AS confidence,
    gc.missing_row_count_estimate
  FROM gap_candidates gc
),
resolved_omnibus AS (
  SELECT
    br.series_id,
    br.series_name,
    'high' AS confidence,
    0 AS missing_row_count_estimate
  FROM book_rollup br
  JOIN omnibus_series os ON os.series_id = br.series_id
  WHERE COALESCE(br.total_books, 0) > COALESCE(br.active_rows, 0)
),
resolved_standalone AS (
  SELECT
    br.series_id,
    br.series_name,
    'high' AS confidence,
    0 AS missing_row_count_estimate
  FROM book_rollup br
  JOIN standalone_series ss ON ss.series_id = br.series_id
  WHERE COALESCE(br.total_books, 0) > COALESCE(br.active_rows, 0)
),
proposals AS (
  SELECT * FROM generated_proposals
  UNION ALL
  SELECT * FROM resolved_omnibus
  UNION ALL
  SELECT * FROM resolved_standalone
)
SELECT
  series_id,
  series_name,
  MAX(missing_row_count_estimate) AS missing_row_count_estimate,
  COUNT(*) AS proposal_rows,
  SUM(CASE WHEN confidence = 'high' THEN 1 ELSE 0 END) AS high_confidence_rows,
  SUM(CASE WHEN confidence = 'medium' THEN 1 ELSE 0 END) AS medium_confidence_rows,
  SUM(CASE WHEN confidence = 'low' THEN 1 ELSE 0 END) AS low_confidence_rows
FROM proposals
GROUP BY series_id, series_name
ORDER BY series_id;
" > "$OUT_SUMMARY"

echo "Generated: $OUT_PROPOSALS"
echo "Generated: $OUT_SUMMARY"
