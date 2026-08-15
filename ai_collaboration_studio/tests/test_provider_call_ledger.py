from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

# backend.store creates its module-level default store at import time. Force that
# harmless side effect into a disposable location before importing the backend.
_IMPORT_TEMP_DIR = tempfile.TemporaryDirectory(prefix="ai-studio-ledger-import-")
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

from backend.provider_call_ledger import (  # noqa: E402
    ProviderCallBudgetExceeded,
    ProviderCallLedger,
)
from backend.store import StudioStore  # noqa: E402

for _key, _value in _PREVIOUS_IMPORT_ENV.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value


class ProviderCallLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-ledger-test-")
        self.db_path = Path(self.temp_dir.name) / "ledger.sqlite3"
        self.store = StudioStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_ledger(
        self,
        request_id: str,
        *,
        max_calls: int = 28,
        plan: object | None = None,
        skip_provider_ids: object = None,
        artifact_route: object = None,
        member_routes: object = None,
    ) -> ProviderCallLedger:
        return ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="discussion_round",
            client_request_id=request_id,
            plan={"request": request_id} if plan is None else plan,
            max_calls=max_calls,
            skip_provider_ids=skip_provider_ids,
            artifact_route=artifact_route,
            member_routes=member_routes,
        )

    def approved_member_routes(self) -> dict[str, object]:
        members = [
            {
                "member_id": str(member["id"]),
                "approved_member_version": int(member["version"]),
                "provider": str(member.get("provider") or "deepseek").lower(),
                "model": f"approved-model-{index}",
            }
            for index, member in enumerate(
                self.store.enabled_members("room_plan"),
                start=1,
            )
        ]
        members.sort(key=lambda item: item["member_id"])
        return {
            "version": "provider_member_routes_v1",
            "members": members,
        }

    def test_migration_is_idempotent_and_self_heals_missing_tables(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TABLE provider_call_attempts")
            connection.execute("DROP TABLE provider_execution_runs")

        StudioStore(self.db_path)
        StudioStore(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='table' AND name LIKE 'provider_%'"""
                ).fetchall()
            }
            migration_count = int(connection.execute(
                """SELECT COUNT(*) FROM schema_migrations
                   WHERE key='provider_call_ledger_v1'"""
            ).fetchone()[0])
        self.assertEqual(
            tables,
            {"provider_execution_runs", "provider_call_attempts"},
        )
        self.assertEqual(migration_count, 1)

    def test_create_is_idempotent_and_rejects_parameter_conflicts(self) -> None:
        first = self.create_ledger(
            "idem-1",
            max_calls=7,
            plan={"members": ["a", "b"]},
            skip_provider_ids=["OpenAI", "openai"],
        )
        replay = self.create_ledger(
            "idem-1",
            max_calls=7,
            plan={"members": ["a", "b"]},
            skip_provider_ids=["openai"],
        )
        self.assertEqual(first.run_id, replay.run_id)
        self.assertEqual(replay.snapshot()["skip_policy"]["provider_ids"], ["openai"])

        conflict_cases = (
            {"max_calls": 8, "plan": {"members": ["a", "b"]}, "skip": ["openai"]},
            {"max_calls": 7, "plan": {"members": ["a"]}, "skip": ["openai"]},
            {"max_calls": 7, "plan": {"members": ["a", "b"]}, "skip": ["glm"]},
        )
        for case in conflict_cases:
            with self.subTest(case=case), self.assertRaisesRegex(
                ValueError,
                "idempotency key conflicts",
            ):
                self.create_ledger(
                    "idem-1",
                    max_calls=int(case["max_calls"]),
                    plan=case["plan"],
                    skip_provider_ids=case["skip"],
                )

    def test_verified_plan_hash_is_persisted_without_re_fingerprinting(self) -> None:
        confirmed_plan_hash = "a" * 64
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="discussion_round",
            client_request_id="confirmed-plan-hash",
            plan_hash=confirmed_plan_hash,
            max_calls=2,
        )
        self.assertEqual(ledger.snapshot()["plan_hash"], confirmed_plan_hash)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            ProviderCallLedger.create(
                self.store,
                "room_plan",
                scope="artifact_generation",
                client_request_id="ambiguous-plan-input",
                plan={"x": 1},
                plan_hash=confirmed_plan_hash,
                max_calls=1,
            )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            ProviderCallLedger.create(
                self.store,
                "room_plan",
                scope="artifact_generation",
                client_request_id="missing-plan-input",
                max_calls=1,
            )

    def test_artifact_route_is_sealed_and_part_of_idempotency(self) -> None:
        member = self.store.enabled_members("room_plan")[0]
        route = {
            "member_id": str(member["id"]),
            "member_version": int(member["version"]),
            "provider": str(member.get("provider") or "openai").lower(),
            "model": str(member.get("model") or "") or "resolved-default-model",
        }
        ledger = self.create_ledger(
            "artifact-route",
            artifact_route=route,
        )
        snapshot = ledger.snapshot()
        self.assertTrue(snapshot["artifact_route_integrity_ok"])
        self.assertEqual(snapshot["artifact_route"], route)

        replay = self.create_ledger(
            "artifact-route",
            artifact_route=dict(route),
        )
        self.assertEqual(replay.run_id, ledger.run_id)
        with self.assertRaisesRegex(ValueError, "idempotency key conflicts"):
            self.create_ledger(
                "artifact-route",
                artifact_route={**route, "model": "different-model"},
            )

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE provider_execution_runs SET artifact_route_json='{}' WHERE id=?",
                (ledger.run_id,),
            )
        tampered = ledger.snapshot()
        self.assertFalse(tampered["artifact_route_integrity_ok"])
        self.assertEqual(tampered["artifact_route"], {})

    def test_member_routes_are_sealed_idempotent_and_tamper_evident(self) -> None:
        manifest = self.approved_member_routes()
        ledger = self.create_ledger(
            "member-routes-sealed",
            member_routes=manifest,
        )
        snapshot = ledger.snapshot()
        self.assertTrue(snapshot["member_routes_present"])
        self.assertTrue(snapshot["member_routes_integrity_ok"])
        self.assertEqual(snapshot["member_routes"], manifest)

        replay = self.create_ledger(
            "member-routes-sealed",
            member_routes=json.loads(json.dumps(manifest)),
        )
        self.assertEqual(replay.run_id, ledger.run_id)
        changed = json.loads(json.dumps(manifest))
        changed["members"][0]["model"] = "unapproved-model"
        with self.assertRaisesRegex(ValueError, "idempotency key conflicts"):
            self.create_ledger(
                "member-routes-sealed",
                member_routes=changed,
            )

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE provider_execution_runs
                   SET member_routes_json='{}' WHERE id=?""",
                (ledger.run_id,),
            )
        tampered = ledger.snapshot()
        self.assertTrue(tampered["member_routes_present"])
        self.assertFalse(tampered["member_routes_integrity_ok"])
        self.assertEqual(tampered["member_routes"], {})
        with self.assertRaisesRegex(ValueError, "integrity verification"):
            ledger.reserve(
                kind="preflight_probe",
                provider="deepseek",
                model="approved-model-1",
            )
        self.assertEqual(ledger.snapshot()["reserved_calls"], 0)

    def test_sealed_member_routes_bound_preflight_and_latest_identity_call(self) -> None:
        manifest = self.approved_member_routes()
        first_route = manifest["members"][0]
        second_route = manifest["members"][1]
        ledger = self.create_ledger(
            "member-route-enforcement",
            max_calls=4,
            member_routes=manifest,
        )

        preflight = ledger.reserve(
            kind="preflight_probe",
            provider=str(first_route["provider"]),
            model=str(first_route["model"]),
        )
        ledger.finish(
            str(preflight["id"]),
            str(preflight["attempt_token"]),
            status="RESPONDED",
        )
        with self.assertRaisesRegex(ValueError, "outside the approved"):
            ledger.reserve(
                kind="preflight_probe",
                provider=str(first_route["provider"]),
                model="model-not-in-plan",
            )

        current = self.store.get_member(
            "room_plan",
            str(first_route["member_id"]),
        )
        self.assertIsNotNone(current)
        revised = self.store.update_member(
            "room_plan",
            str(first_route["member_id"]),
            {
                "identity": "最新身份仍使用本轮已确认路由",
                "provider": "doubao",
                "model": "edited-next-round-model",
            },
            expected_version=int(current["version"]),
        )
        member_call = ledger.reserve(
            kind="round_speaker",
            provider=str(first_route["provider"]),
            model=str(first_route["model"]),
            member_id=str(first_route["member_id"]),
            member_version=int(revised["version"]),
        )
        self.assertEqual(member_call["member_version"], int(revised["version"]))
        self.assertEqual(member_call["model"], str(first_route["model"]))
        with self.assertRaisesRegex(ValueError, "member's approved route"):
            ledger.reserve(
                kind="round_speaker",
                provider=str(second_route["provider"]),
                model=str(second_route["model"]),
                member_id=str(first_route["member_id"]),
                member_version=int(revised["version"]),
            )
        self.assertEqual(ledger.snapshot()["reserved_calls"], 2)

    def test_twenty_eight_reservations_succeed_and_twenty_ninth_fails_closed(self) -> None:
        ledger = self.create_ledger("limit-28", max_calls=28)
        reservations = [
            ledger.reserve(kind="member_turn", provider="deepseek", model="deepseek-chat")
            for _ in range(28)
        ]
        self.assertEqual([item["sequence_no"] for item in reservations], list(range(1, 29)))
        with self.assertRaises(ProviderCallBudgetExceeded) as raised:
            ledger.reserve(kind="member_turn", provider="deepseek")
        self.assertEqual(raised.exception.code, "provider_call_budget_exhausted")

        run = ledger.snapshot()
        self.assertEqual(run["reserved_calls"], 28)
        self.assertEqual(run["completed_calls"], 0)
        self.assertEqual(run["remaining_calls"], 0)
        self.assertEqual(run["status"], "EXHAUSTED")
        self.assertEqual(len(ledger.attempts()), 28)

    def test_begin_immediate_enforces_budget_across_concurrent_store_instances(self) -> None:
        ledger = self.create_ledger("concurrent-28", max_calls=28)
        worker_count = 40
        stores = [StudioStore(self.db_path) for _ in range(worker_count)]
        barrier = threading.Barrier(worker_count)

        def reserve(index: int) -> tuple[str, int | str]:
            barrier.wait(timeout=10)
            try:
                result = stores[index].reserve_provider_call(
                    ledger.run_id,
                    kind="member_turn",
                    provider="deepseek",
                )
                return "ok", int(result["sequence_no"])
            except ProviderCallBudgetExceeded as exc:
                return "budget", exc.code

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            outcomes = list(executor.map(reserve, range(worker_count)))

        successes = sorted(int(value) for status, value in outcomes if status == "ok")
        failures = [value for status, value in outcomes if status == "budget"]
        self.assertEqual(successes, list(range(1, 29)))
        self.assertEqual(len(failures), 12)
        self.assertTrue(all(value == "provider_call_budget_exhausted" for value in failures))
        self.assertEqual(ledger.snapshot()["reserved_calls"], 28)
        self.assertEqual(len(ledger.attempts()), 28)

    def test_new_store_recovers_run_and_continues_sequence_without_refund(self) -> None:
        ledger = self.create_ledger("restart", max_calls=5)
        ledger.reserve(kind="director", provider="deepseek")
        ledger.reserve(kind="member_turn", provider="glm")

        restarted_store = StudioStore(self.db_path)
        resumed = ProviderCallLedger.resume(restarted_store, ledger.run_id)
        self.assertEqual(resumed.snapshot()["reserved_calls"], 2)
        third = resumed.reserve(kind="artifact", provider="doubao")
        self.assertEqual(third["sequence_no"], 3)
        self.assertEqual(resumed.snapshot()["reserved_calls"], 3)

    def test_finish_is_token_guarded_idempotent_and_usage_is_numeric_only(self) -> None:
        ledger = self.create_ledger(
            "finish-safe",
            max_calls=2,
            plan={"api_key": "plan-secret-must-only-be-hashed"},
        )
        first = ledger.reserve(kind="member_turn", provider="deepseek")
        with self.assertRaisesRegex(ValueError, "token is stale or invalid"):
            ledger.finish(
                str(first["id"]),
                "wrong-token",
                status="RESPONDED",
            )

        finished = ledger.finish(
            str(first["id"]),
            str(first["attempt_token"]),
            status="RESPONDED",
            elapsed_ms=42,
            usage={
                "input_tokens": 11,
                "output_tokens": 7,
                "cost": 0.0125,
                "cached": {"read_tokens": 3},
                "prompt": "usage-secret-must-not-persist",
                "authorization": 123,
                "negative": -1,
                "not_finite": math.nan,
                "boolean": True,
            },
        )
        self.assertEqual(
            finished["usage"],
            {
                "cached.read_tokens": 3,
                "cost": 0.0125,
                "input_tokens": 11,
                "output_tokens": 7,
            },
        )
        self.assertNotIn("attempt_token", finished)
        self.assertNotIn("attempt_token_sha256", finished)
        self.assertNotIn("usage_json", finished)

        replay = ledger.finish(
            str(first["id"]),
            str(first["attempt_token"]),
            status="RESPONDED",
            usage={"prompt": "different-secret"},
        )
        self.assertEqual(replay, finished)
        with self.assertRaisesRegex(ValueError, "another status"):
            ledger.finish(
                str(first["id"]),
                str(first["attempt_token"]),
                status="FAILED",
            )

        second = ledger.reserve(kind="member_turn", provider="glm")
        failed = ledger.finish(
            str(second["id"]),
            str(second["attempt_token"]),
            status="FAILED",
            error_code="provider_timeout",
            elapsed_ms=100,
        )
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(ledger.snapshot()["status"], "COMPLETED")
        self.assertEqual(ledger.snapshot()["completed_calls"], 2)

        with closing(sqlite3.connect(self.db_path)) as connection:
            raw_attempt = connection.execute(
                """SELECT usage_json,attempt_token_sha256
                   FROM provider_call_attempts WHERE id=?""",
                (str(first["id"]),),
            ).fetchone()
            serialized_provider_tables = json.dumps(connection.execute(
                """SELECT plan_hash,skip_policy_json FROM provider_execution_runs
                   WHERE id=?""",
                (ledger.run_id,),
            ).fetchone()) + str(raw_attempt)
        self.assertEqual(len(str(raw_attempt[1])), 64)
        self.assertNotIn("usage-secret", str(raw_attempt[0]))
        self.assertNotIn("plan-secret", serialized_provider_tables)

    def test_abandoned_calls_remain_charged_and_public_projection_is_safe(self) -> None:
        ledger = self.create_ledger("abandon", max_calls=3)
        reservations = [
            ledger.reserve(kind="member_turn", provider="deepseek")
            for _ in range(2)
        ]
        self.assertEqual(ledger.abandon_started(), 2)
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot["reserved_calls"], 2)
        self.assertEqual(snapshot["completed_calls"], 2)
        self.assertEqual(snapshot["remaining_calls"], 1)
        self.assertEqual({item["status"] for item in ledger.attempts()}, {"ABANDONED"})

        last = ledger.reserve(kind="member_turn", provider="deepseek")
        self.assertEqual(last["sequence_no"], 3)
        self.assertEqual(ledger.abandon_started(), 1)
        self.assertEqual(ledger.snapshot()["status"], "COMPLETED")
        with self.assertRaises(ProviderCallBudgetExceeded):
            ledger.reserve(kind="member_turn", provider="deepseek")

        public_payload = json.dumps(
            {"run": ledger.snapshot(), "attempts": ledger.attempts()},
            ensure_ascii=False,
            sort_keys=True,
        ).lower()
        self.assertNotIn("attempt_token", public_payload)
        self.assertNotIn("prompt", public_payload)
        self.assertNotIn("response_body", public_payload)
        self.assertNotIn("api_key", public_payload)
        self.assertTrue(all("usage_sha256" in item for item in ledger.attempts()))
        self.assertTrue(all("attempt_token" in item for item in reservations))

    def test_round_binding_is_late_and_same_value_idempotent(self) -> None:
        ledger = self.create_ledger("late-round", max_calls=1)
        self.assertEqual(ledger.snapshot()["round_id"], "")
        round_row = self.store.create_round("room_plan", "Bind this provider run")
        bound = ledger.bind_round(str(round_row["id"]))
        replay = ledger.bind_round(str(round_row["id"]))
        self.assertEqual(bound["round_id"], str(round_row["id"]))
        self.assertEqual(replay, bound)

        resumed = ProviderCallLedger.resume_for_round(
            StudioStore(self.db_path),
            "room_plan",
            str(round_row["id"]),
            scope="discussion_round",
        )
        self.assertEqual(resumed.run_id, ledger.run_id)

        duplicate_scope = self.create_ledger("same-round-same-scope", max_calls=1)
        with self.assertRaisesRegex(ValueError, "already has a run for this scope"):
            duplicate_scope.bind_round(str(round_row["id"]))

        artifact_ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="artifact_generation",
            client_request_id="same-round-other-scope",
            plan={"artifact": "minutes"},
            max_calls=1,
        )
        artifact_ledger.bind_round(str(round_row["id"]))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            ProviderCallLedger.resume_for_round(
                self.store,
                "room_plan",
                str(round_row["id"]),
            )
        resumed_artifact = ProviderCallLedger.resume_for_round(
            self.store,
            "room_plan",
            str(round_row["id"]),
            scope="artifact_generation",
        )
        self.assertEqual(resumed_artifact.run_id, artifact_ledger.run_id)


if __name__ == "__main__":
    unittest.main()
