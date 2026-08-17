#!/usr/bin/env bash
# Diagnostic: logs into the live Railway backend and prints a short summary
# of /admin/fractional_identity_collisions -- the narrow report of books
# wrongly soft-deleted by the old fractional book-number truncation bug
# (e.g. "3" vs "3.5" being treated as the same book).
#
# Prints a short human-readable summary to the terminal (never the raw
# JSON dump) and saves the full detail to a file you can open separately.
#
# Usage:
#   ./scripts/check_fractional_collisions.sh
# Or, to skip the interactive password prompt:
#   RAILWAY_OWNER_PASSWORD='your-password' ./scripts/check_fractional_collisions.sh
set -euo pipefail

BACKEND_URL="https://book-app-production-a603.up.railway.app"
OUT_FILE="/tmp/fractional_collisions.json"

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

echo "Fetching fractional identity collisions..."
curl -s "$BACKEND_URL/admin/fractional_identity_collisions" \
  -H "Authorization: Bearer $TOKEN" \
  -o "$OUT_FILE"

python3 - "$OUT_FILE" <<'PYEOF'
import json
import sys

with open(sys.argv[1]) as f:
    data = json.load(f)

count = data.get("count", 0)
entries = data.get("entries", [])

print()
print(f"Found {count} affected series.")
print()

for entry in entries:
    print(f"Series: {entry['series_name']} (series_id={entry['series_id']})")
    print(f"  Collided around book number: {entry['collided_truncated_number']}")
    for member in entry["members"]:
        status = member["record_status"]
        marker = "DELETED " if status == "deleted" else "active  "
        print(f"    [{marker}] book_id={member['book_id']:<6} #{member['book_number']:<5} {member['title']}")
    print()

if count:
    print(f"Full JSON detail saved to: {sys.argv[1]}")
    print("To restore a wrongly-deleted book, note its book_id above and run:")
    print("  ./scripts/restore_book.sh <book_id>")
else:
    print("No fractional-collision damage found. Nothing to restore.")
PYEOF
