"""One-off measurement runner (not part of the app) -- fires
run_series_check_job_full repeatedly against one series_id and prints a
compact round-by-round timing/call-count report using the new
DiscoveryTelemetry instrumentation, so TIMEOUT_BUDGET_SECONDS/MAX_ROUNDS can
be set from real data instead of an estimate. See
discovery_catchup_architecture_spec.md.

Usage: python3 scripts/measure_series_check.py <series_id> [num_rounds]
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal
import models
from services.series_check_engine import run_series_check_job_full, series_check_jobs


def main() -> None:
    series_id = int(sys.argv[1]) if len(sys.argv) > 1 else 323
    num_rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 6

    rounds = []
    for round_num in range(1, num_rounds + 1):
        db = SessionLocal()
        series = db.query(models.Series).filter(models.Series.id == series_id).first()
        owned_before = sorted(
            b.book_number
            for b in db.query(models.Book).filter(models.Book.series_id == series_id).all()
            if b.book_number is not None
        )
        db.close()

        print(f"\n===== ROUND {round_num} ===== (owned before: {owned_before})")
        started = time.perf_counter()
        run_series_check_job_full(series_id)
        elapsed = time.perf_counter() - started

        job = series_check_jobs.get(series_id) or {}
        result = job.get("result") or {}
        telemetry = result.get("telemetry")
        added = result.get("added_books") or []

        db = SessionLocal()
        owned_after = sorted(
            b.book_number
            for b in db.query(models.Book).filter(models.Book.series_id == series_id).all()
            if b.book_number is not None
        )
        db.close()

        print(f"ROUND {round_num} wall_time={elapsed:.2f}s added_count={len(added)} owned_after={owned_after}")
        if telemetry:
            print(
                f"  telemetry: web_search_calls={telemetry.get('total_web_search_calls')} "
                f"llm_calls={telemetry.get('total_llm_calls')} "
                f"tokens_in={telemetry.get('total_tokens_in')} tokens_out={telemetry.get('total_tokens_out')} "
                f"cost_usd={telemetry.get('total_cost_usd')}"
            )
            for pass_name, stats in (telemetry.get("by_pass") or {}).items():
                print(f"    {pass_name}: {stats}")
        else:
            print("  telemetry: none captured")

        rounds.append(
            {
                "round": round_num,
                "wall_time_s": round(elapsed, 2),
                "added_count": len(added),
                "owned_after": owned_after,
                "telemetry": telemetry,
                "status": result.get("status"),
                "provider_failures": result.get("provider_failures"),
                "all_providers_failed": result.get("all_providers_failed"),
            }
        )

        if not added and round_num > 1:
            print(f"No new books found in round {round_num}; stopping early.")
            break

    print("\n\n===== SUMMARY =====")
    total_web_search = sum((r["telemetry"] or {}).get("total_web_search_calls", 0) for r in rounds)
    total_llm = sum((r["telemetry"] or {}).get("total_llm_calls", 0) for r in rounds)
    total_tokens_in = sum((r["telemetry"] or {}).get("total_tokens_in", 0) for r in rounds)
    total_tokens_out = sum((r["telemetry"] or {}).get("total_tokens_out", 0) for r in rounds)
    total_cost_usd = sum((r["telemetry"] or {}).get("total_cost_usd", 0) for r in rounds)
    total_wall = sum(r["wall_time_s"] for r in rounds)
    for r in rounds:
        print(
            f"round {r['round']}: {r['wall_time_s']}s, added={r['added_count']}, "
            f"owned_after={r['owned_after']}, status={r['status']}"
        )
    print(f"TOTAL wall_time={total_wall:.2f}s web_search_calls={total_web_search} llm_calls={total_llm} "
          f"tokens_in={total_tokens_in} tokens_out={total_tokens_out} cost_usd={total_cost_usd}")


if __name__ == "__main__":
    main()
