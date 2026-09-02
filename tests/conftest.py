import os

import pytest


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
