from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import http_server
from backend.provider_call_ledger import ProviderCallLedger
from backend.store import StudioStore


class BudgetedLocalRegistry:
    """Local metadata plus a deterministic ledger-aware fake preflight."""

    disabled_provider_ids = frozenset({"openai"})

    def __init__(self) -> None:
        self.status_calls = 0
        self.preflight_calls: list[dict[str, Any]] = []
        self.raise_on_preflight = ""

    def status(self) -> list[dict[str, Any]]:
        self.status_calls += 1
        return [
            {
                "id": "deepseek",
                "model": "deepseek-test",
                "configured": True,
                "policy_disabled": False,
            },
            {
                "id": "openai",
                "model": "gpt-test",
                "configured": True,
                "policy_disabled": True,
            },
        ]

    @staticmethod
    def resolved_model(provider_id: str, configured_model: str = "") -> str:
        return str(configured_model or f"{provider_id}-test")

    def preflight(
        self,
        assignments: list[dict[str, Any]],
        *,
        skip_provider_ids: set[str] | None = None,
        cache_ttl_seconds: float = 30.0,
        ledger: ProviderCallLedger | None = None,
    ) -> list[dict[str, Any]]:
        del cache_ttl_seconds
        if self.raise_on_preflight:
            raise RuntimeError(self.raise_on_preflight)
        skip_ids = set(skip_provider_ids or set())
        self.preflight_calls.append({
            "ledger": ledger,
            "skip_provider_ids": skip_ids,
            "assignment_ids": [str(item.get("id") or "") for item in assignments],
        })
        grouped = sorted({
            (
                str(item.get("provider") or "deepseek").strip().lower(),
                self.resolved_model(
                    str(item.get("provider") or "deepseek").strip().lower(),
                    str(item.get("model") or ""),
                ),
            )
            for item in assignments
        })
        checks: list[dict[str, Any]] = []
        for provider_id, model in grouped:
            skipped = provider_id in skip_ids or provider_id in self.disabled_provider_ids
            if skipped:
                checks.append({
                    "provider": provider_id,
                    "model": model,
                    "configured": True,
                    "reachable": False,
                    "model_access": False,
                    "ready": False,
                    "error_code": "provider_policy_disabled",
                })
                continue
            if ledger is None:
                raise AssertionError("provider preflight must receive the confirmed ledger")
            attempt = ledger.reserve(
                kind="preflight_probe",
                provider=provider_id,
                model=model,
                target_type="provider_route",
                target_id=ledger.route_target_id(provider_id, model),
            )
            ledger.finish(
                str(attempt["id"]),
                str(attempt["attempt_token"]),
                status="RESPONDED",
            )
            checks.append({
                "provider": provider_id,
                "model": model,
                "configured": True,
                "reachable": True,
                "model_access": True,
                "ready": True,
                "error_code": "",
            })
        return checks


