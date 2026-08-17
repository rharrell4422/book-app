#!/usr/bin/env bash
# Applies a partial update to one book via PATCH /books/{book_id} on the
# live Railway backend. Only the fields you pass in the JSON payload are
# changed (including explicit nulls, which clear a field) -- everything
# else on the book is left untouched.
#
# Usage:
#   ./scripts/patch_book.sh <book_id> '<json_payload>' [profile_id]
#
# Example (clear a read-status leak picked up from a dedupe collapse):
#   ./scripts/patch_book.sh 2829 '{"is_read": false, "read_status": "upcoming", "read_date": null}'
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "Usage: $0 <book_id> '<json_payload>' [profile_id]" >&2
  exit 1
fi
BOOK_ID="$1"
PAYLOAD="$2"
PROFILE_ID="${3:-robbie}"

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

echo "Patching book_id=$BOOK_ID (profile=$PROFILE_ID) with: $PAYLOAD"
curl -s -X PATCH "$BACKEND_URL/books/$BOOK_ID" \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Profile-Id: $PROFILE_ID" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" | python3 -m json.tool
