from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

from backend.providers.base import ProviderResponse
from backend.providers.deepseek_provider import DeepSeekProvider
from backend.providers.doubao_provider import DoubaoProvider
from backend.providers.glm_provider import GLMProvider
from backend.providers.output import (
    OUTPUT_CAPABILITIES_VERSION,
    OUTPUT_MODE_JSON_OBJECT,
    OUTPUT_MODE_JSON_SCHEMA,
    OUTPUT_MODE_PRIORITY,
    OUTPUT_MODE_PROMPT_JSON,
    ProviderOutputCapabilities,
    ProviderOutputCapabilityError,
    generate_turn_output,
    provider_output_capabilities,
    provider_output_capability_dict,
    select_provider_output_mode,
)
from backend.providers.registry import ProviderRegistry


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class LegacyPromptProvider:
    provider_id = "legacy"

    def __init__(self) -> None:
        self.generate_calls = 0

    @staticmethod
    def status() -> dict[str, Any]:
        return {
            "id": "legacy",
            "name": "Legacy fixture",
            "configured": True,
            "model": "legacy-model",
            "api": "fixture",
        }

    def generate(self, **_request: Any) -> ProviderResponse:
        self.generate_calls += 1
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model="legacy-model",
            content='{"visible_content":"ok","turn_contract":{}}',
        )


class ProviderOutputCapabilityTests(unittest.TestCase):
    def test_concrete_adapters_declare_only_the_current_verified_modes(self) -> None:
        cases = (
            (DeepSeekProvider(api_key="fixture"), (OUTPUT_MODE_JSON_OBJECT,)),
            (DoubaoProvider(api_key="fixture"), (OUTPUT_MODE_JSON_OBJECT,)),
            (GLMProvider(api_key="fixture"), (OUTPUT_MODE_PROMPT_JSON,)),
        )

        for provider, expected_modes in cases:
            with self.subTest(provider=provider.provider_id):
                capabilities = provider_output_capabilities(provider)
                self.assertEqual(capabilities.version, OUTPUT_CAPABILITIES_VERSION)
                self.assertEqual(capabilities.modes, expected_modes)
                self.assertEqual(capabilities.preferred_mode, expected_modes[0])
                self.assertTrue(capabilities.declared)

    def test_legacy_or_malformed_declarations_default_only_to_prompt_json(self) -> None:
        class MalformedProvider(LegacyPromptProvider):
            @staticmethod
            def output_capabilities() -> dict[str, Any]:
                return {
                    "modes": ["unsupported"],
                    "api_key": "must-never-be-exposed",
                }

        for provider in (LegacyPromptProvider(), MalformedProvider()):
            with self.subTest(provider=provider.__class__.__name__):
                capabilities = provider_output_capabilities(provider)
                self.assertEqual(capabilities.modes, (OUTPUT_MODE_PROMPT_JSON,))
                self.assertFalse(capabilities.declared)

    def test_safe_capability_dict_is_canonical_and_drops_arbitrary_metadata(self) -> None:
        class NoisyProvider(LegacyPromptProvider):
            @staticmethod
            def output_capabilities() -> dict[str, Any]:
                return {
                    "modes": [
                        OUTPUT_MODE_PROMPT_JSON,
                        OUTPUT_MODE_JSON_OBJECT,
                        OUTPUT_MODE_JSON_SCHEMA,
                    ],
                    "api_key": "secret",
                    "authorization": "Bearer secret",
                    "endpoint": "https://internal.invalid",
                }

        metadata = provider_output_capability_dict(NoisyProvider())

        self.assertEqual(metadata, {
            "version": OUTPUT_CAPABILITIES_VERSION,
            "modes": list(OUTPUT_MODE_PRIORITY),
            "preferred_mode": OUTPUT_MODE_JSON_SCHEMA,
            "declared": True,
        })
        serialized = json.dumps(metadata).lower()
        self.assertNotIn("secret", serialized)
        self.assertNotIn("authorization", serialized)
        self.assertNotIn("endpoint", serialized)

    def test_selection_uses_global_priority_not_declaration_or_request_order(self) -> None:
        class AllModesProvider(LegacyPromptProvider):
            @staticmethod
            def output_capabilities() -> ProviderOutputCapabilities:
                return ProviderOutputCapabilities(modes=(
                    OUTPUT_MODE_PROMPT_JSON,
                    OUTPUT_MODE_JSON_OBJECT,
                    OUTPUT_MODE_JSON_SCHEMA,
                ))

        provider = AllModesProvider()
        selected = select_provider_output_mode(
            provider,
            preferred_modes=(
                OUTPUT_MODE_PROMPT_JSON,
                OUTPUT_MODE_JSON_OBJECT,
                OUTPUT_MODE_JSON_SCHEMA,
            ),
        )
        without_schema = select_provider_output_mode(
            provider,
            preferred_modes=(OUTPUT_MODE_PROMPT_JSON, OUTPUT_MODE_JSON_OBJECT),
        )

        self.assertEqual(selected.mode, OUTPUT_MODE_JSON_SCHEMA)
        self.assertEqual(without_schema.mode, OUTPUT_MODE_JSON_OBJECT)

    def test_singleton_preference_enforces_a_frozen_mode_without_fallback(self) -> None:
        class JsonOnlyProvider(LegacyPromptProvider):
            @staticmethod
            def output_capabilities() -> ProviderOutputCapabilities:
                return ProviderOutputCapabilities(modes=(OUTPUT_MODE_JSON_OBJECT,))

        provider = JsonOnlyProvider()

        selected = select_provider_output_mode(
            provider,
            preferred_modes=(OUTPUT_MODE_JSON_OBJECT,),
        )
        with self.assertRaises(ProviderOutputCapabilityError) as mismatch:
            generate_turn_output(
                provider,
                instructions="JSON",
                input_text="input",
                preferred_modes=(OUTPUT_MODE_PROMPT_JSON,),
            )

        self.assertEqual(selected.mode, OUTPUT_MODE_JSON_OBJECT)
        self.assertEqual(mismatch.exception.code, "provider_output_mode_unavailable")
        self.assertEqual(provider.generate_calls, 0)

    def test_registry_status_adds_only_safe_capability_metadata(self) -> None:
        provider = LegacyPromptProvider()
        registry = ProviderRegistry(
            {"openai": provider},
            disabled_provider_ids={"openai"},
        )

        status = registry.status()[0]

        self.assertTrue(status["policy_disabled"])
        self.assertIsNone(registry.get("openai"))
        self.assertEqual(status["output_capabilities"], {
            "version": OUTPUT_CAPABILITIES_VERSION,
            "modes": [OUTPUT_MODE_PROMPT_JSON],
            "preferred_mode": OUTPUT_MODE_PROMPT_JSON,
            "declared": False,
        })


