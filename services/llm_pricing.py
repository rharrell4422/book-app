"""Per-1M-token USD pricing for known LLM model_ids (HTA Orchestrator
Step 2), used exclusively by `DiscoveryTelemetry.record_llm_call()` to
compute `cost_usd` at the moment a call is recorded.

Units: each entry is `(input_price_per_million_usd,
output_price_per_million_usd)` -- standard (non-batch, non-cached) list
pricing, matching the granularity real provider invoices bill at. A
caller must divide `tokens * price / 1_000_000`, never `tokens * price`
directly (see `discovery_telemetry.record_llm_call`).

Fail-soft by design: an unrecognized `model_id` (typo, a new model wired
in before its price is added here, a Groq model_id once that arrives)
has no entry in this table. `record_llm_call()` treats that as `$0.0`
cost plus a logged warning -- never a raised exception -- so a stale or
incomplete pricing table can never break a live discovery run. The
warning is what surfaces the gap; silently returning `$0.0` with no log
would let this cost-accounting feature quietly stop meaning anything
without anyone noticing.
"""

from __future__ import annotations

# Source: platform.claude.com pricing page, standard (non-batch,
# non-cached) rates. Update this table -- and only this table -- when a
# new model_id is wired into `llm_client.py`.
PRICING_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}


def get_price_per_million(model_id: str | None) -> tuple[float, float] | None:
    """Returns `(input_price, output_price)` per 1M tokens for `model_id`,
    or None if `model_id` is falsy or not in the table -- callers must
    treat None as "unknown, apply fail-soft $0.0" rather than raising.
    """
    if not model_id:
        return None
    return PRICING_PER_MILLION_TOKENS.get(model_id)
