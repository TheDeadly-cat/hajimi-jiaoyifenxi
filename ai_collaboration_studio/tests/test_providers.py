from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from backend.providers.base import ProviderResponse, classify_provider_exception
from backend.providers.compatible_chat_provider import provider_http_error
from backend.providers.deepseek_provider import DeepSeekProvider
from backend.providers.doubao_provider import DoubaoProvider
from backend.providers.glm_provider import GLMProvider
from backend.providers.openai_provider import OpenAIProvider, _http_error_text
from backend.providers.registry import ProviderRegistry


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class FakeRawHTTPResponse(FakeHTTPResponse):
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def read(self) -> bytes:
        return self.raw


class ProviderAdapterTests(unittest.TestCase):
    def test_provider_response_keeps_legacy_usage_positional_argument(self) -> None:
        response = ProviderResponse(
            False,
            "",
            "openai",
            "gpt-test",
            "safe error",
            {"total_tokens": 7},
        )

        self.assertEqual(response.usage, {"total_tokens": 7})
        self.assertEqual(response.error_code, "")

    def test_http_error_exception_classifies_as_http_status(self) -> None:
        failure = urllib.error.HTTPError(
            "https://example.test",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b"upstream-secret"),
        )
        try:
            self.assertEqual(classify_provider_exception(failure), "http_status")
        finally:
            failure.close()

    def test_registry_exposes_four_providers_without_secrets(self) -> None:
        statuses = ProviderRegistry().status()

        self.assertEqual([item["id"] for item in statuses], ["openai", "deepseek", "doubao", "glm"])
        serialized = json.dumps(statuses, ensure_ascii=False).lower()
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("authorization", serialized)

    def test_unconfigured_providers_fail_without_network_requests(self) -> None:
        providers = [
            DeepSeekProvider(api_key=""),
            DoubaoProvider(api_key=""),
            GLMProvider(api_key=""),
        ]

        with patch("urllib.request.urlopen") as urlopen:
            responses = [provider.generate(instructions="规则", input_text="问题") for provider in providers]

        self.assertFalse(any(response.ok for response in responses))
        self.assertEqual(urlopen.call_count, 0)
        self.assertIn("DEEPSEEK_API_KEY", responses[0].error)
        self.assertIn("ARK_API_KEY", responses[1].error)
        self.assertIn("GLM_API_KEY", responses[2].error)
        self.assertTrue(all(response.error_code == "provider_error" for response in responses))

    def test_deepseek_uses_chat_completions_and_model_override(self) -> None:
        provider = DeepSeekProvider(
            api_key="fake-deepseek-key",
            base_url="https://example.deepseek.test",
            default_model="deepseek-v4-pro",
        )
        payload = {
            "model": "deepseek-v4-flash",
            "choices": [{"message": {"content": "基于证据的反方观点"}}],
            "usage": {"total_tokens": 31},
        }

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)) as urlopen:
            response = provider.generate(
                instructions="你是反方研究员",
                input_text="审查当前结论",
                model="deepseek-v4-flash",
            )

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://example.deepseek.test/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake-deepseek-key")
        self.assertEqual(body["model"], "deepseek-v4-flash")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "你是反方研究员"})
        self.assertEqual(body["max_tokens"], 4096)
        self.assertTrue(response.ok)
        self.assertEqual(response.content, "基于证据的反方观点")
        self.assertEqual(response.usage["total_tokens"], 31)

    def test_deepseek_rejects_length_finish_reason_even_when_content_is_present(self) -> None:
        provider = DeepSeekProvider(
            api_key="fake-deepseek-key",
            base_url="https://example.deepseek.test",
            default_model="deepseek-v4-pro",
        )
        payload = {
            "model": "deepseek-v4-pro",
            "choices": [{
                "finish_reason": "length",
                "message": {
                    "content": "正文\n<turn_contract>{\"version\":\"turn_contract_v1\"",
                },
            }],
            "usage": {"completion_tokens": 4096},
        }

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            response = provider.generate(
                instructions="输出正文和完整合同",
                input_text="继续正式发言",
            )

        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "invalid_response")
        self.assertEqual(response.content, "")
        self.assertEqual(response.usage, {"completion_tokens": 4096})

    def test_deepseek_preserves_complete_visible_text_and_turn_contract(self) -> None:
        provider = DeepSeekProvider(
            api_key="fake-deepseek-key",
            base_url="https://example.deepseek.test",
            default_model="deepseek-v4-pro",
        )
        content = (
            "第一段可见正文。\n\n第二段可见正文。\n"
            '<turn_contract>{"version":"turn_contract_v1","claims":[]}</turn_contract>'
        )
        payload = {
            "model": "deepseek-v4-pro",
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": content},
            }],
        }

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            response = provider.generate(
                instructions="输出正文和完整合同",
                input_text="继续正式发言",
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.content, content)

    def test_deepseek_json_generation_uses_structured_output_budget(self) -> None:
        provider = DeepSeekProvider(
            api_key="fake-deepseek-key",
            base_url="https://example.deepseek.test",
            default_model="deepseek-v4-pro",
        )
        payload = {
            "model": "deepseek-v4-pro",
            "choices": [{"message": {"content": '{"summary":"ok"}'}}],
        }

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)) as urlopen:
            response = provider.generate_json(
                instructions="只输出 JSON 对象",
                input_text="整理会议记录",
            )

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["max_tokens"], 3200)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 180)
        self.assertTrue(response.ok)

    def test_doubao_uses_responses_api(self) -> None:
        provider = DoubaoProvider(
            api_key="fake-ark-key",
            base_url="https://example.ark.test/api/v3",
            default_model="doubao-seed-2-0-lite-260215",
        )
        payload = {
            "model": "doubao-seed-2-0-lite-260215",
            "output_text": "风险条件仍需验证",
            "usage": {"total_tokens": 22},
        }

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)) as urlopen:
            response = provider.generate(instructions="你是风险经理", input_text="检查风险", model="")

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://example.ark.test/api/v3/responses")
        self.assertEqual(body["model"], "doubao-seed-2-0-lite-260215")
        self.assertEqual(body["instructions"], "你是风险经理")
        self.assertEqual(body["max_output_tokens"], 4096)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertFalse(body["store"])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)
        self.assertTrue(response.ok)
        self.assertEqual(response.content, "风险条件仍需验证")

    def test_doubao_json_generation_uses_long_responses_budget_and_parses_output(self) -> None:
        provider = DoubaoProvider(
            api_key="fake-ark-key",
            base_url="https://example.ark.test/api/v3",
            default_model="doubao-seed-2-0-lite-260215",
        )
        payload = {
            "model": "doubao-seed-2-0-lite-260215",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"summary":"会议结论","risks":["需验证"]}',
                        }
                    ],
                }
            ],
            "usage": {"total_tokens": 3100},
        }

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)) as urlopen:
            response = provider.generate_json(
                instructions="只输出 JSON 对象",
                input_text="整理会议记录",
            )

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://example.ark.test/api/v3/responses")
        self.assertEqual(body["max_output_tokens"], 6400)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["text"], {"format": {"type": "json_object"}})
        self.assertFalse(body["store"])
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 240)
        self.assertTrue(response.ok)
        self.assertEqual(
            json.loads(response.content),
            {"summary": "会议结论", "risks": ["需验证"]},
        )
        self.assertEqual(response.usage["total_tokens"], 3100)

    def test_doubao_rejects_incomplete_response_even_when_it_contains_text(self) -> None:
        provider = DoubaoProvider(
            api_key="fake-ark-key",
            base_url="https://example.ark.test/api/v3",
            default_model="doubao-seed-2-0-lite-260215",
        )
        payload = {
            "model": "doubao-seed-2-0-lite-260215",
            "status": "incomplete",
            "output_text": '{"summary":"截断内容不能落库"}',
            "usage": {"total_tokens": 6400},
        }

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            response = provider.generate_json(
                instructions="只输出 JSON 对象",
                input_text="整理会议记录",
            )

        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "invalid_response")
        self.assertEqual(response.provider, "doubao")
        self.assertNotIn("截断内容", response.error)

    def test_doubao_normal_generation_rejects_incomplete_response(self) -> None:
        provider = DoubaoProvider(
            api_key="fake-ark-key",
            base_url="https://example.ark.test/api/v3",
            default_model="doubao-seed-2-0-lite-260215",
        )
        payload = {
            "model": "doubao-seed-2-0-lite-260215",
            "status": "incomplete",
            "output_text": "正文\n<turn_contract>{\"version\":\"turn_contract_v1\"",
            "usage": {"output_tokens": 4096},
        }

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            response = provider.generate(
                instructions="输出正文和完整合同",
                input_text="继续正式发言",
            )

        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "invalid_response")
        self.assertEqual(response.content, "")
        self.assertEqual(response.usage, {"output_tokens": 4096})

    def test_doubao_preserves_complete_visible_text_and_turn_contract(self) -> None:
        provider = DoubaoProvider(
            api_key="fake-ark-key",
            base_url="https://example.ark.test/api/v3",
            default_model="doubao-seed-2-0-lite-260215",
        )
        content = (
            "第一段可见正文。\n\n第二段可见正文。\n"
            '<turn_contract>{"version":"turn_contract_v1","claims":[]}</turn_contract>'
        )
        payload = {
            "model": "doubao-seed-2-0-lite-260215",
            "status": "completed",
            "output_text": content,
        }

        with patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
            response = provider.generate(
                instructions="输出正文和完整合同",
                input_text="继续正式发言",
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.content, content)

    def test_doubao_failure_codes_are_structured_and_do_not_leak_exception_text(self) -> None:
        provider = DoubaoProvider(
            api_key="fake-ark-key",
            base_url="https://example.ark.test/api/v3",
            default_model="doubao-test",
        )

        with patch(
            "urllib.request.urlopen",
            side_effect=TimeoutError("Bearer upstream-secret must not escape"),
        ):
            response = provider.generate(instructions="规则", input_text="问题")

        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "timeout")
        self.assertEqual(response.provider, "doubao")
        self.assertEqual(response.model, "doubao-test")
        self.assertIn("请求超时", response.error)
        self.assertNotIn("upstream-secret", response.error)

    def test_compatible_provider_classifies_network_invalid_and_empty_responses(self) -> None:
        provider = DeepSeekProvider(
            api_key="fake-deepseek-key",
            base_url="https://example.deepseek.test",
            default_model="deepseek-test",
        )

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("internal endpoint and secret"),
        ):
            network = provider.generate(instructions="规则", input_text="问题")
        with patch(
            "urllib.request.urlopen",
            return_value=FakeRawHTTPResponse(b"not-json upstream-secret"),
        ):
            invalid = provider.generate(instructions="规则", input_text="问题")
        with patch(
            "urllib.request.urlopen",
            return_value=FakeHTTPResponse({"model": "deepseek-test", "choices": []}),
        ):
            empty = provider.generate(instructions="规则", input_text="问题")

        self.assertEqual(network.error_code, "network")
        self.assertNotIn("secret", network.error)
        self.assertEqual(invalid.error_code, "invalid_response")
        self.assertNotIn("upstream-secret", invalid.error)
        self.assertEqual(empty.error_code, "empty_response")

    def test_http_failure_uses_safe_message_and_http_status_code(self) -> None:
        provider = DoubaoProvider(
            api_key="fake-ark-key",
            base_url="https://example.ark.test/api/v3",
            default_model="doubao-test",
        )
        upstream = b'{"error":{"message":"Bearer upstream-secret internal body"}}'
        failure = urllib.error.HTTPError(
            "https://example.ark.test/api/v3/responses",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(upstream),
        )

        with patch("urllib.request.urlopen", side_effect=failure):
            response = provider.generate(instructions="规则", input_text="问题")
        failure.close()

        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "http_status")
        self.assertEqual(response.error, "豆包 / 火山方舟 请求失败（HTTP 500）。")
        self.assertNotIn("upstream-secret", response.error)

    def test_openai_failure_classification_keeps_safe_chinese_messages(self) -> None:
        provider = OpenAIProvider(
            api_key="fake-openai-key",
            base_url="https://example.openai.test/v1",
            default_model="gpt-test",
        )

        with patch(
            "urllib.request.urlopen",
            side_effect=TimeoutError("sensitive-test-token-upstream-secret"),
        ):
            response = provider.generate(instructions="规则", input_text="问题")

        self.assertEqual(response.error_code, "timeout")
        self.assertEqual(response.error, "OpenAI 请求超时。")
        self.assertNotIn("upstream-secret", response.error)
        self.assertEqual(
            _http_error_text(
                '{"error":{"message":"Bearer upstream-secret internal body"}}',
                500,
            ),
            "OpenAI 请求失败（HTTP 500）。",
        )

    def test_glm_status_and_friendly_http_errors(self) -> None:
        status = GLMProvider(api_key="fake-glm-key", default_model="glm-5.2").status()

        self.assertEqual(status["id"], "glm")
        self.assertEqual(status["model"], "glm-5.2")
        self.assertTrue(status["configured"])
        self.assertEqual(
            provider_http_error('{"error":{"code":"rate_limit_exceeded"}}', 429, "智谱 GLM"),
            "智谱 GLM 请求频率受限，请稍后重试。",
        )


if __name__ == "__main__":
    unittest.main()
