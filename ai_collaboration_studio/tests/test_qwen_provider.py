from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from backend.execution_boundary import build_text_provider_request, ExecutionBoundaryViolation
from backend.providers.qwen_provider import QwenProvider, is_qwen_backend_endpoint
from backend.providers.registry import ProviderRegistry


class QwenProviderTests(unittest.TestCase):
    def payload(self, finish_reason="stop"):
        return {"id": "synthetic-response", "model": "qwen3.8-max",
                "choices": [{"finish_reason": finish_reason, "message": {"content": '{"fact":"fixture"}'}}],
                "usage": {"prompt_tokens": 24, "completion_tokens": 32,
                          "completion_tokens_details": {"reasoning_tokens": 20}}}

    def test_unconfigured_or_missing_model_stops_without_network(self):
        with patch("urllib.request.urlopen") as network:
            self.assertFalse(QwenProvider(api_key="", default_model="qwen3.8-max").generate(instructions="JSON", input_text="fixture").ok)
            no_model = QwenProvider(api_key="fixture-key", default_model="")
            self.assertEqual(no_model.generate(instructions="JSON", input_text="fixture").error_code, "model_not_configured")
            self.assertFalse(no_model.probe().ready)
        network.assert_not_called()

    def test_actual_wire_bounds_reasoning_plus_answer_and_preserves_usage(self):
        provider = QwenProvider(api_key="fixture-key", default_model="qwen3.8-max")
        for method, cap in ((provider.generate, 1440), (provider.generate_json, 900)):
            with self.subTest(method=method.__name__), patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(self.payload()).encode())) as network:
                response = method(instructions="Return JSON", input_text="synthetic fixture", max_output_tokens=cap)
                request = network.call_args.args[0]
                body = json.loads(request.data)
                self.assertEqual(request.full_url, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
                self.assertEqual(body["max_completion_tokens"], cap)
                self.assertNotIn("max_tokens", body)
                self.assertNotIn("tools", body)
                self.assertFalse(body["stream"])
                self.assertEqual(body.get("response_format"), {"type": "json_object"} if method.__name__ == "generate_json" else None)
                self.assertTrue(response.ok)
                self.assertEqual(response.usage["completion_tokens"], 32)
                self.assertEqual(response.usage["completion_tokens_details"]["reasoning_tokens"], 20)
                self.assertEqual(response.response_id, "synthetic-response")
                self.assertEqual(response.reported_model, "qwen3.8-max")
                self.assertEqual(network.call_count, 1)

    def test_package_and_lookalike_endpoints_are_local_errors_without_breaking_registry(self):
        for base in (
            "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "https://coding.dashscope.aliyuncs.com/v1",
            "https://dashscope.aliyuncs.com.evil.test/compatible-mode/v1",
            "https://evil.test/compatible-mode/v1", "http://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://user@dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1?token=fixture",
        ):
            with self.subTest(base=base), patch("urllib.request.urlopen") as network:
                provider = QwenProvider(api_key="fixture-key", base_url=base, default_model="qwen3.8-max")
                self.assertFalse(provider.status()["configured"])
                self.assertFalse(provider.probe().ready)
                self.assertEqual(provider.generate_json(instructions="JSON", input_text="fixture").error_code, "QWEN_BACKEND_ENDPOINT_NOT_ALLOWED")
                with patch("backend.providers.registry.QwenProvider", return_value=provider):
                    self.assertIsNotNone(ProviderRegistry().get("doubao"))
                network.assert_not_called()
        self.assertTrue(is_qwen_backend_endpoint("https://workspace123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"))

    def test_truncated_answer_preserves_usage_but_is_not_accepted(self):
        provider = QwenProvider(api_key="fixture-key", default_model="qwen3.8-max")
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(self.payload("length")).encode())) as network:
            response = provider.generate_json(instructions="JSON", input_text="fixture", max_output_tokens=32)
        self.assertFalse(response.ok)
        self.assertEqual(response.usage["completion_tokens"], 32)
        self.assertEqual(response.content, "")
        self.assertEqual(network.call_count, 1)

    def test_http_failure_does_not_retry_and_does_not_expose_upstream_body(self):
        failure = urllib.error.HTTPError("https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions", 429, "fixture", {}, io.BytesIO(b"upstream-fixture-secret"))
        with patch("urllib.request.urlopen", side_effect=failure) as network:
            response = QwenProvider(api_key="fixture-key", default_model="qwen3.8-max").generate(instructions="JSON", input_text="fixture")
        self.assertFalse(response.ok)
        self.assertNotIn("upstream-fixture-secret", response.error)
        self.assertEqual(network.call_count, 1)
        failure.close()

    def test_output_boundary_rejects_ambiguous_or_invalid_completion_caps(self):
        base = {"model": "qwen3.8-max", "messages": [{"role": "user", "content": "fixture"}], "stream": False}
        for cap in (True, 0, -1, "900", 1.2):
            with self.subTest(cap=cap), self.assertRaises(ExecutionBoundaryViolation):
                build_text_provider_request("https://dashscope.aliyuncs.com/compatible-mode/v1", "chat_completions", {**base, "max_completion_tokens": cap}, headers={})
        with self.assertRaises(ExecutionBoundaryViolation):
            build_text_provider_request("https://dashscope.aliyuncs.com/compatible-mode/v1", "chat_completions", {**base, "max_tokens": 900, "max_completion_tokens": 900}, headers={})

    def test_isolated_runner_clears_both_qwen_credential_aliases(self):
        for name in ("QWEN_API_KEY", "DASHSCOPE_API_KEY"):
            self.assertFalse(bool(os.getenv(name)), "isolated runner must clear credential aliases")


if __name__ == "__main__":
    unittest.main()
