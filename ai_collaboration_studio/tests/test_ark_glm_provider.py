from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from backend.providers.ark_glm_provider import ArkGLMProvider
from backend.providers.registry import ProviderRegistry
from backend.providers.output import OUTPUT_MODE_PROMPT_JSON
from scripts.pilot_password_dialog import acceptable_password


class ArkGLMRouteTests(unittest.TestCase):
    def test_platform_is_explicit_and_default_policy_remains_closed(self):
        native = ProviderRegistry(api_keys={"glm": "native-fixture"})
        ark = ProviderRegistry(glm_platform="volcengine_ark", api_keys={"glm": "ark-fixture"})
        self.assertEqual(native.get("glm").service_platform, "zhipu")
        self.assertEqual(ark.get("glm")._base_url, "https://ark.cn-beijing.volces.com/api/v3")
        self.assertEqual(ark.get("glm").status()["service_platform"], "volcengine_ark")
        self.assertIsNone(ark.get("openai"))
        for unknown in ("custom", "https://untrusted.example", None):
            with self.subTest(platform=unknown), self.assertRaises(ValueError):
                ProviderRegistry(glm_platform=unknown)
        with self.assertRaises(ValueError):
            ProviderRegistry({}, glm_platform="volcengine_ark")

    def test_ark_wire_contract_uses_fixed_endpoint_and_combined_output_cap(self):
        provider = ArkGLMProvider(api_key="ark-fixture")
        payload = {"id": "fixture-response", "model": "glm-5-2-260617", "status": "completed",
                   "output_text": '{"fixture":true}', "usage": {"input_tokens": 10, "output_tokens": 5}}
        with patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())) as transport:
            response = provider.generate(instructions="fixture", input_text="fixture", model="glm-5-2-260617", max_output_tokens=8192)
        request = transport.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "https://ark.cn-beijing.volces.com/api/v3/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer ark-fixture")
        self.assertEqual(body["max_output_tokens"], 8192)
        self.assertFalse(body["store"])
        self.assertNotIn("thinking", body)
        self.assertNotIn("text", body)
        self.assertEqual(transport.call_args.kwargs["timeout"], 240)
        self.assertEqual(provider.output_capabilities().modes, (OUTPUT_MODE_PROMPT_JSON,))
        self.assertTrue(response.ok)
        self.assertEqual(response.provider, "glm")
        self.assertEqual(response.model, "glm-5-2-260617")

    def test_no_probe_native_json_claim_or_other_model_call(self):
        provider = ArkGLMProvider(api_key="ark-fixture")
        with patch("urllib.request.OpenerDirector.open") as transport:
            self.assertFalse(provider.probe(model="glm-5-2-260617").reachable)
            self.assertFalse(provider.generate_json(instructions="fixture", input_text="fixture", model="glm-5-2-260617").ok)
            self.assertFalse(provider.generate(instructions="fixture", input_text="fixture", model="gpt-other").ok)
            self.assertFalse(provider.generate(instructions="fixture", input_text="fixture", model="glm-5.2").ok)
        transport.assert_not_called()

    def test_password_input_rejects_control_keys_and_multiple_lines(self):
        self.assertTrue(acceptable_password("synthetic-key-only"))
        for value in ("", "fixture\x16", "one\ntwo", "one two", "x" * 4097):
            with self.subTest(case=len(value)):
                self.assertFalse(acceptable_password(value))
