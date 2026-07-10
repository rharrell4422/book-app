from __future__ import annotations

import checker_core as _checker_core
from checker_core import *
from checker_providers import *
from checker_rules import *


def check_for_new_book(series, progress_callback=None, seed_asins=None):
    return _checker_core.check_for_new_book(
        series=series,
        progress_callback=progress_callback,
        seed_asins=seed_asins,
    )
