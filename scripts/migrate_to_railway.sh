#!/usr/bin/env bash
# Uploads your local books.db to the live Railway backend, replacing
# whatever database is currently there. Run from anywhere; paths below
# are relative to the repo root regardless of your current directory.
set -euo pipefail

BACKEND_URL="https://book-app-production-a603.up.railway.app"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_FILE="$REPO_ROOT/books.db"

if [ ! -f "$DB_FILE" ]; then
  echo "Could not find $DB_FILE" >&2
  exit 1
fi

echo "This will REPLACE the database currently live at $BACKEND_URL"
echo "with your local file: $DB_FILE"
echo

if [ -n "${RAILWAY_OWNER_PASSWORD:-}" ]; then
  OWNER_PASSWORD="$RAILWAY_OWNER_PASSWORD"
  echo "Using password from RAILWAY_OWNER_PASSWORD env var."
else
  read -r -p "Enter your Railway owner password (will be visible as you type): " OWNER_PASSWORD
fi
echo

echo "Logging in..."
TOKEN=$(curl -s -X POST "$BACKEND_URL/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"password\":\"$OWNER_PASSWORD\"}" | python3 -c "import sys,json;print(json.load(sys.stdin).get('token',''))")

if [ -z "$TOKEN" ]; then
  echo "Login failed -- check the password and try again." >&2
  exit 1
fi
echo "Logged in."

echo "Uploading $(ls -lh "$DB_FILE" | awk '{print $5}') database file..."
curl -s -X POST "$BACKEND_URL/admin/import_db" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@$DB_FILE"
echo
echo

echo "Server is restarting to load the new data -- waiting 20 seconds..."
sleep 20

echo "Verifying..."
TOKEN=$(curl -s -X POST "$BACKEND_URL/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"password\":\"$OWNER_PASSWORD\"}" | python3 -c "import sys,json;print(json.load(sys.stdin).get('token',''))")

curl -s "$BACKEND_URL/books/" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f'Book count on Railway: {len(data)}')
"

echo "Done."
