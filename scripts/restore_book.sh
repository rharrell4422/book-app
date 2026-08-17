#!/usr/bin/env bash
# Restores one soft-deleted book (sets record_status back to "active") on
# the live Railway backend via POST /admin/restore_book/{book_id}.
#
# Usage:
#   ./scripts/restore_book.sh <book_id>
# Or, to skip the interactive password prompt:
#   RAILWAY_OWNER_PASSWORD='your-password' ./scripts/restore_book.sh <book_id>
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <book_id>" >&2
  exit 1
fi
BOOK_ID="$1"

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

echo "Restoring book_id=$BOOK_ID..."
curl -s -X POST "$BACKEND_URL/admin/restore_book/$BOOK_ID" \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
