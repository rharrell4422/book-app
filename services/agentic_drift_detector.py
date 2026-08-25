"""Phase 1 agentic discovery, seventh implementation block: a pure,
side-effect-free drift detector comparing the live pipeline's current
skeleton snapshot against `agents/agentic_series_agent.py`'s shadow-mode
skeleton-merge preview.

Per `discovery_agentic_phase1_plan.md`/`discovery_agentic_phase1_evaluation.md`
(settled architecture, not re-litigated here): `detect_skeleton_drift`
takes two already-built dicts and returns a third -- no DB access, no
network calls, no writes, nothing here can be anything but diagnostic.
Not wired into any user-facing route or scheduled job (see
`services/agentic_evaluation_harness.py`'s integration of this function
for the one place it's actually called from today).

Both inputs are expected in the same "book_number key -> entry dict"
shape `services.agentic_evaluation_harness._observe_live_pipeline`'s
`skeleton_snapshot` already uses (`live_skeleton`), and the shadow loop's
skeleton-merge preview reshaped the same way (`agentic_preview` -- see
`_preview_entries_by_number` below, which the evaluation harness uses to
turn `agentic_trace["skeleton_merge_previews"]`'s list-of-one-preview
shape, from `agents/agentic_series_agent.py`, into that same book_number-
keyed dict before calling this function). Keeping `detect_skeleton_drift`
itself agnostic to that list-vs-dict wrapping detail keeps it a small,
easily-tested pure function.
"""

from __future__ import annotations


def _sort_key_for_book_number(key: str):
    try:
        return (0, float(key))
    except (TypeError, ValueError):
        return (1, key)


def _preview_entries_by_number(skeleton_merge_previews: list) -> dict:
    """Reshapes `agents/agentic_series_agent.py`'s `skeleton_merge_
    previews` trace section (a list containing one preview dict with
    "before"/"after" entry lists -- see that module's docstring) into the
    `{"<book_number>": entry, ...}` shape `detect_skeleton_drift` compares
    against `live_skeleton`. Uses the preview's "after" entries -- what
    the shadow loop would merge in, the thing worth diffing against the
    live skeleton's current state.
    """
    by_number: dict = {}
    if not isinstance(skeleton_merge_previews, list) or not skeleton_merge_previews:
        return by_number
    first_preview = skeleton_merge_previews[0]
    if not isinstance(first_preview, dict):
        return by_number
    for entry in first_preview.get("after") or []:
        if isinstance(entry, dict) and entry.get("book_number") is not None:
            by_number[str(entry["book_number"])] = entry
    return by_number


# Fields compared under "metadata_changed" -- deliberately excludes
# title/confidence, which get their own dedicated drift flags below.
_METADATA_FIELDS = ("release_date", "edition_hints", "status")


def detect_skeleton_drift(live_skeleton: dict, agentic_preview: dict) -> dict:
    """Compares live skeleton entries vs. agentic preview entries, keyed
    by `book_number` (as a string, matching both inputs' own keying).

    `author` is compared for structural completeness/future-proofing --
    current skeleton entries (see `models.SeriesSkeleton`'s docstring)
    don't carry an `author` field at all today, so `author_changed` is
    `False` in practice until a future entry shape adds one; this
    function doesn't assume either input has it.

    Returns:
        {
          "by_book_number": {
            "<book_number>": {
              "live": <entry or None>,
              "preview": <entry or None>,
              "drift": {
                "title_changed": bool,
                "author_changed": bool,
                "metadata_changed": bool,
                "confidence_changed": bool,
              },
            },
            ...
          },
          "missing_in_live": [<book_number str>, ...],
          "missing_in_preview": [<book_number str>, ...],
          "summary": {
            "count_changed": int,
            "count_missing_in_live": int,
            "count_missing_in_preview": int,
          },
        }

    Purely diagnostic -- nothing here writes anything or is read back by
    any routing/confidence/gate/merge logic.
    """
    live_skeleton = live_skeleton if isinstance(live_skeleton, dict) else {}
    agentic_preview = agentic_preview if isinstance(agentic_preview, dict) else {}

    all_keys = set(live_skeleton) | set(agentic_preview)

    by_book_number: dict = {}
    missing_in_live: list = []
    missing_in_preview: list = []
    count_changed = 0

    for key in sorted(all_keys, key=_sort_key_for_book_number):
        live_entry = live_skeleton.get(key)
        preview_entry = agentic_preview.get(key)
        live_dict = live_entry if isinstance(live_entry, dict) else {}
        preview_dict = preview_entry if isinstance(preview_entry, dict) else {}

        if live_entry is None:
            missing_in_live.append(key)
        if preview_entry is None:
            missing_in_preview.append(key)

        drift = {
            "title_changed": live_dict.get("title") != preview_dict.get("title"),
            "author_changed": live_dict.get("author") != preview_dict.get("author"),
            "metadata_changed": any(live_dict.get(field) != preview_dict.get(field) for field in _METADATA_FIELDS),
            "confidence_changed": live_dict.get("confidence") != preview_dict.get("confidence"),
        }
        if any(drift.values()):
            count_changed += 1

        by_book_number[key] = {
            "live": live_entry,
            "preview": preview_entry,
            "drift": drift,
        }

    return {
        "by_book_number": by_book_number,
        "missing_in_live": missing_in_live,
        "missing_in_preview": missing_in_preview,
        "summary": {
            "count_changed": count_changed,
            "count_missing_in_live": len(missing_in_live),
            "count_missing_in_preview": len(missing_in_preview),
        },
    }
