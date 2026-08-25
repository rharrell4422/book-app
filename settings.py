"""Phase 3 kickoff (`discovery_agentic_phase1_plan.md`/`discovery_agentic_
phase1_evaluation.md`'s settled architecture, not re-litigated here): the
project's first centralized feature-flag module. Every other env-driven
toggle in this codebase (`AUTH_DISABLED`, `HARDCOVER_API_KEY`, etc.) is
read ad hoc, module-level, wherever it's needed (see `routers/deps.py`,
`provider_io.py`) -- this module exists specifically for
`AGENTIC_ROUTING_ENABLED`, the first flag that gates a *behavior*
(candidate promotion in `agents/series_agent.py`'s live routing path)
rather than just an integration's availability, and is deliberately kept
to that one flag.

Read as a module attribute (`settings.AGENTIC_ROUTING_ENABLED`), not
imported by value (`from settings import AGENTIC_ROUTING_ENABLED`), by
every caller -- the value is computed once at import time from the
environment, same as every other flag in this codebase, but tests need
to flip it without touching `os.environ`/process restarts, which only
works if callers re-read the module attribute each time rather than
capturing a stale copy at their own import time.

Defaults to `False`: this is a feature-flagged, gradual promotion
mechanism (`services/agentic_promotion_evaluator.py`), not a default-on
behavior change. With the flag unset/off, `agents/series_agent.py`'s
live routing path is byte-for-byte identical to before this flag
existed.
"""

import os

AGENTIC_ROUTING_ENABLED = bool(os.getenv("AGENTIC_ROUTING_ENABLED", "false").lower() == "true")
