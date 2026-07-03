#!/usr/bin/env bash
set -euo pipefail

required_patterns=(
  "search_web_read_candidates"
  "discovery_mode\""
  "search_amazon_products"
  "search_fantastic_fiction"
  "provider\": \"amazon\""
  "provider\": \"fantastic_fiction\""
)

for pattern in "${required_patterns[@]}"; do
  if ! grep -RInE --exclude-dir=.git --exclude-dir=.next --exclude-dir=node_modules --exclude-dir=__pycache__ --exclude-dir=venv "$pattern" agents/series_agent.py intelligence.py >/dev/null; then
    echo "Required discovery source pattern missing: $pattern"
    exit 1
  fi
done

echo "Discovery source guard passed."
