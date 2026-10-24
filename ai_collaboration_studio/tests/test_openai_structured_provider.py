from __future__ import annotations

import json
import os
import unittest
import urllib.request
from typing import Any
from unittest.mock import patch


_previous_skip_local_env = os.environ.get("AI_STUDIO_SKIP_LOCAL_ENV")
os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
try:
    from backend.providers.openai_provider import OpenAIProvider, _NoRedirectHandler
    from backend.providers.output import (
        OUTPUT_MODE_JSON_SCHEMA,
        generate_turn_output,
        provider_output_capabilities,
    )
finally:
    if _previous_skip_local_env is None:
        os.environ.pop("AI_STUDIO_SKIP_LOCAL_ENV", None)
    else:
        os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = _previous_skip_local_env


class FakeHTTPResponse:
    def __init__(self, payload: Any) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        raw = json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
        return raw if size < 0 else raw[:size]


class OversizedHTTPResponse(FakeHTTPResponse):
    def __init__(self) -> None:
        super().__init__({})

    def read(self, *_args: object) -> bytes:
        return b"x" * (2 * 1024 * 1024 + 1)


def _completed_payload(content: str) -> dict[str, Any]:
    return {
        "id": "resp_fixture_123",
        "status": "completed",
        "model": "gpt-fixture",
        "output": [{
            "type": "message",
            "content": [{"type": "output_text", "text": content}],
        }],
        "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150},
    }


class OpenAIStructuredProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OpenAIProvider(
            api_key="fixture-openai-key",
            base_url="https://openai.invalid/v1",
            default_model="gpt-fixture",
        )
        self.schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
        }

    def test_declares_and_sends_one_strict_json_schema_response(self) -> None:
        with patch(
            "backend.providers.openai_provider._urlopen_no_redirect",
            return_value=FakeHTTPResponse(_completed_payload('{"ok":true}')),
        ) as urlopen:
            turn = generate_turn_output(
                self.provider,
                instructions="Return one safe object.",
                input_text="untrusted input",
                json_schema=self.schema,
                schema_name="telegram_analysis_v1",
            )

        capabilities = provider_output_capabilities(self.provider)
        self.assertEqual(capabilities.modes, (OUTPUT_MODE_JSON_SCHEMA,))
        self.assertEqual(turn.mode, OUTPUT_MODE_JSON_SCHEMA)
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://openai.invalid/v1/responses")
        self.assertEqual(body["text"]["format"], {
            "type": "json_schema",
            "name": "telegram_analysis_v1",
            "schema": self.schema,
            "strict": True,
        })
        self.assertFalse(body["store"])
        self.assertNotIn("tools", body)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 60)
        self.assertTrue(turn.response.ok)
        self.assertEqual(json.loads(turn.response.content), {"ok": True})
        self.assertEqual(turn.response.usage["request_id"], "resp_fixture_123")

    def test_refusal_is_safe_and_never_returns_refusal_text(self) -> None:
        payload = {
            "id": "resp_refused",
            "status": "completed",
            "model": "gpt-fixture",
            "output": [{
                "type": "message",
                "content": [{
                    "type": "refusal",
                    "refusal": "Bearer upstream-secret must not escape",
                }],
            }],
        }
        with patch("backend.providers.openai_provider._urlopen_no_redirect", return_value=FakeHTTPResponse(payload)):
            response = self.provider.generate_json_schema(
                instructions="safe",
                input_text="input",
                json_schema=self.schema,
                schema_name="telegram_analysis_v1",
            )

        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "provider_error")
        self.assertNotIn("upstream-secret", response.error)
        self.assertEqual(response.content, "")

    def test_incomplete_or_non_json_response_fails_closed(self) -> None:
        incomplete = _completed_payload('{"ok":true}')
        incomplete["status"] = "incomplete"
        invalid = _completed_payload("not-json upstream-secret")

        with patch("backend.providers.openai_provider._urlopen_no_redirect", return_value=FakeHTTPResponse(incomplete)):
            incomplete_response = self.provider.generate_json_schema(
                instructions="safe",
                input_text="input",
                json_schema=self.schema,
                schema_name="telegram_analysis_v1",
            )
        with patch("backend.providers.openai_provider._urlopen_no_redirect", return_value=FakeHTTPResponse(invalid)):
            invalid_response = self.provider.generate_json_schema(
                instructions="safe",
                input_text="input",
                json_schema=self.schema,
                schema_name="telegram_analysis_v1",
            )

        self.assertEqual(incomplete_response.error_code, "invalid_response")
        self.assertEqual(incomplete_response.usage["total_tokens"], 150)
        self.assertEqual(invalid_response.error_code, "invalid_response")
        self.assertNotIn("upstream-secret", invalid_response.error)

    def test_missing_key_does_not_attempt_transport(self) -> None:
        provider = OpenAIProvider(
            api_key="",
            base_url="https://openai.invalid/v1",
            default_model="gpt-fixture",
        )
        with patch("backend.providers.openai_provider._urlopen_no_redirect") as urlopen:
            response = provider.generate_json_schema(
                instructions="safe",
                input_text="input",
                json_schema=self.schema,
                schema_name="telegram_analysis_v1",
            )

        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "provider_error")
        urlopen.assert_not_called()

    def test_redirect_handler_never_forwards_authorization(self) -> None:
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": "Bearer fixture-secret"},
            method="POST",
        )

        redirected = _NoRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://attacker.invalid/collect",
        )

        self.assertIsNone(redirected)

    def test_oversized_response_fails_closed(self) -> None:
        with patch(
            "backend.providers.openai_provider._urlopen_no_redirect",
            return_value=OversizedHTTPResponse(),
        ):
            response = self.provider.generate_json_schema(
                instructions="safe",
                input_text="input",
                json_schema=self.schema,
                schema_name="telegram_analysis_v1",
            )

        self.assertFalse(response.ok)
        self.assertEqual(response.error_code, "invalid_response")

    def test_invalid_schema_name_or_body_fails_before_transport(self) -> None:
        cases = (
            (self.schema, "bad name with spaces"),
            ({"type": "object", "invalid": object()}, "valid_schema"),
        )

        for schema, name in cases:
            with self.subTest(name=name):
                with patch("backend.providers.openai_provider._urlopen_no_redirect") as urlopen:
                    response = self.provider.generate_json_schema(
                        instructions="safe",
                        input_text="input",
                        json_schema=schema,
                        schema_name=name,
                    )
                self.assertFalse(response.ok)
                self.assertEqual(response.error_code, "invalid_response")
                urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
