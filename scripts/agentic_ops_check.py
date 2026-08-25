"""Phase 10 post-phase operational checklist -- a read-only ops tool for
checking the live, deployed agentic system's health against production.

Mints a short-lived owner JWT locally from AUTH_SECRET_KEY (read from the
process environment -- run this via `railway run python3 scripts/
agentic_ops_check.py` so Railway injects the real production secret
without it ever being typed, echoed, or committed anywhere) and then
makes GET-only requests against the live API:

  - GET /admin/agentic/startup-check
  - GET /admin/agentic/summary
  - GET /admin/agentic/metrics
  - GET /admin/agentic/readiness/{series_id} for every series in every
    profile in the library

No writes, no provider calls, no discovery triggered -- every endpoint
this script calls is itself read-only (see docs/agentic_system_overview.md
Section 7). Prints a concise pass/fail summary; pass --json for the full
raw payload instead.

Usage:
    railway run python3 scripts/agentic_ops_check.py [--json] [--base-url URL]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
import jwt

DEFAULT_BASE_URL = "https://book-app-production-a603.up.railway.app"


def _mint_owner_token(secret_key: str) -> str:
    return jwt.encode(
        {"role": "owner", "exp": datetime.now(timezone.utc) + timedelta(minutes=10)},
        secret_key,
        algorithm="HS256",
    )


def _get(base_url: str, path: str, headers: dict):
    resp = httpx.get(f"{base_url}{path}", headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def run_checks(base_url: str) -> dict:
    secret_key = os.environ.get("AUTH_SECRET_KEY")
    if not secret_key:
        print(
            "ERROR: AUTH_SECRET_KEY not present in environment. Run this via "
            "`railway run python3 scripts/agentic_ops_check.py` so the real "
            "production secret is injected.",
            file=sys.stderr,
        )
        sys.exit(1)

    headers = {"Authorization": f"Bearer {_mint_owner_token(secret_key)}"}

    results: dict = {
        "startup_check": _get(base_url, "/admin/agentic/startup-check", headers),
        "summary": _get(base_url, "/admin/agentic/summary", headers),
        "metrics": _get(base_url, "/admin/agentic/metrics", headers),
    }

    profiles = _get(base_url, "/profiles", headers)
    if isinstance(profiles, dict):
        profiles = profiles.get("profiles", profiles.get("items", []))

    readiness_by_series: dict = {}
    for profile in profiles:
        profile_id = profile.get("id") if isinstance(profile, dict) else profile
        profile_headers = dict(headers)
        if profile_id:
            profile_headers["x-profile-id"] = str(profile_id)

        series_list = httpx.get(f"{base_url}/series", headers=profile_headers, timeout=30)
        series_list.raise_for_status()

        for series in series_list.json():
            series_id = series.get("id") if isinstance(series, dict) else series
            if series_id is None or series_id in readiness_by_series:
                continue
            name = series.get("name") if isinstance(series, dict) else None
            try:
                readiness = _get(base_url, f"/admin/agentic/readiness/{series_id}", headers)["readiness"]
            except Exception as exc:  # noqa: BLE001 -- surfaced in the report, not swallowed
                readiness = {"error": str(exc)}
            readiness_by_series[series_id] = {"name": name, "profile_id": profile_id, "readiness": readiness}

    results["readiness_by_series"] = readiness_by_series
    return results


def summarize(results: dict) -> None:
    print(f"startup_check.invariants_ok: {results['startup_check'].get('invariants_ok')}")
    print(f"summary: {results['summary']}")
    print()

    by_series = results["readiness_by_series"]
    total = len(by_series)
    ready_count = sum(1 for v in by_series.values() if v["readiness"].get("ready") is True)
    activated_count = sum(1 for v in by_series.values() if v["readiness"].get("activation_state") is True)
    errored = {sid: v["readiness"]["error"] for sid, v in by_series.items() if "error" in v["readiness"]}

    non_activation_concerns = {
        sid: v["readiness"]
        for sid, v in by_series.items()
        if "error" not in v["readiness"]
        and (
            not v["readiness"].get("promotion_history_ok")
            or not v["readiness"].get("determinism_ok")
            or not v["readiness"].get("metrics_ok")
            or not v["readiness"].get("cache_ok")
            or v["readiness"].get("safety_violations_recent", 0) > 0
        )
    }

    print(f"series checked: {total}")
    print(f"  ready=True: {ready_count}")
    print(f"  activated (in AGENTIC_SERIES_ACTIVATION): {activated_count}")
    print(f"  readiness check errored: {len(errored)}")
    print(f"  genuine concern (non-activation field false, or violations>0): {len(non_activation_concerns)}")
    if non_activation_concerns:
        print("  series with a concern:")
        for sid, r in non_activation_concerns.items():
            print(f"    {sid}: {r}")
    if errored:
        print("  series that errored:")
        for sid, err in errored.items():
            print(f"    {sid}: {err}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the full raw JSON payload instead of a summary")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="override the API base URL")
    args = parser.parse_args()

    results = run_checks(args.base_url)
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        summarize(results)


if __name__ == "__main__":
    main()
