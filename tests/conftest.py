import os
from unittest.mock import patch

import pytest

import settings


@pytest.fixture(autouse=True)
def _no_auth_bypass_during_tests():
    """AUTH_DISABLED (routers/deps.py) is a local-dev-only convenience that
    lets you skip login when running the app against your own machine, set
    via a developer's local .env. That .env is also loaded by
    discovery_engine's load_dotenv() as a side effect of importing `main`,
    which most of this suite does -- without this fixture, a developer who
    turned AUTH_DISABLED on for their own local browsing would silently
    defeat every 401/403 assertion in this suite the next time they ran the
    tests. Popping it for the duration of each test (and restoring whatever
    was there afterward) keeps the two completely independent.
    """
    previous = os.environ.pop("AUTH_DISABLED", None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ["AUTH_DISABLED"] = previous


@pytest.fixture(autouse=True)
def _no_real_anthropic_key_during_tests():
    """HTA Orchestrator Step 5: agents/series_agent.py's classification
    loop can now issue a Tier C shadow `call_llm(...)` directly, from
    inside `run_series_check` itself -- unlike every pre-Step-5 LLM call
    site, which only ever ran inside `discovery_engine.
    discover_candidates_for_series` (comprehensively mocked by every
    integration test in this suite via `patch("discovery_engine.
    discover_candidates_for_series", ...)`). That means a developer's real
    `ANTHROPIC_API_KEY` (loaded from their local `.env` by
    `provider_io.load_dotenv()` as a side effect of importing `main`/
    `discovery_engine`, same mechanism `AUTH_DISABLED` above guards
    against) would otherwise let an "ambiguous candidate" integration test
    anywhere in this suite silently fire a real, billed Anthropic call
    every time it runs locally.

    Same pattern as `_no_auth_bypass_during_tests` above: popped for the
    duration of every test and restored afterward, so it's a total no-op
    in CI (where no such key exists) and only actually changes behavior on
    a developer machine with a real key configured. A test that
    specifically wants to exercise the Tier C shadow LLM call (or any
    other LLM call site) already re-supplies its own fake key via
    `patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"})` plus a
    mocked `anthropic.Anthropic` -- that per-test override still works
    fine layered on top of this fixture.
    """
    previous = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ["ANTHROPIC_API_KEY"] = previous


@pytest.fixture(autouse=True)
def _no_parallel_shadow_fan_out_during_tests():
    """Step 10 Phase 6 (Multi-Provider Tier C, activation): `settings.
    TIER_C_PARALLEL_SHADOW_SAMPLE_RATE` is no longer 0.0 by default in
    production (see that setting's own docstring) -- `services.tier_c_
    orchestrator._should_fan_out` rolls `random.random()` against it on
    every non-"live" Tier C shadow call. Without this fixture, any test
    anywhere in this suite that reaches `run_tier_c_shadow_call` without
    explicitly patching the sample rate itself (nearly all of `tests/
    test_series_discovery.py`'s Tier C shadow coverage, most of `tests/
    test_tier_c_multi_provider.py`'s Phase 3 class) would have a ~1-in-20
    chance per call of silently taking the fan-out branch instead of the
    single-provider one it was written to assert on -- an intermittent,
    hard-to-reproduce flake, not a real bug.

    Pins the rate to `0.0` for the duration of every test by default, same
    pattern as `_no_real_anthropic_key_during_tests` above -- a test that
    specifically wants to exercise fan-out (`Phase4ParallelFanOutTest`,
    `Phase6ConcurrencyAndTimeoutTest`, etc.) already re-patches `settings.
    TIER_C_PARALLEL_SHADOW_SAMPLE_RATE` to a real value itself; that
    per-test override layers fine on top of this one (whichever patch is
    innermost/most-recently-entered wins, same as any other nested
    `unittest.mock.patch`).
    """
    with patch.object(settings, "TIER_C_PARALLEL_SHADOW_SAMPLE_RATE", 0.0):
        yield
