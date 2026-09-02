"""Provider-agnostic LLM client wrapper (HTA Orchestrator Step 1; Groq
added as a second provider in Step 7).

Originally every LLM call in this codebase went through a single
Anthropic SDK call, duplicated near-identically across provider_io.py's
three call sites (`_structure_web_results_with_llm`, `generate_series_
overview`, `_reconcile_candidates_with_llm`): construct a fresh
`anthropic.Anthropic` client, call `.messages.create(...)`, join the
response's text blocks, and read `usage.input_tokens`/`usage.
output_tokens`. This module centralizes that dispatch. Step 7 adds Groq
(`_call_groq`, OpenAI-compatible SDK shape) alongside Anthropic
(`_call_anthropic`) -- no `TIER_MODEL_MAP` entry points at Groq yet, so
its dispatch path exists and is tested but nothing routes to it in
production today.

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


# HTA Orchestrator Step 7: tier -> (provider, model_id) resolution, used
# only when a caller omits `model_id` and passes `tier` instead. Each tier
# entry now names its provider explicitly rather than leaving it to be
# inferred from the model_id string (see `_resolve_dispatch` below) --
# every tier is still pinned to the same already-tested Claude model
# today (`services/llm_pricing.py` already has a pricing entry for it),
# deliberately not routing any tier at Groq yet. Tier metadata stays
# forward-compatible (a later step can point a tier at a different
# provider/model_id pair without touching any call site that already
# passes `tier=` instead of a literal `model_id=`/`provider=`), just
# inert for now.
TIER_MODEL_MAP: dict[str, dict[str, str]] = {
    "A": {"provider": "anthropic", "model_id": "claude-haiku-4-5-20251001"},
    "B": {"provider": "anthropic", "model_id": "claude-haiku-4-5-20251001"},
    "C": {"provider": "anthropic", "model_id": "claude-haiku-4-5-20251001"},
}

# HTA Orchestrator Step 7: static per-provider capability metadata (context
# window, JSON-mode support) -- deliberately a plain dict next to the
# dispatch functions below rather than a provider class hierarchy; see
# this module's docstring for why the dispatch layer stays thin. Not read
# by anything yet (no caller branches on context length or JSON mode
# today); exists so a future tier-selection or prompt-sizing decision has
# a single place to look this up rather than hardcoding it again.
PROVIDER_METADATA: dict[str, dict] = {
    "anthropic": {"context_length": 200_000, "supports_json_mode": False},
    "groq": {"context_length": 128_000, "supports_json_mode": True},
}


def _resolve_dispatch(
    model_id: str | None, tier: str | None, provider: str | None
) -> tuple[str, str]:
    """HTA Orchestrator Step 7: resolves `(provider, model_id)` for one
    call. Provider is now always explicit -- either supplied directly by
    the caller (required whenever `model_id` is also explicit) or looked
    up alongside `model_id` in `TIER_MODEL_MAP[tier]`. There is no longer
    any prefix/pattern guess from the model_id string (the pre-Step-7
    `_provider_for_model` this replaces): with two providers whose model
    families don't share a naming convention (Groq hosts many unrelated
    model families), guessing stopped being reliable.

    Precedence (matches the pre-Step-7 "explicit model_id always wins
    over tier" contract, extended to cover provider):

    - `model_id` given -> `provider` MUST also be given (raises
      `LLMCallError` otherwise); `tier` is ignored entirely, even for
      validation, exactly as it already was pre-Step-7.
    - `model_id` omitted, `tier` given -> both `provider`/`model_id` come
      from `TIER_MODEL_MAP[tier]`. If the caller also passed an explicit
      `provider`, it's validated against the tier's own provider (never
      used for dispatch) -- a mismatch raises `LLMCallError` rather than
      silently routing to whichever one happens to win, so a typo'd
      `provider=` can never produce inconsistent routing.
    - Neither given -> `LLMCallError`, same as before.
    """
    if model_id:
        if not provider:
            raise LLMCallError("provider is required when model_id is given explicitly")
        return provider, model_id
    if not tier:
        raise LLMCallError("model_id or tier required")
    tier_entry = TIER_MODEL_MAP.get(tier)
    if not tier_entry:
        raise LLMCallError("Unrecognized or missing model_id/tier")
    tier_provider = tier_entry["provider"]
    if provider and provider != tier_provider:
        raise LLMCallError(
            f"provider={provider!r} does not match tier {tier!r}'s provider={tier_provider!r}"
        )
    return tier_provider, tier_entry["model_id"]


def call_llm(
    model_id: str | None = None,
    prompt: str = "",
    *,
    tier: str | None = None,
    provider: str | None = None,
    shadow: bool = False,
    max_tokens: int,
    temperature: float | None = None,
    timeout: float | None = None,
) -> LLMResponse:
    """Dispatches one LLM call and returns a normalized `LLMResponse`
    (plain text plus `(tokens_in, tokens_out)`).

    `model_id` is optional (HTA Orchestrator Step 4): a caller may instead
    omit it and pass `tier=` (`"A"`/`"B"`/`"C"`), which resolves both
    `model_id` and `provider` via `TIER_MODEL_MAP`.

    `provider` (HTA Orchestrator Step 7) is required whenever `model_id`
    is given explicitly -- there is no longer any inference from the
    model_id string, since Groq's model families don't share a naming
    convention the way "claude-*" did for Anthropic alone. When `tier` is
    given instead, `provider` is normally omitted (it's resolved from the
    tier); if a caller passes both, `provider` is validated against the
    tier's own provider and a mismatch raises `LLMCallError` rather than
    silently picking one. See `_resolve_dispatch`'s docstring for the
    full precedence table.

    `shadow` is a caller-facing, no-op metadata flag -- it never changes
    dispatch, the model_id/provider resolved above, or anything recorded
    here (this function still records no telemetry at all; see this
    module's docstring/HTA Orchestrator Step 4 item 4). It exists purely
    so a caller can write `call_llm(..., shadow=True)` at the call site
    and decide for itself, afterward, whether to call `record_llm_call`
    or `record_shadow_llm_call` -- the flag is never inspected inside
    this function.

    `temperature`/`timeout` are forwarded to the provider only when not
    None -- omitting `temperature` entirely (rather than passing
    `temperature=None`) matches `generate_series_overview`'s existing,
    deliberate "let the SDK default apply" behavior; see this module's
    docstring.

    Raises `LLMCallError` on any failure (missing credentials, network/
    SDK error, unrecognized model_id/tier, missing/mismatched provider)
    -- never returns a partial/None response. Callers that need "record
    telemetry even on failure, with zero tokens" (matching today's
    behavior at every existing call site) should catch `LLMCallError` and
    record their own zero-token entry, the same way they previously did
    with `response` staying `None`.
    """
    del shadow  # no-op metadata only, see docstring above
    resolved_provider, resolved_model_id = _resolve_dispatch(model_id, tier, provider)
    if resolved_provider == "anthropic":
        return _call_anthropic(
            resolved_model_id, prompt, max_tokens=max_tokens, temperature=temperature, timeout=timeout
        )
    if resolved_provider == "groq":
        return _call_groq(
            resolved_model_id, prompt, max_tokens=max_tokens, temperature=temperature, timeout=timeout
        )
    raise LLMCallError(
        f"no dispatch implemented for provider={resolved_provider!r} (model_id={resolved_model_id!r})"
    )


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


def _call_groq(
    model_id: str,
    prompt: str,
    *,
    max_tokens: int,
    temperature: float | None,
    timeout: float | None,
) -> LLMResponse:
    """HTA Orchestrator Step 7: Groq's Python SDK is OpenAI-compatible
    (`chat.completions.create`, `usage.prompt_tokens`/`completion_tokens`)
    rather than Anthropic-shaped (`messages.create`, `usage.input_tokens`/
    `output_tokens`) -- this function normalizes that difference the same
    way `_call_anthropic` normalizes Anthropic's shape, so `call_llm`'s
    caller-facing `LLMResponse` stays provider-independent either way.
    No `TIER_MODEL_MAP` entry points at Groq yet (see that map's own
    comment) -- this dispatch path exists so it's wired and testable
    ahead of that, not because anything routes to it in production today.
    """
    # Lazy import, same convention as `_call_anthropic` above -- only
    # touched when a key is actually present and this branch actually runs.
    import groq

    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        raise LLMCallError("GROQ_API_KEY is not set")

    kwargs: dict = {
        "model": model_id,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if timeout is not None:
        kwargs["timeout"] = timeout

    client = groq.Groq(api_key=api_key)
    try:
        response = client.chat.completions.create(**kwargs)
    except Exception as exc:
        raise LLMCallError(str(exc)) from exc

    choices = getattr(response, "choices", None) or []
    text = ""
    if choices:
        message = getattr(choices[0], "message", None)
        text = (getattr(message, "content", None) or "").strip()
    usage = getattr(response, "usage", None)
    tokens_in = int(getattr(usage, "prompt_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "completion_tokens", 0) or 0)
    return LLMResponse(text=text, tokens_in=tokens_in, tokens_out=tokens_out, model_id=model_id)
