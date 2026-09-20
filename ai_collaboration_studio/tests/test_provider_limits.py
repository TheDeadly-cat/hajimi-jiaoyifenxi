from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from backend.providers.deepseek_provider import DeepSeekProvider
from backend.providers.doubao_provider import DoubaoProvider
from backend.providers.glm_provider import GLMProvider
from backend.providers.openai_provider import OpenAIProvider
from tests.test_providers import FakeHTTPResponse


class ProviderOutputLimitTests(unittest.TestCase):
    def routes(self):
        return [
            (DeepSeekProvider(api_key="fixture", base_url="https://example.deepseek.test", default_model="fixture-model"), "generate", "max_tokens", 4096),
            (DeepSeekProvider(api_key="fixture", base_url="https://example.deepseek.test", default_model="fixture-model"), "generate_json", "max_tokens", 3200),
            (GLMProvider(api_key="fixture", base_url="https://example.glm.test", default_model="fixture-model"), "generate", "max_tokens", 4096),
            (DoubaoProvider(api_key="fixture", base_url="https://example.doubao.test", default_model="fixture-model"), "generate", "max_output_tokens", 4096),
            (DoubaoProvider(api_key="fixture", base_url="https://example.doubao.test", default_model="fixture-model"), "generate_json", "max_output_tokens", 6400),
            (OpenAIProvider(api_key="fixture", base_url="https://example.openai.test", default_model="fixture-model"), "generate", "max_output_tokens", 900),
        ]

    def payload(self):
        return {"id": "fixture-response", "model": "fixture-model", "status": "completed",
                "output_text": '{"summary":"fixture only"}',
                "choices": [{"finish_reason": "stop", "message": {"content": '{"summary":"fixture only"}'}}]}

    def test_explicit_limit_is_the_actual_request_parameter_for_each_supported_route(self):
        for provider, method, field, _ in self.routes():
            with self.subTest(provider=provider.provider_id, method=method):
                with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(self.payload())) as transport:
                    result = getattr(provider, method)(instructions="Return JSON.", input_text="Local fixture.", model="fixture-model", max_output_tokens=900)
                self.assertTrue(result.ok)
                self.assertEqual(transport.call_count, 1)
                body = json.loads(transport.call_args.args[0].data)
                self.assertEqual(body[field], 900)
                self.assertEqual(body["model"], "fixture-model")
                self.assertNotIn("tools", body)

    def test_omitted_limit_preserves_ordinary_generation_defaults(self):
        for provider, method, field, default in self.routes():
            with self.subTest(provider=provider.provider_id, method=method):
                with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(self.payload())) as transport:
                    getattr(provider, method)(instructions="Return JSON.", input_text="Local fixture.")
                self.assertEqual(json.loads(transport.call_args.args[0].data)[field], default)

    def test_invalid_limit_is_rejected_before_any_transport_call(self):
        with patch("urllib.request.urlopen") as transport:
            for provider, method, _, _ in self.routes():
                for invalid in [True, False, 0, -1, "900", 900.0]:
                    with self.subTest(provider=provider.provider_id, method=method, value=invalid):
                        with self.assertRaises(ValueError):
                            getattr(provider, method)(instructions="Return JSON.", input_text="Local fixture.", max_output_tokens=invalid)
        transport.assert_not_called()

    def test_response_identity_is_received_metadata_not_the_requested_model_fallback(self):
        provider = OpenAIProvider(api_key="fixture", base_url="https://example.openai.test", default_model="requested-model")
        payload = {"status": "completed", "output_text": "A fixture answer."}
        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            response = provider.generate(instructions="Fixture", input_text="Fixture")
        self.assertEqual(response.model, "requested-model")  # Legacy display fallback.
        self.assertEqual(response.reported_model, "")
        self.assertEqual(response.response_id, "")
        self.assertEqual(response.usage, {})  # Missing usage is not fabricated as zero tokens.

    def test_incomplete_refused_or_nonterminal_responses_are_not_success_even_with_text(self):
        for provider in [OpenAIProvider(api_key="fixture"), DoubaoProvider(api_key="fixture")]:
            for status in ["incomplete", "failed", "cancelled", "queued", "in_progress"]:
                payload = {"id": "fixture-response", "model": "actual-model", "status": status,
                           "output_text": "This text does not prove completion.",
                           "incomplete_details": {"reason": "max_output_tokens"},
                           "usage": {"input_tokens": 12, "output_tokens": 900}}
                with self.subTest(provider=provider.provider_id, status=status):
                    with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
                        response = provider.generate(instructions="Fixture", input_text="Fixture")
                    self.assertFalse(response.ok)
                    self.assertEqual(response.response_status, status)
                    self.assertEqual(response.reported_model, "actual-model")
                    self.assertEqual(response.response_id, "fixture-response")
                    self.assertEqual(response.usage["output_tokens"], 900)
            payload = {"status": "completed", "output_text": "Not an accepted answer.",
                       "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "Refused"}]}]}
            with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
                response = provider.generate(instructions="Fixture", input_text="Fixture")
            self.assertFalse(response.ok)
            self.assertTrue(response.refused)

    def test_chat_nonstop_terminal_reasons_preserve_usage_and_stop_the_call(self):
        provider = DeepSeekProvider(api_key="fixture")
        for reason in ["length", "content_filter", "tool_calls", "insufficient_system_resource"]:
            payload = {"id": "fixture-id", "model": "actual-model", "usage": {"prompt_tokens": 12, "completion_tokens": 900},
                       "choices": [{"finish_reason": reason, "message": {"content": "Partial fixture."}}]}
            with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)) as transport:
                response = provider.generate_json(instructions="Return JSON", input_text="Fixture", max_output_tokens=900)
            self.assertFalse(response.ok)
            self.assertEqual(response.finish_reason, reason)
            self.assertEqual(response.reported_model, "actual-model")
            self.assertEqual(response.usage["completion_tokens"], 900)
            self.assertEqual(transport.call_count, 1)
