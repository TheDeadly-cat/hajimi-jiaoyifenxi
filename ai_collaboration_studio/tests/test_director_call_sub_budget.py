from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterator

# backend.store creates a module-level store at import time. Keep that import
# side effect away from the formal database and do not load any local secrets.
_IMPORT_TEMP_DIR = tempfile.TemporaryDirectory(prefix="ai-studio-p17-import-")
_PREVIOUS_IMPORT_ENV = {
    key: os.environ.get(key)
    for key in (
        "AI_STUDIO_SKIP_LOCAL_ENV",
        "AI_STUDIO_RUNTIME_DIR",
        "AI_STUDIO_DATABASE_PATH",
    )
}
os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
os.environ["AI_STUDIO_RUNTIME_DIR"] = _IMPORT_TEMP_DIR.name
os.environ["AI_STUDIO_DATABASE_PATH"] = str(
    Path(_IMPORT_TEMP_DIR.name) / "import-only.sqlite3"
)

from backend import http_server  # noqa: E402
from backend import provider_call_ledger as ledger_module  # noqa: E402
from backend.orchestrator import DiscussionOrchestrator  # noqa: E402
from backend.provider_call_ledger import (  # noqa: E402
    ProviderCallBudgetExceeded,
    ProviderCallLedger,
)
from backend.providers.base import ProviderResponse  # noqa: E402
from backend.round_launch_plan import RoundLaunchPlanService  # noqa: E402
from backend.store import (  # noqa: E402
    ProviderCallKindBudgetExceeded,
    StudioStore,
)
from backend.turn_envelope import (  # noqa: E402
    TURN_ENVELOPE_SCHEMA_SHA256,
    TURN_ENVELOPE_VERSION,
)
from tests.turn_contract_fixture import append_valid_turn_contract  # noqa: E402
from backend.workflow_policy import default_workflow_policy  # noqa: E402

for _key, _value in _PREVIOUS_IMPORT_ENV.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value


MAX_28_PROVIDER_CALLS = 28
DIRECTOR_KIND = "round_director"


def _kind_budget_exception_type() -> type[Exception]:
    candidate = getattr(ledger_module, "ProviderCallKindBudgetExceeded", None)
    if (
        not isinstance(candidate, type)
        or not issubclass(candidate, Exception)
    ):
        raise AssertionError(
            "ProviderCallKindBudgetExceeded must be exported by "
            "backend.provider_call_ledger"
        )
    return candidate


class OfflineProvider:
    """Deterministic provider fixture; it never performs I/O."""

    provider_id = "deepseek"

    def __init__(self) -> None:
        self.director_calls = 0
        self.speaker_calls = 0

    def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
    ) -> ProviderResponse:
        if '"action":"speak|finish"' in instructions:
            self.director_calls += 1
            return ProviderResponse(
                ok=True,
                provider=self.provider_id,
                model=model,
                content="{}",
            )
        self.speaker_calls += 1
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model,
            content=append_valid_turn_contract(
                f"offline speaker response {self.speaker_calls}",
                instructions=instructions,
                input_text=input_text,
            ),
        )


