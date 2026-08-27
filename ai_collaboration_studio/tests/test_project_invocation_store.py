from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend.project_invocation import (
    project_invocation_semantics,
    seal_project_invocation_envelope,
)
from backend.store import ProjectInvocationStoreError, StudioStore
from tests.test_project_invocation_capability import unsealed_envelope


def authorization(*, jti: str = "capability-jti-00000001") -> dict[str, object]:
    return {
        "authorization_sha256": hashlib.sha256(
            b"project-capability-claims-v1"
        ).hexdigest(),
        "jti": jti,
        "expires_at": 2_000_000_300,
    }


class ProjectInvocationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-project-invocation-store-"
        )
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.db_path)
        self.envelope = seal_project_invocation_envelope(unsealed_envelope())
        self.semantics = project_invocation_semantics(self.envelope)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create(self, *, reauthorize=None):
        return self.store.create_project_invocation(
            copy.deepcopy(self.envelope),
            request_semantics=copy.deepcopy(self.semantics),
            authorization=authorization(),
            reauthorize=reauthorize,
        )

    def test_creation_is_atomic_room_version_bound_and_idempotent(self) -> None:
        calls: list[str] = []
        first, created = self.create(reauthorize=lambda: calls.append("authorized"))
        second, replay_created = self.create(
            reauthorize=lambda: calls.append("reauthorized")
        )

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first, second)
        self.assertEqual(calls, ["authorized", "reauthorized"])
        self.assertEqual(first["room_binding"]["room_id"], self.envelope["room_id"])
        self.assertEqual(first["room_binding"]["settings_version"], 1)
        self.assertEqual(first["safety"]["provider_calls_performed"], 0)
        self.assertEqual(first["safety"]["market_reads_performed"], 0)
        self.assertEqual(first["safety"]["business_writes_performed"], 0)

        fetched = self.store.get_project_invocation(
            caller_id=str(self.envelope["caller_id"]),
            project_id=str(self.envelope["project_id"]),
            client_request_id=str(self.envelope["client_request_id"]),
        )
        self.assertEqual(fetched, first)
        details = self.store.get_project_invocation_details(
            caller_id=str(self.envelope["caller_id"]),
            project_id=str(self.envelope["project_id"]),
            client_request_id=str(self.envelope["client_request_id"]),
        )
        self.assertEqual(details["invocation"], first)
        self.assertEqual(details["request_semantics"], self.semantics)
        details_json = json.dumps(details, ensure_ascii=False)
        self.assertNotIn(self.envelope["room_spec"]["title"], details_json)
        self.assertNotIn(self.envelope["room_spec"]["objective"], details_json)

        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute(
                "SELECT request_semantics_json,creation_capability_jti_sha256 "
                "FROM project_invocation_intakes"
            ).fetchall()
            rooms = connection.execute(
                "SELECT COUNT(*) FROM rooms WHERE id=?",
                (self.envelope["room_id"],),
            ).fetchone()[0]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rooms, 1)
        self.assertNotIn("capability-jti-00000001", rows[0][0])
        self.assertEqual(
            rows[0][1],
            hashlib.sha256(b"capability-jti-00000001").hexdigest(),
        )

    def test_same_id_with_semantic_drift_fails_without_duplicate_room(self) -> None:
        self.create()
        drifted = copy.deepcopy(self.semantics)
        drifted["budget"]["max_result_bytes"] += 1

        with self.assertRaises(ProjectInvocationStoreError) as raised:
            self.store.create_project_invocation(
                copy.deepcopy(self.envelope),
                request_semantics=drifted,
                authorization=authorization(),
            )
        self.assertEqual(
            raised.exception.code,
            "PROJECT_INVOCATION_IDEMPOTENCY_CONFLICT",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM project_invocation_intakes"
                ).fetchone()[0],
                1,
            )

    def test_reauthorization_failure_rolls_back_room_and_intake(self) -> None:
        def reject() -> None:
            raise RuntimeError("expired during lock acquisition")

        with self.assertRaisesRegex(RuntimeError, "expired during lock"):
            self.create(reauthorize=reject)

        with closing(sqlite3.connect(self.db_path)) as connection:
            intake_count = connection.execute(
                "SELECT COUNT(*) FROM project_invocation_intakes"
            ).fetchone()[0]
            room_count = connection.execute(
                "SELECT COUNT(*) FROM rooms WHERE id=?",
                (self.envelope["room_id"],),
            ).fetchone()[0]
        self.assertEqual((intake_count, room_count), (0, 0))

    def test_no_payload_retention_redacts_every_classification(self) -> None:
        classifications = (
            "public",
            "internal",
            "confidential",
            "sensitive_personal",
            "sensitive_financial",
        )
        for index, classification in enumerate(classifications, start=1):
            with self.subTest(classification=classification):
                title = f"retention-title-marker-{classification}"
                objective = f"retention-objective-marker-{classification}"
                raw = unsealed_envelope(
                    project_id=f"project_retention_{index}",
                    client_request_id=f"request-retention-{index:04d}",
                )
                raw["room_spec"]["title"] = title
                raw["room_spec"]["objective"] = objective
                raw["data_handling"] = {
                    "classification": classification,
                    "retention_policy": "no_payload_retention",
                    "retention_days": None,
                }
                envelope = seal_project_invocation_envelope(raw)
                projection, created = self.store.create_project_invocation(
                    envelope,
                    request_semantics=project_invocation_semantics(envelope),
                    authorization=authorization(
                        jti=f"capability-jti-retention-{index:04d}"
                    ),
                )
                self.assertTrue(created)

                snapshot = self.store.room_snapshot(str(envelope["room_id"]))
                serialized = json.dumps(
                    {"projection": projection, "snapshot": snapshot},
                    ensure_ascii=False,
                )
                self.assertNotIn(title, serialized)
                self.assertNotIn(objective, serialized)
                self.assertIn("调用方正文未由 Studio 保留", serialized)
                self.assertFalse(
                    projection["retention"]["payload_retention_allowed"]
                )
                self.assertFalse(projection["retention"]["room_payload_persisted"])
                self.assertEqual(projection["retention"]["expires_at"], 0)

    def test_time_bounded_retention_never_persists_room_payload_and_expires_reads(self) -> None:
        cases = (("ephemeral_24h", None, 86_400_000), ("bounded_days", 2, 172_800_000))
        for index, (policy, days, duration_ms) in enumerate(cases, start=1):
            with self.subTest(policy=policy):
                created_at = 1_000_000_000 + index * 1_000
                title = f"bounded-title-marker-{policy}"
                objective = f"bounded-objective-marker-{policy}"
                raw = unsealed_envelope(
                    project_id=f"project_bounded_{index}",
                    client_request_id=f"request-bounded-{index:04d}",
                )
                raw["room_spec"]["title"] = title
                raw["room_spec"]["objective"] = objective
                raw["data_handling"] = {
                    "classification": "confidential",
                    "retention_policy": policy,
                    "retention_days": days,
                }
                envelope = seal_project_invocation_envelope(raw)
                with patch("backend.store.now_ms", return_value=created_at):
                    projection, created = self.store.create_project_invocation(
                        envelope,
                        request_semantics=project_invocation_semantics(envelope),
                        authorization=authorization(
                            jti=f"capability-jti-bounded-{index:04d}"
                        ),
                    )
                self.assertTrue(created)
                expires_at = created_at + duration_ms
                self.assertEqual(projection["retention"]["expires_at"], expires_at)
                self.assertTrue(
                    projection["retention"]["payload_retention_allowed"]
                )
                self.assertFalse(projection["retention"]["room_payload_persisted"])

                snapshot = self.store.room_snapshot(str(envelope["room_id"]))
                serialized = json.dumps(snapshot, ensure_ascii=False)
                self.assertNotIn(title, serialized)
                self.assertNotIn(objective, serialized)
                with closing(sqlite3.connect(self.db_path)) as connection:
                    stored = connection.execute(
                        "SELECT retention_policy,retention_expires_at,"
                        "room_payload_persisted FROM project_invocation_intakes "
                        "WHERE caller_id=? AND project_id=? AND client_request_id=?",
                        (
                            envelope["caller_id"],
                            envelope["project_id"],
                            envelope["client_request_id"],
                        ),
                    ).fetchone()
                self.assertEqual(stored, (policy, expires_at, 0))

                with patch("backend.store.now_ms", return_value=expires_at - 1):
                    self.assertIsNotNone(
                        self.store.get_project_invocation(
                            caller_id=str(envelope["caller_id"]),
                            project_id=str(envelope["project_id"]),
                            client_request_id=str(envelope["client_request_id"]),
                        )
                    )
                with patch("backend.store.now_ms", return_value=expires_at):
                    with self.assertRaises(ProjectInvocationStoreError) as raised:
                        self.store.get_project_invocation_details(
                            caller_id=str(envelope["caller_id"]),
                            project_id=str(envelope["project_id"]),
                            client_request_id=str(envelope["client_request_id"]),
                        )
                self.assertEqual(
                    raised.exception.code,
                    "PROJECT_INVOCATION_RETENTION_EXPIRED",
                )
                self.assertEqual(raised.exception.status, 410)

    def test_tampered_intake_or_room_version_fails_closed(self) -> None:
        self.create()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE project_invocation_intakes "
                "SET request_semantics_json='{}'"
            )

        with self.assertRaises(ProjectInvocationStoreError) as raised:
            self.store.get_project_invocation(
                caller_id=str(self.envelope["caller_id"]),
                project_id=str(self.envelope["project_id"]),
                client_request_id=str(self.envelope["client_request_id"]),
            )
        self.assertEqual(
            raised.exception.code,
            "PROJECT_INVOCATION_INTEGRITY_FAILED",
        )


if __name__ == "__main__":
    unittest.main()
