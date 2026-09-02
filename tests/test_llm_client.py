"""HTA Orchestrator Step 1: coverage for llm_client.py's provider-agnostic
dispatch wrapper -- normalized text/token-usage extraction, the
temperature-omission behavior generate_series_overview relies on, and
fail-soft error wrapping.

HTA Orchestrator Step 4 additions: tier -> model_id resolution when
`model_id` is omitted (TIER_MODEL_MAP), its two LLMCallError cases, and
the `shadow` parameter's no-op contract.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

from llm_client import TIER_MODEL_MAP, LLMCallError, call_llm


def _mock_anthropic_client(response_text, *, input_tokens=10, output_tokens=20):
    mock_text_block = MagicMock()
    mock_text_block.type = "text"
    mock_text_block.text = response_text
    mock_usage = MagicMock()
    mock_usage.input_tokens = input_tokens
    mock_usage.output_tokens = output_tokens
    mock_message = MagicMock()
    mock_message.content = [mock_text_block]
    mock_message.usage = mock_usage
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_message
    return mock_client


class CallLlmDispatchTest(unittest.TestCase):
    def test_returns_normalized_text_and_token_counts(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=_mock_anthropic_client("hello world", input_tokens=5, output_tokens=7)
        ):
            result = call_llm("claude-haiku-4-5-20251001", "prompt text", max_tokens=100)

        self.assertEqual(result.text, "hello world")
        self.assertEqual(result.tokens_in, 5)
        self.assertEqual(result.tokens_out, 7)
        self.assertEqual(result.model_id, "claude-haiku-4-5-20251001")

    def test_missing_usage_defaults_token_counts_to_zero(self):
        mock_text_block = MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = "text"
        mock_message = MagicMock()
        mock_message.content = [mock_text_block]
        mock_message.usage = None
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_message

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=mock_client
        ):
            result = call_llm("claude-haiku-4-5-20251001", "prompt", max_tokens=100)

        self.assertEqual(result.tokens_in, 0)
        self.assertEqual(result.tokens_out, 0)

    def test_omits_temperature_kwarg_entirely_when_not_provided(self):
        # generate_series_overview's deliberate asymmetry (SDK default,
        # more-generative behavior) depends on `temperature` never being
        # passed as an explicit None -- see llm_client.py's docstring.
        captured = {}

        def fake_anthropic(api_key):
            client = _mock_anthropic_client("overview text")

            def capture_create(**kwargs):
                captured.update(kwargs)
                return client.messages.create.return_value

            client.messages.create.side_effect = capture_create
            return client

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", side_effect=fake_anthropic
        ):
            call_llm("claude-haiku-4-5-20251001", "prompt", max_tokens=400, timeout=20.0)

        self.assertNotIn("temperature", captured)
        self.assertEqual(captured["timeout"], 20.0)
        self.assertEqual(captured["max_tokens"], 400)

    def test_includes_temperature_kwarg_when_explicitly_provided(self):
        captured = {}

        def fake_anthropic(api_key):
            client = _mock_anthropic_client("structured")

            def capture_create(**kwargs):
                captured.update(kwargs)
                return client.messages.create.return_value

            client.messages.create.side_effect = capture_create
            return client

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", side_effect=fake_anthropic
        ):
            call_llm("claude-haiku-4-5-20251001", "prompt", max_tokens=2000, temperature=0)

        self.assertEqual(captured["temperature"], 0)

    def test_raises_llm_call_error_when_api_key_missing(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            with self.assertRaises(LLMCallError):
                call_llm("claude-haiku-4-5-20251001", "prompt", max_tokens=100)

    def test_wraps_sdk_exception_in_llm_call_error(self):
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("boom")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=mock_client
        ):
            with self.assertRaises(LLMCallError):
                call_llm("claude-haiku-4-5-20251001", "prompt", max_tokens=100)

    def test_unrecognized_model_id_raises_llm_call_error(self):
        with self.assertRaises(LLMCallError):
            call_llm("some-unknown-model", "prompt", max_tokens=100)


class CallLlmTierResolutionTest(unittest.TestCase):
    """HTA Orchestrator Step 4: tier -> model_id resolution when a caller
    omits `model_id` and passes `tier` instead.
    """

    def test_tier_resolves_to_the_mapped_model_id(self):
        for tier in ("A", "B", "C"):
            with self.subTest(tier=tier):
                with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
                    "anthropic.Anthropic", return_value=_mock_anthropic_client("ok")
                ):
                    result = call_llm(tier=tier, prompt="prompt", max_tokens=100)
                self.assertEqual(result.model_id, TIER_MODEL_MAP[tier])

    def test_explicit_model_id_wins_over_tier(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=_mock_anthropic_client("ok")
        ):
            result = call_llm(
                "claude-haiku-4-5-20251001", "prompt", tier="B", max_tokens=100
            )
        self.assertEqual(result.model_id, "claude-haiku-4-5-20251001")

    def test_missing_tier_and_model_id_raises_llm_call_error(self):
        with self.assertRaises(LLMCallError):
            call_llm(prompt="prompt", max_tokens=100)

    def test_unrecognized_tier_raises_llm_call_error(self):
        with self.assertRaises(LLMCallError):
            call_llm(tier="Z", prompt="prompt", max_tokens=100)

    def test_shadow_flag_is_a_no_op_and_does_not_alter_dispatch(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=_mock_anthropic_client("ok")
        ):
            result = call_llm(tier="A", prompt="prompt", max_tokens=100, shadow=True)
        self.assertEqual(result.model_id, TIER_MODEL_MAP["A"])
        self.assertEqual(result.text, "ok")


if __name__ == "__main__":
    unittest.main()
