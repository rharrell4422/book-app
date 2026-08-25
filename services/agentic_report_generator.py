"""Phase 1 agentic discovery, ninth implementation block: a pure report
generator consolidating every shadow-mode diagnostic
`services/agentic_evaluation_harness.run_agentic_evaluation_for_series`
already produces into one normalized structure -- and, separately, a
pure string-generation HTML-style rendering of that same structure.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(settled architecture, not re-litigated here): both functions here take
an already-built `evaluation` dict and return a new dict/string --
neither touches a database, a network call, or the filesystem. This is
strictly a presentation/normalization layer on top of diagnostics Phase
1's earlier blocks (RT-1b/PB-5/`agents/agentic_series_agent.py`/`services/
agentic_evaluation_harness.py`/`services/agentic_drift_detector.py`/
`services/agentic_ttl_validator.py`) already computed -- nothing here can
be "shadow mode only" in a meaningful sense because it never has write
access to be shadow *from* in the first place.

`generate_agentic_html_report`'s output is plain, hand-built HTML markup
(`<h2>`/`<div>`/`<h3>`/`<pre>` only, no CSS/JS) with every dynamic value
passed through `html.escape` -- safe to embed in a log line or an
admin-only page without risk of the escaped content being interpreted as
markup (a series/book title containing `<`/`&`/etc. must never break out
of its `<pre>` block).
"""

from __future__ import annotations

import html
import json

_REQUIRED_LIVE_SECTIONS = ("skeleton", "confidence", "gate")
_REQUIRED_AGENTIC_SECTIONS = (
    "provider_calls",
    "probes",
    "confidence_traces",
    "gate_traces",
    "skeleton_merge_previews",
    "reasoning_steps",
)


def generate_agentic_report(evaluation: dict) -> dict:
    """Consolidates `run_agentic_evaluation_for_series`'s full evaluation
    dict into one normalized structure:

        {
          "series_id": ...,
          "timestamp": ...,
          "live": {"skeleton": ..., "confidence": ..., "gate": ...},
          "agentic": {
            "provider_calls": [...], "probes": [...],
            "confidence_traces": [...], "gate_traces": [...],
            "skeleton_merge_previews": [...], "reasoning_steps": [...],
          },
          "comparison": ...,
          "drift_report": ...,
          "ttl_report": ...,
        }

    Every section above is always present, defaulting to an empty
    dict/list when the corresponding key is missing/malformed on
    `evaluation` (e.g. a partial or hand-built input in a test) -- this
    function never raises on a merely-incomplete input; a completely
    non-dict `evaluation` still returns the same fully-shaped, all-empty
    structure.

    Purely diagnostic -- a normalization/merge step only, reading nothing
    back into any routing/confidence/gate/merge decision.
    """
    evaluation = evaluation if isinstance(evaluation, dict) else {}
    live_observation = evaluation.get("live_observation")
    live_observation = live_observation if isinstance(live_observation, dict) else {}
    agentic_trace = evaluation.get("agentic_trace")
    agentic_trace = agentic_trace if isinstance(agentic_trace, dict) else {}
    comparison = evaluation.get("comparison")
    drift_report = evaluation.get("drift_report")
    ttl_report = evaluation.get("ttl_report")

    return {
        "series_id": evaluation.get("series_id"),
        "timestamp": evaluation.get("timestamp"),
        "live": {
            "skeleton": live_observation.get("skeleton_snapshot") or {},
            "confidence": live_observation.get("confidence_snapshot") or {},
            "gate": live_observation.get("gate_snapshot") or {},
        },
        "agentic": {
            "provider_calls": agentic_trace.get("provider_calls") or [],
            "probes": agentic_trace.get("probes") or [],
            "confidence_traces": agentic_trace.get("confidence_traces") or [],
            "gate_traces": agentic_trace.get("gate_traces") or [],
            "skeleton_merge_previews": agentic_trace.get("skeleton_merge_previews") or [],
            "reasoning_steps": agentic_trace.get("reasoning_steps") or [],
        },
        "comparison": comparison if isinstance(comparison, dict) else {"by_book_number": {}},
        "drift_report": drift_report if isinstance(drift_report, dict) else {},
        "ttl_report": ttl_report if isinstance(ttl_report, dict) else {},
    }


def _pre_block(title: str, data) -> str:
    """One `<div><h3>...</h3><pre>...</pre></div>` section -- `data` is
    JSON-serialized (sorted keys, for stable/diffable output) and the
    resulting text is `html.escape`d before being placed inside `<pre>`,
    so any `<`/`>`/`&` in a title/URL/etc. renders as literal text
    instead of markup.
    """
    json_text = html.escape(json.dumps(data, indent=2, default=str, sort_keys=True))
    return f"<div>\n  <h3>{html.escape(str(title))}</h3>\n  <pre>{json_text}</pre>\n</div>"


def generate_agentic_html_report(evaluation: dict) -> str:
    """Human-readable HTML-style rendering of `evaluation` (the same
    full evaluation dict `generate_agentic_report` consumes -- this
    function calls that one internally rather than re-deriving the same
    normalized sections a second way).

    Pure string generation: no file writes, no `<script>`/`<style>`,
    every dynamic value HTML-escaped (see `_pre_block`). Safe to embed in
    a log line or an admin-only page.
    """
    consolidated = generate_agentic_report(evaluation)

    sections = [
        _pre_block("Live Snapshot", consolidated["live"]),
        _pre_block("Agentic Trace", consolidated["agentic"]),
        _pre_block("Comparison", consolidated["comparison"]),
        _pre_block("Drift Report", consolidated["drift_report"]),
        _pre_block("TTL Report", consolidated["ttl_report"]),
    ]
    header = f"<h2>Series ID: {html.escape(str(consolidated['series_id']))}</h2>"
    return header + "\n" + "\n".join(sections)
