#!/usr/bin/env bash
# Fetches two books by id from the live Railway backend and prints their
# fields side by side. Useful after restoring a soft-deleted book to check
# whether the surviving "keeper" picked up any of its fields during the
# dedupe collapse (see _merge_loser_fields_into_keeper in
# services/series_check_engine.py) that need manual correction.
#
# Usage:
#   ./scripts/compare_books.sh <book_id_1> <book_id_2>
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "Usage: $0 <book_id_1> <book_id_2>" >&2
  exit 1
fi

BACKEND_URL="https://book-app-production-a603.up.railway.app"

if [ -n "${RAILWAY_OWNER_PASSWORD:-}" ]; then
  OWNER_PASSWORD="$RAILWAY_OWNER_PASSWORD"
  echo "Using password from RAILWAY_OWNER_PASSWORD env var."
else
  read -r -s -p "Enter your Railway owner password (hidden as you type): " OWNER_PASSWORD
  echo
fi

echo "Logging in..."
TOKEN=$(curl -s -X POST "$BACKEND_URL/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"password\":\"$OWNER_PASSWORD\"}" | python3 -c "import sys,json;print(json.load(sys.stdin).get('token',''))")

if [ -z "$TOKEN" ]; then
  echo "Login failed -- check the password and try again." >&2
  exit 1
fi
echo "Logged in."
echo

curl -s "$BACKEND_URL/books/$1" -H "Authorization: Bearer $TOKEN" -o /tmp/book_a.json
curl -s "$BACKEND_URL/books/$2" -H "Authorization: Bearer $TOKEN" -o /tmp/book_b.json

python3 - /tmp/book_a.json /tmp/book_b.json <<'PYEOF'
import json
import sys

with open(sys.argv[1]) as f:
    a = json.load(f)
with open(sys.argv[2]) as f:
    b = json.load(f)

fields = [
    "id", "title", "book_number", "is_read", "read_status", "read_date",
    "rating", "review", "notes", "publication_date", "release_date",
    "source_url", "isbn", "isbn13", "asin", "record_status",
]

print(f"{'field':<18} {'book ' + str(a.get('id')):<35} {'book ' + str(b.get('id')):<35}")
print("-" * 90)
for field in fields:
    va = a.get(field)
    vb = b.get(field)
    flag = "  <-- DIFFERENT" if va != vb else ""
    print(f"{field:<18} {str(va):<35} {str(vb):<35}{flag}")
PYEOF
