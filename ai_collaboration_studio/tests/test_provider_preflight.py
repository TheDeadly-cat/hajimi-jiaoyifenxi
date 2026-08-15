from __future__ import annotations

import copy
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from unittest.mock import patch

from backend import http_server
from backend.orchestrator import DiscussionOrchestrator
from backend.provider_call_ledger import ProviderCallLedger
from backend.provider_preflight import ProviderPreflightService
from backend.providers.base import ProviderProbeResult
from backend.providers.deepseek_provider import DeepSeekProvider
from backend.providers.doubao_provider import DoubaoProvider
from backend.providers.glm_provider import GLMProvider
from backend.providers.registry import ProviderRegistry
from backend.round_launch_plan import RoundLaunchPlanService
from backend.round_launch_plan import (
    ROUND_LAUNCH_PLAN_VERSION_V5,
    _canonical_sha256,
)
from backend.round_contexts import (
    build_round_context_authorization_set,
    round_context_authorization_entry,
)
from backend.store import StudioStore


class ReadyHTTPResponse:
    def __init__(self, payload: dict[str, object] | bytes) -> None:
        self.raw = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

    def __enter__(self) -> "ReadyHTTPResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        return self.raw if amount < 0 else self.raw[:amount]


class StubProvider:
    def __init__(
        self,
        provider_id: str,
        *,
        configured: bool = True,
        model: str = "shared-model",
        ready: bool = True,
        secret: str = "",
    ) -> None:
        self.provider_id = provider_id
        self.configured = configured
        self.model = model
        self.ready = ready
        self.secret = secret
        self.probe_calls: list[str] = []

    def status(self) -> dict[str, object]:
        return {
            "id": self.provider_id,
            "name": self.provider_id.title(),
            "configured": self.configured,
            "model": self.model,
            "api": "test",
        }

    def probe(self, *, model: str = "") -> ProviderProbeResult:
        self.probe_calls.append(model)
        if self.secret:
            raise RuntimeError(self.secret)
        return ProviderProbeResult(
            provider=self.provider_id,
            model=model or self.model,
            configured=self.configured,
            reachable=self.ready,
            model_access=self.ready,
            latency_ms=7,
            error_code="" if self.ready else "provider_unavailable",
            message="连接与模型访问正常。" if self.ready else "服务暂时不可用。",
        )

    def generate(self, *, instructions: str, input_text: str, model: str = ""):
        raise AssertionError("会前检查不应调用 generate")


class OfflineMarketService:
    def __init__(self) -> None:
        self.calls = 0

    def snapshot(self) -> dict[str, object]:
        self.calls += 1
        raise RuntimeError("Futu OpenD offline")


class ProviderProbeAdapterTests(unittest.TestCase):
    def test_deepseek_glm_and_doubao_have_minimal_live_probe_requests(self) -> None:
        providers = [
            DeepSeekProvider(
                api_key="fake-deepseek",
                base_url="https://deepseek.example/v1",
                default_model="deepseek-test",
            ),
            GLMProvider(
                api_key="fake-glm",
                base_url="https://glm.example/v4",
                default_model="glm-test",
            ),
            DoubaoProvider(
                api_key="fake-ark",
                base_url="https://ark.example/api/v3",
                default_model="doubao-test",
            ),
        ]

        with patch(
            "backend.providers.probe.urllib.request.urlopen",
            side_effect=[
                ReadyHTTPResponse({
                    "choices": [{
                        "finish_reason": "length",
                        "message": {
                            "content": "",
                            "reasoning_content": "READY deepseek-body-secret",
                        },
                    }],
                }),
                ReadyHTTPResponse({
                    "choices": [{"message": {"content": "READY glm-body-secret"}}],
                }),
                ReadyHTTPResponse({"output_text": "READY doubao-body-secret"}),
            ],
        ) as mocked_urlopen:
            results = [provider.probe() for provider in providers]

        self.assertTrue(all(result.ready for result in results))
        serialized = json.dumps(
            [result.as_dict() for result in results],
            ensure_ascii=False,
        )
        self.assertNotIn("body-secret", serialized)
        self.assertEqual(mocked_urlopen.call_count, 3)
        requests = [call.args[0] for call in mocked_urlopen.call_args_list]
        self.assertTrue(requests[0].full_url.endswith("/chat/completions"))
        self.assertTrue(requests[1].full_url.endswith("/chat/completions"))
        self.assertTrue(requests[2].full_url.endswith("/responses"))
        for request in requests:
            body = json.loads(request.data.decode("utf-8"))
            self.assertEqual(body.get("max_tokens") or body.get("max_output_tokens"), 4)

    def test_chat_probe_rejects_empty_invalid_and_textless_success_bodies(self) -> None:
        providers = [
            DeepSeekProvider(
                api_key="fake-deepseek",
                base_url="https://deepseek.example/v1",
                default_model="deepseek-test",
            ),
            GLMProvider(
                api_key="fake-glm",
                base_url="https://glm.example/v4",
                default_model="glm-test",
            ),
        ]
        cases = [
            (b"", "empty_response"),
            (b"not-json upstream-body-secret", "invalid_response"),
            (
                json.dumps({
                    "choices": [{"message": {"content": "   "}}],
                    "debug": "upstream-body-secret",
                }).encode("utf-8"),
                "empty_response",
            ),
        ]

        for provider in providers:
            for raw, expected_code in cases:
                with self.subTest(provider=provider.provider_id, error_code=expected_code):
                    with patch(
                        "backend.providers.probe.urllib.request.urlopen",
                        return_value=ReadyHTTPResponse(raw),
                    ):
                        result = provider.probe()

                    serialized = json.dumps(result.as_dict(), ensure_ascii=False)
                    self.assertFalse(result.ready)
                    self.assertTrue(result.reachable)
                    self.assertFalse(result.model_access)
                    self.assertEqual(result.error_code, expected_code)
                    self.assertNotIn("upstream-body-secret", serialized)

    def test_doubao_probe_rejects_empty_invalid_and_textless_success_bodies(self) -> None:
        provider = DoubaoProvider(
            api_key="fake-ark",
            base_url="https://ark.example/api/v3",
            default_model="doubao-test",
        )
        cases = [
            (b"", "empty_response"),
            (b"not-json upstream-body-secret", "invalid_response"),
            (
                json.dumps({
                    "output_text": "   ",
                    "debug": "upstream-body-secret",
                }).encode("utf-8"),
                "empty_response",
            ),
            (
                json.dumps({
                    "status": "incomplete",
                    "output_text": "upstream-body-secret",
                }).encode("utf-8"),
                "invalid_response",
            ),
        ]

        for raw, expected_code in cases:
            with self.subTest(error_code=expected_code):
                with patch(
                    "backend.providers.probe.urllib.request.urlopen",
                    return_value=ReadyHTTPResponse(raw),
                ):
                    result = provider.probe()

                serialized = json.dumps(result.as_dict(), ensure_ascii=False)
                self.assertFalse(result.ready)
                self.assertTrue(result.reachable)
                self.assertFalse(result.model_access)
                self.assertEqual(result.error_code, expected_code)
                self.assertNotIn("upstream-body-secret", serialized)

    def test_probe_http_error_is_safely_mapped_without_secret_or_body(self) -> None:
        secret = "secret-value-that-must-not-leak"
        error_body = json.dumps({
            "error": {
                "code": "invalid_api_key",
                "message": f"Authorization failed for {secret}",
            }
        }).encode("utf-8")
        http_error = urllib.error.HTTPError(
            "https://deepseek.example/v1/chat/completions",
            401,
            "Unauthorized",
            {},
            io.BytesIO(error_body),
        )
        provider = DeepSeekProvider(
            api_key=secret,
            base_url="https://deepseek.example/v1",
            default_model="deepseek-test",
        )

        with patch(
            "backend.providers.probe.urllib.request.urlopen",
            side_effect=http_error,
        ):
            result = provider.probe()
        http_error.close()

        serialized = json.dumps(result.as_dict(), ensure_ascii=False)
        self.assertEqual(result.error_code, "authentication_or_model_access_denied")
        self.assertTrue(result.reachable)
        self.assertFalse(result.model_access)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("Authorization failed", serialized)
        self.assertNotIn("Bearer", serialized)


class ProviderPreflightServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "preflight.sqlite3")
        self.ledger_sequence = 0

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def assign_room(self, provider_id: str, model: str) -> list[dict[str, object]]:
        snapshot = self.store.room_snapshot("room_plan")
        self.assertIsNotNone(snapshot)
        members = list((snapshot or {}).get("members") or [])
        for member in members:
            self.store.update_member(
                "room_plan",
                str(member["id"]),
                {"provider": provider_id, "model": model},
            )
        return members

    def create_ledger(self, *, max_calls: int = 10) -> ProviderCallLedger:
        self.ledger_sequence += 1
        request_id = f"preflight-ledger-{self.ledger_sequence}"
        return ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="provider_preflight",
            client_request_id=request_id,
            plan={"test_request_id": request_id},
            max_calls=max_calls,
        )

    def create_launch_ledger(
        self,
        launch_plan: dict[str, object],
        *,
        scope: str = "round",
        skip_provider_ids: set[str] | None = None,
        max_calls: int | None = None,
    ) -> ProviderCallLedger:
        self.ledger_sequence += 1
        request_id = f"launch-preflight-ledger-{self.ledger_sequence}"
        calls = launch_plan.get("calls")
        recommended_calls = (
            int(calls.get("recommended_provider_calls") or 1)
            if isinstance(calls, dict)
            else 1
        )
        plan_skip_ids = launch_plan.get("skip_provider_ids")
        persisted_skip_ids = (
            set(str(item) for item in plan_skip_ids)
            if skip_provider_ids is None and isinstance(plan_skip_ids, list)
            else set(skip_provider_ids or set())
        )
        member_routes = http_server.StudioRequestHandler._launch_plan_member_routes(
            launch_plan  # type: ignore[arg-type]
        )
        return ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope=scope,
            client_request_id=request_id,
            plan_hash=str(launch_plan["plan_hash"]),
            max_calls=max_calls or recommended_calls,
            skip_provider_ids=persisted_skip_ids,
            member_routes=member_routes,
        )

    def test_launch_plan_preflight_uses_only_frozen_routes_after_hot_edit(self) -> None:
        self.assign_room("deepseek", "frozen-model")
        deepseek = StubProvider("deepseek", model="frozen-model")
        glm = StubProvider("glm", model="hot-edit-model")
        registry = ProviderRegistry({"deepseek": deepseek, "glm": glm})
        plan = RoundLaunchPlanService(self.store, registry).build(
            "room_plan",
            "Probe the confirmed frozen launch plan",
            set(),
        )
        self.assertTrue(plan["ready_for_authorization"])
        first_ledger = self.create_launch_ledger(plan)

        self.assign_room("glm", "hot-edit-model")
        service = ProviderPreflightService(self.store, registry)
        second_ledger = self.create_launch_ledger(plan)
        with patch.object(
            self.store,
            "room_snapshot",
            side_effect=AssertionError("launch-plan preflight reread current routes"),
        ):
            first = service.check_launch_plan(
                "room_plan",
                launch_plan=plan,
                skip_provider_ids=set(),
                ledger=first_ledger,
            )
            second = service.check_launch_plan(
                "room_plan",
                launch_plan=plan,
                skip_provider_ids=set(),
                ledger=second_ledger,
            )

        self.assertTrue(first["ready"])
        self.assertTrue(second["ready"])
        self.assertEqual(first["plan_hash"], plan["plan_hash"])
        self.assertEqual(first["route_source"], "launch_plan")
        self.assertEqual(deepseek.probe_calls, ["frozen-model", "frozen-model"])
        self.assertEqual(glm.probe_calls, [])
        self.assertEqual(
            {(member["provider"], member["model"]) for member in first["members"]},
            {("deepseek", "frozen-model")},
        )
        self.assertEqual(first["moderator"]["route_source"], "launch_plan")
        for ledger in (first_ledger, second_ledger):
            attempts = ledger.attempts()
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["kind"], "preflight_probe")
            self.assertEqual(attempts[0]["status"], "RESPONDED")

    def test_v5_validation_is_generic_canonical_closed_and_hash_sealed(self) -> None:
        self.assign_room("deepseek", "frozen-model")
        registry = ProviderRegistry({
            "deepseek": StubProvider("deepseek", model="frozen-model"),
        })
        plan = RoundLaunchPlanService(self.store, registry).build(
            "room_plan",
            "Validate a generic frozen context set.",
            set(),
        )
        contexts = build_round_context_authorization_set([
            round_context_authorization_entry(
                "alpha_context",
                "core.alpha.context/v1",
                {"version": "opaque_alpha_v1", "user_confirmed": True},
            ),
            round_context_authorization_entry(
                "zeta_context",
                "core.zeta.context/v1",
                {"version": "opaque_zeta_v1", "seal": "a" * 64},
            ),
        ])

        def as_v5(
            source: dict[str, object],
            authorization_set: dict[str, object],
        ) -> dict[str, object]:
            frozen = copy.deepcopy(source)
            frozen["version"] = ROUND_LAUNCH_PLAN_VERSION_V5
            frozen["round_context_authorizations"] = copy.deepcopy(
                authorization_set
            )
            moderator = dict(frozen["moderator"])  # type: ignore[arg-type]
            selection_source = moderator.pop("selection_source")
            room = dict(frozen["room"])  # type: ignore[arg-type]
            room.pop("settings_version")
            frozen["plan_hash"] = _canonical_sha256({
                "version": frozen["version"],
                "objective": frozen["objective"],
                "room": room,
                "members": frozen["members"],
                "moderator": moderator,
                "moderator_selection_source": selection_source,
                "skip_provider_ids": frozen["skip_provider_ids"],
                "preflight_routes": frozen["preflight_routes"],
                "provider_call_projection": frozen["provider_call_projection"],
                "calls": frozen["calls"],
                "safety": frozen["safety"],
                "round_context_authorizations": authorization_set,
            })
            return frozen

        v5_plan = as_v5(plan, contexts)
        ProviderPreflightService._validate_launch_plan("room_plan", v5_plan)

        reordered = copy.deepcopy(contexts)
        reordered["contexts"].reverse()  # type: ignore[union-attr]
        noncanonical = as_v5(plan, reordered)
        with self.assertRaisesRegex(ValueError, "not canonical"):
            ProviderPreflightService._validate_launch_plan(
                "room_plan", noncanonical
            )

        tampered = copy.deepcopy(v5_plan)
        tampered["round_context_authorizations"]["contexts"][0]["request"] = {
            "version": "tampered"
        }
        with self.assertRaisesRegex(ValueError, "hash"):
            ProviderPreflightService._validate_launch_plan("room_plan", tampered)

        domain_field = copy.deepcopy(v5_plan)
        domain_field["project_round_focus_authorization"] = {}
        with self.assertRaisesRegex(ValueError, "closed shape"):
            ProviderPreflightService._validate_launch_plan(
                "room_plan", domain_field
            )

    def test_launch_plan_preflight_rejects_plan_room_and_skip_drift_before_probe(self) -> None:
        self.assign_room("deepseek", "frozen-model")
        provider = StubProvider("deepseek", model="frozen-model")
        registry = ProviderRegistry({"deepseek": provider})
        plan = RoundLaunchPlanService(self.store, registry).build(
            "room_plan",
            "Reject every unconfirmed launch-plan mutation",
            set(),
        )
        service = ProviderPreflightService(self.store, registry)

        matching_ledger = self.create_launch_ledger(plan)
        with self.assertRaisesRegex(ValueError, "skip policy"):
            service.check_launch_plan(
                "room_plan",
                launch_plan=plan,
                skip_provider_ids={"openai"},
                ledger=matching_ledger,
            )

        with self.assertRaisesRegex(ValueError, "belong"):
            service.check_launch_plan(
                "room_storage",
                launch_plan=plan,
                skip_provider_ids=set(),
                ledger=matching_ledger,
            )

        tampered_plan = copy.deepcopy(plan)
        tampered_plan["objective"] = "Tampered after confirmation"
        with self.assertRaisesRegex(ValueError, "hash"):
            service.check_launch_plan(
                "room_plan",
                launch_plan=tampered_plan,
                skip_provider_ids=set(),
                ledger=matching_ledger,
            )

        malformed_plan = copy.deepcopy(plan)
        malformed_plan["preflight_routes"][0]["model"] = 123
        with self.assertRaisesRegex(ValueError, "route"):
            service.check_launch_plan(
                "room_plan",
                launch_plan=malformed_plan,
                skip_provider_ids=set(),
                ledger=matching_ledger,
            )

        wrong_skip_ledger = self.create_launch_ledger(
            plan,
            skip_provider_ids={"openai"},
        )
        with self.assertRaisesRegex(ValueError, "ledger skip policy"):
            service.check_launch_plan(
                "room_plan",
                launch_plan=plan,
                skip_provider_ids=set(),
                ledger=wrong_skip_ledger,
            )

        wrong_scope_ledger = self.create_launch_ledger(
            plan,
            scope="provider_preflight",
        )
        with self.assertRaisesRegex(ValueError, "unbound round ledger"):
            service.check_launch_plan(
                "room_plan",
                launch_plan=plan,
                skip_provider_ids=set(),
                ledger=wrong_scope_ledger,
            )

        self.assertEqual(provider.probe_calls, [])
        for ledger in (matching_ledger, wrong_skip_ledger, wrong_scope_ledger):
            self.assertEqual(ledger.attempts(), [])

    def test_launch_plan_preflight_returns_only_sanitized_provider_fields(self) -> None:
        self.assign_room("deepseek", "frozen-model")
        provider = StubProvider("deepseek", model="frozen-model")
        registry = ProviderRegistry({"deepseek": provider})
        plan = RoundLaunchPlanService(self.store, registry).build(
            "room_plan",
            "Sanitize formal preflight output",
            set(),
        )
        ledger = self.create_launch_ledger(plan)
        unsafe_secret = "sk-proj-unsafe-provider-secret"
        unsafe_check = {
            "provider": "deepseek",
            "model": "frozen-model",
            "configured": False,
            "reachable": True,
            "model_access": True,
            "ready": True,
            "latency_ms": 7,
            "error_code": unsafe_secret,
            "message": f"Bearer {unsafe_secret}",
            "raw_response": unsafe_secret,
        }

        with patch.object(registry, "preflight", return_value=[unsafe_check]):
            result = ProviderPreflightService(
                self.store,
                registry,
            ).check_launch_plan(
                "room_plan",
                launch_plan=plan,
                skip_provider_ids=set(),
                ledger=ledger,
            )

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertFalse(result["ready"])
        self.assertFalse(result["provider_checks"][0]["ready"])
        self.assertEqual(
            result["provider_checks"][0]["error_code"],
            "provider_preflight_failed",
        )
        self.assertNotIn(unsafe_secret, serialized)
        self.assertNotIn("Bearer", serialized)
        self.assertNotIn("raw_response", serialized)

    def test_unique_provider_model_pairs_are_probed_once_and_mapped_to_members(self) -> None:
        members = self.assign_room("deepseek", "shared-model")
        provider = StubProvider("deepseek")
        service = ProviderPreflightService(
            self.store,
            ProviderRegistry({"deepseek": provider}),
        )
        ledger = self.create_ledger(max_calls=1)

        result = service.check_room("room_plan", ledger=ledger)

        self.assertTrue(result["ready"])
        self.assertEqual(result["provider_check_count"], 1)
        self.assertEqual(provider.probe_calls, ["shared-model"])
        self.assertEqual(result["provider_checks"][0]["member_count"], len(members))
        self.assertTrue(result["moderator"]["available"])
        self.assertTrue(result["moderator"]["is_moderator"])
        self.assertEqual(result["unavailable_members"], [])
        attempts = ledger.attempts()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["kind"], "preflight_probe")
        self.assertEqual(attempts[0]["status"], "RESPONDED")

    def test_missing_configuration_blocks_moderator_and_members_without_probe(self) -> None:
        members = self.assign_room("glm", "glm-test")
        provider = StubProvider("glm", configured=False, model="glm-test")
        service = ProviderPreflightService(
            self.store,
            ProviderRegistry({"glm": provider}),
        )

        result = service.check_room("room_plan")

        self.assertFalse(result["ready"])
        self.assertEqual(provider.probe_calls, [])
        self.assertTrue(result["blocking"]["moderator_unavailable"])
        self.assertEqual(result["blocking"]["unavailable_member_count"], len(members))
        self.assertEqual(
            {member["error_code"] for member in result["unavailable_members"]},
            {"not_configured"},
        )

    def test_explicit_moderator_must_be_in_selected_round_members(self) -> None:
        members = self.assign_room("deepseek", "shared-model")
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "moderator_member_id": members[1]["id"],
        })
        provider = StubProvider("deepseek")
        service = ProviderPreflightService(
            self.store,
            ProviderRegistry({"deepseek": provider}),
        )

        result = service.check_room(
            "room_plan",
            member_ids=[members[0]["id"]],
        )

        self.assertFalse(result["ready"])
        self.assertTrue(result["blocking"]["moderator_unavailable"])
        self.assertEqual(result["moderator"]["id"], members[1]["id"])
        self.assertEqual(result["moderator"]["error_code"], "moderator_not_selected")

    def test_skipped_provider_never_probes_and_is_explicitly_unavailable(self) -> None:
        self.assign_room("openai", "gpt-test")
        provider = StubProvider("openai", model="gpt-test")
        service = ProviderPreflightService(
            self.store,
            ProviderRegistry({"openai": provider}),
        )

        result = service.check_room(
            "room_plan",
            skip_provider_ids={"openai"},
        )

        self.assertFalse(result["ready"])
        self.assertEqual(provider.probe_calls, [])
        self.assertEqual(
            result["provider_checks"][0]["error_code"],
            "PROVIDER_SKIPPED",
        )
        self.assertFalse(result["provider_checks"][0]["reachable"])

    def test_short_ttl_cache_reuses_explicit_preflight_result(self) -> None:
        self.assign_room("deepseek", "shared-model")
        provider = StubProvider("deepseek")
        service = ProviderPreflightService(
            self.store,
            ProviderRegistry({"deepseek": provider}),
        )

        first = service.check_room("room_plan")
        second = service.check_room("room_plan")

        self.assertTrue(first["ready"])
        self.assertTrue(second["ready"])
        self.assertEqual(provider.probe_calls, ["shared-model"])
        self.assertFalse(first["provider_checks"][0]["cached"])
        self.assertTrue(second["provider_checks"][0]["cached"])

    def test_ledger_spends_only_for_cache_miss_immediately_before_probe(self) -> None:
        ready = StubProvider("deepseek", model="ready-model")
        unconfigured = StubProvider("glm", configured=False, model="glm-model")
        skipped = StubProvider("openai", model="gpt-model")
        disabled = StubProvider("doubao", model="doubao-model")
        registry = ProviderRegistry(
            {
                "deepseek": ready,
                "glm": unconfigured,
                "openai": skipped,
                "doubao": disabled,
            },
            disabled_provider_ids={"doubao"},
        )
        ledger = self.create_ledger(max_calls=10)

        local_only_checks = registry.preflight(
            [
                {"provider": "unsupported", "model": "missing-model"},
                {"provider": "glm", "model": "glm-model"},
                {"provider": "openai", "model": "gpt-model"},
                {"provider": "doubao", "model": "doubao-model"},
            ],
            skip_provider_ids={"openai"},
            ledger=ledger,
        )
        self.assertEqual(len(local_only_checks), 4)
        self.assertEqual(ledger.snapshot()["reserved_calls"], 0)
        self.assertEqual(ledger.attempts(), [])
        self.assertEqual(unconfigured.probe_calls, [])
        self.assertEqual(skipped.probe_calls, [])
        self.assertEqual(disabled.probe_calls, [])

        first = registry.preflight(
            [{"provider": "deepseek", "model": ""}],
            ledger=ledger,
        )
        second = registry.preflight(
            [{"provider": "deepseek", "model": ""}],
            ledger=ledger,
        )
        self.assertFalse(first[0]["cached"])
        self.assertTrue(second[0]["cached"])
        self.assertEqual(ready.probe_calls, ["ready-model"])
        self.assertEqual(ledger.snapshot()["reserved_calls"], 1)
        self.assertEqual(len(ledger.attempts()), 1)
        self.assertEqual(ledger.attempts()[0]["model"], "ready-model")

    def test_ledger_terminalizes_ready_failed_and_exception_probes_safely(self) -> None:
        secret = "upstream-probe-secret-must-not-leak"
        ready = StubProvider("deepseek", model="ready-model", ready=True)
        failed = StubProvider("glm", model="failed-model", ready=False)
        exceptional = StubProvider("doubao", model="exception-model", secret=secret)
        registry = ProviderRegistry({
            "deepseek": ready,
            "glm": failed,
            "doubao": exceptional,
        })
        ledger = self.create_ledger(max_calls=3)

        checks = registry.preflight(
            [
                {"provider": "deepseek", "model": "ready-model"},
                {"provider": "glm", "model": "failed-model"},
                {"provider": "doubao", "model": "exception-model"},
            ],
            cache_ttl_seconds=0,
            ledger=ledger,
        )

        self.assertEqual([check["ready"] for check in checks], [True, False, False])
        self.assertEqual(checks[2]["error_code"], "probe_failed")
        attempts = ledger.attempts()
        self.assertEqual(
            [(item["provider"], item["model"], item["status"]) for item in attempts],
            [
                ("deepseek", "ready-model", "RESPONDED"),
                ("glm", "failed-model", "FAILED"),
                ("doubao", "exception-model", "FAILED"),
            ],
        )
        self.assertTrue(all(item["kind"] == "preflight_probe" for item in attempts))
        self.assertEqual(attempts[1]["error_code"], "provider_unavailable")
        self.assertEqual(attempts[2]["error_code"], "probe_failed")
        self.assertEqual(ledger.snapshot()["reserved_calls"], 3)
        self.assertEqual(ledger.snapshot()["completed_calls"], 3)
        self.assertEqual(ledger.snapshot()["status"], "COMPLETED")
        serialized = json.dumps(
            {"checks": checks, "attempts": attempts},
            ensure_ascii=False,
        )
        self.assertNotIn(secret, serialized)

    def test_exhausted_ledger_returns_safe_check_without_probe(self) -> None:
        provider = StubProvider("deepseek", model="blocked-model")
        registry = ProviderRegistry({"deepseek": provider})
        ledger = self.create_ledger(max_calls=1)
        spent = ledger.reserve(
            kind="preflight_probe",
            provider="deepseek",
            model="earlier-model",
        )
        ledger.finish(
            str(spent["id"]),
            str(spent["attempt_token"]),
            status="CANCELLED",
        )

        checks = registry.preflight(
            [{"provider": "deepseek", "model": "blocked-model"}],
            cache_ttl_seconds=0,
            ledger=ledger,
        )

        self.assertEqual(provider.probe_calls, [])
        self.assertEqual(len(checks), 1)
        self.assertFalse(checks[0]["ready"])
        self.assertFalse(checks[0]["cached"])
        self.assertEqual(
            checks[0]["error_code"],
            "PROVIDER_CALL_BUDGET_EXCEEDED",
        )
        self.assertEqual(len(ledger.attempts()), 1)
        self.assertNotIn("provider_call_budget_exhausted", checks[0]["message"].lower())

    def test_resume_probes_frozen_moderator_route_not_current_room_moderator(self) -> None:
        members = self.assign_room("openai", "speaker-model")
        frozen_moderator = self.store.update_member(
            "room_plan",
            members[0]["id"],
            {"provider": "deepseek", "model": "frozen-director-model"},
        )
        self.store.update_member(
            "room_plan",
            frozen_moderator["id"],
            {"provider": "doubao", "model": "next-round-speaker-model"},
        )
        unrelated_moderator = self.store.add_member("room_plan", {
            "name": "下一轮主持",
            "identity": "只影响下一轮的主持身份",
            "responsibilities": "验证当前房间设置不会覆盖暂停轮次。",
            "boundaries": "不参与当前暂停轮次。",
            "provider": "broken",
            "model": "must-not-probe",
        })
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "moderator_member_id": unrelated_moderator["id"],
        })
        deepseek = StubProvider("deepseek", model="frozen-director-model")
        doubao = StubProvider("doubao", model="next-round-speaker-model")
        openai = StubProvider("openai", model="speaker-model")
        service = ProviderPreflightService(
            self.store,
            ProviderRegistry({
                "deepseek": deepseek,
                "doubao": doubao,
                "openai": openai,
            }),
        )
        ledger = self.create_ledger(max_calls=3)

        result = service.check_resume_round(
            "room_plan",
            checkpoint_state={
                "version": 7,
                "member_ids": [member["id"] for member in members],
                "discussion_mode": "dynamic",
                "domain": "open_collaboration",
                "moderator_member_id": frozen_moderator["id"],
                "moderator_member_version": frozen_moderator["version"],
                "moderator_provider": "deepseek",
                "moderator_model": "frozen-director-model",
            },
            member_ids=[member["id"] for member in members],
            ledger=ledger,
        )

        self.assertTrue(result["ready"])
        self.assertEqual(result["context"], "round_resume")
        self.assertEqual(result["moderator"]["id"], frozen_moderator["id"])
        self.assertEqual(result["moderator"]["provider"], "deepseek")
        self.assertEqual(
            result["moderator"]["route_source"],
            "frozen_checkpoint",
        )
        self.assertEqual(deepseek.probe_calls, ["frozen-director-model"])
        self.assertEqual(doubao.probe_calls, ["next-round-speaker-model"])
        self.assertEqual(openai.probe_calls, ["speaker-model"])
        self.assertEqual(
            {
                (attempt["provider"], attempt["model"], attempt["status"])
                for attempt in ledger.attempts()
            },
            {
                ("deepseek", "frozen-director-model", "RESPONDED"),
                ("doubao", "next-round-speaker-model", "RESPONDED"),
                ("openai", "speaker-model", "RESPONDED"),
            },
        )
        self.assertEqual(ledger.snapshot()["reserved_calls"], 3)
        self.assertNotIn(
            "must-not-probe",
            [
                check["model"]
                for check in result["provider_checks"]
            ],
        )

    def test_resume_with_formal_ledger_probes_approved_member_routes_after_hot_edit(self) -> None:
        members = self.assign_room("openai", "speaker-model")
        frozen_moderator = self.store.update_member(
            "room_plan",
            members[0]["id"],
            {"provider": "deepseek", "model": "frozen-director-model"},
        )
        approved_members = self.store.enabled_members("room_plan")
        member_routes = {
            "version": "provider_member_routes_v1",
            "members": sorted(
                [
                    {
                        "member_id": str(member["id"]),
                        "approved_member_version": int(member["version"]),
                        "provider": str(member["provider"]),
                        "model": str(member["model"]),
                    }
                    for member in approved_members
                ],
                key=lambda item: item["member_id"],
            ),
        }
        self.store.update_member(
            "room_plan",
            frozen_moderator["id"],
            {
                "identity": "恢复时读取的新身份",
                "provider": "doubao",
                "model": "next-round-moderator-model",
            },
        )
        self.store.update_member(
            "room_plan",
            members[1]["id"],
            {"provider": "glm", "model": "next-round-speaker-model"},
        )
        deepseek = StubProvider("deepseek", model="frozen-director-model")
        openai = StubProvider("openai", model="speaker-model")
        doubao = StubProvider("doubao", model="next-round-moderator-model")
        glm = StubProvider("glm", model="next-round-speaker-model")
        service = ProviderPreflightService(
            self.store,
            ProviderRegistry({
                "deepseek": deepseek,
                "openai": openai,
                "doubao": doubao,
                "glm": glm,
            }),
        )
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="resume-approved-member-routes",
            plan_hash="6" * 64,
            max_calls=3,
            skip_provider_ids=[],
            member_routes=member_routes,
        )

        result = service.check_resume_round(
            "room_plan",
            checkpoint_state={
                "version": 7,
                "member_ids": [member["id"] for member in members],
                "discussion_mode": "dynamic",
                "domain": "open_collaboration",
                "moderator_member_id": frozen_moderator["id"],
                "moderator_member_version": frozen_moderator["version"],
                "moderator_provider": "deepseek",
                "moderator_model": "frozen-director-model",
            },
            member_ids=[member["id"] for member in members],
            ledger=ledger,
        )

        self.assertTrue(result["ready"])
        self.assertEqual(deepseek.probe_calls, ["frozen-director-model"])
        self.assertEqual(openai.probe_calls, ["speaker-model"])
        self.assertEqual(doubao.probe_calls, [])
        self.assertEqual(glm.probe_calls, [])
        self.assertTrue(all(
            member["route_source"] == "approved_round_ledger"
            for member in result["members"]
        ))
        self.assertEqual(
            result["moderator"]["route_source"],
            "frozen_checkpoint_and_round_ledger",
        )
        self.assertEqual(
            {(attempt["provider"], attempt["model"]) for attempt in ledger.attempts()},
            {
                ("deepseek", "frozen-director-model"),
                ("openai", "speaker-model"),
            },
        )


class ProviderPreflightHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_store = http_server.STORE
        self.original_providers = http_server.PROVIDERS
        self.original_orchestrator = http_server.ORCHESTRATOR
        http_server.STORE = StudioStore(Path(self.temp_dir.name) / "http-preflight.sqlite3")
        for member in http_server.STORE.room_snapshot("room_plan")["members"]:
            http_server.STORE.update_member(
                "room_plan",
                str(member["id"]),
                {"provider": "openai", "model": "gpt-test"},
            )
        self.provider = StubProvider("openai", model="gpt-test")
        http_server.PROVIDERS = ProviderRegistry({"openai": self.provider})
        self.market_service = OfflineMarketService()
        http_server.ORCHESTRATOR = DiscussionOrchestrator(
            http_server.STORE,
            http_server.PROVIDERS,
            self.market_service,
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.round_request_sequence = 0

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        http_server.PROVIDERS = self.original_providers
        http_server.ORCHESTRATOR = self.original_orchestrator
        self.temp_dir.cleanup()

    def post_preflight(self, payload: dict[str, object], *, token: str = "") -> tuple[int, dict[str, object]]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-AI-Studio-Token"] = token
        request = Request(
            f"{self.base_url}/api/rooms/room_plan/providers/preflight",
            method="POST",
            headers=headers,
            data=json.dumps(payload).encode("utf-8"),
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def post_round(
        self,
        payload: dict[str, object],
        *,
        room_id: str = "room_plan",
    ) -> tuple[int, list[dict[str, object]]]:
        request_payload = dict(payload)
        if (
            "member_ids" not in request_payload
            and "client_round_request_id" not in request_payload
        ):
            plan_status, plan_payload = self.post_plan(
                room_id,
                {
                    "objective": request_payload.get("objective")
                    or request_payload.get("content")
                    or "",
                    "skip_providers": request_payload.get("skip_providers", ["openai"]),
                },
            )
            if plan_status != 200:
                return plan_status, [plan_payload]
            plan = plan_payload["plan"]
            self.round_request_sequence += 1
            request_payload.update({
                "client_round_request_id": (
                    f"provider-preflight-http-{self.round_request_sequence}"
                ),
                "plan_hash": plan["plan_hash"],
                "max_provider_calls": max(
                    1,
                    int(plan["calls"]["recommended_provider_calls"]),
                ),
            })
        request = Request(
            f"{self.base_url}/api/rooms/{room_id}/rounds/stream",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps(request_payload).encode("utf-8"),
        )
        try:
            with urlopen(request, timeout=3) as response:
                events = [
                    json.loads(line)
                    for line in response.read().decode("utf-8").splitlines()
                    if line.strip()
                ]
                return response.status, events
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, [json.loads(exc.read().decode("utf-8"))]
            finally:
                exc.close()

    def post_plan(
        self,
        room_id: str,
        payload: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        request = Request(
            f"{self.base_url}/api/rooms/{room_id}/round-launch-plan",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps(payload).encode("utf-8"),
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    @staticmethod
    def bind_round_ledger(
        room_id: str,
        round_id: str,
        *,
        skip_provider_ids: set[str] | None = None,
    ) -> ProviderCallLedger:
        ledger = ProviderCallLedger.create(
            http_server.STORE,
            room_id,
            scope="round",
            client_request_id=f"resume-{round_id}",
            plan_hash="a" * 64,
            max_calls=100,
            skip_provider_ids=skip_provider_ids or set(),
        )
        ledger.bind_round(round_id)
        return ledger

    def post_resume(
        self,
        room_id: str,
        round_id: str,
        payload: dict[str, object],
    ) -> tuple[int, list[dict[str, object]]]:
        request = Request(
            f"{self.base_url}/api/rooms/{room_id}/rounds/{round_id}/resume/stream",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps(payload).encode("utf-8"),
        )
        with urlopen(request, timeout=3) as response:
            events = [
                json.loads(line)
                for line in response.read().decode("utf-8").splitlines()
                if line.strip()
            ]
            return response.status, events

    def test_endpoint_requires_session_guard_and_health_never_probes(self) -> None:
        with urlopen(f"{self.base_url}/api/health", timeout=3) as response:
            health = json.loads(response.read().decode("utf-8"))
        self.assertTrue(health["ok"])
        self.assertEqual(self.provider.probe_calls, [])

        status, _ = self.post_preflight({})
        self.assertEqual(status, 403)
        self.assertEqual(self.provider.probe_calls, [])

    def test_endpoint_skips_openai_by_default_without_network_or_secret_fields(self) -> None:
        status, payload = self.post_preflight(
            {},
            token=http_server.LOCAL_SESSION_TOKEN,
        )

        self.assertEqual(status, 200)
        self.assertEqual(self.provider.probe_calls, [])
        preflight = payload["preflight"]
        self.assertFalse(preflight["ready"])
        self.assertEqual(
            preflight["provider_checks"][0]["error_code"],
            "PROVIDER_SKIPPED",
        )
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("authorization", serialized)
        self.assertNotIn("bearer", serialized)

    def test_server_policy_cannot_be_weakened_by_an_empty_skip_list(self) -> None:
        registry = ProviderRegistry(
            {"openai": self.provider},
            disabled_provider_ids={"openai"},
        )
        http_server.PROVIDERS = registry
        http_server.ORCHESTRATOR = DiscussionOrchestrator(
            http_server.STORE,
            registry,
            self.market_service,
        )

        self.assertEqual(
            http_server.StudioRequestHandler._skip_provider_ids({
                "skip_providers": [],
            }),
            {"openai"},
        )
        status, payload = self.post_preflight(
            {"skip_providers": []},
            token=http_server.LOCAL_SESSION_TOKEN,
        )

        self.assertEqual(status, 200)
        self.assertFalse(payload["preflight"]["ready"])
        self.assertEqual(self.provider.probe_calls, [])
        self.assertEqual(
            payload["preflight"]["provider_checks"][0]["error_code"],
            "PROVIDER_POLICY_DISABLED",
        )

    def test_round_stream_preflight_failure_has_no_persistent_side_effects(self) -> None:
        before = http_server.STORE.room_snapshot("room_plan")
        self.provider.ready = False

        status, events = self.post_round({
            "objective": "这轮不应被创建",
            "skip_providers": [],
        })

        after = http_server.STORE.room_snapshot("room_plan")
        self.assertEqual(status, 200)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(
            events[0]["code"],
            "ROUND_PROVIDER_PREFLIGHT_FAILED",
        )
        self.assertEqual(self.provider.probe_calls, ["gpt-test"])
        self.assertEqual(after["latest_round"], before["latest_round"])
        self.assertEqual(after["round_checkpoint"], before["round_checkpoint"])
        self.assertEqual(after["messages"], before["messages"])
        self.assertEqual(after["materials"], before["materials"])

    def test_workflow_failure_precedes_market_and_provider_preflight_without_side_effects(self) -> None:
        snapshot = http_server.STORE.room_snapshot("room_plan")
        decision_member = next(
            member
            for member in snapshot["members"]
            if member["workflow_stage"] == "decision"
        )
        http_server.STORE.update_member(
            "room_plan",
            decision_member["id"],
            {"workflow_stage": "flexible"},
        )
        before = http_server.STORE.room_snapshot("room_plan")

        status, events = self.post_round({
            "objective": "缺少最终整合角色时不能启动",
            "skip_providers": [],
        })

        after = http_server.STORE.room_snapshot("room_plan")
        self.assertEqual(status, 409)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["error_code"], "ROUND_LAUNCH_PLAN_BLOCKED")
        self.assertIn(
            "WORKFLOW_PROVIDER_COVERAGE_INSUFFICIENT",
            [item["code"] for item in events[0]["blockers"]],
        )
        self.assertEqual(self.market_service.calls, 0)
        self.assertEqual(self.provider.probe_calls, [])
        self.assertEqual(after["latest_round"], before["latest_round"])
        self.assertEqual(after["round_checkpoint"], before["round_checkpoint"])
        self.assertEqual(after["messages"], before["messages"])

    def test_storage_market_failure_precedes_provider_probe_and_has_no_side_effects(self) -> None:
        deepseek = StubProvider("deepseek", model="deepseek-test")
        registry = ProviderRegistry({
            "openai": self.provider,
            "deepseek": deepseek,
        })
        http_server.PROVIDERS = registry
        http_server.ORCHESTRATOR = DiscussionOrchestrator(
            http_server.STORE,
            registry,
            self.market_service,
        )
        before = http_server.STORE.room_snapshot("room_storage")

        status, events = self.post_round(
            {
                "objective": "离线行情不能启动存储投委会",
                "skip_providers": [],
            },
            room_id="room_storage",
        )

        after = http_server.STORE.room_snapshot("room_storage")
        self.assertEqual(status, 200)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["code"], "ROUND_MARKET_PREFLIGHT_FAILED")
        self.assertEqual(self.market_service.calls, 1)
        self.assertEqual(self.provider.probe_calls, [])
        self.assertEqual(deepseek.probe_calls, [])
        self.assertEqual(after["latest_round"], before["latest_round"])
        self.assertEqual(after["round_checkpoint"], before["round_checkpoint"])
        self.assertEqual(after["messages"], before["messages"])

    def test_storage_resume_rejects_offline_frozen_snapshot_before_provider_probe(self) -> None:
        members = http_server.STORE.enabled_members("room_storage")[:2]
        round_row = http_server.STORE.create_round(
            "room_storage",
            "HTTP 旧离线检查点",
        )
        http_server.STORE.add_message(
            "room_storage",
            sender_type="user",
            sender_id="user",
            sender_name="我",
            content=round_row["objective"],
            round_id=round_row["id"],
        )
        http_server.STORE.save_round_checkpoint(
            "room_storage",
            round_row["id"],
            {
                "member_ids": [member["id"] for member in members],
                "next_order": 1,
                "max_turns": len(members),
                "skip_provider_ids": [],
                "shared_context": "legacy-offline-context",
                "market_snapshot": {
                    "ok": False,
                    "state": "offline",
                    "source": "futu_opend",
                    "snapshot_id": "http-legacy-offline",
                    "captured_at": "2026-07-18T20:00:00Z",
                    "rows": [],
                    "missing_symbols": [
                        "US.MU",
                        "US.SNDK",
                        "US.WDC",
                        "US.STX",
                    ],
                    "source_errors": [{
                        "code": "FUTU_OPEND_OFFLINE",
                        "message": "OpenD was offline.",
                    }],
                    "execution_capability": "none",
                    "live_trading_allowed": False,
                },
            },
        )
        http_server.STORE.complete_round(round_row["id"], "PAUSED")
        self.bind_round_ledger("room_storage", round_row["id"])
        before = http_server.STORE.room_snapshot("room_storage")

        status, events = self.post_resume(
            "room_storage",
            round_row["id"],
            {},
        )

        after = http_server.STORE.room_snapshot("room_storage")
        self.assertEqual(status, 200)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["code"], "ROUND_MARKET_PREFLIGHT_FAILED")
        self.assertEqual(
            events[0]["preflight"]["snapshot_origin"],
            "frozen_checkpoint",
        )
        self.assertEqual(self.market_service.calls, 0)
        self.assertEqual(self.provider.probe_calls, [])
        self.assertEqual(after["latest_round"], before["latest_round"])
        self.assertEqual(after["round_checkpoint"], before["round_checkpoint"])
        self.assertEqual(after["messages"], before["messages"])

    def test_resume_preflight_excludes_terminally_failed_members(self) -> None:
        members = http_server.STORE.enabled_members("room_plan")[:2]
        healthy_member, failed_member = members
        http_server.STORE.update_member(
            "room_plan",
            failed_member["id"],
            {"provider": "broken", "model": "broken-model"},
        )
        http_server.STORE.update_member(
            "room_plan",
            healthy_member["id"],
            {"provider": "openai", "model": "gpt-test"},
        )
        broken_provider = StubProvider(
            "broken",
            model="broken-model",
            ready=False,
        )
        registry = ProviderRegistry({
            "broken": broken_provider,
            "openai": self.provider,
        })
        http_server.PROVIDERS = registry
        http_server.ORCHESTRATOR = DiscussionOrchestrator(
            http_server.STORE,
            registry,
            self.market_service,
        )
        round_row = http_server.STORE.create_round(
            "room_plan",
            "仅恢复健康成员",
        )
        http_server.STORE.save_round_checkpoint(
            "room_plan",
            round_row["id"],
            {
                "member_ids": [failed_member["id"], healthy_member["id"]],
                "spoken_counts": {failed_member["id"]: 1},
                "failed_member_ids": [failed_member["id"]],
                "next_order": 2,
                "max_turns": 2,
                "skip_provider_ids": [],
            },
        )
        http_server.STORE.complete_round(round_row["id"], "PAUSED")
        self.bind_round_ledger("room_plan", round_row["id"])

        status, events = self.post_resume(
            "room_plan",
            round_row["id"],
            {},
        )

        self.assertEqual(status, 200)
        self.assertEqual(events[0]["type"], "round_resumed")
        self.assertEqual(broken_provider.probe_calls, [])
        self.assertEqual(self.provider.probe_calls, ["gpt-test"])
        self.assertNotIn(
            "ROUND_PROVIDER_PREFLIGHT_FAILED",
            [event.get("code") for event in events],
        )

    def test_http_resume_preflight_keeps_checkpoint_moderator_after_hot_edit(self) -> None:
        members = http_server.STORE.enabled_members("room_plan")[:2]
        frozen_moderator = http_server.STORE.update_member(
            "room_plan",
            members[0]["id"],
            {"provider": "deepseek", "model": "frozen-director-model"},
        )
        round_room = http_server.STORE.room_snapshot("room_plan")["room"]
        round_row = http_server.STORE.create_round(
            "room_plan",
            "恢复时沿用冻结主持路由",
        )
        http_server.STORE.save_round_checkpoint(
            "room_plan",
            round_row["id"],
            {
                "version": 7,
                "member_ids": [frozen_moderator["id"], members[1]["id"]],
                "discussion_mode": "dynamic",
                "domain": "open_collaboration",
                "moderator_member_id": frozen_moderator["id"],
                "moderator_member_version": frozen_moderator["version"],
                "moderator_provider": "deepseek",
                "moderator_model": "frozen-director-model",
                "workflow_policy": round_room["workflow_policy"],
                "next_order": 1,
                "max_turns": 2,
                "skip_provider_ids": [],
            },
        )
        http_server.STORE.complete_round(round_row["id"], "PAUSED")
        self.bind_round_ledger("room_plan", round_row["id"])
        http_server.STORE.update_member(
            "room_plan",
            frozen_moderator["id"],
            {"provider": "doubao", "model": "next-round-speaker-model"},
        )
        unrelated_moderator = http_server.STORE.add_member("room_plan", {
            "name": "新房间主持",
            "identity": "只从下一轮开始主持",
            "responsibilities": "不覆盖暂停轮次的主持路由。",
            "boundaries": "不参加当前暂停轮次。",
            "provider": "broken",
            "model": "must-not-probe",
        })
        current_room = http_server.STORE.room_snapshot("room_plan")["room"]
        http_server.STORE.update_room("room_plan", {
            "expected_settings_version": current_room["settings_version"],
            "moderator_member_id": unrelated_moderator["id"],
        })
        deepseek = StubProvider("deepseek", model="frozen-director-model")
        doubao = StubProvider("doubao", model="next-round-speaker-model")
        broken = StubProvider("broken", model="must-not-probe", ready=False)
        registry = ProviderRegistry({
            "openai": self.provider,
            "deepseek": deepseek,
            "doubao": doubao,
            "broken": broken,
        })
        http_server.PROVIDERS = registry
        http_server.ORCHESTRATOR = DiscussionOrchestrator(
            http_server.STORE,
            registry,
            self.market_service,
        )

        status, events = self.post_resume(
            "room_plan",
            round_row["id"],
            {},
        )

        self.assertEqual(status, 200)
        self.assertNotIn(
            "ROUND_PROVIDER_PREFLIGHT_FAILED",
            [event.get("code") for event in events],
        )
        self.assertEqual(deepseek.probe_calls, ["frozen-director-model"])
        self.assertEqual(doubao.probe_calls, ["next-round-speaker-model"])
        self.assertEqual(self.provider.probe_calls, ["gpt-test"])
        self.assertEqual(broken.probe_calls, [])

    def test_round_stream_does_not_expand_an_invalid_member_selection_to_the_whole_room(self) -> None:
        status, events = self.post_round({
            "objective": "无效成员选择不应退回全房间",
            "member_ids": ["member-does-not-exist"],
            "skip_providers": ["openai"],
        })

        self.assertEqual(status, 400)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["error_code"], "ROUND_MEMBER_IDS_NOT_ALLOWED")
        self.assertEqual(self.provider.probe_calls, [])


if __name__ == "__main__":
    unittest.main()
