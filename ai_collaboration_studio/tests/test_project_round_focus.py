from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from copy import deepcopy
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.request import urlopen

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend import http_server
from backend.decision_lineage import canonical_sha256
from backend.orchestrator import DiscussionOrchestrator
from backend.project_round_focus import (
    ProjectRoundFocusService,
)
from backend.round_contexts import (
    RoundContextError,
    build_round_context_authorization_set,
    round_context_authorization_entry,
)
from backend.provider_preflight import ProviderPreflightService
from backend.round_launch_plan import (
    ROUND_LAUNCH_PLAN_VERSION,
    ROUND_LAUNCH_PLAN_VERSION_V5,
    RoundLaunchPlanService,
)
from backend.store import StudioStore


class LocalRegistry:
    disabled_provider_ids = frozenset()

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.provider = type(
            "NoCallProvider",
            (),
            {
                "provider_id": "deepseek",
                "generate": lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    AssertionError("orchestrator setup must not call Provider")
                ),
            },
        )()

    def status(self) -> list[dict[str, Any]]:
        self.calls.append("status")
        return [{
            "id": "deepseek",
            "name": "DeepSeek",
            "model": "deepseek-test",
            "configured": True,
            "policy_disabled": False,
        }]

    def preflight(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls.append("preflight")
        raise AssertionError("planning/configuration projection must not call Provider")

    def get(self, _provider_id: str) -> Any:
        self.calls.append("get")
        return self.provider

    def generate(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls.append("generate")
        raise AssertionError("planning/configuration projection must not call Provider")


class ProjectRoundFocusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-p27-",
            ignore_cleanup_errors=True,
        )
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_room(self, *, focus: bool = True) -> dict[str, Any]:
        result = self.store.create_room(
            "P27 focus",
            "Define the next bounded project step.",
            capability_pack_ids=["project_round_focus"] if focus else [],
        )
        room = result["room"]
        for member in result.get("members") or []:
            if member.get("enabled") is True:
                self.store.update_member(
                    room["id"],
                    member["id"],
                    {"provider": "deepseek", "model": "deepseek-test"},
                    expected_version=member["version"],
                )
        return self.store.room_snapshot(room["id"])["room"]

    @staticmethod
    def authorization(preview: dict[str, Any]) -> dict[str, Any]:
        artifact = preview["artifact_binding"]
        binding = (
            {"status": "none"}
            if artifact["status"] == "none"
            else {
                "status": "exact",
                "artifact_id": artifact["artifact_id"],
                "artifact_version": artifact["artifact_version"],
            }
        )
        return {
            "version": "project_round_focus_authorization_v1",
            "artifact_binding": binding,
            "preview_sha256": preview["preview_sha256"],
            "user_confirmed": True,
        }

    def create_confirmed_artifact(self, room_id: str) -> dict[str, Any]:
        material = self.store.add_material(room_id, {
            "title": "Evidence",
            "kind": "note",
            "content": "Exact local evidence.",
        })
        evidence = [{
            "type": "material",
            "id": material["id"],
            "evidence_role": "support",
            "verification_status": "source_checked",
            "review_note": "checked",
        }]
        artifact = self.store.create_artifact(
            room_id,
            title="Frozen plan",
            content={
                "summary": "A bounded plan.",
                "summary_evidence": evidence,
                "requirements": [{
                    "id": "req_one",
                    "text": "Ship the isolated slice.",
                    "status": "confirmed",
                    "owner": "owner_one",
                    "acceptance_criteria": "All isolated checks pass.",
                    "evidence": evidence,
                }],
                "risks": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        )
        return self.store.confirm_artifact(
            room_id,
            artifact["id"],
            expected_version=artifact["version"],
            confirmed_by="user",
        )

    def counts(self) -> tuple[int, int, int]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("rounds", "round_domain_contexts", "messages")
            )

    def test_bootstrap_and_exact_preview_freeze_one_atomic_context(self) -> None:
        room = self.create_room()
        service = ProjectRoundFocusService(self.store)
        bootstrap = service.preview(room["id"])
        self.assertEqual(bootstrap["state"], "bootstrap")
        self.assertEqual(bootstrap["artifact_binding"]["status"], "none")
        self.assertEqual(bootstrap["focus_items"], [])
        self.assertEqual(set(bootstrap["counts"].values()), {0})

        prepared = service.prepare_authorized(
            room["id"], self.authorization(bootstrap)
        )
        before = self.counts()
        round_row = self.store.create_formal_round(
            room["id"],
            bootstrap["suggested_objective"],
            project_round_focus_prepared=prepared,
        )
        self.assertEqual(
            self.counts(),
            (before[0] + 1, before[1] + 1, before[2]),
        )
        record = self.store.get_round_project_focus(room["id"], round_row["id"])
        self.assertTrue(record["integrity_ok"])
        self.assertRegex(record["frozen_at"], r"^\d{4}-\d\d-\d\dT.*\.\d{3}Z$")
        self.assertEqual(record["preview_sha256"], bootstrap["preview_sha256"])

        second_room = self.create_room()
        artifact = self.create_confirmed_artifact(second_room["id"])
        exact = service.preview(second_room["id"])
        self.assertEqual(exact["artifact_binding"]["status"], "exact")
        self.assertEqual(exact["artifact_binding"]["artifact_id"], artifact["id"])
        workspace = service.legacy_workspace_from_preview(exact)
        self.assertTrue(workspace["applicable"])
        self.assertEqual(workspace["artifact_status"], "CONFIRMED")

    def test_source_drift_and_insert_fault_leave_no_partial_round(self) -> None:
        room = self.create_room()
        service = ProjectRoundFocusService(self.store)
        preview = service.preview(room["id"])
        prepared = service.prepare_authorized(
            room["id"], self.authorization(preview)
        )
        before = self.counts()
        current = self.store.room_snapshot(room["id"])["room"]
        self.store.update_room(room["id"], {
            "expected_settings_version": current["settings_version"],
            "objective": "A changed behavioral objective.",
        })
        with self.assertRaises(RoundContextError) as drift:
            self.store.create_formal_round(
                room["id"], "start", project_round_focus_prepared=prepared
            )
        self.assertIn("DRIFT", drift.exception.code)
        self.assertEqual(self.counts(), before)

        preview = service.preview(room["id"])
        prepared = service.prepare_authorized(
            room["id"], self.authorization(preview)
        )
        before_fault = self.counts()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """CREATE TRIGGER p27_fail_context BEFORE INSERT
                     ON round_domain_contexts
                     BEGIN SELECT RAISE(ABORT,'injected context failure'); END"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_formal_round(
                room["id"], "start", project_round_focus_prepared=prepared
            )
        self.assertEqual(self.counts(), before_fault)

    def test_v4_plan_and_configuration_only_provider_check(self) -> None:
        room = self.create_room()
        preview = ProjectRoundFocusService(self.store).preview(room["id"])
        authorization = self.authorization(preview)
        registry = LocalRegistry()
        plan_service = RoundLaunchPlanService(self.store, registry)
        plan = plan_service.build(
            room["id"],
            "Discuss the sealed next step.",
            set(),
            authorization,
        )
        self.assertEqual(plan["version"], ROUND_LAUNCH_PLAN_VERSION_V5)
        self.assertEqual(
            plan["round_context_authorizations"],
            build_round_context_authorization_set([
                round_context_authorization_entry(
                    "project_round_focus",
                    "core.round.context/v1",
                    authorization,
                ),
            ]),
        )
        self.assertNotIn("project_round_focus_authorization", plan)
        ProviderPreflightService._validate_launch_plan(room["id"], plan)
        self.assertNotIn("preflight", registry.calls)
        self.assertNotIn("generate", registry.calls)

        config_plan = plan_service.build(
            room["id"],
            "Local configuration only",
            set(),
            deepcopy(authorization) | {"preview_sha256": "0" * 64},
            configuration_only=True,
        )
        self.assertEqual(config_plan["version"], ROUND_LAUNCH_PLAN_VERSION)
        self.assertNotIn("project_round_focus_authorization", config_plan)
        config = http_server.StudioRequestHandler._local_configuration_preflight(
            config_plan
        )
        self.assertTrue(config["ready"])
        self.assertEqual(config["external_call_count"], 0)

        malformed_config_plan = plan_service.build(
            room["id"],
            "Local configuration ignores malformed focus authorization",
            set(),
            {"unexpected": "raw"},
            configuration_only=True,
        )
        self.assertEqual(
            malformed_config_plan["version"], ROUND_LAUNCH_PLAN_VERSION
        )
        self.assertNotIn(
            "project_round_focus_authorization", malformed_config_plan
        )
        malformed_config = (
            http_server.StudioRequestHandler._local_configuration_preflight(
                malformed_config_plan
            )
        )
        self.assertTrue(malformed_config["ready"])
        self.assertEqual(malformed_config["external_call_count"], 0)

        no_focus = self.create_room(focus=False)
        no_focus_plan = RoundLaunchPlanService(
            self.store, LocalRegistry()
        ).build(no_focus["id"], "No focus compatibility")
        self.assertEqual(no_focus_plan["version"], ROUND_LAUNCH_PLAN_VERSION)
        self.assertNotIn("project_round_focus_authorization", no_focus_plan)

    def test_v4_authorization_reaches_orchestrator_atomic_round_context(self) -> None:
        room = self.create_room()
        preview = ProjectRoundFocusService(self.store).preview(room["id"])
        authorization = self.authorization(preview)
        registry = LocalRegistry()
        objective = "Discuss the sealed project-round focus."
        launch_plan = RoundLaunchPlanService(self.store, registry).build(
            room["id"],
            objective,
            project_round_focus_authorization=authorization,
        )
        before = self.counts()
        stream = DiscussionOrchestrator(
            self.store,
            registry,
            market_service=None,
        ).run_round(
            room["id"],
            objective,
            expected_launch_plan_hash=launch_plan["plan_hash"],
            project_round_focus_authorization=authorization,
        )
        started: dict[str, Any] | None = None
        try:
            for event in stream:
                if event.get("type") == "error":
                    self.fail(f"orchestrator rejected sealed focus: {event}")
                if event.get("type") == "round_started":
                    started = event
                    break
        finally:
            stream.close()
        self.assertIsNotNone(started)
        self.assertEqual(
            self.counts(),
            (before[0] + 1, before[1] + 1, before[2] + 1),
        )
        round_id = str((started or {}).get("round", {}).get("id") or "")
        frozen = self.store.get_round_project_focus(room["id"], round_id)
        self.assertIsNotNone(frozen)
        self.assertTrue((frozen or {})["integrity_ok"])
        self.assertEqual((frozen or {})["preview_sha256"], preview["preview_sha256"])
        resumed = self.store.resume_round(room["id"], round_id)
        self.assertIsNotNone(resumed)
        self.assertEqual((resumed or {})["status"], "RUNNING")
        resumed_focus = self.store.get_round_project_focus(room["id"], round_id)
        self.assertTrue((resumed_focus or {})["integrity_ok"])
        self.assertEqual(
            (resumed_focus or {})["preview_sha256"], preview["preview_sha256"]
        )
        self.assertNotIn("preflight", registry.calls)
        self.assertNotIn("generate", registry.calls)

    def test_context_and_round_tamper_redacts_record_and_invalidates_trace(self) -> None:
        room = self.create_room()
        service = ProjectRoundFocusService(self.store)
        preview = service.preview(room["id"])
        round_row = self.store.create_formal_round(
            room["id"],
            "start",
            project_round_focus_prepared=service.prepare_authorized(
                room["id"], self.authorization(preview)
            ),
        )
        self.store.complete_round(round_row["id"], "PAUSED")
        healthy = self.store.get_round_project_focus(room["id"], round_row["id"])
        self.assertTrue(healthy["integrity_ok"])

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_round_domain_contexts_no_update")
            row = connection.execute(
                "SELECT preview_json FROM round_domain_contexts WHERE round_id=?",
                (round_row["id"],),
            ).fetchone()
            value = json.loads(row[0])
            value["suggested_objective"] = "tampered hidden content"
            connection.execute(
                "UPDATE round_domain_contexts SET preview_json=? WHERE round_id=?",
                (json.dumps(value), round_row["id"]),
            )
        hidden = self.store.get_round_project_focus(room["id"], round_row["id"])
        self.assertFalse(hidden["integrity_ok"])
        self.assertFalse(hidden["metrics_visible"])
        self.assertEqual(hidden["focus_items"], [])
        self.assertNotIn("tampered hidden content", json.dumps(hidden))
        trace = self.store.round_execution_trace(room["id"], round_row["id"], limit=5)
        self.assertEqual(trace["integrity"]["status"], "invalid")
        self.assertIn(
            "ROUND_DOMAIN_CONTEXT_INTEGRITY_FAILED",
            {item["code"] for item in trace["integrity"]["issues"]},
        )

    def test_round_registry_tamper_is_redacted_without_hiding_core_round(self) -> None:
        room = self.create_room()
        service = ProjectRoundFocusService(self.store)
        preview = service.preview(room["id"])
        round_row = self.store.create_formal_round(
            room["id"],
            "start",
            project_round_focus_prepared=service.prepare_authorized(
                room["id"], self.authorization(preview)
            ),
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE rounds SET plugin_registry_snapshot_json='{}' WHERE id=?",
                (round_row["id"],),
            )
        record = self.store.get_round_project_focus(room["id"], round_row["id"])
        self.assertFalse(record["integrity_ok"])
        self.assertIsNotNone(self.store.get_round(room["id"], round_row["id"]))
        trace = self.store.round_execution_trace(room["id"], round_row["id"], limit=5)
        self.assertEqual(trace["integrity"]["status"], "invalid")

    def test_terminal_trace_anchor_rejects_context_and_round_self_reseal(self) -> None:
        room = self.create_room()
        service = ProjectRoundFocusService(self.store)
        preview = service.preview(room["id"])
        round_row = self.store.create_formal_round(
            room["id"],
            "start",
            project_round_focus_prepared=service.prepare_authorized(
                room["id"], self.authorization(preview)
            ),
        )
        running = self.store.get_round_project_focus(room["id"], round_row["id"])
        self.assertTrue(running["integrity_ok"])
        self.store.complete_round(round_row["id"], "PAUSED")
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("DROP TRIGGER trg_round_domain_contexts_no_update")
            connection.execute("DROP TRIGGER trg_round_domain_context_anchor_no_update")
            context = dict(connection.execute(
                "SELECT * FROM round_domain_contexts WHERE round_id=?",
                (round_row["id"],),
            ).fetchone())
            changed_preview = json.loads(context["preview_json"])
            changed_preview["suggested_objective"] = "self resealed bait"
            changed_preview.pop("preview_sha256", None)
            changed_preview["preview_sha256"] = canonical_sha256(changed_preview)
            changed_auth = json.loads(context["authorization_json"])
            changed_auth["preview_sha256"] = changed_preview["preview_sha256"]
            connection.execute(
                """UPDATE round_domain_contexts
                      SET preview_json=?,preview_sha256=?,output_sha256=?,
                          authorization_json=?,authorization_sha256=?
                    WHERE round_id=?""",
                (
                    json.dumps(changed_preview),
                    changed_preview["preview_sha256"],
                    canonical_sha256(changed_preview),
                    json.dumps(changed_auth),
                    canonical_sha256(changed_auth),
                    round_row["id"],
                ),
            )
            changed_context = dict(connection.execute(
                "SELECT * FROM round_domain_contexts WHERE round_id=?",
                (round_row["id"],),
            ).fetchone())
            binding_sha256 = canonical_sha256(
                StudioStore._round_domain_context_binding_payload(changed_context)
            )
            connection.execute(
                "UPDATE round_domain_contexts SET binding_sha256=? WHERE round_id=?",
                (binding_sha256, round_row["id"]),
            )
            round_anchor = canonical_sha256({
                "version": "round_domain_context_anchor_v1",
                "binding_sha256s": [binding_sha256],
            })
            connection.execute(
                "UPDATE rounds SET round_domain_contexts_sha256=? WHERE id=?",
                (round_anchor, round_row["id"]),
            )
        hidden = self.store.get_round_project_focus(room["id"], round_row["id"])
        self.assertFalse(hidden["integrity_ok"])
        self.assertNotIn("self resealed bait", json.dumps(hidden))
        trace = self.store.round_execution_trace(room["id"], round_row["id"], limit=5)
        self.assertFalse(trace["integrity"]["snapshot_hash_persisted"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE rounds SET status='RUNNING' WHERE id=?",
                (round_row["id"],),
            )
        status_bypass = self.store.get_round_project_focus(
            room["id"], round_row["id"]
        )
        self.assertFalse((status_bypass or {})["integrity_ok"])
        self.assertNotIn("self resealed bait", json.dumps(status_bypass))

    def test_untrusted_round_timestamp_and_context_count_redact(self) -> None:
        def frozen_round() -> tuple[dict[str, Any], dict[str, Any]]:
            local_room = self.create_room()
            service = ProjectRoundFocusService(self.store)
            preview = service.preview(local_room["id"])
            local_round = self.store.create_formal_round(
                local_room["id"],
                "start",
                project_round_focus_prepared=service.prepare_authorized(
                    local_room["id"], self.authorization(preview)
                ),
            )
            return local_room, local_round

        timestamp_room, timestamp_round = frozen_round()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE rounds SET created_at='not-a-time' WHERE id=?",
                (timestamp_round["id"],),
            )
        timestamp_record = self.store.get_round_project_focus(
            timestamp_room["id"], timestamp_round["id"]
        )
        self.assertFalse((timestamp_record or {})["integrity_ok"])
        self.assertEqual(
            (timestamp_record or {})["frozen_at"],
            "1970-01-01T00:00:00.000Z",
        )

        count_room, count_round = frozen_round()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "DROP TRIGGER trg_round_domain_context_anchor_no_update"
            )
            connection.execute(
                "UPDATE rounds SET round_domain_context_count='not-a-count' WHERE id=?",
                (count_round["id"],),
            )
        count_record = self.store.get_round_project_focus(
            count_room["id"], count_round["id"]
        )
        self.assertFalse((count_record or {})["integrity_ok"])
        self.assertFalse((count_record or {})["metrics_visible"])


class ProjectRoundFocusHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-p27-http-",
            ignore_cleanup_errors=True,
        )
        self.original_store = http_server.STORE
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        http_server.STORE = self.store
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), http_server.StudioRequestHandler
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        http_server.STORE = self.original_store
        self.temp_dir.cleanup()

    def test_preview_and_frozen_record_get_routes(self) -> None:
        room = self.store.create_room(
            "P27 HTTP",
            "HTTP bootstrap objective",
            capability_pack_ids=["project_round_focus"],
        )["room"]
        preview_url = f"{self.base_url}/api/rooms/{room['id']}/project-round-focus"
        with urlopen(preview_url, timeout=5) as response:
            payload = json.loads(response.read())
        self.assertTrue(payload["ok"])
        preview = payload["project_round_focus"]
        service = ProjectRoundFocusService(self.store)
        authorization = ProjectRoundFocusTests.authorization(preview)
        round_row = self.store.create_formal_round(
            room["id"],
            "HTTP bootstrap objective",
            project_round_focus_prepared=service.prepare_authorized(
                room["id"], authorization
            ),
        )
        record_url = (
            f"{self.base_url}/api/rooms/{room['id']}/rounds/{round_row['id']}"
            "/project-round-focus"
        )
        with urlopen(record_url, timeout=5) as response:
            frozen = json.loads(response.read())
        self.assertTrue(frozen["ok"])
        self.assertEqual(
            frozen["project_round_focus"]["version"],
            "project_round_focus_record_v1",
        )
        self.assertEqual(
            frozen["project_round_focus"]["preview_sha256"],
            preview["preview_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
