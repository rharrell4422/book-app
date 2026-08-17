#!/usr/bin/env bash
# Finds series_id(s) matching a name substring for one profile, via
# GET /series/. Case-insensitive substring match on the series name.
#
# Usage:
#   ./scripts/find_series_id.sh "<series name substring>" <profile_id>
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "Usage: $0 '<series name substring>' <profile_id>" >&2
  exit 1
fi
NAME_SUBSTR="$1"
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

curl -s "$BACKEND_URL/series/" -H "Authorization: Bearer $TOKEN" -H "X-Profile-Id: $PROFILE_ID" -o /tmp/all_series.json

python3 - /tmp/all_series.json "$NAME_SUBSTR" "$PROFILE_ID" <<'PYEOF'
import json
import sys

path, needle, profile_id = sys.argv[1], sys.argv[2].lower(), sys.argv[3]
with open(path) as f:
    series = json.load(f)

matches = [s for s in series if needle in (s.get("name") or "").lower()]
print(f"Profile: {profile_id}")
if not matches:
    print("No matching series found.")
for s in matches:
    print(f"  series_id={s.get('id'):<6} name={s.get('name')!r} author={s.get('author')!r}")
PYEOF