class ProviderOutputDispatchTests(unittest.TestCase):
    def test_json_schema_dispatch_calls_only_the_schema_handler_once(self) -> None:
        class SchemaProvider:
            provider_id = "schema"

            def __init__(self) -> None:
                self.schema_calls = 0
                self.json_calls = 0
                self.prompt_calls = 0
                self.received: dict[str, Any] = {}

            @staticmethod
            def output_capabilities() -> ProviderOutputCapabilities:
                return ProviderOutputCapabilities(modes=OUTPUT_MODE_PRIORITY)

            def generate_json_schema(self, **request: Any) -> ProviderResponse:
                self.schema_calls += 1
                self.received = request
                request["json_schema"]["mutated"] = True
                return ProviderResponse(ok=True, provider="schema", content='{"ok":true}')

            def generate_json(self, **_request: Any) -> ProviderResponse:
                self.json_calls += 1
                raise AssertionError("must not fall back to json_object")

            def generate(self, **_request: Any) -> ProviderResponse:
                self.prompt_calls += 1
                raise AssertionError("must not fall back to prompt_json")

        provider = SchemaProvider()
        schema = {"type": "object", "additionalProperties": False}

        result = generate_turn_output(
            provider,
            instructions="Return the envelope.",
            input_text="discussion",
            json_schema=schema,
            schema_name="turn_envelope_v1",
        )

        self.assertEqual(result.mode, OUTPUT_MODE_JSON_SCHEMA)
        self.assertTrue(result.response.ok)
        self.assertEqual(provider.schema_calls, 1)
        self.assertEqual(provider.json_calls, 0)
        self.assertEqual(provider.prompt_calls, 0)
        self.assertEqual(schema, {"type": "object", "additionalProperties": False})
        self.assertEqual(provider.received["schema_name"], "turn_envelope_v1")

    def test_json_object_and_legacy_prompt_each_dispatch_once(self) -> None:
        class JsonObjectProvider:
            provider_id = "json"

            def __init__(self) -> None:
                self.json_calls = 0
                self.prompt_calls = 0

            @staticmethod
            def output_capabilities() -> ProviderOutputCapabilities:
                return ProviderOutputCapabilities(modes=(OUTPUT_MODE_JSON_OBJECT,))

            def generate_json(self, **_request: Any) -> ProviderResponse:
                self.json_calls += 1
                return ProviderResponse(ok=True, provider="json", content='{"ok":true}')

            def generate(self, **_request: Any) -> ProviderResponse:
                self.prompt_calls += 1
                raise AssertionError("must not fall back to prompt_json")

        structured = JsonObjectProvider()
        legacy = LegacyPromptProvider()

        json_result = generate_turn_output(
            structured,
            instructions="JSON",
            input_text="input",
        )
        prompt_result = generate_turn_output(
            legacy,
            instructions="JSON",
            input_text="input",
        )

        self.assertEqual(json_result.mode, OUTPUT_MODE_JSON_OBJECT)
        self.assertEqual(structured.json_calls, 1)
        self.assertEqual(structured.prompt_calls, 0)
        self.assertEqual(prompt_result.mode, OUTPUT_MODE_PROMPT_JSON)
        self.assertEqual(legacy.generate_calls, 1)

    def test_failed_selected_handler_is_not_retried_or_downgraded(self) -> None:
        class FailingProvider:
            provider_id = "failing"

            def __init__(self) -> None:
                self.json_calls = 0
                self.prompt_calls = 0

            @staticmethod
            def output_capabilities() -> ProviderOutputCapabilities:
                return ProviderOutputCapabilities(modes=(
                    OUTPUT_MODE_JSON_OBJECT,
                    OUTPUT_MODE_PROMPT_JSON,
                ))

            def generate_json(self, **_request: Any) -> ProviderResponse:
                self.json_calls += 1
                raise TimeoutError("fixture timeout")

            def generate(self, **_request: Any) -> ProviderResponse:
                self.prompt_calls += 1
                raise AssertionError("must not retry with prompt_json")

        provider = FailingProvider()

        with self.assertRaises(TimeoutError):
            generate_turn_output(
                provider,
                instructions="JSON",
                input_text="input",
            )

        self.assertEqual(provider.json_calls, 1)
        self.assertEqual(provider.prompt_calls, 0)

    def test_missing_handler_or_schema_fails_before_any_provider_call(self) -> None:
        class MissingHandlerProvider(LegacyPromptProvider):
            @staticmethod
            def output_capabilities() -> ProviderOutputCapabilities:
                return ProviderOutputCapabilities(modes=(OUTPUT_MODE_JSON_OBJECT,))

        class SchemaProvider(LegacyPromptProvider):
            @staticmethod
            def output_capabilities() -> ProviderOutputCapabilities:
                return ProviderOutputCapabilities(modes=(OUTPUT_MODE_JSON_SCHEMA,))

            def generate_json_schema(self, **_request: Any) -> ProviderResponse:
                raise AssertionError("missing schema must fail before this call")

        missing_handler = MissingHandlerProvider()
        with self.assertRaises(ProviderOutputCapabilityError) as handler_error:
            generate_turn_output(
                missing_handler,
                instructions="JSON",
                input_text="input",
            )
        self.assertEqual(handler_error.exception.code, "provider_output_handler_missing")
        self.assertEqual(missing_handler.generate_calls, 0)

        schema_provider = SchemaProvider()
        with self.assertRaises(ProviderOutputCapabilityError) as schema_error:
            generate_turn_output(
                schema_provider,
                instructions="JSON",
                input_text="input",
            )
        self.assertEqual(schema_error.exception.code, "provider_output_schema_required")
        self.assertEqual(schema_provider.generate_calls, 0)

    def test_real_adapters_use_mocked_transport_for_the_selected_mode(self) -> None:
        deepseek = DeepSeekProvider(
            api_key="fixture-deepseek-key",
            base_url="https://deepseek.invalid",
            default_model="deepseek-fixture",
        )
        doubao = DoubaoProvider(
            api_key="fixture-ark-key",
            base_url="https://ark.invalid/api/v3",
            default_model="doubao-fixture",
        )
        glm = GLMProvider(
            api_key="fixture-glm-key",
            base_url="https://glm.invalid/api/paas/v4",
            default_model="glm-fixture",
        )

        with patch(
            "urllib.request.urlopen",
            return_value=FakeHTTPResponse({
                "model": "deepseek-fixture",
                "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
            }),
        ) as urlopen:
            deepseek_result = generate_turn_output(
                deepseek,
                instructions="JSON",
                input_text="input",
            )
        deepseek_body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))

        with patch(
            "urllib.request.urlopen",
            return_value=FakeHTTPResponse({
                "model": "doubao-fixture",
                "status": "completed",
                "output_text": '{"ok":true}',
            }),
        ) as urlopen:
            doubao_result = generate_turn_output(
                doubao,
                instructions="JSON",
                input_text="input",
            )
        doubao_body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))

        with patch(
            "urllib.request.urlopen",
            return_value=FakeHTTPResponse({
                "model": "glm-fixture",
                "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
            }),
        ) as urlopen:
            glm_result = generate_turn_output(
                glm,
                instructions="JSON",
                input_text="input",
            )
        glm_body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))

        self.assertEqual(deepseek_result.mode, OUTPUT_MODE_JSON_OBJECT)
        self.assertEqual(deepseek_body["response_format"], {"type": "json_object"})
        self.assertEqual(doubao_result.mode, OUTPUT_MODE_JSON_OBJECT)
        self.assertEqual(doubao_body["text"], {"format": {"type": "json_object"}})
        self.assertEqual(glm_result.mode, OUTPUT_MODE_PROMPT_JSON)
        self.assertNotIn("response_format", glm_body)


if __name__ == "__main__":
    unittest.main()
