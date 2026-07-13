#!/usr/bin/env bash
# Downloads the live Railway database and saves a timestamped copy locally.
# Run this periodically (weekly is plenty) so your book data has a backup
# outside of Railway's single volume. Run from anywhere; paths below are
# relative to the repo root regardless of your current directory.
set -euo pipefail

BACKEND_URL="https://book-app-production-a603.up.railway.app"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$REPO_ROOT/backups"
mkdir -p "$BACKUP_DIR"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
OUT_FILE="$BACKUP_DIR/books_backup_$TIMESTAMP.db"

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

echo "Downloading database..."
curl -s -X GET "$BACKEND_URL/admin/export_db" \
  -H "Authorization: Bearer $TOKEN" \
  -o "$OUT_FILE"

SIZE=$(ls -lh "$OUT_FILE" | awk '{print $5}')
echo
echo "Saved backup: $OUT_FILE ($SIZE)"

# Keep only the 10 most recent backups so this doesn't grow forever.
ls -1t "$BACKUP_DIR"/books_backup_*.db 2>/dev/null | tail -n +11 | xargs -r rm --
echo "Done. (Older backups beyond the most recent 10 were cleaned up.)"
