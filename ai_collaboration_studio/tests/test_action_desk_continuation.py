from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend import http_server  # noqa: E402
from backend.action_desk import (  # noqa: E402
    ACTION_CONTINUATION_VERSION,
    ACTION_TRANSITION_REQUEST_VERSION,
    ActionDeskError,
)
from backend.store import StudioStore  # noqa: E402


class ActionDeskContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "action-continuation.sqlite3"
        self.store = StudioStore(self.db_path)
        self.room = self.store.create_room(
            "Action continuity",
            "Explicitly connect old and new confirmed actions.",
            capability_pack_ids=[],
        )["room"]
        self.material = self.store.add_material(self.room["id"], {
            "title": "Continuity evidence",
            "kind": "note",
            "content": "local",
        })
        self.evidence = [{
            "type": "material",
            "id": self.material["id"],
            "evidence_role": "support",
            "verification_status": "source_checked",
            "review_note": "checked",
        }]
        self.old = self._create_confirmed("Old confirmed plan", "old_action", "Old action")
        self.old_candidate = self._candidate()
        self.store.transition_artifact_action(
            self.room["id"],
            self._action_request(self.old_candidate, "adopt_old"),
        )
        revised_content = copy.deepcopy(self.old_content)
        updated = self.store.update_artifact(
            self.room["id"],
            self.old["id"],
            {
                "expected_version": self.old["version"],
                "title": "New confirmed plan",
                "content": revised_content,
            },
        )
        assert updated is not None
        self.new = self.store.confirm_artifact(
            self.room["id"],
            self.old["id"],
            expected_version=updated["version"],
            confirmed_by="user",
        )
        assert self.new is not None
        self.new_candidate = next(
            candidate for candidate in self.store.action_desk(self.room["id"])["candidates"]
            if candidate["artifact_version"] == self.new["version"]
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_confirmed(self, title: str, action_id: str, text: str) -> dict:
        artifact = self.store.create_artifact(
            self.room["id"],
            title=title,
            content={
                "summary": "summary",
                "summary_evidence": self.evidence,
                "requirements": [],
                "risks": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [{
                    "id": action_id,
                    "text": text,
                    "owner": "Old owner",
                    "due": "",
                    "state": "open",
                    "evidence": self.evidence,
                }],
            },
        )
        self.old_content = copy.deepcopy(artifact["content"])
        confirmed = self.store.confirm_artifact(
            self.room["id"], artifact["id"], expected_version=artifact["version"], confirmed_by="user",
        )
        assert confirmed is not None
        return confirmed

    def _candidate(self) -> dict:
        candidates = self.store.action_desk(self.room["id"])["candidates"]
        self.assertEqual(len(candidates), 1)
        return candidates[0]

    @staticmethod
    def _action_request(candidate: dict, request_id: str) -> dict:
        return {
            "version": ACTION_TRANSITION_REQUEST_VERSION,
            "client_request_id": request_id,
            "artifact_id": candidate["artifact_id"],
            "artifact_version": candidate["artifact_version"],
            "action_id": candidate["action_id"],
            "expected_action_snapshot_sha256": candidate["action_snapshot_sha256"],
            "expected_revision": 0,
            "transition": "adopt",
            "patch": {},
            "user_confirmed": True,
        }

    def _request(self, request_id: str = "continuation_1") -> dict:
        return {
            "version": ACTION_CONTINUATION_VERSION,
            "client_request_id": request_id,
            "source_artifact_id": self.old_candidate["artifact_id"],
            "source_artifact_version": self.old_candidate["artifact_version"],
            "source_action_id": self.old_candidate["action_id"],
            "source_action_snapshot_sha256": self.old_candidate["action_snapshot_sha256"],
            "source_expected_revision": 1,
            "target_artifact_id": self.new_candidate["artifact_id"],
            "target_artifact_version": self.new_candidate["artifact_version"],
            "target_action_id": self.new_candidate["action_id"],
            "target_action_snapshot_sha256": self.new_candidate["action_snapshot_sha256"],
            "reason": "同一事项在新版确认产物中继续。",
            "user_confirmed": True,
        }

    def _counts(self) -> tuple[int, ...]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "artifact_action_continuation_events",
                    "artifact_action_continuation_heads",
                    "artifact_action_continuation_anchors",
                    "artifact_action_continuation_anchor_heads",
                )
            )

    def test_explicit_relation_does_not_migrate_old_item_or_adopt_target(self) -> None:
        result, created = self.store.transition_artifact_action_continuation(
            self.room["id"], self._request(),
        )
        self.assertTrue(created)
        self.assertEqual(result["relation"]["source"]["artifact_version"], self.old_candidate["artifact_version"])
        self.assertEqual(result["relation"]["target"]["artifact_version"], self.new_candidate["artifact_version"])
        self.assertEqual(result["relation"]["source_revision"], 1)
        desk = self.store.action_desk(self.room["id"])
        self.assertEqual(len(desk["items"]), 1)
        self.assertEqual(desk["items"][0]["state"], "open")
        self.assertEqual(len(desk["candidates"]), 1)
        self.assertEqual(desk["candidates"][0]["action_id"], "old_action")
        relations = self.store.action_desk_continuations(self.room["id"])
        self.assertTrue(relations["integrity_ok"])
        self.assertEqual(relations["counts"]["relation_count"], 1)

    def test_relation_survives_a_later_old_action_update_without_transfer(self) -> None:
        self.store.transition_artifact_action_continuation(self.room["id"], self._request("continuation_then_update"))
        update = self._action_request(self.old_candidate, "update_after_continuation")
        update.update({
            "expected_revision": 1,
            "transition": "update",
            "patch": {"note": "progress recorded after the explicit link"},
        })
        self.store.transition_artifact_action(self.room["id"], update)
        relations = self.store.action_desk_continuations(self.room["id"])
        self.assertTrue(relations["integrity_ok"])
        self.assertEqual(relations["counts"]["relation_count"], 1)
        self.assertEqual(relations["relations"][0]["source_revision"], 1)

    def test_newer_same_lineage_and_user_confirmation_are_required(self) -> None:
        bad = self._request("continuation_bad")
        bad["target_artifact_version"] = self.old_candidate["artifact_version"]
        with self.assertRaises(ActionDeskError) as error:
            self.store.transition_artifact_action_continuation(self.room["id"], bad)
        self.assertEqual(error.exception.code, "ACTION_CONTINUATION_TARGET_NOT_NEWER")
        missing_confirmation = self._request("continuation_confirm")
        missing_confirmation["user_confirmed"] = False
        with self.assertRaises(ActionDeskError) as error:
            self.store.transition_artifact_action_continuation(self.room["id"], missing_confirmation)
        self.assertEqual(error.exception.code, "ACTION_CONTINUATION_USER_CONFIRMATION_REQUIRED")
        self.assertEqual(self._counts(), (0, 0, 0, 0))

    def test_idempotency_and_conflict_return_the_exact_original_relation(self) -> None:
        request = self._request("continuation_replay")
        first, created = self.store.transition_artifact_action_continuation(self.room["id"], request)
        replay, replay_created = self.store.transition_artifact_action_continuation(self.room["id"], request)
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first, replay)
        changed = copy.deepcopy(request)
        changed["reason"] = "different"
        with self.assertRaises(ActionDeskError) as error:
            self.store.transition_artifact_action_continuation(self.room["id"], changed)
        self.assertEqual(error.exception.code, "ACTION_CONTINUATION_IDEMPOTENCY_CONFLICT")
        self.assertEqual(self._counts(), (1, 1, 1, 1))

    def test_source_and_target_can_only_be_linked_once(self) -> None:
        self.store.transition_artifact_action_continuation(self.room["id"], self._request("continuation_once"))
        second = self._request("continuation_twice")
        with self.assertRaises(ActionDeskError) as error:
            self.store.transition_artifact_action_continuation(self.room["id"], second)
        self.assertEqual(error.exception.code, "ACTION_CONTINUATION_SOURCE_ALREADY_LINKED")
        target_adopt = self._action_request(self.new_candidate, "adopt_new_after_relation")
        self.store.transition_artifact_action(self.room["id"], target_adopt)
        self.assertEqual(self._counts(), (1, 1, 1, 1))

    def test_relation_anchor_rejects_event_and_head_self_reseal(self) -> None:
        request = self._request("continuation_tamper")
        self.store.transition_artifact_action_continuation(self.room["id"], request)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_artifact_action_continuation_events_no_update")
            connection.execute("DROP TRIGGER trg_artifact_action_continuation_heads_no_update")
            connection.execute(
                "UPDATE artifact_action_continuation_events SET reason='forged without anchor rewrite'"
            )
            # Recompute the event/head hashes would still leave the independent anchor stale;
            # the read path must hide the relation rather than display the forged reason.
        relations = self.store.action_desk_continuations(self.room["id"])
        self.assertFalse(relations["integrity_ok"])
        self.assertEqual(relations["relations"], [])
        with self.assertRaises(ActionDeskError) as error:
            self.store.transition_artifact_action_continuation(self.room["id"], request)
        self.assertEqual(error.exception.code, "ACTION_CONTINUATION_INTEGRITY_FAILED")

    def test_relation_revalidates_the_source_action_chain(self) -> None:
        request = self._request("continuation_source_chain_tamper")
        self.store.transition_artifact_action_continuation(self.room["id"], request)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_artifact_action_events_no_update")
            connection.execute(
                "UPDATE artifact_action_events SET item_snapshot_json=?",
                (json.dumps({"state": "forged"}),),
            )
        relations = self.store.action_desk_continuations(self.room["id"])
        self.assertFalse(relations["integrity_ok"])
        self.assertEqual(relations["relations"], [])
        with self.assertRaises(ActionDeskError) as error:
            self.store.transition_artifact_action_continuation(self.room["id"], request)
        self.assertEqual(error.exception.code, "ACTION_CONTINUATION_INTEGRITY_FAILED")

    def test_anchor_insert_failure_rolls_back_all_four_tables(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """CREATE TRIGGER fail_continuation_anchor_insert
                   BEFORE INSERT ON artifact_action_continuation_anchors
                   BEGIN SELECT RAISE(ABORT,'injected continuation anchor failure'); END"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.transition_artifact_action_continuation(self.room["id"], self._request("continuation_fault"))
        self.assertEqual(self._counts(), (0, 0, 0, 0))

    def test_http_get_post_and_replay_are_public_only(self) -> None:
        original_store = http_server.STORE
        http_server.STORE = self.store
        server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(f"{base}/api/rooms/{self.room['id']}/action-desk/continuations", timeout=5) as response:
                self.assertEqual(response.status, 200)
                initial = json.loads(response.read().decode("utf-8"))
            self.assertTrue(initial["ok"])
            self.assertEqual(initial["continuations"]["counts"]["relation_count"], 0)
            payload = self._request("continuation_http")
            request = Request(
                f"{base}/api/rooms/{self.room['id']}/action-desk/continuations",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json", "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN},
            )
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 201)
                first = json.loads(response.read().decode("utf-8"))
            with urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                replay = json.loads(response.read().decode("utf-8"))
            self.assertFalse(first["idempotent_replay"])
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(first["continuation"], replay["continuation"])
            body = json.dumps(first, ensure_ascii=False)
            for forbidden in ("request_semantics_json", "head_sha256", "anchor_sha256", "source_snapshot_json"):
                self.assertNotIn(forbidden, body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            http_server.STORE = original_store

    def test_two_store_same_request_creates_one_relation(self) -> None:
        request = self._request("continuation_race")
        stores = [self.store, StudioStore(self.db_path)]
        barrier = threading.Barrier(2)

        def run(store: StudioStore):
            barrier.wait(timeout=5)
            return store.transition_artifact_action_continuation(self.room["id"], request)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, stores))
        self.assertEqual(sorted(created for _, created in results), [False, True])
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(self._counts(), (1, 1, 1, 1))


if __name__ == "__main__":
    unittest.main()