class OfflineRegistry:
    disabled_provider_ids = frozenset({"openai"})

    def __init__(self, provider: OfflineProvider | None = None) -> None:
        self.provider = provider or OfflineProvider()
        self.preflight_ledgers: list[ProviderCallLedger] = []

    def get(self, provider_id: str) -> OfflineProvider:
        self.provider.provider_id = str(provider_id or "deepseek").strip().lower()
        return self.provider

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "deepseek",
                "model": "offline-model",
                "configured": True,
                "policy_disabled": False,
            },
            {
                "id": "openai",
                "model": "disabled-model",
                "configured": False,
                "policy_disabled": True,
            },
        ]

    @staticmethod
    def resolved_model(provider_id: str, configured_model: str = "") -> str:
        return str(configured_model or "offline-model")

    def preflight(
        self,
        assignments: list[dict[str, Any]],
        *,
        skip_provider_ids: set[str] | None = None,
        cache_ttl_seconds: float = 30.0,
        ledger: ProviderCallLedger | None = None,
    ) -> list[dict[str, Any]]:
        del cache_ttl_seconds
        if ledger is None:
            raise AssertionError("confirmed preflight must reuse the round ledger")
        self.preflight_ledgers.append(ledger)
        skip_ids = set(skip_provider_ids or set())
        routes = sorted({
            (
                str(member.get("provider") or "deepseek").strip().lower(),
                self.resolved_model(
                    str(member.get("provider") or "deepseek"),
                    str(member.get("model") or ""),
                ),
            )
            for member in assignments
        })
        results: list[dict[str, Any]] = []
        for provider_id, model in routes:
            skipped = provider_id in skip_ids or provider_id in self.disabled_provider_ids
            if skipped:
                results.append({
                    "provider": provider_id,
                    "model": model,
                    "configured": provider_id == "openai",
                    "reachable": False,
                    "model_access": False,
                    "ready": False,
                    "error_code": "provider_policy_disabled",
                })
                continue
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
            results.append({
                "provider": provider_id,
                "model": model,
                "configured": True,
                "reachable": True,
                "model_access": True,
                "ready": True,
                "error_code": "",
            })
        return results


class SnapshotLedger:
    """Read-only/race ledger fixture; it never authorizes a Provider call."""

    def __init__(
        self,
        *,
        global_remaining: int,
        director_remaining: int,
        raise_kind_on_reserve: bool = False,
    ) -> None:
        self.global_remaining = global_remaining
        self.director_remaining = director_remaining
        self.raise_kind_on_reserve = raise_kind_on_reserve
        self.reserve_calls = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "remaining_calls": self.global_remaining,
            "kind_call_budgets": {
                DIRECTOR_KIND: {"remaining": self.director_remaining},
            },
        }

    def reserve(self, **_kwargs: Any) -> dict[str, Any]:
        self.reserve_calls += 1
        if self.raise_kind_on_reserve:
            raise ProviderCallKindBudgetExceeded(
                "offline-race",
                DIRECTOR_KIND,
                1,
            )
        raise AssertionError("test fixture must never authorize a Provider call")


class FixedConvergenceOrchestrator(DiscussionOrchestrator):
    def _convergence_state(
        self,
        _room_id: str,
        _round_id: str,
        _successful_member_ids: set[str],
        _market_snapshot: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "project_workspace": {},
            "research_evidence_gate": {},
            "candidate_risk_review_gate": {},
            "can_host_finish": False,
            "discussion_gate": {
                "ready": False,
                "successful_member_count": 1,
                "required_success_count": 1,
                "stage_coverage": [],
                "role_coverage": [],
                "blockers": [{"title": "offline unresolved gate"}],
            },
        }


class ReadyConvergence:
    @staticmethod
    def workflow_configuration_preflight(
        _snapshot: dict[str, Any],
        *,
        workflow_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del workflow_policy
        return {"applicable": True, "ready": True, "blockers": []}


class LedgerRecordingOrchestrator:
    """No-network HTTP launch fixture used only to inspect frozen authorization."""

    def __init__(self, store: StudioStore, providers: OfflineRegistry) -> None:
        self.store = store
        self.providers = providers
        self.convergence = ReadyConvergence()
        self.ledgers: list[ProviderCallLedger] = []

    @staticmethod
    def preflight_market(
        _room_id: str,
        *,
        snapshot: dict[str, Any],
    ) -> tuple[dict[str, Any], None]:
        del snapshot
        return ({
            "applicable": False,
            "ready": True,
            "state": "offline",
            "execution_capability": "none",
            "live_trading_allowed": False,
        }, None)

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
        project_round_focus_authorization: dict[str, Any] | None = None,
        round_context_authorizations: Any = None,
    ) -> Iterator[dict[str, Any]]:
        del (
            member_ids,
            resume_round_id,
            prefetched_market_snapshot,
            skip_provider_ids,
            expected_launch_plan_hash,
            project_round_focus_authorization,
            round_context_authorizations,
        )
        if provider_call_ledger is None:
            raise AssertionError("round launch omitted its confirmed ledger")
        self.ledgers.append(provider_call_ledger)
        round_row = self.store.create_formal_round(room_id, objective)
        provider_call_ledger.bind_round(str(round_row["id"]))
        yield {"type": "round_started", "round": round_row}
        self.store.complete_round(str(round_row["id"]), "PARTIAL")
        yield {"type": "round_completed", "round_id": round_row["id"]}


class DirectorCallSubBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-p17-test-")
        self.store = StudioStore(Path(self.temp_dir.name) / "p17.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_ledger(
        self,
        request_id: str,
        *,
        max_calls: int = MAX_28_PROVIDER_CALLS,
        director_limit: int = 1,
        member_routes: dict[str, Any] | None = None,
    ) -> ProviderCallLedger:
        return ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id=request_id,
            plan_hash=(request_id[0].lower() if request_id else "a") * 64,
            max_calls=max_calls,
            kind_call_limits={DIRECTOR_KIND: director_limit},
            skip_provider_ids=[],
            member_routes=member_routes,
        )

    @staticmethod
    def assert_director_budget(
        testcase: unittest.TestCase,
        snapshot: dict[str, Any],
        *,
        limit: int,
        reserved: int,
    ) -> None:
        testcase.assertTrue(snapshot["execution_policy_integrity_ok"])
        testcase.assertEqual(
            snapshot["execution_policy"]["kind_call_limits"],
            {DIRECTOR_KIND: limit},
        )
        testcase.assertEqual(snapshot["kind_call_limits"], {DIRECTOR_KIND: limit})
        testcase.assertEqual(
            snapshot["kind_call_budgets"][DIRECTOR_KIND],
            {
                "limit": limit,
                "reserved": reserved,
                "remaining": max(0, limit - reserved),
            },
        )

    def test_director_sub_budget_is_frozen_sealed_and_idempotent(self) -> None:
        ledger = self.create_ledger("a-frozen", director_limit=6)
        replay = self.create_ledger("a-frozen", director_limit=6)

        self.assertEqual(replay.run_id, ledger.run_id)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["max_calls"], MAX_28_PROVIDER_CALLS)
        self.assertEqual(snapshot["remaining_calls"], MAX_28_PROVIDER_CALLS)
        self.assert_director_budget(self, snapshot, limit=6, reserved=0)

        with self.assertRaisesRegex(ValueError, "idempotency key conflicts"):
            self.create_ledger("a-frozen", director_limit=7)

    def test_upgrade_adds_operation_column_before_creating_its_index(self) -> None:
        """A legacy ledger table must upgrade without requiring a fresh database."""

        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute("DROP INDEX IF EXISTS uq_provider_call_operation_id")
            connection.execute(
                "ALTER TABLE provider_call_attempts DROP COLUMN operation_id"
            )

        # Regression guard: CREATE INDEX(operation_id) must run only after the
        # compatibility column exists on an already-present v1 table.
        StudioStore(self.store.path)

        with closing(sqlite3.connect(self.store.path)) as connection:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(provider_call_attempts)"
                ).fetchall()
            }
            indexes = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA index_list(provider_call_attempts)"
                ).fetchall()
            }
        self.assertIn("operation_id", columns)
        self.assertIn("uq_provider_call_operation_id", indexes)

    def test_rejected_director_reservation_does_not_spend_global_budget(self) -> None:
        kind_error = _kind_budget_exception_type()
        ledger = self.create_ledger("b-independent", director_limit=1)
        first = ledger.reserve(
            kind=DIRECTOR_KIND,
            provider="deepseek",
            model="offline-model",
        )
        ledger.finish(
            str(first["id"]),
            str(first["attempt_token"]),
            status="INVALID",
            error_code="director_response_invalid_json",
        )

        with self.assertRaises(kind_error) as raised:
            ledger.reserve(
                kind=DIRECTOR_KIND,
                provider="deepseek",
                model="offline-model",
            )
        self.assertEqual(getattr(raised.exception, "code", ""), "provider_call_kind_budget_exhausted")
        self.assertEqual(getattr(raised.exception, "kind", ""), DIRECTOR_KIND)
        self.assertEqual(getattr(raised.exception, "max_calls", -1), 1)

        speaker = ledger.reserve(
            kind="round_speaker",
            provider="deepseek",
            model="offline-model",
        )
        ledger.finish(
            str(speaker["id"]),
            str(speaker["attempt_token"]),
            status="RESPONDED",
        )
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["reserved_calls"], 2)
        self.assertEqual(snapshot["remaining_calls"], 26)
        self.assert_director_budget(self, snapshot, limit=1, reserved=1)
        self.assertEqual(
            [attempt["kind"] for attempt in ledger.attempts()],
            [DIRECTOR_KIND, "round_speaker"],
        )

    def test_global_hard_limit_is_checked_before_kind_sub_budget(self) -> None:
        ledger = self.create_ledger(
            "c-global-first",
            max_calls=1,
            director_limit=0,
        )
        speaker = ledger.reserve(
            kind="round_speaker",
            provider="deepseek",
            model="offline-model",
        )
        ledger.finish(
            str(speaker["id"]),
            str(speaker["attempt_token"]),
            status="RESPONDED",
        )

        with self.assertRaises(ProviderCallBudgetExceeded) as raised:
            ledger.reserve(
                kind=DIRECTOR_KIND,
                provider="deepseek",
                model="offline-model",
            )
        self.assertIs(type(raised.exception), ProviderCallBudgetExceeded)
        self.assertEqual(raised.exception.code, "provider_call_budget_exhausted")
        self.assertEqual(len(ledger.attempts()), 1)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["remaining_calls"], 0)
        self.assert_director_budget(self, snapshot, limit=0, reserved=0)

    def test_concurrent_director_reservations_cannot_oversubscribe(self) -> None:
        kind_error = _kind_budget_exception_type()
        ledger = self.create_ledger("d-concurrent", director_limit=1)
        barrier = threading.Barrier(2)

        def reserve_once() -> str:
            barrier.wait(timeout=5)
            try:
                ledger.reserve(
                    kind=DIRECTOR_KIND,
                    provider="deepseek",
                    model="offline-model",
                )
                return "reserved"
            except kind_error:
                return "kind_exhausted"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(lambda _index: reserve_once(), range(2)))

        self.assertEqual(outcomes, ["kind_exhausted", "reserved"])
        self.assertEqual(len(ledger.attempts()), 1)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["reserved_calls"], 1)
        self.assert_director_budget(self, snapshot, limit=1, reserved=1)

    def test_tampered_kind_limit_fails_closed_without_a_reservation(self) -> None:
        ledger = self.create_ledger("e-tamper", director_limit=1)
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(provider_execution_runs)"
                ).fetchall()
            }
            required = {"execution_policy_json", "execution_policy_sha256"}
            if not required.issubset(columns):
                self.fail("provider execution runs must persist sealed kind call limits")
            row = connection.execute(
                "SELECT execution_policy_json FROM provider_execution_runs WHERE id=?",
                (ledger.run_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            policy = json.loads(str(row[0]))
            policy["kind_call_limits"][DIRECTOR_KIND] = 27
            connection.execute(
                """UPDATE provider_execution_runs
                   SET execution_policy_json=? WHERE id=?""",
                (
                    json.dumps(policy, sort_keys=True, separators=(",", ":")),
                    ledger.run_id,
                ),
            )

        try:
            snapshot = ledger.snapshot()
        except ValueError:
            pass
        else:
            self.assertFalse(snapshot["execution_policy_integrity_ok"])
            self.assertEqual(snapshot["execution_policy"], {})
            self.assertEqual(snapshot["kind_call_limits"], {})
            self.assertEqual(snapshot["kind_call_budgets"], {})
        with self.assertRaisesRegex(ValueError, "integrity"):
            ledger.reserve(
                kind=DIRECTOR_KIND,
                provider="deepseek",
                model="offline-model",
            )
        self.assertEqual(ledger.attempts(), [])

    def _make_flexible_candidates_semantically_tied(self) -> None:
        for member in self.store.enabled_members("room_plan"):
            if member.get("workflow_stage") != "flexible":
                continue
            self.store.update_member(
                "room_plan",
                str(member["id"]),
                {
                    "capabilities": sorted({
                        *list(member.get("capabilities") or []),
                        "critical_review",
                        "evidence_review",
                    }),
                },
            )

    def _member_routes(self) -> dict[str, Any]:
        return {
            "version": "provider_member_routes_v2",
            "members": sorted(
                [
                    {
                        "member_id": str(member["id"]),
                        "approved_member_version": int(member["version"]),
                        "provider": str(member.get("provider") or "deepseek"),
                        "model": str(member.get("model") or "offline-model"),
                        "turn_output_mode": "prompt_json",
                        "turn_envelope_version": TURN_ENVELOPE_VERSION,
                        "turn_envelope_schema_sha256": TURN_ENVELOPE_SCHEMA_SHA256,
                    }
                    for member in self.store.enabled_members("room_plan")
                ],
                key=lambda item: item["member_id"],
            ),
        }

    def _direct_selection_fixture(
        self,
        ledger: SnapshotLedger,
    ) -> tuple[
        FixedConvergenceOrchestrator,
        OfflineProvider,
        dict[str, Any],
        dict[str, Any],
    ]:
        members = self.store.enabled_members("room_plan")
        moderator = self.store.update_member(
            "room_plan",
            str(members[-1]["id"]),
            {"provider": "deepseek", "model": "offline-model"},
        )
        self.assertIsNotNone(moderator)
        room = dict(self.store.room_snapshot("room_plan")["room"])
        room.update({
            "moderator_member_id": str(moderator["id"]),
            "moderator_member_version": int(moderator["version"]),
            "moderator_provider": "deepseek",
            "moderator_model": "offline-model",
            "moderator_approved_route": {},
            "discussion_mode": "dynamic",
        })
        members = self.store.enabled_members("room_plan")
        round_row = self.store.create_round(
            "room_plan",
            "offline direct scheduler fixture",
        )
        provider = OfflineProvider()
        orchestrator = FixedConvergenceOrchestrator(
            self.store,
            OfflineRegistry(provider),
            market_service=None,
        )
        spoken_member_id = str(members[0]["id"])
        selection = orchestrator._select_next_member(
            room,
            default_workflow_policy("open_collaboration"),
            "offline direct scheduler fixture",
            members,
            {spoken_member_id: 1},
            set(),
            {spoken_member_id},
            set(),
            1,
            round_id=str(round_row["id"]),
            provider_call_ledger=ledger,  # type: ignore[arg-type]
        )
        return orchestrator, provider, room, selection

    def test_zero_global_and_required_continuation_never_calls_director(self) -> None:
        ledger = SnapshotLedger(
            global_remaining=0,
            director_remaining=1,
        )

        orchestrator, provider, room, selection = self._direct_selection_fixture(
            ledger
        )

        self.assertEqual(provider.director_calls, 0)
        self.assertEqual(ledger.reserve_calls, 0)
        self.assertEqual(selection["source"], "provider_call_budget_reserve")
        scheduling = selection["scheduling_context"]
        self.assertEqual(scheduling["global_remaining_calls"], 0)
        self.assertEqual(
            scheduling["minimum_remaining_visible_speaker_calls"],
            1,
        )
        decision = orchestrator._persist_director_decision(
            "room_plan",
            str(self.store.room_snapshot("room_plan")["latest_round"]["id"]),
            selection,
            room,
        )
        self.assertEqual(
            decision["moderator_context"]["scheduling_context"],
            scheduling,
        )
        self.assertTrue(str(decision.get("decision_sha256") or ""))

    def test_last_global_call_is_reserved_for_visible_continuation(self) -> None:
        ledger = SnapshotLedger(
            global_remaining=1,
            director_remaining=1,
        )

        _orchestrator, provider, _room, selection = (
            self._direct_selection_fixture(ledger)
        )

        self.assertEqual(provider.director_calls, 0)
        self.assertEqual(ledger.reserve_calls, 0)
        self.assertEqual(selection["source"], "provider_call_budget_reserve")
        self.assertEqual(
            selection["scheduling_context"][
                "minimum_remaining_visible_speaker_calls"
            ],
            1,
        )

    def test_director_kind_budget_race_cancels_attempt_and_falls_back(self) -> None:
        ledger = SnapshotLedger(
            global_remaining=10,
            director_remaining=1,
            raise_kind_on_reserve=True,
        )

        _orchestrator, provider, _room, selection = (
            self._direct_selection_fixture(ledger)
        )

        self.assertEqual(provider.director_calls, 0)
        self.assertEqual(ledger.reserve_calls, 1)
        self.assertEqual(selection["source"], "director_call_budget_exhausted")
        round_id = str(self.store.room_snapshot("room_plan")["latest_round"]["id"])
        attempts = self.store.list_director_attempts(
            "room_plan",
            round_id=round_id,
        )
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "CANCELLED")
        self.assertEqual(
            attempts[0]["error_code"],
            "provider_call_kind_budget_exhausted",
        )

    def test_zero_director_budget_uses_safe_fallback_without_provider_call(self) -> None:
        self._make_flexible_candidates_semantically_tied()
        provider = OfflineProvider()
        ledger = self.create_ledger(
            "f-runtime-fallback",
            director_limit=0,
            member_routes=self._member_routes(),
        )

        events = list(
            DiscussionOrchestrator(
                self.store,
                OfflineRegistry(provider),
                market_service=None,
            ).run_round(
                "room_plan",
                "exercise the director sub-budget fallback",
                provider_call_ledger=ledger,
            )
        )

        self.assertEqual(provider.director_calls, 0)
        self.assertGreater(provider.speaker_calls, 0)
        self.assertFalse(any(
            event.get("code") == "PROVIDER_CALL_BUDGET_EXCEEDED"
            for event in events
        ))
        fallback_decisions = [
            event["decision"]
            for event in events
            if event.get("type") == "director_decision"
            and event.get("source") == "director_call_budget_exhausted"
        ]
        self.assertTrue(fallback_decisions)
        self.assertTrue(all(
            decision["moderator_context"]["decision_authority"]
            == "safety_fallback"
            and decision["moderator_context"]["model_used"] is False
            for decision in fallback_decisions
        ))
        self.assertNotIn(
            DIRECTOR_KIND,
            [attempt["kind"] for attempt in ledger.attempts()],
        )
        self.assertFalse(any(
            attempt["status"] == "STARTED"
            for attempt in self.store.list_director_attempts(
                "room_plan",
                round_id=str(next(
                    event["round"]["id"]
                    for event in events
                    if event.get("type") == "round_started"
                )),
            )
        ))
        self.assert_director_budget(self, ledger.snapshot(), limit=0, reserved=0)

    def test_runtime_global_limit_remains_terminal_when_both_are_empty(self) -> None:
        self._make_flexible_candidates_semantically_tied()
        provider = OfflineProvider()
        ledger = self.create_ledger(
            "a-runtime-global",
            max_calls=1,
            director_limit=0,
            member_routes=self._member_routes(),
        )

        events = list(
            DiscussionOrchestrator(
                self.store,
                OfflineRegistry(provider),
                market_service=None,
            ).run_round(
                "room_plan",
                "global ceiling outranks the director sub-budget",
                provider_call_ledger=ledger,
            )
        )

        self.assertEqual(provider.speaker_calls, 1)
        self.assertEqual(provider.director_calls, 0)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(events[-1]["code"], "PROVIDER_CALL_BUDGET_EXCEEDED")
        self.assertEqual(events[-1]["stage"], "speaker")
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["reserved_calls"], 1)
        self.assertEqual(snapshot["remaining_calls"], 0)
        self.assert_director_budget(self, snapshot, limit=0, reserved=0)


