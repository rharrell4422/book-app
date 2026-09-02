"""HTA Orchestrator Step 5: coverage for prompts.py's three tier-specific
prompt builders -- Tier A/build_extraction_prompt and Tier B/
build_reconciliation_prompt (thin, behavior-preserving wrappers around the
inline `.format()` calls they replaced in provider_io.py) and Tier C/
build_belongs_to_series_prompt (the genuinely new shadow-only prompt).
"""
import unittest

from prompts import (
    _LLM_RECONCILIATION_PROMPT,
    _WEB_SEARCH_STRUCTURING_PROMPT,
    build_belongs_to_series_prompt,
    build_extraction_prompt,
    build_reconciliation_prompt,
)


class BuildExtractionPromptTest(unittest.TestCase):
    def test_matches_the_original_inline_format_call_exactly(self):
        # Zero behavior change from the pre-Step-5 inline `.format()` call
        # this replaces -- same template, same substitution.
        expected = _WEB_SEARCH_STRUCTURING_PROMPT.format(
            scope_line='Target series: "Safehold"',
            author="David Weber",
            count=3,
            snippets="[0] Title: A\nSnippet: B\nURL: C",
            skip_other_series=" unrelated books by other authors, other series by the same author,",
            title_scope="this series by this author",
        )
        actual = build_extraction_prompt(
            scope_line='Target series: "Safehold"',
            author="David Weber",
            count=3,
            snippets="[0] Title: A\nSnippet: B\nURL: C",
            skip_other_series=" unrelated books by other authors, other series by the same author,",
            title_scope="this series by this author",
        )
        self.assertEqual(actual, expected)

    def test_author_wide_scope_line_is_interpolated(self):
        prompt = build_extraction_prompt(
            scope_line="Target: ANY book by this author, across all of their series and standalone works.",
            author="Harmon Cooper",
            count=1,
            snippets="[0] Title: X\nSnippet: Y\nURL: Z",
            skip_other_series=" unrelated books by other authors,",
            title_scope="this author",
        )
        self.assertIn("Harmon Cooper", prompt)
        self.assertIn("ANY book by this author", prompt)


class BuildReconciliationPromptTest(unittest.TestCase):
    def test_matches_the_original_inline_format_call_exactly(self):
        expected = _LLM_RECONCILIATION_PROMPT.format(
            series_name="Safehold",
            count=2,
            candidate_listing="[0] Title: A\n[1] Title: B",
            max_index=1,
        )
        actual = build_reconciliation_prompt(
            series_name="Safehold",
            count=2,
            candidate_listing="[0] Title: A\n[1] Title: B",
            max_index=1,
        )
        self.assertEqual(actual, expected)

    def test_missing_series_name_falls_back_to_unknown(self):
        prompt = build_reconciliation_prompt(
            series_name=None,
            count=1,
            candidate_listing="[0] Title: A",
            max_index=0,
        )
        self.assertIn('Series: "unknown"', prompt)


class BuildBelongsToSeriesPromptTest(unittest.TestCase):
    def test_includes_core_candidate_fields(self):
        prompt = build_belongs_to_series_prompt(
            title="Off Armageddon Reef",
            series_name="Safehold",
            inferred_number=1,
            provider_metadata=[{"source": "hardcover", "title": "Off Armageddon Reef", "series_number_hint": 1}],
            known_series_titles={"safehold book two"},
            owned_core_title_texts={"off armageddon reef"},
            highest_owned_book_number=5,
            candidate_confidence="targeted",
            reason_flags={"explicit_series_match": True, "is_universe_tie_in": False},
            description="A world lit only by fire...",
            sibling_candidates=[{"title": "By Schism Rent Asunder", "number": 2}],
        )
        self.assertIn("Off Armageddon Reef", prompt)
        self.assertIn("Safehold", prompt)
        self.assertIn("hardcover", prompt)
        self.assertIn("By Schism Rent Asunder", prompt)
        self.assertIn("A world lit only by fire", prompt)
        self.assertIn("explicit_series_match=True", prompt)
        self.assertIn("shadow mode", prompt)
        self.assertIn("belongs_to_series", prompt)

    def test_degrades_gracefully_with_no_optional_context(self):
        # Every optional input can legitimately be missing (no description,
        # no provider metadata beyond the one hit, no siblings in a
        # single-candidate batch, no known/owned titles yet) -- this must
        # never raise, and should say so in the prompt rather than
        # rendering an empty/broken section.
        prompt = build_belongs_to_series_prompt(
            title="Untitled Sequel",
            series_name="Some Series",
            inferred_number=None,
            provider_metadata=None,
            known_series_titles=None,
            owned_core_title_texts=None,
            highest_owned_book_number=None,
            candidate_confidence=None,
            reason_flags=None,
            description=None,
            sibling_candidates=None,
        )
        self.assertIn("no description available", prompt)
        self.assertIn("no additional provider metadata available", prompt)
        self.assertIn("no other candidates in this batch", prompt)
        self.assertIn("none known", prompt)

    def test_response_shape_is_specified_as_json_only(self):
        prompt = build_belongs_to_series_prompt(title="X", series_name="Y", inferred_number=None)
        self.assertIn('"belongs_to_series"', prompt)
        self.assertIn('"confidence"', prompt)
        self.assertIn('"inferred_number"', prompt)
        self.assertIn("ONLY a JSON object", prompt)


if __name__ == "__main__":
    unittest.main()