class ReadyConvergence:
    def workflow_configuration_preflight(
        self,
        _snapshot: dict[str, Any],
        *,
        workflow_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del workflow_policy
        return {"applicable": True, "ready": True, "blockers": []}


class LedgerRecordingOrchestrator:
    def __init__(self, store: StudioStore, providers: BudgetedLocalRegistry) -> None:
        self.store = store
        self.providers = providers
        self.convergence = ReadyConvergence()
        self.market_calls = 0
        self.frozen_market_calls = 0
        self.run_ledgers: list[ProviderCallLedger | None] = []
        self.expected_plan_hashes: list[str] = []
        self.market_hook: Any = None

    def preflight_market(
        self,
        _room_id: str,
        *,
        snapshot: dict[str, Any],
    ) -> tuple[dict[str, Any], None]:
        del snapshot
        self.market_calls += 1
        if callable(self.market_hook):
            self.market_hook()
        return ({
            "applicable": False,
            "ready": True,
            "state": "offline",
            "execution_capability": "none",
            "live_trading_allowed": False,
        }, None)

    def preflight_frozen_market(
        self,
        _room_id: str,
        _round_id: str,
        *,
        snapshot: dict[str, Any],
        checkpoint: dict[str, Any],
    ) -> tuple[dict[str, Any], None]:
        del snapshot, checkpoint
        self.frozen_market_calls += 1
        return ({
            "applicable": False,
            "ready": True,
            "state": "offline",
            "snapshot_origin": "frozen_checkpoint",
            "execution_capability": "none",
            "live_trading_allowed": False,
        }, None)

    @staticmethod
    def checkpoint_failed_member_ids(
        _checkpoint_state: dict[str, Any],
        _member_ids: Any,
    ) -> set[str]:
        return set()

    def run_round(
        self,
        room_id: str,
        objective: str,
        member_ids: list[str] | None,
        *,
        resume_round_id: str = "",
        prefetched_market_snapshot: dict[str, Any] | None = None,
        skip_provider_ids: set[str] | None = None,
        provider_call_ledger: ProviderCallLedger | None = None,
        expected_launch_plan_hash: str = "",
        round_context_authorizations: dict[str, Any] | None = None,
        project_round_focus_authorization: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        del (
            member_ids,
            prefetched_market_snapshot,
            skip_provider_ids,
            round_context_authorizations,
            project_round_focus_authorization,
        )
        self.run_ledgers.append(provider_call_ledger)
        self.expected_plan_hashes.append(expected_launch_plan_hash)
        if provider_call_ledger is None:
            raise AssertionError("round execution must receive the confirmed ledger")
        if resume_round_id:
            yield {"type": "round_resumed", "round_id": resume_round_id}
            return
        round_row = self.store.create_formal_round(room_id, objective)
        provider_call_ledger.bind_round(str(round_row["id"]))
        yield {"type": "round_started", "round": round_row}
        self.store.complete_round(str(round_row["id"]), "PARTIAL")
        yield {"type": "round_completed", "round_id": round_row["id"]}


class LedgerRecordingArtifacts:
    def __init__(self, providers: BudgetedLocalRegistry) -> None:
        self.providers = providers
        self.calls: list[dict[str, Any]] = []

    def generate_minutes(
        self,
        room_id: str,
        round_id: str,
        synthesizer_member_id: str,
        *,
        skip_provider_ids: set[str] | None = None,
        ledger: ProviderCallLedger | None = None,
        frozen_synthesizer_route: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del synthesizer_member_id
        self.calls.append({
            "room_id": room_id,
            "round_id": round_id,
            "skip_provider_ids": set(skip_provider_ids or set()),
            "ledger": ledger,
            "frozen_synthesizer_route": dict(frozen_synthesizer_route or {}),
        })
        provider_execution = ledger.snapshot() if ledger is not None else {}
        exhausted = (
            int(provider_execution.get("reserved_calls") or 0)
            >= int(provider_execution.get("max_calls") or 1)
        )
        return {
            "id": "artifact_fake",
            "room_id": room_id,
            "round_id": round_id,
            "generation_source": "template_fallback" if exhausted else "provider",
            "provider_error_code": (
                "PROVIDER_CALL_BUDGET_EXCEEDED" if exhausted else ""
            ),
            "idempotent_replay": False,
        }


class RoundLaunchHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = StudioStore(Path(self.temp_dir.name) / "round-launch-http.sqlite3")
        self.providers = BudgetedLocalRegistry()
        self.orchestrator = LedgerRecordingOrchestrator(self.store, self.providers)
        self.artifacts = LedgerRecordingArtifacts(self.providers)

        self.original_store = http_server.STORE
        self.original_providers = http_server.PROVIDERS
        self.original_orchestrator = http_server.ORCHESTRATOR
        self.original_artifacts = http_server.ARTIFACTS
        http_server.STORE = self.store
        http_server.PROVIDERS = self.providers  # type: ignore[assignment]
        http_server.ORCHESTRATOR = self.orchestrator  # type: ignore[assignment]
        http_server.ARTIFACTS = self.artifacts  # type: ignore[assignment]
        http_server.StudioRequestHandler._formal_execution_locks = {}

        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        http_server.PROVIDERS = self.original_providers
        http_server.ORCHESTRATOR = self.original_orchestrator
        http_server.ARTIFACTS = self.original_artifacts

    def post(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> tuple[int, Any, str]:
        request = Request(
            f"{self.base_url}{path}",
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps(payload).encode("utf-8"),
        )
        try:
            with urlopen(request, timeout=5) as response:
                raw = response.read().decode("utf-8")
                content_type = response.headers.get("Content-Type", "")
                if "ndjson" in content_type:
                    body: Any = [
                        json.loads(line) for line in raw.splitlines() if line.strip()
                    ]
                else:
                    body = json.loads(raw)
                return response.status, body, raw
        except HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8")
                return exc.code, json.loads(raw), raw
            finally:
                exc.close()

    def launch_plan(
        self,
        *,
        room_id: str = "room_plan",
        objective: str = "Build an evidence-backed decision",
    ) -> dict[str, Any]:
        status, body, _raw = self.post(
            f"/api/rooms/{room_id}/round-launch-plan",
            {"objective": objective, "skip_providers": []},
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["plan"]["ready_for_authorization"])
        return body["plan"]

    @staticmethod
    def authorization_payload(
        plan: dict[str, Any],
        request_id: str,
        *,
        max_calls: int | None = None,
    ) -> dict[str, Any]:
        return {
            "objective": plan["objective"],
            "skip_providers": [],
            "client_round_request_id": request_id,
            "plan_hash": plan["plan_hash"],
            "max_provider_calls": (
                max_calls
                if max_calls is not None
                else max(1, int(plan["calls"]["recommended_provider_calls"]))
            ),
        }

    def create_completed_round(self, objective: str = "Completed round") -> dict[str, Any]:
        row = self.store.create_round("room_plan", objective)
        self.store.complete_round(str(row["id"]), "PARTIAL")
        return row

    def artifact_route(self, objective: str = "Artifact route") -> dict[str, Any]:
        plan = http_server.RoundLaunchPlanService(
            self.store,
            self.providers,
        ).build("room_plan", objective, {"openai"})
        return http_server.StudioRequestHandler._launch_plan_artifact_route(plan)

    @staticmethod
    def member_routes(plan: dict[str, Any]) -> dict[str, Any]:
        return http_server.StudioRequestHandler._launch_plan_member_routes(plan)

    def test_launch_plan_is_read_only_and_forces_policy_disabled_openai_skip(self) -> None:
        before = self.store.room_snapshot("room_plan")
        before_runs = self.store.list_provider_execution_runs("room_plan")

        plan = self.launch_plan()

        after = self.store.room_snapshot("room_plan")
        self.assertIn("openai", plan["skip_provider_ids"])
        openai_projection = next(
            item for item in plan["provider_call_projection"]
            if item["provider"] == "openai"
        )
        self.assertEqual(openai_projection["projected_provider_calls"], 0)
        self.assertEqual(self.providers.preflight_calls, [])
        self.assertEqual(self.orchestrator.market_calls, 0)
        self.assertEqual(self.orchestrator.run_ledgers, [])
        self.assertEqual(self.store.list_provider_execution_runs("room_plan"), before_runs)
        self.assertEqual(after["latest_round"], before["latest_round"])
        self.assertEqual(after["messages"], before["messages"])

    def test_new_round_requires_authorization_and_rejects_member_ids(self) -> None:
        for payload, expected_code in (
            ({"objective": "No authorization"}, "ROUND_AUTHORIZATION_REQUIRED"),
            ({"objective": "No override", "member_ids": []}, "ROUND_MEMBER_IDS_NOT_ALLOWED"),
        ):
            with self.subTest(expected_code=expected_code):
                status, body, _raw = self.post(
                    "/api/rooms/room_plan/rounds/stream",
                    payload,
                )
                self.assertEqual(status, 400)
                self.assertEqual(body["error_code"], expected_code)
        self.assertEqual(self.orchestrator.market_calls, 0)
        self.assertEqual(self.providers.preflight_calls, [])
        self.assertEqual(self.store.list_provider_execution_runs("room_plan"), [])

    def test_plan_drift_fails_before_market_provider_or_ledger(self) -> None:
        plan = self.launch_plan()
        member = self.store.enabled_members("room_plan")[0]
        self.store.update_member(
            "room_plan",
            str(member["id"]),
            {"identity": "Changed after confirmation"},
        )

        status, body, _raw = self.post(
            "/api/rooms/room_plan/rounds/stream",
            self.authorization_payload(plan, "drift-request"),
        )

        self.assertEqual(status, 409)
        self.assertEqual(body["error_code"], "ROUND_LAUNCH_PLAN_DRIFT")
        self.assertEqual(self.orchestrator.market_calls, 0)
        self.assertEqual(self.providers.preflight_calls, [])
        self.assertEqual(self.store.list_provider_execution_runs("room_plan"), [])

    def test_plan_drift_during_market_gate_fails_before_ledger_or_provider(self) -> None:
        plan = self.launch_plan(objective="Market-gate drift")
        member = self.store.enabled_members("room_plan")[0]

        def mutate_route() -> None:
            self.store.update_member(
                "room_plan",
                str(member["id"]),
                {"identity": "Changed inside market preflight"},
            )

        self.orchestrator.market_hook = mutate_route
        status, body, _raw = self.post(
            "/api/rooms/room_plan/rounds/stream",
            self.authorization_payload(plan, "market-gate-drift"),
        )

        self.assertEqual(status, 409)
        self.assertEqual(body["error_code"], "ROUND_LAUNCH_PLAN_DRIFT")
        self.assertEqual(self.orchestrator.market_calls, 1)
        self.assertEqual(self.providers.preflight_calls, [])
        self.assertEqual(self.store.list_provider_execution_runs("room_plan"), [])

    def test_public_preflight_is_local_only_and_never_creates_a_ledger(self) -> None:
        before_runs = self.store.list_provider_execution_runs("room_plan")
        status, body, raw = self.post(
            "/api/rooms/room_plan/providers/preflight",
            {"skip_providers": []},
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["preflight"]["verification_scope"], "local_configuration_only")
        self.assertEqual(body["preflight"]["external_call_count"], 0)
        self.assertEqual(self.providers.preflight_calls, [])
        self.assertEqual(self.store.list_provider_execution_runs("room_plan"), before_runs)
        self.assertNotIn("Bearer", raw)
        self.assertNotIn("api_key", raw.lower())

    def test_formal_execution_lock_rejects_parallel_round_before_any_side_effect(self) -> None:
        plan = self.launch_plan(objective="Busy room")
        lock = http_server.StudioRequestHandler._formal_execution_lock("room_plan")
        self.assertTrue(lock.acquire(blocking=False))
        try:
            status, body, _raw = self.post(
                "/api/rooms/room_plan/rounds/stream",
                self.authorization_payload(plan, "busy-room"),
            )
        finally:
            lock.release()

        self.assertEqual(status, 409)
        self.assertEqual(body["error_code"], "ROUND_EXECUTION_BUSY")
        self.assertEqual(self.orchestrator.market_calls, 0)
        self.assertEqual(self.providers.preflight_calls, [])
        self.assertEqual(self.store.list_provider_execution_runs("room_plan"), [])

    def test_authorized_round_budgets_preflight_and_passes_same_ledger(self) -> None:
        plan = self.launch_plan()
        max_calls = max(2, int(plan["calls"]["recommended_provider_calls"]))

        status, events, _raw = self.post(
            "/api/rooms/room_plan/rounds/stream",
            self.authorization_payload(plan, "authorized-request", max_calls=max_calls),
        )

        self.assertEqual(status, 200)
        self.assertEqual(events[0]["type"], "round_authorization")
        self.assertEqual(events[0]["plan_hash"], plan["plan_hash"])
        self.assertTrue(self.providers.preflight_calls)
        preflight_ledger = self.providers.preflight_calls[0]["ledger"]
        self.assertIs(preflight_ledger, self.orchestrator.run_ledgers[0])
        execution = preflight_ledger.snapshot()
        self.assertEqual(execution["max_calls"], max_calls)
        self.assertEqual(execution["plan_hash"], plan["plan_hash"])
        self.assertIn("openai", execution["skip_policy"]["provider_ids"])
        self.assertTrue(execution["member_routes_present"])
        self.assertTrue(execution["member_routes_integrity_ok"])
        self.assertEqual(execution["member_routes"], self.member_routes(plan))
        self.assertEqual(
            len(execution["member_routes"]["members"]),
            len(plan["members"]),
        )
        self.assertTrue(execution["round_id"])
        self.assertEqual(execution["reserved_calls"], 1)

    def test_below_recommended_budget_is_explicitly_warned_but_valid(self) -> None:
        plan = self.launch_plan()
        self.assertGreater(int(plan["calls"]["recommended_provider_calls"]), 1)

        status, events, _raw = self.post(
            "/api/rooms/room_plan/rounds/stream",
            self.authorization_payload(plan, "below-recommended", max_calls=1),
        )

        self.assertEqual(status, 200)
        self.assertEqual(events[0]["type"], "round_authorization")
        self.assertFalse(events[0]["sufficient"])
        self.assertEqual(
            events[0]["warning_code"],
            "BELOW_RECOMMENDED_PROVIDER_CALLS",
        )

    def test_same_request_id_conflict_does_not_create_a_second_ledger(self) -> None:
        plan = self.launch_plan()
        first_max = max(2, int(plan["calls"]["recommended_provider_calls"]))
        first_status, _events, _raw = self.post(
            "/api/rooms/room_plan/rounds/stream",
            self.authorization_payload(plan, "same-request", max_calls=first_max),
        )
        self.assertEqual(first_status, 200)

        status, body, _raw = self.post(
            "/api/rooms/room_plan/rounds/stream",
            self.authorization_payload(plan, "same-request", max_calls=first_max + 1),
        )

        self.assertEqual(status, 409)
        self.assertEqual(body["error_code"], "ROUND_REQUEST_ID_CONFLICT")
        runs = self.store.list_provider_execution_runs("room_plan", scope="round")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["max_calls"], first_max)

    def test_unbound_request_replay_abandons_started_call_without_refund(self) -> None:
        plan = self.launch_plan()
        max_calls = max(3, int(plan["calls"]["recommended_provider_calls"]))
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="unbound-replay",
            plan_hash=plan["plan_hash"],
            max_calls=max_calls,
            skip_provider_ids={"openai"},
            artifact_route=self.artifact_route(plan["objective"]),
            member_routes=self.member_routes(plan),
            kind_call_limits={
                "round_director": int(
                    plan["calls"]["recommended_director_calls"]
                ),
            },
            operation_binding_version=(
                http_server.PROVIDER_OPERATION_BINDING_VERSION
            ),
        )
        ledger.reserve(
            kind="preflight_probe",
            provider="deepseek",
            model="deepseek-test",
            target_type="provider_route",
            target_id=ledger.route_target_id("deepseek", "deepseek-test"),
        )

        status, _events, _raw = self.post(
            "/api/rooms/room_plan/rounds/stream",
            self.authorization_payload(plan, "unbound-replay", max_calls=max_calls),
        )

        self.assertEqual(status, 200)
        attempts = ledger.attempts()
        self.assertEqual(attempts[0]["status"], "ABANDONED")
        self.assertEqual(
            attempts[0]["error_code"],
            "provider_call_abandoned_before_round",
        )
        self.assertEqual(attempts[1]["status"], "RESPONDED")
        self.assertEqual(ledger.snapshot()["reserved_calls"], 2)
        self.assertIs(self.orchestrator.run_ledgers[-1], self.providers.preflight_calls[-1]["ledger"])

    def test_resume_rejects_legacy_round_without_ledger(self) -> None:
        round_row = self.store.create_round("room_plan", "Legacy paused round")
        self.store.complete_round(str(round_row["id"]), "PAUSED")

        status, body, _raw = self.post(
            f"/api/rooms/room_plan/rounds/{round_row['id']}/resume/stream",
            {},
        )

        self.assertEqual(status, 409)
        self.assertEqual(body["error_code"], "ROUND_PROVIDER_LEDGER_REQUIRED")
        self.assertEqual(self.orchestrator.frozen_market_calls, 0)
        self.assertEqual(self.providers.preflight_calls, [])

    def test_resume_reuses_exact_ledger_and_abandons_started_call(self) -> None:
        members = self.store.enabled_members("room_plan")
        round_row = self.store.create_round("room_plan", "Resume authorized round")
        self.store.save_round_checkpoint(
            "room_plan",
            str(round_row["id"]),
            {
                "member_ids": [str(member["id"]) for member in members],
                "next_order": 1,
                "max_turns": len(members),
                "skip_provider_ids": ["openai"],
            },
        )
        self.store.complete_round(str(round_row["id"]), "PAUSED")
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="resume-ledger",
            plan_hash="a" * 64,
            max_calls=10,
            skip_provider_ids={"openai"},
        )
        ledger.bind_round(str(round_row["id"]))
        ledger.reserve(kind="director", provider="deepseek", model="deepseek-test")

        status, events, _raw = self.post(
            f"/api/rooms/room_plan/rounds/{round_row['id']}/resume/stream",
            {},
        )

        self.assertEqual(status, 200)
        self.assertEqual(events[0]["type"], "round_resumed")
        self.assertTrue(self.providers.preflight_calls)
        self.assertTrue(all(
            call["ledger"].run_id == ledger.run_id
            for call in self.providers.preflight_calls
        ))
        self.assertEqual(self.orchestrator.run_ledgers[-1].run_id, ledger.run_id)
        self.assertEqual(len(self.store.list_provider_execution_runs("room_plan")), 1)
        self.assertEqual(ledger.snapshot()["max_calls"], 10)
        first_attempt = ledger.attempts()[0]
        self.assertEqual(first_attempt["status"], "ABANDONED")
        self.assertEqual(
            first_attempt["error_code"],
            "provider_call_abandoned_before_resume",
        )

    def test_provider_exception_is_sanitized(self) -> None:
        plan = self.launch_plan()
        self.providers.raise_on_preflight = "Bearer SUPER_SECRET_PROVIDER_TOKEN"

        status, body, raw = self.post(
            "/api/rooms/room_plan/rounds/stream",
            self.authorization_payload(plan, "secret-error"),
        )

        self.assertEqual(status, 409)
        self.assertEqual(body["error_code"], "ROUND_PROVIDER_PREFLIGHT_ERROR")
        self.assertNotIn("SUPER_SECRET_PROVIDER_TOKEN", raw)
        self.assertNotIn("Bearer", raw)

    def test_artifact_rejects_started_call_without_clobbering_it(self) -> None:
        round_row = self.create_completed_round("Artifact source round")
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="artifact-ledger",
            plan_hash="b" * 64,
            max_calls=3,
            skip_provider_ids={"openai"},
            artifact_route=self.artifact_route("Artifact source round"),
        )
        ledger.bind_round(str(round_row["id"]))
        ledger.reserve(kind="artifact", provider="deepseek", model="deepseek-test")

        status, body, _raw = self.post(
            "/api/rooms/room_plan/artifacts/generate",
            {"round_id": round_row["id"]},
        )

        self.assertEqual(status, 409)
        self.assertEqual(body["error_code"], "ARTIFACT_ROUND_LEDGER_BUSY")
        self.assertEqual(ledger.attempts()[0]["status"], "STARTED")
        self.assertEqual(self.artifacts.calls, [])

    def test_artifact_reuses_round_ledger_and_frozen_route(self) -> None:
        round_row = self.create_completed_round("Artifact frozen route")
        frozen_route = self.artifact_route("Artifact frozen route")
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="artifact-frozen-route",
            plan_hash="d" * 64,
            max_calls=3,
            skip_provider_ids={"openai"},
            artifact_route=frozen_route,
        )
        ledger.bind_round(str(round_row["id"]))

        status, body, _raw = self.post(
            "/api/rooms/room_plan/artifacts/generate",
            {"round_id": round_row["id"]},
        )

        self.assertEqual(status, 201)
        self.assertTrue(body["created"])
        call = self.artifacts.calls[-1]
        self.assertEqual(call["ledger"].run_id, ledger.run_id)
        self.assertEqual(call["skip_provider_ids"], {"openai"})
        self.assertEqual(call["frozen_synthesizer_route"], frozen_route)

    def test_artifact_budget_exhaustion_uses_honest_fallback(self) -> None:
        round_row = self.create_completed_round("Exhausted artifact source")
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id="artifact-exhausted",
            plan_hash="c" * 64,
            max_calls=1,
            skip_provider_ids={"openai"},
            artifact_route=self.artifact_route("Exhausted artifact source"),
        )
        ledger.bind_round(str(round_row["id"]))
        attempt = ledger.reserve(
            kind="speaker",
            provider="deepseek",
            model="deepseek-test",
        )
        ledger.finish(
            str(attempt["id"]),
            str(attempt["attempt_token"]),
            status="RESPONDED",
        )

        status, body, _raw = self.post(
            "/api/rooms/room_plan/artifacts/generate",
            {"round_id": round_row["id"]},
        )

        self.assertEqual(status, 201)
        self.assertEqual(body["artifact"]["generation_source"], "template_fallback")
        self.assertEqual(
            body["artifact"]["provider_error_code"],
            "PROVIDER_CALL_BUDGET_EXCEEDED",
        )
        self.assertEqual(ledger.snapshot()["reserved_calls"], 1)

    def test_artifact_idempotent_replay_precedes_ledger_recovery(self) -> None:
        round_row = self.create_completed_round("Existing artifact source")
        generation_key = self.store.artifact_generation_key(
            "room_plan",
            str(round_row["id"]),
            "meeting_minutes",
        )
        existing = self.store.create_artifact(
            "room_plan",
            title="Existing minutes",
            content={},
            generation_key=generation_key,
        )
        self.assertIsNotNone(existing)

        status, body, _raw = self.post(
            "/api/rooms/room_plan/artifacts/generate",
            {"round_id": round_row["id"]},
        )

        self.assertEqual(status, 200)
        self.assertFalse(body["created"])
        self.assertTrue(body["artifact"]["idempotent_replay"])
        self.assertEqual(body["artifact"]["id"], existing["id"])
        self.assertEqual(self.artifacts.calls, [])

    def test_artifact_rejects_missing_round_ledger_and_roundless_provider_use(self) -> None:
        round_row = self.create_completed_round("No ledger")
        status, body, _raw = self.post(
            "/api/rooms/room_plan/artifacts/generate",
            {"round_id": round_row["id"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(body["error_code"], "ARTIFACT_ROUND_LEDGER_REQUIRED")

        status, body, _raw = self.post(
            "/api/rooms/room_plan/artifacts/generate",
            {},
        )
        self.assertEqual(status, 409)
        self.assertEqual(
            body["error_code"],
            "ARTIFACT_ROUND_AUTHORIZATION_REQUIRED",
        )
        self.assertEqual(self.artifacts.calls, [])


if __name__ == "__main__":
    unittest.main()
