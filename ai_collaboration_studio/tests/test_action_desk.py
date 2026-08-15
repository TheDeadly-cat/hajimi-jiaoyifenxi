from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.action_desk import (  # noqa: E402
    ACTION_TRANSITION_REQUEST_VERSION,
    ActionDeskError,
    action_event_payload,
    action_head_payload,
)
from backend import http_server  # noqa: E402
from backend.decision_lineage import canonical_sha256  # noqa: E402
from backend.store import StudioStore  # noqa: E402


class ActionDeskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "action-desk.sqlite3"
        self.store = StudioStore(self.db_path)
        self.room = self.store.create_room(
            "Action Desk",
            "Turn confirmed work into explicit user-owned follow-through.",
            capability_pack_ids=[],
        )["room"]
        self.artifact = self._create_confirmed_artifact()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_confirmed_artifact(self) -> dict:
        material = self.store.add_material(self.room["id"], {
            "title": "Action evidence",
            "kind": "note",
            "content": "A local source for the bounded action.",
        })
        evidence = [{
            "type": "material",
            "id": material["id"],
            "evidence_role": "support",
            "verification_status": "source_checked",
            "review_note": "checked",
        }]
        artifact = self.store.create_artifact(
            self.room["id"],
            title="Confirmed action plan",
            content={
                "summary": "One bounded follow-through item.",
                "summary_evidence": evidence,
                "requirements": [],
                "risks": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [{
                    "id": "action_one",
                    "text": "Prepare the isolated acceptance note.",
                    "owner": "Original owner",
                    "due": "2026-08-20",
                    "state": "open",
                    "evidence": evidence,
                }],
            },
        )
        confirmed = self.store.confirm_artifact(
            self.room["id"],
            artifact["id"],
            expected_version=artifact["version"],
            confirmed_by="user",
        )
        assert confirmed is not None
        return confirmed

    def _candidate(self) -> dict:
        desk = self.store.action_desk(self.room["id"])
        self.assertTrue(desk["integrity_ok"])
        self.assertEqual(len(desk["candidates"]), 1)
        return desk["candidates"][0]

    @staticmethod
    def _request(
        candidate: dict,
        *,
        request_id: str,
        transition: str = "adopt",
        revision: int = 0,
        patch: dict | None = None,
    ) -> dict:
        return {
            "version": ACTION_TRANSITION_REQUEST_VERSION,
            "client_request_id": request_id,
            "artifact_id": candidate["artifact_id"],
            "artifact_version": candidate["artifact_version"],
            "action_id": candidate["action_id"],
            "expected_action_snapshot_sha256": candidate["action_snapshot_sha256"],
            "expected_revision": revision,
            "transition": transition,
            "patch": copy.deepcopy(patch or {}),
            "user_confirmed": True,
        }

    def _business_counts(self) -> tuple[int, int, int, int]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "artifact_action_events",
                    "artifact_action_heads",
                    "artifact_versions",
                    "artifact_user_decisions",
                )
            )

    def _action_storage_counts(self) -> tuple[int, int, int, int]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "artifact_action_events",
                    "artifact_action_heads",
                    "artifact_action_anchors",
                    "artifact_action_anchor_heads",
                )
            )

    def _self_reseal_event_and_action_head(self, sequence_no: int, note: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("DROP TRIGGER trg_artifact_action_events_no_update")
            event = dict(connection.execute(
                "SELECT * FROM artifact_action_events WHERE sequence_no=?",
                (sequence_no,),
            ).fetchone())
            patch = json.loads(event["patch_json"])
            patch["note"] = note
            semantics = json.loads(event["request_semantics_json"])
            semantics["patch"] = copy.deepcopy(patch)
            item_snapshot = json.loads(event["item_snapshot_json"])
            item_snapshot["note"] = note
            semantics_sha256 = canonical_sha256(semantics)
            item_snapshot_sha256 = canonical_sha256(item_snapshot)
            event_payload = {
                "event_version": event["event_version"],
                "id": event["id"],
                "room_id": event["room_id"],
                "artifact_id": event["artifact_id"],
                "artifact_version": event["artifact_version"],
                "action_id": event["action_id"],
                "action_snapshot_sha256": event["action_snapshot_sha256"],
                "sequence_no": event["sequence_no"],
                "revision": event["revision"],
                "transition": event["transition"],
                "patch": patch,
                "item_snapshot": item_snapshot,
                "item_snapshot_sha256": item_snapshot_sha256,
                "client_request_id": event["client_request_id"],
                "request_semantics": semantics,
                "request_semantics_sha256": semantics_sha256,
                "previous_event_sha256": event["previous_event_sha256"],
                "created_at": event["created_at"],
            }
            event_sha256 = canonical_sha256(action_event_payload(event_payload))
            connection.execute(
                """UPDATE artifact_action_events SET
                    patch_json=?,item_snapshot_json=?,item_snapshot_sha256=?,
                    request_semantics_json=?,request_semantics_sha256=?,event_sha256=?
                   WHERE id=?""",
                (
                    json.dumps(patch, ensure_ascii=False),
                    json.dumps(item_snapshot, ensure_ascii=False),
                    item_snapshot_sha256,
                    json.dumps(semantics, ensure_ascii=False),
                    semantics_sha256,
                    event_sha256,
                    event["id"],
                ),
            )
            head = dict(connection.execute(
                "SELECT * FROM artifact_action_heads"
            ).fetchone())
            head_payload = {
                "head_version": head["head_version"],
                "room_id": head["room_id"],
                "artifact_id": head["artifact_id"],
                "artifact_version": head["artifact_version"],
                "action_id": head["action_id"],
                "action_snapshot_sha256": head["action_snapshot_sha256"],
                "revision": head["revision"],
                "sequence_no": head["sequence_no"],
                "event_count": head["event_count"],
                "head_event_sha256": event_sha256,
                "created_at": head["created_at"],
                "updated_at": head["updated_at"],
            }
            connection.execute(
                """UPDATE artifact_action_heads
                   SET head_event_sha256=?,head_sha256=?""",
                (
                    event_sha256,
                    canonical_sha256(action_head_payload(head_payload)),
                ),
            )

    def test_candidate_requires_adopt_then_cas_update_without_artifact_mutation(self) -> None:
        candidate = self._candidate()
        before = self._business_counts()
        result, created = self.store.transition_artifact_action(
            self.room["id"],
            self._request(
                candidate,
                request_id="action_adopt_1",
                patch={
                    "owner": "User owner",
                    "due": "2026-08-25",
                    "state": "in_progress",
                    "note": "Explicitly adopted by the user.",
                },
            ),
        )
        self.assertTrue(created)
        self.assertEqual(result["revision"], 1)
        after_adopt = self._business_counts()
        self.assertEqual(after_adopt[:2], (before[0] + 1, before[1] + 1))
        self.assertEqual(after_adopt[2:], before[2:])

        desk = self.store.action_desk(self.room["id"])
        self.assertEqual(desk["candidates"], [])
        self.assertEqual(len(desk["items"]), 1)
        item = desk["items"][0]
        self.assertEqual(item["owner"], "User owner")
        self.assertEqual(item["state"], "in_progress")
        self.assertEqual(item["note"], "Explicitly adopted by the user.")
        self.assertEqual(desk["counts"]["in_progress_count"], 1)
        self.assertEqual(desk["execution_capability"], "none")
        self.assertFalse(desk["external_write"])
        self.assertFalse(desk["can_autonomously_decide"])
        self.assertFalse(desk["can_replace_user_decision"])
        self.assertTrue(desk["user_final_decision_required"])

        update, update_created = self.store.transition_artifact_action(
            self.room["id"],
            self._request(
                candidate,
                request_id="action_update_1",
                transition="update",
                revision=1,
                patch={"state": "done", "note": "Acceptance note completed."},
            ),
        )
        self.assertTrue(update_created)
        self.assertEqual(update["revision"], 2)
        self.assertEqual(self.store.action_desk(self.room["id"])["items"][0]["state"], "done")
        self.assertEqual(self._business_counts()[2:], before[2:])

    def test_idempotent_replay_conflict_and_revision_cas(self) -> None:
        candidate = self._candidate()
        adopt_request = self._request(
            candidate,
            request_id="action_replay_1",
            patch={"owner": "Owner", "due": "", "state": "open", "note": ""},
        )
        first, first_created = self.store.transition_artifact_action(
            self.room["id"], adopt_request
        )
        counts = self._business_counts()
        replay, replay_created = self.store.transition_artifact_action(
            self.room["id"], adopt_request
        )
        self.assertFalse(replay_created)
        self.assertEqual(first, replay)
        self.assertTrue(first_created)
        self.assertEqual(self._business_counts(), counts)

        changed = copy.deepcopy(adopt_request)
        changed["patch"]["note"] = "changed semantics"
        with self.assertRaises(ActionDeskError) as conflict:
            self.store.transition_artifact_action(self.room["id"], changed)
        self.assertEqual(conflict.exception.code, "ACTION_DESK_IDEMPOTENCY_CONFLICT")
        self.assertEqual(self._business_counts(), counts)

        stale = self._request(
            candidate,
            request_id="action_stale_1",
            transition="update",
            revision=2,
            patch={"state": "blocked"},
        )
        with self.assertRaises(ActionDeskError) as stale_error:
            self.store.transition_artifact_action(self.room["id"], stale)
        self.assertEqual(stale_error.exception.code, "ACTION_DESK_REVISION_CONFLICT")
        self.assertEqual(self._business_counts(), counts)

    def test_two_store_idempotent_race_commits_one_event_and_one_head(self) -> None:
        candidate = self._candidate()
        request = self._request(candidate, request_id="action_race_1")
        stores = [self.store, StudioStore(self.db_path)]
        barrier = threading.Barrier(2)

        def run(store: StudioStore) -> tuple[dict, bool]:
            barrier.wait(timeout=5)
            return store.transition_artifact_action(self.room["id"], request)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, stores))
        self.assertEqual(sorted(created for _, created in results), [False, True])
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(self._business_counts()[:2], (1, 1))

    def test_new_artifact_version_does_not_migrate_old_adopted_item(self) -> None:
        old_candidate = self._candidate()
        self.store.transition_artifact_action(
            self.room["id"],
            self._request(old_candidate, request_id="action_old_version"),
        )
        revised = self.store.update_artifact(
            self.room["id"],
            self.artifact["id"],
            {
                "expected_version": self.artifact["version"],
                "title": "Confirmed action plan v2",
                "content": self.artifact["content"],
            },
        )
        assert revised is not None
        reconfirmed = self.store.confirm_artifact(
            self.room["id"],
            revised["id"],
            expected_version=revised["version"],
            confirmed_by="user",
        )
        assert reconfirmed is not None

        desk = self.store.action_desk(self.room["id"])
        self.assertTrue(desk["integrity_ok"])
        self.assertEqual(len(desk["items"]), 1)
        self.assertEqual(desk["items"][0]["artifact_version"], old_candidate["artifact_version"])
        self.assertFalse(desk["items"][0]["source_current"])
        self.assertEqual(desk["items"][0]["current_artifact_version"], reconfirmed["version"])
        self.assertEqual(len(desk["candidates"]), 1)
        self.assertEqual(desk["candidates"][0]["artifact_version"], reconfirmed["version"])
        self.assertNotEqual(
            desk["candidates"][0]["action_snapshot_sha256"],
            old_candidate["action_snapshot_sha256"],
        )

    def test_source_drift_and_head_insert_failure_leave_zero_partial_writes(self) -> None:
        candidate = self._candidate()
        drift = self._request(candidate, request_id="action_drift_1")
        drift["expected_action_snapshot_sha256"] = "f" * 64
        before = self._business_counts()
        with self.assertRaises(ActionDeskError) as drift_error:
            self.store.transition_artifact_action(self.room["id"], drift)
        self.assertEqual(drift_error.exception.code, "ACTION_DESK_SOURCE_DRIFT")
        self.assertEqual(self._business_counts(), before)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """CREATE TRIGGER fail_action_head_insert
                   BEFORE INSERT ON artifact_action_heads
                   BEGIN SELECT RAISE(ABORT,'injected head failure'); END"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.transition_artifact_action(
                self.room["id"],
                self._request(candidate, request_id="action_fault_1"),
            )
        self.assertEqual(self._business_counts(), before)

    def test_event_or_head_tamper_redacts_only_action_item(self) -> None:
        candidate = self._candidate()
        request = self._request(candidate, request_id="action_tamper_1")
        self.store.transition_artifact_action(
            self.room["id"],
            request,
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_artifact_action_events_no_update")
            connection.execute(
                "UPDATE artifact_action_events SET item_snapshot_json='{}'"
            )
        desk = self.store.action_desk(self.room["id"])
        self.assertFalse(desk["integrity_ok"])
        self.assertEqual(desk["candidates"], [])
        self.assertEqual(len(desk["items"]), 1)
        self.assertFalse(desk["items"][0]["integrity_ok"])
        self.assertEqual(desk["items"][0]["text"], "")
        self.assertEqual(desk["items"][0]["note"], "")
        self.assertEqual(desk["items"][0]["latest_event_sha256"], "")
        self.assertTrue(any(
            issue["code"] == "ACTION_DESK_ITEM_INTEGRITY_FAILED"
            for issue in desk["issues"]
        ))
        artifact = self.store.get_artifact(self.room["id"], self.artifact["id"])
        self.assertIsNotNone(artifact)
        self.assertEqual(artifact["status"], "CONFIRMED")
        with self.assertRaises(ActionDeskError) as replay:
            self.store.transition_artifact_action(self.room["id"], request)
        self.assertEqual(replay.exception.code, "ACTION_DESK_INTEGRITY_FAILED")
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_artifact_action_heads_no_delete")
            connection.execute("DROP TRIGGER trg_artifact_action_events_no_delete")
            connection.execute("DELETE FROM artifact_action_heads")
            connection.execute("DELETE FROM artifact_action_events")
        orphaned = self.store.action_desk(self.room["id"])
        self.assertFalse(orphaned["integrity_ok"])
        self.assertEqual(orphaned["candidates"], [])
        self.assertEqual(len(orphaned["items"]), 1)
        self.assertFalse(orphaned["items"][0]["integrity_ok"])
        orphan_update = self._request(
            candidate,
            request_id="action_tamper_orphan_update",
            transition="update",
            revision=1,
            patch={"state": "blocked"},
        )
        with self.assertRaises(ActionDeskError) as orphan_transition:
            self.store.transition_artifact_action(self.room["id"], orphan_update)
        self.assertEqual(
            orphan_transition.exception.code,
            "ACTION_DESK_INTEGRITY_FAILED",
        )

    def test_independent_anchor_rejects_single_event_self_reseal(self) -> None:
        candidate = self._candidate()
        request = self._request(candidate, request_id="action_anchor_single")
        self.store.transition_artifact_action(self.room["id"], request)
        self.assertEqual(self._action_storage_counts(), (1, 1, 1, 1))
        self._self_reseal_event_and_action_head(1, "forged single-event note")

        desk = self.store.action_desk(self.room["id"])
        self.assertFalse(desk["integrity_ok"])
        self.assertEqual(desk["candidates"], [])
        self.assertFalse(desk["items"][0]["integrity_ok"])
        before = self._action_storage_counts()
        with self.assertRaises(ActionDeskError) as replay:
            self.store.transition_artifact_action(self.room["id"], request)
        self.assertEqual(replay.exception.code, "ACTION_DESK_INTEGRITY_FAILED")
        self.assertEqual(self._action_storage_counts(), before)

    def test_independent_anchor_rejects_multi_event_last_event_self_reseal(self) -> None:
        candidate = self._candidate()
        adopt = self._request(candidate, request_id="action_anchor_multi_adopt")
        update = self._request(
            candidate,
            request_id="action_anchor_multi_update",
            transition="update",
            revision=1,
            patch={"state": "in_progress"},
        )
        self.store.transition_artifact_action(self.room["id"], adopt)
        self.store.transition_artifact_action(self.room["id"], update)
        self.assertEqual(self._action_storage_counts(), (2, 1, 2, 1))
        self._self_reseal_event_and_action_head(2, "forged second-event note")

        desk = self.store.action_desk(self.room["id"])
        self.assertFalse(desk["integrity_ok"])
        self.assertFalse(desk["items"][0]["integrity_ok"])
        before = self._action_storage_counts()
        with self.assertRaises(ActionDeskError) as replay:
            self.store.transition_artifact_action(self.room["id"], update)
        self.assertEqual(replay.exception.code, "ACTION_DESK_INTEGRITY_FAILED")
        self.assertEqual(self._action_storage_counts(), before)

    def test_anchor_insert_and_anchor_head_update_faults_rollback_every_table(self) -> None:
        candidate = self._candidate()
        adopt = self._request(candidate, request_id="action_anchor_fault_adopt")
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """CREATE TRIGGER fail_action_anchor_insert
                   BEFORE INSERT ON artifact_action_anchors
                   BEGIN SELECT RAISE(ABORT,'injected anchor insert failure'); END"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.transition_artifact_action(self.room["id"], adopt)
        self.assertEqual(self._action_storage_counts(), (0, 0, 0, 0))
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER fail_action_anchor_insert")
        self.store.transition_artifact_action(self.room["id"], adopt)
        self.assertEqual(self._action_storage_counts(), (1, 1, 1, 1))

        update = self._request(
            candidate,
            request_id="action_anchor_fault_update",
            transition="update",
            revision=1,
            patch={"state": "blocked"},
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """CREATE TRIGGER fail_action_anchor_head_update
                   BEFORE UPDATE ON artifact_action_anchor_heads
                   BEGIN SELECT RAISE(ABORT,'injected anchor head failure'); END"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.transition_artifact_action(self.room["id"], update)
        self.assertEqual(self._action_storage_counts(), (1, 1, 1, 1))
        item = self.store.action_desk(self.room["id"])["items"][0]
        self.assertEqual(item["revision"], 1)
        self.assertEqual(item["state"], "open")

    def test_corrupt_newer_version_does_not_silently_fallback_to_old_confirmed(self) -> None:
        old_candidate = self._candidate()
        revised = self.store.update_artifact(
            self.room["id"],
            self.artifact["id"],
            {
                "expected_version": self.artifact["version"],
                "title": "Draft after confirmation",
                "content": self.artifact["content"],
            },
        )
        assert revised is not None
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE artifact_versions SET snapshot_sha256=?
                   WHERE artifact_id=? AND version=?""",
                ("e" * 64, revised["id"], revised["version"]),
            )
        desk = self.store.action_desk(self.room["id"])
        self.assertFalse(desk["integrity_ok"])
        self.assertEqual(desk["candidates"], [])
        self.assertTrue(any(
            issue["code"] == "ACTION_DESK_SOURCE_INTEGRITY_FAILED"
            for issue in desk["issues"]
        ))
        self.assertEqual(old_candidate["artifact_version"], self.artifact["version"])

    def test_request_contract_rejects_artifact_mutation_fields(self) -> None:
        candidate = self._candidate()
        request = self._request(candidate, request_id="action_closed_1")
        request["patch"] = {"text": "Silently rewrite the confirmed artifact action."}
        before = self._business_counts()
        with self.assertRaises(ActionDeskError) as invalid:
            self.store.transition_artifact_action(self.room["id"], request)
        self.assertEqual(invalid.exception.code, "ACTION_DESK_PATCH_INVALID")
        self.assertEqual(self._business_counts(), before)

    def test_reopen_preserves_verified_event_head_and_single_migration(self) -> None:
        candidate = self._candidate()
        self.store.transition_artifact_action(
            self.room["id"],
            self._request(candidate, request_id="action_reopen_1"),
        )
        reopened = StudioStore(self.db_path)
        desk = reopened.action_desk(self.room["id"])
        self.assertTrue(desk["integrity_ok"])
        self.assertEqual(desk["items"][0]["revision"], 1)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT COUNT(*) FROM schema_migrations
                       WHERE key='artifact_action_desk_v1'"""
                ).fetchone()[0],
                1,
            )

    def test_http_get_post_replay_and_conflict_use_public_projection(self) -> None:
        original_store = http_server.STORE
        http_server.STORE = self.store
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(
                f"{base_url}/api/rooms/{self.room['id']}/action-desk",
                timeout=5,
            ) as response:
                self.assertEqual(response.status, 200)
                initial = json.loads(response.read().decode("utf-8"))
            self.assertTrue(initial["ok"])
            candidate = initial["action_desk"]["candidates"][0]
            payload = self._request(
                candidate,
                request_id="action_http_1",
                patch={
                    "owner": "HTTP owner",
                    "due": "",
                    "state": "open",
                    "note": "HTTP adopted",
                },
            )

            def post(body: dict) -> tuple[int, dict]:
                request = Request(
                    f"{base_url}/api/rooms/{self.room['id']}/action-desk/transitions",
                    data=json.dumps(body).encode("utf-8"),
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
                    },
                )
                with urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))

            first_status, first = post(payload)
            replay_status, replay = post(payload)
            self.assertEqual(first_status, 201)
            self.assertEqual(replay_status, 200)
            self.assertFalse(first["idempotent_replay"])
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(first["transition"], replay["transition"])
            response_text = json.dumps(first, ensure_ascii=False)
            for forbidden in (
                "request_semantics_json",
                "item_snapshot_json",
                "patch_json",
                "snapshot_json",
            ):
                self.assertNotIn(forbidden, response_text)

            conflict = copy.deepcopy(payload)
            conflict["patch"]["note"] = "different semantics"
            with self.assertRaises(HTTPError) as raised:
                post(conflict)
            self.assertEqual(raised.exception.code, 409)
            error_payload = json.loads(raised.exception.read().decode("utf-8"))
            self.assertEqual(
                error_payload["code"],
                "ACTION_DESK_IDEMPOTENCY_CONFLICT",
            )
            raised.exception.close()

            with urlopen(
                f"{base_url}/api/rooms/{self.room['id']}/action-desk",
                timeout=5,
            ) as response:
                current = json.loads(response.read().decode("utf-8"))
            self.assertEqual(current["action_desk"]["candidates"], [])
            self.assertEqual(current["action_desk"]["items"][0]["note"], "HTTP adopted")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            http_server.STORE = original_store


if __name__ == "__main__":
    unittest.main()
