#!/usr/bin/env bash
# Restores a backup (from ./backups/, created by backup_from_railway.sh)
# back up to the live Railway backend. Use this if the live database ever
# gets lost or corrupted.
#
# Usage:
#   ./scripts/restore_to_railway.sh                 # restores the most recent backup
#   ./scripts/restore_to_railway.sh path/to/file.db  # restores a specific backup file
set -euo pipefail

BACKEND_URL="https://book-app-production-a603.up.railway.app"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$REPO_ROOT/backups"

if [ "${1:-}" != "" ]; then
  DB_FILE="$1"
else
  DB_FILE="$(ls -1t "$BACKUP_DIR"/books_backup_*.db 2>/dev/null | head -n 1)"
  if [ -z "$DB_FILE" ]; then
    echo "No backups found in $BACKUP_DIR. Run scripts/backup_from_railway.sh first," >&2
    echo "or pass a specific file path as an argument." >&2
    exit 1
  fi
fi

if [ ! -f "$DB_FILE" ]; then
  echo "Could not find $DB_FILE" >&2
  exit 1
fi

echo "This will REPLACE the database currently live at $BACKEND_URL"
echo "with: $DB_FILE ($(ls -lh "$DB_FILE" | awk '{print $5}'))"
echo
read -r -p "Type YES to confirm: " CONFIRM
if [ "$CONFIRM" != "YES" ]; then
  echo "Aborted."
  exit 1
fi

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

echo "Uploading $DB_FILE ..."
curl -s -X POST "$BACKEND_URL/admin/import_db" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@$DB_FILE"
echo
echo

echo "Server is restarting to load the restored data -- waiting 20 seconds..."
sleep 20

echo "Verifying..."
TOKEN=$(curl -s -X POST "$BACKEND_URL/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"password\":\"$OWNER_PASSWORD\"}" | python3 -c "import sys,json;print(json.load(sys.stdin).get('token',''))")

curl -s "$BACKEND_URL/books/" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(f'Book count on Railway after restore: {len(data)}')
"

echo "Done."
