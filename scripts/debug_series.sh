#!/usr/bin/env bash
# Diagnostic: logs into the live Railway backend and prints the raw
# /series/ response so we can see the actual error (if any) instead of
# just an empty list in the UI.
set -euo pipefail

BACKEND_URL="https://book-app-production-a603.up.railway.app"

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
echo

FRONTEND_URL="https://determined-achievement-production-c69b.up.railway.app"

echo "--- Direct to backend: GET /series/ (size + timing) ---"
curl -s -o /tmp/series_direct.json -w "http_code=%{http_code} size_bytes=%{size_download} total_time=%{time_total}s\n" \
  "$BACKEND_URL/series/" -H "Authorization: Bearer $TOKEN"
python3 -c "
import json
data = json.load(open('/tmp/series_direct.json'))
print('series count:', len(data) if isinstance(data, list) else 'not a list')
"
echo

echo "--- Through frontend proxy: GET /api/series (size + timing, 30s max) ---"
curl -s -o /tmp/series_proxy.json -w "http_code=%{http_code} size_bytes=%{size_download} total_time=%{time_total}s\n" \
  --max-time 30 \
  "$FRONTEND_URL/api/series" -H "Authorization: Bearer $TOKEN"
echo "First 300 chars of proxy response:"
head -c 300 /tmp/series_proxy.json
echo
python3 -c "
import json
try:
    data = json.load(open('/tmp/series_proxy.json'))
    print('PROXY series count:', len(data) if isinstance(data, list) else data)
except Exception as e:
    print('Could not parse proxy response as JSON:', e)
"
