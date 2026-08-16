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