class DirectorSubBudgetAuthorizationTests(unittest.TestCase):
    """Exercise launch authorization directly, without opening a socket."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-p17-http-")
        self.store = StudioStore(Path(self.temp_dir.name) / "p17-http.sqlite3")
        self.providers = OfflineRegistry()
        self.orchestrator = LedgerRecordingOrchestrator(
            self.store,
            self.providers,
        )
        self.original_store = http_server.STORE
        self.original_providers = http_server.PROVIDERS
        self.original_orchestrator = http_server.ORCHESTRATOR
        http_server.STORE = self.store
        http_server.PROVIDERS = self.providers  # type: ignore[assignment]
        http_server.ORCHESTRATOR = self.orchestrator  # type: ignore[assignment]

    def tearDown(self) -> None:
        http_server.STORE = self.original_store
        http_server.PROVIDERS = self.original_providers
        http_server.ORCHESTRATOR = self.original_orchestrator
        self.temp_dir.cleanup()

    def test_confirmed_plan_freezes_director_limit_but_keeps_max_28_global(self) -> None:
        plan = RoundLaunchPlanService(self.store, self.providers).build(
            "room_plan",
            "freeze the independent director budget",
            set(),
        )
        director_limit = int(plan["calls"]["recommended_director_calls"])
        self.assertGreater(director_limit, 0)
        self.assertLess(director_limit, MAX_28_PROVIDER_CALLS)

        handler = object.__new__(http_server.StudioRequestHandler)
        handler.headers = {}  # type: ignore[assignment]
        handler.wfile = io.BytesIO()  # type: ignore[assignment]
        response_statuses: list[int] = []
        response_headers: list[tuple[str, str]] = []
        json_errors: list[tuple[dict[str, Any], int]] = []
        handler.send_response = (  # type: ignore[method-assign]
            lambda status, message=None: response_statuses.append(int(status))
        )
        handler.send_header = (  # type: ignore[method-assign]
            lambda name, value: response_headers.append((str(name), str(value)))
        )
        handler.end_headers = lambda: None  # type: ignore[method-assign]
        handler._send_json = (  # type: ignore[method-assign]
            lambda payload, status=HTTPStatus.OK: json_errors.append(
                (dict(payload), int(status))
            )
        )

        handler._stream_round_locked(
            "room_plan",
            str(plan["objective"]),
            None,
            skip_provider_ids=set(),
            client_round_request_id="p17-direct-handler",
            plan_hash=str(plan["plan_hash"]),
            max_provider_calls=MAX_28_PROVIDER_CALLS,
        )

        self.assertEqual(json_errors, [])
        self.assertIn(int(HTTPStatus.OK), response_statuses)
        self.assertTrue(self.orchestrator.ledgers)
        ledger = self.orchestrator.ledgers[0]
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["max_calls"], MAX_28_PROVIDER_CALLS)
        self.assertEqual(snapshot["reserved_calls"], 1)
        self.assertEqual(snapshot["remaining_calls"], 27)
        DirectorCallSubBudgetTests.assert_director_budget(
            self,
            snapshot,
            limit=director_limit,
            reserved=0,
        )
        events = [
            json.loads(line)
            for line in handler.wfile.getvalue().decode("utf-8").splitlines()
            if line.strip()
        ]
        authorization = next(
            event for event in events if event.get("type") == "round_authorization"
        )
        self.assertEqual(
            authorization["max_provider_calls"],
            MAX_28_PROVIDER_CALLS,
        )
        self.assertEqual(
            authorization["kind_call_limits"],
            {DIRECTOR_KIND: director_limit},
        )
        self.assertEqual(
            authorization["recommended_director_calls"],
            director_limit,
        )
        self.assertEqual(
            self.providers.preflight_ledgers[0].run_id,
            ledger.run_id,
        )


if __name__ == "__main__":
    unittest.main()
