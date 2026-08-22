#!/usr/bin/env bash
set -euo pipefail

# Guards against silently regressing to zero working discovery providers.
# Book discovery is API-based (discovery_engine.py calling Google Books,
# OpenLibrary, Hardcover.app, and a Serper Search + Claude web-search
# provider) rather than HTML scraping -- this checks that each provider's
# fetch function still exists and is still wired into the series discovery
# agent.

required_in_discovery_engine=(
  "_fetch_google_books"
  "_fetch_openlibrary"
  "_fetch_hardcover"
  "_fetch_web_search"
)

for pattern in "${required_in_discovery_engine[@]}"; do
  if ! grep -qE "$pattern" discovery_engine.py; then
    echo "Required discovery provider function missing from discovery_engine.py: $pattern"
    exit 1
  fi
done

if ! grep -qE "discovery_engine\.discover_candidates_for_series" agents/series_agent.py; then
  echo "series_agent.py no longer calls discovery_engine.discover_candidates_for_series -- discovery pipeline is disconnected."
  exit 1
fi

echo "Discovery source guard passed."
