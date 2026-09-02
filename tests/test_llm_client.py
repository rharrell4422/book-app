"""HTA Orchestrator Step 1: coverage for llm_client.py's provider-agnostic
dispatch wrapper -- normalized text/token-usage extraction, the
temperature-omission behavior generate_series_overview relies on, and
fail-soft error wrapping.

HTA Orchestrator Step 4 additions: tier -> model_id resolution when
`model_id` is omitted (TIER_MODEL_MAP), its two LLMCallError cases, and
the `shadow` parameter's no-op contract.

HTA Orchestrator Step 7 additions: `provider` becomes an explicit,
required part of dispatch whenever `model_id` is given directly (no more
prefix-based guessing -- see llm_client._resolve_dispatch's docstring for
the full precedence table), `TIER_MODEL_MAP` entries are now
`{"provider": ..., "model_id": ...}` dicts instead of bare model_id
strings, and Groq is wired up as a second provider (`_call_groq`) even
though no tier points at it yet.

Step 10 Phase 2 additions (Multi-Provider Tier C): OpenAI wired up as a
third provider (`_call_openai`), same "wired ahead of use" status as
Groq's own Step 7 addition, plus the `response_format` parameter on
`call_llm` (translates `"json"` into Groq's/OpenAI's native
`{"type": "json_object"}` chat-completions kwarg; a no-op for Anthropic).
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from llm_client import PROVIDER_METADATA, TIER_MODEL_MAP, LLMCallError, call_llm


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


def _mock_groq_client(response_text, *, prompt_tokens=10, completion_tokens=20):
    # Groq's SDK is OpenAI-shaped (`choices[0].message.content`,
    # `usage.prompt_tokens`/`completion_tokens`) rather than Anthropic-
    # shaped -- see llm_client._call_groq's docstring for why this needs
    # its own mock shape instead of reusing `_mock_anthropic_client`.
    mock_message = MagicMock()
    mock_message.content = response_text
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_usage = MagicMock()
    mock_usage.prompt_tokens = prompt_tokens
    mock_usage.completion_tokens = completion_tokens
    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    mock_completion.usage = mock_usage
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_completion
    return mock_client


def _mock_openai_client(response_text, *, prompt_tokens=10, completion_tokens=20):
    # OpenAI's own SDK is the shape Groq's SDK deliberately mirrors --
    # see llm_client._call_openai's docstring -- so this is identical to
    # _mock_groq_client above, just constructed independently since each
    # dispatch test class patches a different real module path
    # ("openai.OpenAI" vs "groq.Groq").
    mock_message = MagicMock()
    mock_message.content = response_text
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    mock_usage = MagicMock()
    mock_usage.prompt_tokens = prompt_tokens
    mock_usage.completion_tokens = completion_tokens
    mock_completion = MagicMock()
    mock_completion.choices = [mock_choice]
    mock_completion.usage = mock_usage
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_completion
    return mock_client


class CallLlmDispatchTest(unittest.TestCase):
    def test_returns_normalized_text_and_token_counts(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=_mock_anthropic_client("hello world", input_tokens=5, output_tokens=7)
        ):
            result = call_llm(
                "claude-haiku-4-5-20251001", "prompt text", provider="anthropic", max_tokens=100
            )

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
            result = call_llm("claude-haiku-4-5-20251001", "prompt", provider="anthropic", max_tokens=100)

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
            call_llm(
                "claude-haiku-4-5-20251001", "prompt", provider="anthropic", max_tokens=400, timeout=20.0
            )

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
            call_llm(
                "claude-haiku-4-5-20251001", "prompt", provider="anthropic", max_tokens=2000, temperature=0
            )

        self.assertEqual(captured["temperature"], 0)

    def test_raises_llm_call_error_when_api_key_missing(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            with self.assertRaises(LLMCallError):
                call_llm("claude-haiku-4-5-20251001", "prompt", provider="anthropic", max_tokens=100)

    def test_wraps_sdk_exception_in_llm_call_error(self):
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("boom")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=mock_client
        ):
            with self.assertRaises(LLMCallError):
                call_llm("claude-haiku-4-5-20251001", "prompt", provider="anthropic", max_tokens=100)

    def test_unrecognized_model_id_raises_llm_call_error(self):
        # Deliberately no `provider=` here -- this test is about dispatch
        # failing closed on a bad model_id, and (HTA Orchestrator Step 7)
        # omitting `provider` alongside a bare `model_id` now fails first,
        # before the SDK is ever touched, for a related but distinct
        # reason ("provider is required", not "no provider recognized for
        # model_id"). Still LLMCallError either way -- see
        # CallLlmProviderRequiredTest below for that rule's own dedicated
        # coverage.
        with self.assertRaises(LLMCallError):
            call_llm("some-unknown-model", "prompt", max_tokens=100)


class CallLlmTierResolutionTest(unittest.TestCase):
    """HTA Orchestrator Step 4: tier -> model_id resolution when a caller
    omits `model_id` and passes `tier` instead. HTA Orchestrator Step 7:
    `TIER_MODEL_MAP[tier]` is now a `{"provider": ..., "model_id": ...}`
    dict rather than a bare model_id string.
    """

    def test_tier_resolves_to_the_mapped_provider_and_model_id(self):
        for tier in ("A", "B", "C"):
            with self.subTest(tier=tier):
                with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
                    "anthropic.Anthropic", return_value=_mock_anthropic_client("ok")
                ):
                    result = call_llm(tier=tier, prompt="prompt", max_tokens=100)
                self.assertEqual(result.model_id, TIER_MODEL_MAP[tier]["model_id"])

    def test_explicit_model_id_wins_over_tier(self):
        # HTA Orchestrator Step 7: `provider` must now accompany the
        # explicit `model_id` even though `tier` is also passed -- `tier`
        # stays fully irrelevant to dispatch in this combination, exactly
        # as it already was pre-Step-7 for `model_id` alone.
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=_mock_anthropic_client("ok")
        ):
            result = call_llm(
                "claude-haiku-4-5-20251001", "prompt", tier="B", provider="anthropic", max_tokens=100
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
        self.assertEqual(result.model_id, TIER_MODEL_MAP["A"]["model_id"])
        self.assertEqual(result.text, "ok")


class CallLlmProviderRequiredTest(unittest.TestCase):
    """HTA Orchestrator Step 7: `provider` is required whenever `model_id`
    is given explicitly (with or without `tier` also present), and is
    validated (not used for dispatch) when passed alongside `tier` with no
    `model_id`. See `llm_client._resolve_dispatch`'s docstring for the
    full precedence table this class exercises.
    """

    def test_model_id_without_provider_and_without_tier_raises(self):
        with self.assertRaises(LLMCallError):
            call_llm("claude-haiku-4-5-20251001", "prompt", max_tokens=100)

    def test_model_id_and_tier_without_provider_raises(self):
        with self.assertRaises(LLMCallError):
            call_llm("claude-haiku-4-5-20251001", "prompt", tier="B", max_tokens=100)

    def test_tier_with_matching_provider_dispatches_normally(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", return_value=_mock_anthropic_client("ok")
        ):
            result = call_llm(tier="A", provider="anthropic", prompt="prompt", max_tokens=100)
        self.assertEqual(result.model_id, TIER_MODEL_MAP["A"]["model_id"])

    def test_tier_with_mismatched_provider_raises(self):
        # TIER_MODEL_MAP["A"]["provider"] is "anthropic" -- passing
        # provider="groq" alongside tier="A" is a caller-side contradiction
        # that must fail loudly rather than silently picking one side.
        with self.assertRaises(LLMCallError):
            call_llm(tier="A", provider="groq", prompt="prompt", max_tokens=100)


class CallLlmGroqDispatchTest(unittest.TestCase):
    """HTA Orchestrator Step 7: Groq dispatch path (`_call_groq`) -- wired
    and tested even though no `TIER_MODEL_MAP` tier points at Groq yet, so
    a caller must reach it via an explicit `model_id` + `provider="groq"`
    override, mirroring `CallLlmDispatchTest`'s Anthropic coverage above.
    """

    def test_returns_normalized_text_and_token_counts(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}), patch(
            "groq.Groq",
            return_value=_mock_groq_client("hello from groq", prompt_tokens=6, completion_tokens=9),
        ):
            result = call_llm(
                "llama-3.3-70b-versatile", "prompt text", provider="groq", max_tokens=100
            )

        self.assertEqual(result.text, "hello from groq")
        self.assertEqual(result.tokens_in, 6)
        self.assertEqual(result.tokens_out, 9)
        self.assertEqual(result.model_id, "llama-3.3-70b-versatile")

    def test_missing_usage_defaults_token_counts_to_zero(self):
        mock_message = MagicMock()
        mock_message.content = "text"
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]
        mock_completion.usage = None
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_completion

        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}), patch(
            "groq.Groq", return_value=mock_client
        ):
            result = call_llm("llama-3.3-70b-versatile", "prompt", provider="groq", max_tokens=100)

        self.assertEqual(result.tokens_in, 0)
        self.assertEqual(result.tokens_out, 0)

    def test_raises_llm_call_error_when_api_key_missing(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": ""}):
            with self.assertRaises(LLMCallError):
                call_llm("llama-3.3-70b-versatile", "prompt", provider="groq", max_tokens=100)

    def test_wraps_sdk_exception_in_llm_call_error(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("boom")
        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}), patch(
            "groq.Groq", return_value=mock_client
        ):
            with self.assertRaises(LLMCallError):
                call_llm("llama-3.3-70b-versatile", "prompt", provider="groq", max_tokens=100)


class CallLlmOpenAIDispatchTest(unittest.TestCase):
    """Step 10 Phase 2 (Multi-Provider Tier C): OpenAI dispatch path
    (`_call_openai`) -- wired and tested even though no `TIER_MODEL_MAP`
    tier points at OpenAI yet, mirroring `CallLlmGroqDispatchTest`'s own
    coverage exactly (same OpenAI-compatible SDK shape).
    """

    def test_returns_normalized_text_and_token_counts(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch(
            "openai.OpenAI",
            return_value=_mock_openai_client("hello from openai", prompt_tokens=6, completion_tokens=9),
        ):
            result = call_llm("gpt-4o-mini", "prompt text", provider="openai", max_tokens=100)

        self.assertEqual(result.text, "hello from openai")
        self.assertEqual(result.tokens_in, 6)
        self.assertEqual(result.tokens_out, 9)
        self.assertEqual(result.model_id, "gpt-4o-mini")

    def test_missing_usage_defaults_token_counts_to_zero(self):
        mock_message = MagicMock()
        mock_message.content = "text"
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_completion = MagicMock()
        mock_completion.choices = [mock_choice]
        mock_completion.usage = None
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_completion

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch(
            "openai.OpenAI", return_value=mock_client
        ):
            result = call_llm("gpt-4o-mini", "prompt", provider="openai", max_tokens=100)

        self.assertEqual(result.tokens_in, 0)
        self.assertEqual(result.tokens_out, 0)

    def test_raises_llm_call_error_when_api_key_missing(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaises(LLMCallError):
                call_llm("gpt-4o-mini", "prompt", provider="openai", max_tokens=100)

    def test_wraps_sdk_exception_in_llm_call_error(self):
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("boom")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch(
            "openai.OpenAI", return_value=mock_client
        ):
            with self.assertRaises(LLMCallError):
                call_llm("gpt-4o-mini", "prompt", provider="openai", max_tokens=100)


class CallLlmResponseFormatTest(unittest.TestCase):
    """Step 10 Phase 2: `response_format="json"` -- translated to Groq's/
    OpenAI's native `{"type": "json_object"}` chat-completions kwarg,
    a no-op for Anthropic. No caller passes this yet (the existing Tier C
    shadow call site is unchanged in Phase 2); this exercises the
    plumbing in isolation, same "wired ahead of use" convention as the
    dispatch paths themselves.
    """

    def _capture_groq_kwargs(self, **call_kwargs):
        captured = {}

        def fake_groq(api_key):
            client = _mock_groq_client("ok")

            def capture_create(**kwargs):
                captured.update(kwargs)
                return client.chat.completions.create.return_value

            client.chat.completions.create.side_effect = capture_create
            return client

        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}), patch(
            "groq.Groq", side_effect=fake_groq
        ):
            call_llm("llama-3.3-70b-versatile", "prompt", provider="groq", max_tokens=100, **call_kwargs)
        return captured

    def _capture_openai_kwargs(self, **call_kwargs):
        captured = {}

        def fake_openai(api_key):
            client = _mock_openai_client("ok")

            def capture_create(**kwargs):
                captured.update(kwargs)
                return client.chat.completions.create.return_value

            client.chat.completions.create.side_effect = capture_create
            return client

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}), patch(
            "openai.OpenAI", side_effect=fake_openai
        ):
            call_llm("gpt-4o-mini", "prompt", provider="openai", max_tokens=100, **call_kwargs)
        return captured

    def test_groq_receives_json_object_response_format_when_requested(self):
        captured = self._capture_groq_kwargs(response_format="json")
        self.assertEqual(captured["response_format"], {"type": "json_object"})

    def test_groq_omits_response_format_kwarg_when_not_requested(self):
        captured = self._capture_groq_kwargs()
        self.assertNotIn("response_format", captured)

    def test_openai_receives_json_object_response_format_when_requested(self):
        captured = self._capture_openai_kwargs(response_format="json")
        self.assertEqual(captured["response_format"], {"type": "json_object"})

    def test_openai_omits_response_format_kwarg_when_not_requested(self):
        captured = self._capture_openai_kwargs()
        self.assertNotIn("response_format", captured)

    def test_anthropic_ignores_response_format_entirely(self):
        # Anthropic has no native JSON-mode parameter
        # (PROVIDER_METADATA["anthropic"]["supports_json_mode"] is
        # already False) -- passing response_format="json" alongside
        # provider="anthropic" must not raise, and must not appear in
        # the exact kwargs sent to anthropic.Anthropic().messages.create,
        # preserving llm_client.py's documented "assert exact kwargs"
        # test-compatibility constraint for every pre-existing Anthropic
        # call site.
        captured = {}

        def fake_anthropic(api_key):
            client = _mock_anthropic_client("ok")

            def capture_create(**kwargs):
                captured.update(kwargs)
                return client.messages.create.return_value

            client.messages.create.side_effect = capture_create
            return client

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}), patch(
            "anthropic.Anthropic", side_effect=fake_anthropic
        ):
            result = call_llm(
                "claude-haiku-4-5-20251001",
                "prompt",
                provider="anthropic",
                max_tokens=100,
                response_format="json",
            )

        self.assertEqual(result.text, "ok")
        self.assertNotIn("response_format", captured)


class ProviderMetadataTest(unittest.TestCase):
    """Step 10 Phase 2: OpenAI's entry in PROVIDER_METADATA -- not
    consulted by call_llm itself yet (see that dict's own docstring), but
    must exist and report supports_json_mode=True for a future caller
    (a later Step 10 phase's orchestrator) to key its response_format
    decision off of.
    """

    def test_openai_entry_supports_json_mode(self):
        self.assertTrue(PROVIDER_METADATA["openai"]["supports_json_mode"])
        self.assertGreater(PROVIDER_METADATA["openai"]["context_length"], 0)

    def test_anthropic_entry_does_not_support_json_mode(self):
        self.assertFalse(PROVIDER_METADATA["anthropic"]["supports_json_mode"])


if __name__ == "__main__":
    unittest.main()
