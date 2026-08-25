"""Phase 8 (`discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_
evaluation.md`, not re-litigated here): the agentic performance &
efficiency layer.

`AgenticTurnCache` is a plain, per-turn, in-memory memoization cache --
nothing here is persisted, nothing here calls a provider, and nothing
here calls the database. It exists purely so a caller that might
otherwise recompute the same (already-pure) result for the same
`book_number` more than once within a single turn/request can share one
cache instance and pay for that computation exactly once.

Entirely opt-in: every Phase 8 caller (`services/agentic_promotion_
evaluator.evaluate_promotion`/`build_activation_preview`, `services/
agentic_resolution.resolve_routing_decisions`) accepts an optional
`cache` parameter defaulting to `None`; omitting it reproduces the exact
pre-Phase-8 behavior (fresh computation every call), so nothing here can
change any existing caller's *result*, only how many times that result
gets (re)computed.

Important: each of those three call sites uses its own, independently-
scoped cache namespace conceptually (an `evaluate_promotion` cache entry
is the outcome *string* for a book; a `resolve_routing_decisions` cache
entry is that book's full decision *dict*; a `build_activation_preview`
cache entry is a constructed preview *dict*) -- they are NOT meant to
share one `AgenticTurnCache` instance across two different functions'
calls, since the cached *value shapes* differ. Give each function that
needs caching its own fresh `AgenticTurnCache()` per turn; do not pass
one instance into more than one of those three functions.
"""

from __future__ import annotations


class AgenticTurnCache:
    """Per-turn cache for agentic decisions, keyed by `book_number`.
    Three independent namespaces (`promotion`/`confidence`/`gate`),
    matching the three kinds of per-book agentic decision this codebase
    computes -- a caller that only cares about one of the three simply
    never touches the other two dicts.

    Not thread-safe (matches every other agentic module's assumption
    that one turn == one call stack -- see e.g. `agents/agentic_series_
    agent.run_agentic_turn`'s own docstring). Not persisted anywhere --
    create one per turn/request and let it go out of scope when that
    turn is done; reusing an instance across turns would silently serve
    stale decisions from a prior turn's DB state.
    """

    def __init__(self) -> None:
        self.promotion: dict = {}
        self.confidence: dict = {}
        self.gate: dict = {}

    def get_or_set_promotion(self, book_number, compute_fn):
        """Returns the cached value for `book_number` in the `promotion`
        namespace, computing and caching it via `compute_fn()` (a
        zero-argument callable) on the first request for that
        `book_number` only.
        """
        return self._get_or_set(self.promotion, book_number, compute_fn)

    def get_or_set_confidence(self, book_number, compute_fn):
        """Same pattern as `get_or_set_promotion`, for the `confidence`
        namespace.
        """
        return self._get_or_set(self.confidence, book_number, compute_fn)

    def get_or_set_gate(self, book_number, compute_fn):
        """Same pattern as `get_or_set_promotion`, for the `gate`
        namespace.
        """
        return self._get_or_set(self.gate, book_number, compute_fn)

    @staticmethod
    def _get_or_set(store: dict, book_number, compute_fn):
        if book_number in store:
            return store[book_number]
        value = compute_fn()
        store[book_number] = value
        return value
