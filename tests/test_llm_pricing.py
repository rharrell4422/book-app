"""HTA Orchestrator Step 2: coverage for services/llm_pricing.py's
fail-soft lookup contract -- see that module's docstring.
"""
import unittest

from services.llm_pricing import get_price_per_million


class GetPricePerMillionTest(unittest.TestCase):
    def test_known_model_returns_input_output_tuple(self):
        self.assertEqual(get_price_per_million("claude-haiku-4-5-20251001"), (1.00, 5.00))

    def test_unknown_model_returns_none_not_a_raise(self):
        self.assertIsNone(get_price_per_million("some-unrecognized-model"))

    def test_falsy_model_id_returns_none(self):
        self.assertIsNone(get_price_per_million(None))
        self.assertIsNone(get_price_per_million(""))


if __name__ == "__main__":
    unittest.main()
