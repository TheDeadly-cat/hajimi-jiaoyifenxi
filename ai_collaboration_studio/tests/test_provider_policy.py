from __future__ import annotations

import os
import unittest
from unittest.mock import patch


# This module's isolated import must not inspect any local credential file, and
# it must not leave a process-wide flag behind for later discovery tests.
_previous_skip_local_env = os.environ.get("AI_STUDIO_SKIP_LOCAL_ENV")
os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
try:
    from backend.config import _deployment_disabled_provider_ids
    from backend.providers.base import ProviderProbeResult, ProviderResponse
    from backend.providers.registry import ProviderRegistry
finally:
    if _previous_skip_local_env is None:
        os.environ.pop("AI_STUDIO_SKIP_LOCAL_ENV", None)
    else:
        os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = _previous_skip_local_env


class RecordingProvider:
    provider_id = "openai"

    def __init__(self) -> None:
        self.probe_calls: list[str] = []

    def status(self) -> dict[str, object]:
        return {
            "id": self.provider_id,
            "name": "Test OpenAI",
            "configured": True,
            "model": "gpt-test",
            "api": "test",
        }

    def probe(self, *, model: str = "") -> ProviderProbeResult:
        self.probe_calls.append(model)
        return ProviderProbeResult(
            provider=self.provider_id,
            model=model or "gpt-test",
            configured=True,
            reachable=True,
            model_access=True,
            latency_ms=1,
        )

    def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
    ) -> ProviderResponse:
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model or "gpt-test",
            content="unused",
        )


class ProviderPolicyTests(unittest.TestCase):
    def test_deployment_disable_cannot_omit_openai_via_environment(self) -> None:
        for configured_value in ("", "deepseek", "DEEPSEEK,glm"):
            with self.subTest(configured_value=configured_value):
                with patch.dict(
                    os.environ,
                    {"AI_STUDIO_DISABLED_PROVIDERS": configured_value},
                ):
                    disabled = _deployment_disabled_provider_ids(
                        "AI_STUDIO_DISABLED_PROVIDERS",
                        "",
                    )

                self.assertIn("openai", disabled)

    def test_production_registry_defaults_to_openai_disabled(self) -> None:
        with patch(
            "backend.providers.registry.DISABLED_PROVIDER_IDS",
            frozenset({"openai"}),
        ):
            registry = ProviderRegistry()

        self.assertIsNone(registry.get("openai"))
        self.assertIsNotNone(registry.get("deepseek"))
        status = {item["id"]: item for item in registry.status()}
        self.assertTrue(status["openai"]["policy_disabled"])
        self.assertFalse(status["deepseek"]["policy_disabled"])
        self.assertTrue(registry.resolved_model("openai"))

    def test_production_registry_cannot_remove_openai_fixed_disable(self) -> None:
        provider = RecordingProvider()
        with (
            patch(
                "backend.providers.registry.DISABLED_PROVIDER_IDS",
                frozenset(),
            ),
            patch(
                "backend.providers.registry.OpenAIProvider",
                return_value=provider,
            ),
        ):
            registry = ProviderRegistry(disabled_provider_ids=set())

        self.assertIn("openai", registry.disabled_provider_ids)
        self.assertIsNone(registry.get("openai"))
        checks = registry.preflight(
            [{"provider": "openai", "model": "gpt-test"}],
            skip_provider_ids=set(),
            cache_ttl_seconds=0,
        )

        self.assertEqual(provider.probe_calls, [])
        self.assertEqual(checks[0]["error_code"], "PROVIDER_POLICY_DISABLED")

    def test_production_registry_preserves_configured_and_explicit_disables(
        self,
    ) -> None:
        with patch(
            "backend.providers.registry.DISABLED_PROVIDER_IDS",
            frozenset({"deepseek"}),
        ):
            registry = ProviderRegistry(disabled_provider_ids={"glm"})

        self.assertEqual(
            registry.disabled_provider_ids,
            frozenset({"openai", "deepseek", "glm"}),
        )

    def test_injected_registry_defaults_to_no_fixed_disables(self) -> None:
        provider = RecordingProvider()
        registry = ProviderRegistry({"openai": provider})

        self.assertIs(registry.get("openai"), provider)
        self.assertFalse(registry.status()[0]["policy_disabled"])
        checks = registry.preflight(
            [{"provider": "openai", "model": "gpt-test"}],
            skip_provider_ids=set(),
            cache_ttl_seconds=0,
        )

        self.assertTrue(checks[0]["ready"])
        self.assertEqual(provider.probe_calls, ["gpt-test"])

    def test_fixed_disable_blocks_execution_and_probe_but_keeps_metadata(self) -> None:
        provider = RecordingProvider()
        registry = ProviderRegistry(
            {"openai": provider},
            disabled_provider_ids={"OPENAI"},
        )

        self.assertIsNone(registry.get("openai"))
        self.assertEqual(registry.resolved_model("openai"), "gpt-test")
        self.assertTrue(registry.status()[0]["policy_disabled"])

        checks = registry.preflight(
            [{"provider": "openai", "model": "gpt-test"}],
            skip_provider_ids=set(),
            cache_ttl_seconds=0,
        )

        self.assertEqual(provider.probe_calls, [])
        self.assertFalse(checks[0]["ready"])
        self.assertEqual(checks[0]["error_code"], "PROVIDER_POLICY_DISABLED")
        self.assertIn("未发送网络请求", checks[0]["message"])


if __name__ == "__main__":
    unittest.main()
