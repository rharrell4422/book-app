"""Publication-date parsing used by services/series_check_engine.py.

DC-1: this module used to also carry normalize_book_title()/
normalize_book_metadata(), a title-reformatting pipeline superseded by
services/title_normalization.py with zero remaining callers anywhere in
the repo. Removed; parse_publication_date() below is the only piece of
this module that's still live.
"""
from __future__ import annotations

import re
from datetime import date


def parse_publication_date(value: str | None) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw)
        if re.fullmatch(r"\d{4}", raw):
            return date(int(raw), 1, 1)
    except ValueError:
        return None

    return None
