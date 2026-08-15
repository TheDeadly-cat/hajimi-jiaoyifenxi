from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from backend import http_server
from backend.artifact_evidence_graph import ARTIFACT_EVIDENCE_GRAPH_VERSION
from backend.store import StudioStore


def reviewed(source_type: str, source_id: str) -> dict[str, str]:
    return {
        "type": source_type,
        "id": source_id,
        "evidence_role": "support",
        "verification_status": "source_checked",
        "review_note": "checked against the saved source",
    }


class ArtifactEvidenceGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.db_path)
        self.message = self.store.room_snapshot("room_plan")["messages"][0]
        self.material = self.store.add_material("room_plan", {
            "title": "Saved requirement",
            "kind": "note",
            "content": "A frozen, local test source.",
        })

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_artifact(self) -> dict:
        material_ref = reviewed("material", self.material["id"])
        return self.store.create_artifact(
            "room_plan",
            title="Evidence graph fixture",
            content={
                "summary": "Use the saved evidence only.",
                "summary_evidence": [material_ref],
                "conclusions": [{
                    "id": "conclusion_one",
                    "text": "Start with a bounded prototype.",
                    "evidence": [material_ref],
                }],
                "unknowns": [{
                    "id": "unknown_one",
                    "text": "Measure the remaining uncertainty.",
                    "evidence": [{
                        **reviewed("message", self.message["id"]),
                        "evidence_role": "counter",
                        "verification_status": "disputed",
                    }],
                }],
            },
        )

    def test_graph_is_deterministic_exact_and_review_chain_is_append_only(self) -> None:
        artifact = self.create_artifact()

        first = self.store.artifact_evidence_graph("room_plan", artifact["id"])
        second = self.store.artifact_evidence_graph("room_plan", artifact["id"])

        self.assertEqual(first, second)
        self.assertEqual(first["version"], ARTIFACT_EVIDENCE_GRAPH_VERSION)
        self.assertEqual(first["integrity"]["status"], "verified")
        self.assertTrue(first["integrity"]["current_projection_matches"])
        self.assertEqual(first["review_chain"]["event_count"], 1)
        self.assertEqual(first["review_chain"]["events"][0]["event_type"], "created")
        self.assertEqual(first["summary"]["relation_count"], 3)
        self.assertEqual(first["summary"]["source_count"], 2)
        relation_edges = [
            edge for edge in first["edges"]
            if edge["edge_type"] in {"supports", "counters", "context_for"}
        ]
        self.assertEqual(len(relation_edges), 3)
        self.assertEqual(
            len({edge["relation_id"] for edge in relation_edges}),
            3,
        )
        self.assertTrue(all(node.get("node_id") for node in first["nodes"]))
        self.assertEqual(first["execution_capability"], "none")
        self.assertFalse(first["live_trading_allowed"])
        self.assertFalse(first["can_autonomously_decide"])

    def test_changed_target_resets_review_and_creates_a_new_relation_identity(self) -> None:
        artifact = self.create_artifact()
        original_graph = self.store.artifact_evidence_graph("room_plan", artifact["id"])
        original_summary_edge = next(
            edge for edge in original_graph["edges"]
            if edge.get("item_key") == "summary"
        )

        revised = self.store.update_artifact("room_plan", artifact["id"], {
            "expected_version": artifact["version"],
            "content": {
                **artifact["content"],
                "summary": "The evaluated summary has materially changed.",
            },
        })
        graph = self.store.artifact_evidence_graph("room_plan", artifact["id"])
        revised_summary_edge = next(
            edge for edge in graph["edges"]
            if edge.get("item_key") == "summary"
        )

        self.assertEqual(revised["content"]["summary_evidence"][0]["verification_status"], "unreviewed")
        self.assertEqual(revised["content"]["summary_evidence"][0]["review_note"], "")
        self.assertNotEqual(
            original_summary_edge["relation_id"],
            revised_summary_edge["relation_id"],
        )
        self.assertEqual(graph["review_chain"]["event_count"], 2)
        latest = graph["review_chain"]["events"][-1]
        self.assertEqual(latest["event_type"], "revised")
        self.assertGreaterEqual(latest["added_relation_count"], 1)
        self.assertGreaterEqual(latest["removed_relation_count"], 1)

    def test_review_event_tampering_fails_closed(self) -> None:
        artifact = self.create_artifact()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "DROP TRIGGER trg_artifact_evidence_review_events_no_update"
            )
            connection.execute(
                """UPDATE artifact_evidence_review_events
                   SET relation_snapshot_json='[]'
                   WHERE artifact_id=?""",
                (artifact["id"],),
            )

        with self.assertRaisesRegex(ValueError, "snapshot"):
            self.store.artifact_evidence_graph("room_plan", artifact["id"])

    def test_review_events_cannot_be_deleted(self) -> None:
        artifact = self.create_artifact()
        with self.assertRaises(sqlite3.IntegrityError):
            with closing(sqlite3.connect(self.db_path)) as connection, connection:
                connection.execute(
                    "DELETE FROM artifact_evidence_review_events WHERE artifact_id=?",
                    (artifact["id"],),
                )

        graph = self.store.artifact_evidence_graph("room_plan", artifact["id"])
        self.assertEqual(graph["integrity"]["status"], "verified")

    def test_deleted_events_and_head_after_guard_bypass_fail_closed(self) -> None:
        artifact = self.create_artifact()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_artifact_evidence_review_events_no_delete")
            connection.execute("DROP TRIGGER trg_artifact_evidence_review_heads_no_delete")
            connection.execute(
                "DELETE FROM artifact_evidence_review_events WHERE artifact_id=?",
                (artifact["id"],),
            )
            connection.execute(
                "DELETE FROM artifact_evidence_review_heads WHERE artifact_id=?",
                (artifact["id"],),
            )

        with self.assertRaisesRegex(ValueError, "history is missing"):
            self.store.artifact_evidence_graph("room_plan", artifact["id"])

    def test_material_and_message_source_tampering_fails_closed(self) -> None:
        artifact = self.create_artifact()
        with self.assertRaises(sqlite3.IntegrityError):
            with closing(sqlite3.connect(self.db_path)) as connection, connection:
                connection.execute(
                    """UPDATE material_versions SET snapshot_json='{}'
                       WHERE room_id=? AND material_id=? AND version=?""",
                    ("room_plan", self.material["id"], self.material["version"]),
                )

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_material_versions_no_update")
            row = connection.execute(
                """SELECT snapshot_json FROM material_versions
                   WHERE room_id=? AND material_id=? AND version=?""",
                ("room_plan", self.material["id"], self.material["version"]),
            ).fetchone()
            snapshot = json.loads(row[0])
            snapshot["content"] = "tampered material content"
            connection.execute(
                """UPDATE material_versions SET snapshot_json=?
                   WHERE room_id=? AND material_id=? AND version=?""",
                (
                    json.dumps(snapshot),
                    "room_plan",
                    self.material["id"],
                    self.material["version"],
                ),
            )
        with self.assertRaisesRegex(ValueError, "material source snapshot"):
            self.store.artifact_evidence_graph("room_plan", artifact["id"])

        second = self.create_artifact()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE messages SET content=? WHERE id=?",
                ("tampered message content", self.message["id"]),
            )
        with self.assertRaisesRegex(ValueError, "message source snapshot"):
            self.store.artifact_evidence_graph("room_plan", second["id"])

    def test_workflow_field_changes_reset_the_bound_reviews(self) -> None:
        evidence = [reviewed("material", self.material["id"])]
        artifact = self.store.create_artifact(
            "room_plan",
            title="Workflow fields",
            content={
                "summary": "Track every evaluated field.",
                "summary_evidence": evidence,
                "requirements": [{
                    "id": "req_one", "text": "Requirement", "status": "pending",
                    "owner": "A", "acceptance_criteria": "Criterion", "evidence": evidence,
                }],
                "risks": [{
                    "id": "risk_one", "text": "Risk", "status": "open",
                    "probability": "medium", "impact": "high", "blocking": True,
                    "trigger": "Trigger", "mitigation": "Mitigation", "owner": "A",
                    "evidence": evidence,
                }],
                "disagreements": [{
                    "id": "dis_one", "text": "Disagreement", "positions": ["A", "B"],
                    "status": "open", "blocking": True, "owner": "A", "resolution": "",
                    "evidence": evidence,
                }],
                "actions": [{
                    "id": "act_one", "text": "Action", "owner": "A", "due": "Soon",
                    "state": "open", "evidence": evidence,
                }],
            },
        )
        content = json.loads(json.dumps(artifact["content"]))
        content["requirements"][0]["owner"] = "B"
        content["risks"][0]["mitigation"] = "Revised mitigation"
        content["disagreements"][0]["resolution"] = "Resolved with a stop condition"
        content["actions"][0]["state"] = "in_progress"
        revised = self.store.update_artifact("room_plan", artifact["id"], {
            "expected_version": artifact["version"],
            "content": content,
        })

        for section in ("requirements", "risks", "disagreements", "actions"):
            relation = revised["content"][section][0]["evidence"][0]
            self.assertEqual(relation["verification_status"], "unreviewed")
            self.assertEqual(relation["review_note"], "")

    def test_review_event_limit_rejects_the_next_version_atomically(self) -> None:
        with patch("backend.store.ARTIFACT_EVIDENCE_GRAPH_MAX_REVIEW_EVENTS", 2):
            artifact = self.create_artifact()
            revised = self.store.update_artifact("room_plan", artifact["id"], {
                "expected_version": artifact["version"],
                "title": "Version two",
            })
            with self.assertRaisesRegex(ValueError, "reached the safe limit"):
                self.store.update_artifact("room_plan", artifact["id"], {
                    "expected_version": revised["version"],
                    "title": "Version three must roll back",
                })

            current = self.store.get_artifact("room_plan", artifact["id"])
            graph = self.store.artifact_evidence_graph("room_plan", artifact["id"])
            self.assertEqual(current["version"], 2)
            self.assertEqual(current["title"], "Version two")
            self.assertEqual(graph["review_chain"]["event_count"], 2)

    def test_missing_source_and_legacy_history_gap_are_explicitly_partial(self) -> None:
        artifact = self.create_artifact()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """DELETE FROM material_versions
                   WHERE room_id=? AND material_id=? AND version=?""",
                ("room_plan", self.material["id"], self.material["version"]),
            )
        missing_graph = self.store.artifact_evidence_graph("room_plan", artifact["id"])
        self.assertEqual(missing_graph["integrity"]["status"], "partial")
        self.assertIn("MATERIAL_SOURCE_MISSING", missing_graph["integrity"]["issues"])
        self.assertGreater(
            missing_graph["integrity"]["source_integrity"]["missing_source_count"],
            0,
        )

        other_material = self.store.add_material("room_plan", {
            "title": "Legacy gap source",
            "kind": "note",
            "content": "Local source for a simulated pre-migration artifact.",
        })
        legacy = self.store.create_artifact(
            "room_plan",
            title="Legacy gap",
            content={
                "summary": "Old version before review history tracking.",
                "summary_evidence": [reviewed("material", other_material["id"])],
            },
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_artifact_evidence_review_events_no_delete")
            connection.execute("DROP TRIGGER trg_artifact_evidence_review_heads_no_delete")
            connection.execute(
                "DELETE FROM artifact_evidence_review_events WHERE artifact_id=?",
                (legacy["id"],),
            )
            connection.execute(
                "DELETE FROM artifact_evidence_review_heads WHERE artifact_id=?",
                (legacy["id"],),
            )
            first_version_time = connection.execute(
                """SELECT changed_at FROM artifact_versions
                   WHERE artifact_id=? AND version=1""",
                (legacy["id"],),
            ).fetchone()[0]
            connection.execute(
                """UPDATE schema_migrations SET applied_at=?
                   WHERE key='artifact_evidence_review_events_v1'""",
                (first_version_time + 1,),
            )
        revised = self.store.update_artifact("room_plan", legacy["id"], {
            "expected_version": legacy["version"],
            "title": "Legacy gap revised",
        })
        partial_graph = self.store.artifact_evidence_graph("room_plan", revised["id"])
        self.assertEqual(partial_graph["integrity"]["status"], "partial")
        self.assertIn("LEGACY_REVIEW_HISTORY_GAP", partial_graph["integrity"]["issues"])
        self.assertEqual(
            partial_graph["review_chain"]["legacy_untracked_version_count"],
            1,
        )

    def test_user_decision_edges_require_exact_artifact_binding(self) -> None:
        artifact = self.create_artifact()
        confirmed = self.store.confirm_artifact(
            "room_plan",
            artifact["id"],
            expected_version=artifact["version"],
            confirmed_by="user",
        )
        decision = self.store.create_artifact_user_decision(
            "room_plan",
            confirmed["id"],
            expected_version=confirmed["version"],
            action="hold",
            rationale="Keep the bounded research decision under review.",
        )
        graph = self.store.artifact_evidence_graph("room_plan", confirmed["id"])
        self.assertTrue(any(
            edge["edge_type"] == "decides_on" for edge in graph["edges"]
        ))

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE artifact_user_decisions SET artifact_snapshot_sha256=?
                   WHERE id=?""",
                ("0" * 64, decision["id"]),
            )
        with self.assertRaisesRegex(ValueError, "artifact binding"):
            self.store.artifact_evidence_graph("room_plan", confirmed["id"])

    def test_material_version_identity_migration_never_discards_legacy_duplicates(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP INDEX uq_material_versions_identity")
            connection.execute("DROP TRIGGER trg_material_versions_identity_insert")
            connection.execute("DROP TRIGGER trg_material_versions_identity_update")
            original = connection.execute(
                """SELECT * FROM material_versions
                   WHERE room_id=? AND material_id=? AND version=?""",
                ("room_plan", self.material["id"], self.material["version"]),
            ).fetchone()
            connection.execute(
                """INSERT INTO material_versions(
                       id,material_id,room_id,version,snapshot_json,changed_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    "legacy_duplicate",
                    original[1],
                    original[2],
                    original[3],
                    original[4],
                    original[5] + 1,
                ),
            )

        reloaded = StudioStore(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection:
            duplicate_count = connection.execute(
                """SELECT COUNT(*) FROM material_versions
                   WHERE room_id=? AND material_id=? AND version=?""",
                ("room_plan", self.material["id"], self.material["version"]),
            ).fetchone()[0]
            unique_index = connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='index' AND name='uq_material_versions_identity'"""
            ).fetchone()
            marker = connection.execute(
                """SELECT 1 FROM schema_migrations
                   WHERE key='material_versions_unique_identity_v1'"""
            ).fetchone()
        self.assertEqual(duplicate_count, 2)
        self.assertIsNone(unique_index)
        self.assertIsNone(marker)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            reloaded.get_material_version(
                "room_plan",
                self.material["id"],
                self.material["version"],
            )
        with self.assertRaises(sqlite3.IntegrityError):
            with closing(sqlite3.connect(self.db_path)) as connection, connection:
                connection.execute(
                    """INSERT INTO material_versions(
                           id,material_id,room_id,version,snapshot_json,changed_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        "blocked_duplicate",
                        self.material["id"],
                        "room_plan",
                        self.material["version"],
                        json.dumps(self.material),
                        1,
                    ),
                )


class ArtifactEvidenceGraphHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_store = http_server.STORE
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "http.sqlite3")
        http_server.STORE = self.store
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
        self.thread.join(timeout=2)
        http_server.STORE = self.original_store
        self.temp_dir.cleanup()

    def test_graph_route_is_read_only_and_room_scoped(self) -> None:
        message = self.store.room_snapshot("room_plan")["messages"][0]
        artifact = self.store.create_artifact(
            "room_plan",
            title="HTTP graph",
            content={
                "summary": "Saved statement.",
                "summary_evidence": [reviewed("message", message["id"])],
            },
        )
        with urlopen(
            f"{self.base_url}/api/rooms/room_plan/artifacts/{artifact['id']}/evidence-graph",
            timeout=5,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["artifact"]["id"], artifact["id"])
        self.assertEqual(payload["review_chain"]["event_count"], 1)

        with self.assertRaises(HTTPError) as error:
            urlopen(
                f"{self.base_url}/api/rooms/room_storage/artifacts/{artifact['id']}/evidence-graph",
                timeout=5,
            )
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
