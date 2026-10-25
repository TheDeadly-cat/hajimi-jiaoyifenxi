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

    def read(self, size: int = -1) -> bytes:
        raw = json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        return raw if size < 0 else raw[:size]


class FakeRawHTTPResponse(FakeHTTPResponse):
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def read(self, size: int = -1) -> bytes:
        return self.raw if size < 0 else self.raw[:size]


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

        with patch("backend.providers.http_transport._NO_REDIRECT_OPENER.open") as urlopen:
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

        with patch("backend.providers.http_transport._NO_REDIRECT_OPENER.open", return_value=FakeHTTPResponse(payload)) as urlopen:
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

        with patch("backend.providers.http_transport._NO_REDIRECT_OPENER.open", return_value=FakeHTTPResponse(payload)):
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

        with patch("backend.providers.http_transport._NO_REDIRECT_OPENER.open", return_value=FakeHTTPResponse(payload)):
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

        with patch("backend.providers.http_transport._NO_REDIRECT_OPENER.open", return_value=FakeHTTPResponse(payload)) as urlopen:
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

        with patch("backend.providers.http_transport._NO_REDIRECT_OPENER.open", return_value=FakeHTTPResponse(payload)) as urlopen:
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

        with patch("backend.providers.http_transport._NO_REDIRECT_OPENER.open", return_value=FakeHTTPResponse(payload)) as urlopen:
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

        with patch("backend.providers.http_transport._NO_REDIRECT_OPENER.open", return_value=FakeHTTPResponse(payload)):
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

        with patch("backend.providers.http_transport._NO_REDIRECT_OPENER.open", return_value=FakeHTTPResponse(payload)):
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

        with patch("backend.providers.http_transport._NO_REDIRECT_OPENER.open", return_value=FakeHTTPResponse(payload)):
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
            "backend.providers.http_transport._NO_REDIRECT_OPENER.open",
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
            "backend.providers.http_transport._NO_REDIRECT_OPENER.open",
            side_effect=urllib.error.URLError("internal endpoint and secret"),
        ):
            network = provider.generate(instructions="规则", input_text="问题")
        with patch(
            "backend.providers.http_transport._NO_REDIRECT_OPENER.open",
            return_value=FakeRawHTTPResponse(b"not-json upstream-secret"),
        ):
            invalid = provider.generate(instructions="规则", input_text="问题")
        with patch(
            "backend.providers.http_transport._NO_REDIRECT_OPENER.open",
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

        with patch("backend.providers.http_transport._NO_REDIRECT_OPENER.open", side_effect=failure):
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
            "backend.providers.openai_provider._urlopen_no_redirect",
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



class BoundedResponseFixture:
    def __init__(self, raw: bytes, *, fail_read: bool = False) -> None:
        self.stream = io.BytesIO(raw)
        self.read_sizes: list[int] = []
        self.closed = False
        self.fail_read = fail_read

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if self.fail_read:
            raise OSError("synthetic-secret-must-not-escape")
        return self.stream.read(size)

    def close(self) -> None:
        self.closed = True
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class ProviderResponseBoundaryTests(unittest.TestCase):
    @staticmethod
    def cases():
        return [
            (DeepSeekProvider(api_key="synthetic-key"), "generate"),
            (DeepSeekProvider(api_key="synthetic-key"), "generate_json"),
            (GLMProvider(api_key="synthetic-key"), "generate"),
            (DoubaoProvider(api_key="synthetic-key"), "generate"),
            (DoubaoProvider(api_key="synthetic-key"), "generate_json"),
            (OpenAIProvider(api_key="synthetic-key"), "generate"),
        ]

    @staticmethod
    def target(provider):
        return ("backend.providers.openai_provider._urlopen_no_redirect"
                if provider.provider_id == "openai" else "backend.providers.http_transport._NO_REDIRECT_OPENER.open")

    @staticmethod
    def payload(provider):
        if provider.provider_id in {"doubao", "openai"}:
            return {"status": "completed", "output_text": "正常正文", "usage": {"total_tokens": 3}}
        return {"choices": [{"message": {"content": "正常正文"}, "finish_reason": "stop"}], "usage": {"total_tokens": 3}}

    def test_exact_limit_success_preserves_text_usage_and_one_call(self) -> None:
        from backend.providers.http_transport import MAX_PROVIDER_RESPONSE_BYTES
        for provider, method in self.cases():
            with self.subTest(provider=provider.provider_id, method=method):
                raw = json.dumps(self.payload(provider), ensure_ascii=False).encode("utf-8")
                body = BoundedResponseFixture(raw + b" " * (MAX_PROVIDER_RESPONSE_BYTES - len(raw)))
                with patch(self.target(provider), return_value=body) as opener:
                    result = getattr(provider, method)(instructions="rules", input_text="control")
                self.assertTrue(result.ok)
                self.assertEqual(result.content, "正常正文")
                self.assertEqual(result.usage, {"total_tokens": 3})
                self.assertEqual(body.read_sizes, [MAX_PROVIDER_RESPONSE_BYTES + 1])
                self.assertTrue(body.closed)
                self.assertEqual(opener.call_count, 1)

    def test_limit_plus_one_success_rejected_without_parse_or_retry(self) -> None:
        from backend.providers.http_transport import MAX_PROVIDER_RESPONSE_BYTES
        for provider, method in self.cases():
            with self.subTest(provider=provider.provider_id, method=method):
                body = BoundedResponseFixture(b" " * (MAX_PROVIDER_RESPONSE_BYTES + 1))
                with patch(self.target(provider), return_value=body) as opener:
                    with patch("json.loads", side_effect=AssertionError("oversized body parsed")):
                        result = getattr(provider, method)(instructions="rules", input_text="control")
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, "invalid_response")
                self.assertEqual(body.read_sizes, [MAX_PROVIDER_RESPONSE_BYTES + 1])
                self.assertTrue(body.closed)
                self.assertEqual(opener.call_count, 1)

    def test_error_body_limit_and_read_failure_keep_safe_http_status(self) -> None:
        from backend.providers.http_transport import MAX_PROVIDER_ERROR_BYTES
        for provider, method in self.cases():
            if provider.provider_id == "openai":
                continue  # Existing OpenAI error behavior is covered separately.
            for fail_read in (False, True):
                with self.subTest(provider=provider.provider_id, method=method, fail_read=fail_read):
                    body = BoundedResponseFixture(b"x" * (MAX_PROVIDER_ERROR_BYTES + 1), fail_read=fail_read)
                    failure = urllib.error.HTTPError("https://provider.example", 503, "synthetic-secret", {}, body)
                    with patch(self.target(provider), side_effect=failure) as opener:
                        result = getattr(provider, method)(instructions="rules", input_text="control")
                    self.assertFalse(result.ok)
                    self.assertEqual(result.error_code, "http_status")
                    self.assertNotIn("synthetic-secret", result.error)
                    self.assertEqual(body.read_sizes, [MAX_PROVIDER_ERROR_BYTES + 1])
                    self.assertTrue(body.closed)
                    self.assertEqual(opener.call_count, 1)

    def test_success_limit_is_bytes_not_characters(self) -> None:
        from backend.providers.http_transport import MAX_PROVIDER_RESPONSE_BYTES
        body = BoundedResponseFixture(("汉" * (MAX_PROVIDER_RESPONSE_BYTES // 3 + 1)).encode("utf-8"))
        provider = DeepSeekProvider(api_key="synthetic-key")
        with patch(self.target(provider), return_value=body) as opener:
            result = provider.generate(instructions="rules", input_text="control")
        self.assertEqual(result.error_code, "invalid_response")
        self.assertEqual(opener.call_count, 1)
        self.assertTrue(body.closed)

    def test_type_error_never_retries_unbounded_read(self) -> None:
        from backend.providers.http_transport import read_provider_response
        from backend.providers.openai_provider import _bounded_read
        for reader in (read_provider_response, _bounded_read):
            calls = []
            class NoSizedRead:
                def read(self, *args):
                    calls.append(args)
                    if args:
                        raise TypeError("wrapper does not accept a size")
                    return b"forbidden fallback"
            with self.subTest(reader=reader.__name__):
                with self.assertRaises(TypeError):
                    reader(NoSizedRead(), 32)
                self.assertEqual(calls, [(33,)])


class ProviderRedirectBoundaryTests(unittest.TestCase):
    def test_generation_and_probe_never_follow_any_redirect_target(self) -> None:
        import email.message
        import urllib.request
        from urllib.response import addinfourl
        from backend.providers.http_transport import NoProviderRedirectHandler
        from backend.providers.openai_provider import _NoRedirectHandler
        cases = ProviderResponseBoundaryTests.cases() + [
            (DeepSeekProvider(api_key="synthetic-key"), "probe"),
            (GLMProvider(api_key="synthetic-key"), "probe"),
            (DoubaoProvider(api_key="synthetic-key"), "probe"),
            (OpenAIProvider(api_key="synthetic-key"), "probe"),
        ]
        for provider, method in cases:
            for code in (300, 301, 302, 303, 304, 307, 308):
                for destination in ("/same-origin", "https://other.example/collect", "http://other.example/downgrade"):
                    calls = []
                    class SyntheticTransport(urllib.request.BaseHandler):
                        handler_order = 100
                        def https_open(self, request):
                            calls.append(request)
                            headers = email.message.Message()
                            headers["Location"] = destination
                            response = addinfourl(io.BytesIO(b'{"error":{"message":"synthetic-secret"}}'), headers, request.full_url, code)
                            response.msg = "Synthetic redirect"
                            return response
                        http_open = https_open
                    opener = urllib.request.build_opener(
                        SyntheticTransport(),
                        _NoRedirectHandler() if provider.provider_id == "openai" else NoProviderRedirectHandler(),
                    )
                    target = ("backend.providers.openai_provider._NO_REDIRECT_OPENER"
                              if provider.provider_id == "openai" else "backend.providers.http_transport._NO_REDIRECT_OPENER")
                    with self.subTest(provider=provider.provider_id, method=method, code=code, destination=destination):
                        with patch(target, opener):
                            result = (provider.probe() if method == "probe" else getattr(provider, method)(instructions="rules", input_text="control"))
                        self.assertEqual(len(calls), 1)
                        self.assertEqual(calls[0].get_method(), "POST")
                        self.assertFalse(result.ready if method == "probe" else result.ok)
                        self.assertNotIn("synthetic-secret", result.message if method == "probe" else result.error)
                        self.assertNotIn("other.example", result.message if method == "probe" else result.error)

    def test_same_transport_normal_200_preserves_generation_and_probe(self) -> None:
        import email.message
        import urllib.request
        from urllib.response import addinfourl
        from backend.providers.http_transport import NoProviderRedirectHandler
        for provider in (DeepSeekProvider(api_key="synthetic-key"), GLMProvider(api_key="synthetic-key"), DoubaoProvider(api_key="synthetic-key")):
            calls = []
            raw = json.dumps(ProviderResponseBoundaryTests.payload(provider), ensure_ascii=False).encode("utf-8")
            class SyntheticTransport(urllib.request.BaseHandler):
                handler_order = 100
                def https_open(self, request):
                    calls.append(request)
                    response = addinfourl(io.BytesIO(raw), email.message.Message(), request.full_url, 200)
                    response.msg = "OK"
                    return response
            opener = urllib.request.build_opener(SyntheticTransport(), NoProviderRedirectHandler())
            with self.subTest(provider=provider.provider_id):
                with patch("backend.providers.http_transport._NO_REDIRECT_OPENER", opener):
                    generated = provider.generate(instructions="rules", input_text="control")
                    probed = provider.probe()
                self.assertTrue(generated.ok)
                self.assertEqual(generated.content, "正常正文")
                self.assertTrue(probed.ready)
                self.assertEqual(len(calls), 2)

if __name__ == "__main__":
    unittest.main()
