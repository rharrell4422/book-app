#!/usr/bin/env bash
# Lists every book row for one series_id, scoped to one profile, with the
# fields needed to diagnose bogus/duplicate discovery results (title,
# book_number, source_url, record_status, is_missing, date_added).
#
# Usage:
#   ./scripts/list_series_books.sh <series_id> <profile_id>
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "Usage: $0 <series_id> <profile_id>" >&2
  exit 1
fi
SERIES_ID="$1"
PROFILE_ID="$2"

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

OUT_FILE="/tmp/series_${SERIES_ID}_${PROFILE_ID}.json"
curl -s "$BACKEND_URL/books/by_series/$SERIES_ID" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Profile-Id: $PROFILE_ID" \
  -o "$OUT_FILE"

python3 - "$OUT_FILE" "$PROFILE_ID" <<'PYEOF'
import json
import sys

path, profile_id = sys.argv[1], sys.argv[2]
with open(path) as f:
    books = json.load(f)

if isinstance(books, dict):
    print("Error response:", json.dumps(books, indent=2))
    sys.exit(1)

print(f"Profile: {profile_id} -- {len(books)} book(s)")
print()
for b in books:
    print(f"id={b.get('id'):<6} #{str(b.get('book_number')):<5} status={str(b.get('record_status')):<9} "
          f"read_status={str(b.get('read_status')):<10} missing={b.get('is_missing')!s:<5} "
          f"asin={b.get('asin') or '-':<16} title={b.get('title')}")
    print(f"       date_added={b.get('date_added')} release_date={b.get('release_date')} "
          f"publication_date={b.get('publication_date')} source_url={b.get('source_url')}")
    print()

print(f"Full JSON saved to: {path}")
PYEOF
