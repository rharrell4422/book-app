#!/usr/bin/env bash
set -euo pipefail

if grep -RInE --exclude='check_legacy_discovery.sh' --exclude-dir=.git --exclude-dir=.next --exclude-dir=node_modules --exclude-dir=__pycache__ --exclude-dir=venv '(SearchOrchestrator|legacy_series_discovery|suggest_book_by_series|_discover_book_metadata)' .; then
  echo 'Legacy discovery references found.'
  exit 1
fi

echo 'Legacy discovery references not found.'
