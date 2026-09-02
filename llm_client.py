"""Provider-agnostic LLM client wrapper (HTA Orchestrator Step 1).

Every LLM call in this codebase goes through a single Anthropic SDK call
today, duplicated near-identically across provider_io.py's three call
sites (`_structure_web_results_with_llm`, `generate_series_overview`,
`_reconcile_candidates_with_llm`): construct a fresh `anthropic.Anthropic`
client, call `.messages.create(...)`, join the response's text blocks, and
read `usage.input_tokens`/`usage.output_tokens`. This module centralizes
that dispatch so a second provider (Groq, OpenAI-compatible) can be added
later without touching every call site again.

Deliberately thin, by design (see `llm-client-wrapper-evaluation` canvas
from the Step 1 review):
  - This module owns: provider dispatch, raw text extraction, and
    normalizing token-usage field names across providers.
  - Call sites keep owning: markdown-fence stripping, JSON parsing, and
    shape-specific validation -- those differ enough per call site
    (list vs. dict, strict index partitioning) that pushing them in here
    would stop this from being a thin wrapper.

Compatibility constraint this module exists to satisfy: roughly a dozen
existing tests (tests/test_series_discovery.py) patch `anthropic.Anthropic`
directly at its real module path and assert on exact `messages.create(...)`
kwargs. `_call_anthropic` below preserves that exact call shape (same
kwarg names, `temperature` only included when the caller actually wants
one set -- see `generate_series_overview`'s deliberate "no temperature"
asymmetry) so none of those tests need to change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class LLMCallError(Exception):
    """Raised for any failure calling the underlying provider (missing
    credentials, network/timeout error, SDK exception, unrecognized
    model_id). Callers should treat this exactly like the raw SDK
    exception they used to catch directly -- every existing call site
    already wraps its call in a broad `except Exception`, so this
    requires no change to that error-handling shape.
    """


@dataclass(frozen=True)
class LLMResponse:
    """Normalized result of one LLM call, provider-independent."""

    text: str
    tokens_in: int
    tokens_out: int
    model_id: str


# Anthropic model_ids recognized today. Extended (or replaced by a
# prefix/pattern check) once Groq model_ids exist -- see this module's
# docstring; no such model_id exists anywhere in this codebase yet, so
# there is nothing to branch on beyond Anthropic today.
_ANTHROPIC_MODEL_PREFIXES = ("claude-",)


# HTA Orchestrator Step 4: tier -> model_id resolution, used only when a
# caller omits `model_id` and passes `tier` instead. Every tier is pinned
# to the same already-tested Claude model (`services/llm_pricing.py`
# already has a pricing entry for it) -- deliberately not introducing any
# new/untested model variant or pricing-table change in this step. Tier
# metadata stays forward-compatible (a later step can point a tier at a
# different model_id without touching any call site that already passes
# `tier=` instead of a literal `model_id=`), just inert for now.
TIER_MODEL_MAP: dict[str, str] = {
    "A": "claude-haiku-4-5-20251001",
    "B": "claude-haiku-4-5-20251001",
    "C": "claude-haiku-4-5-20251001",
}


def _provider_for_model(model_id: str) -> str:
    if model_id.startswith(_ANTHROPIC_MODEL_PREFIXES):
        return "anthropic"
    raise LLMCallError(f"no provider recognized for model_id={model_id!r}")


def _resolve_model_id(model_id: str | None, tier: str | None) -> str:
    """Implements the Step 4 resolution order: an explicit `model_id`
    always wins; otherwise `tier` is looked up in `TIER_MODEL_MAP`. Both
    "no tier given" and "tier given but unrecognized" raise the same
    `LLMCallError` a caller already treats like any other dispatch
    failure (see `call_llm`'s docstring) -- this never falls through to a
    `KeyError` or a `None` model_id reaching the provider dispatch below.
    """
    if model_id:
        return model_id
    if not tier:
        raise LLMCallError("model_id or tier required")
    resolved = TIER_MODEL_MAP.get(tier)
    if not resolved:
        raise LLMCallError("Unrecognized or missing model_id/tier")
    return resolved


def call_llm(
    model_id: str | None = None,
    prompt: str = "",
    *,
    tier: str | None = None,
    shadow: bool = False,
    max_tokens: int,
    temperature: float | None = None,
    timeout: float | None = None,
) -> LLMResponse:
    """Dispatches one LLM call by `model_id` and returns a normalized
    `LLMResponse` (plain text plus `(tokens_in, tokens_out)`).

    `model_id` is now optional (HTA Orchestrator Step 4): every existing
    positional call site (`call_llm(ANTHROPIC_MODEL, prompt, ...)`) is
    unchanged, but a caller may instead omit it and pass `tier=` (`"A"`/
    `"B"`/`"C"`), which resolves to a model_id via `TIER_MODEL_MAP` -- see
    `_resolve_model_id` above for the exact resolution order and its two
    `LLMCallError` cases (missing/unrecognized tier, or neither given).

    `shadow` is a caller-facing, no-op metadata flag -- it never changes
    dispatch, the model_id resolved above, or anything recorded here
    (this function still records no telemetry at all; see this module's
    docstring/HTA Orchestrator Step 4 item 4). It exists purely so a
    caller can write `call_llm(..., shadow=True)` at the call site and
    decide for itself, afterward, whether to call `record_llm_call` or
    `record_shadow_llm_call` -- the flag is never inspected inside this
    function.

    `temperature`/`timeout` are forwarded to the provider only when not
    None -- omitting `temperature` entirely (rather than passing
    `temperature=None`) matches `generate_series_overview`'s existing,
    deliberate "let the SDK default apply" behavior; see this module's
    docstring.

    Raises `LLMCallError` on any failure (missing credentials, network/
    SDK error, unrecognized model_id/tier) -- never returns a partial/
    None response. Callers that need "record telemetry even on failure,
    with zero tokens" (matching today's behavior at all 3 existing call
    sites) should catch `LLMCallError` and record their own zero-token
    entry, the same way they previously did with `response` staying
    `None`.
    """
    del shadow  # no-op metadata only, see docstring above
    resolved_model_id = _resolve_model_id(model_id, tier)
    provider = _provider_for_model(resolved_model_id)
    if provider == "anthropic":
        return _call_anthropic(
            resolved_model_id, prompt, max_tokens=max_tokens, temperature=temperature, timeout=timeout
        )
    raise LLMCallError(f"no dispatch implemented for provider={provider!r} (model_id={resolved_model_id!r})")


def _call_anthropic(
    model_id: str,
    prompt: str,
    *,
    max_tokens: int,
    temperature: float | None,
    timeout: float | None,
) -> LLMResponse:
    # Lazy import, same as every existing call site -- only touched when a
    # key is actually present and this branch actually runs, not at module
    # import time.
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise LLMCallError("ANTHROPIC_API_KEY is not set")

    kwargs: dict = {
        "model": model_id,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if timeout is not None:
        kwargs["timeout"] = timeout

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(**kwargs)
    except Exception as exc:
        raise LLMCallError(str(exc)) from exc

    text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text").strip()
    usage = getattr(response, "usage", None)
    tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    return LLMResponse(text=text, tokens_in=tokens_in, tokens_out=tokens_out, model_id=model_id)
