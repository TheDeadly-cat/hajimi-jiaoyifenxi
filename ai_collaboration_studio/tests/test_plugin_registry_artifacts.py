from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.capability_packs import CAPABILITY_PACKS, capability_pack_catalog
from backend.decision_lineage import canonical_sha256
from backend.plugin_registry import (
    PluginRegistryError,
    build_room_plugin_registry_snapshot,
)
from backend.store import StudioStore
from backend.turn_contract import (
    CANDIDATE_RISK_REVIEW_VERSION,
    TURN_CONTRACT_VERSION,
)
from backend.turn_envelope import (
    TURN_ENVELOPE_SCHEMA_SHA256,
    TURN_ENVELOPE_VERSION,
)


class _ExecuteHookConnection:
    """Delegate a SQLite connection while exposing deterministic test gates."""

    def __init__(self, connection, *, before_execute=None, after_execute=None):
        self._connection = connection
        self._before_execute = before_execute
        self._after_execute = after_execute

    def execute(self, sql, parameters=()):
        if self._before_execute is not None:
            self._before_execute(str(sql))
        cursor = self._connection.execute(sql, parameters)
        if self._after_execute is not None:
            self._after_execute(str(sql))
        return cursor

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._connection.__exit__(exc_type, exc_value, traceback)

    def close(self) -> None:
        self._connection.close()

    def __getattr__(self, name):
        return getattr(self._connection, name)


class ArtifactPluginRegistryContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-p24-artifact-registry-"
        )
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _content(summary: str = "P24 frozen registry context") -> dict:
        return {
            "summary": summary,
            "conclusions": [],
            "disagreements": [],
            "unknowns": [],
            "actions": [],
        }

    def _create_room(self, pack_ids: list[str] | None = None) -> dict:
        return self.store.create_room(
            "P24 artifact registry",
            "Freeze the exact plugin registry used by an artifact",
            capability_pack_ids=(
                ["structured_project_research"] if pack_ids is None else pack_ids
            ),
        )["room"]

    def _create_artifact(
        self,
        room: dict,
        *,
        round_id: str = "",
        title: str = "P24 frozen artifact",
    ) -> dict:
        artifact = self.store.create_artifact(
            room["id"],
            round_id=round_id,
            title=title,
            content=self._content(title),
        )
        self.assertIsNotNone(artifact)
        return artifact or {}

    def _formal_round(self, room: dict) -> dict:
        formal_round = self.store.create_formal_round(
            room["id"],
            "P24 formal registry freeze",
            expected_settings_version=room["settings_version"],
            expected_plugin_registry_snapshot_sha256=room[
                "plugin_registry_snapshot_sha256"
            ],
        )
        members = self.store.enabled_members(room["id"])
        member_ids = [str(member["id"]) for member in members]
        shared_context, manifest = self.store.material_prompt_bundle(room["id"])
        frozen_manifest = self.store.finalize_round_evidence_manifest(
            manifest,
            shared_context=shared_context,
            market_snapshot=None,
        )
        self.store.save_round_checkpoint(
            room["id"],
            formal_round["id"],
            {
                "version": 9,
                "member_ids": member_ids,
                "moderator_member_id": member_ids[0],
                "spoken_counts": {},
                "spoken_stances": [],
                "successful_member_ids": [],
                "failed_member_ids": [],
                "previous_name": "host",
                "completed": 0,
                "failures": 0,
                "skipped": 0,
                "proposals_created": 0,
                "next_order": 1,
                "max_turns": max(1, len(member_ids)),
                "shared_context": shared_context,
                "market_snapshot": None,
                "round_evidence_manifest": frozen_manifest,
                "skip_provider_ids": [],
                "workflow_policy": room["workflow_policy"],
                "capability_pack_ids": room["capability_pack_ids"],
                "plugin_registry_snapshot": formal_round[
                    "plugin_registry_snapshot"
                ],
                "project_workspace": None,
                "turn_contract_version": TURN_CONTRACT_VERSION,
                "turn_contract_required": True,
                "candidate_risk_review_version": formal_round.get(
                    "candidate_risk_review_version"
                ),
                "candidate_risk_review_required": formal_round.get(
                    "candidate_risk_review_version"
                )
                == CANDIDATE_RISK_REVIEW_VERSION,
                "turn_envelope_version": TURN_ENVELOPE_VERSION,
                "turn_envelope_schema_sha256": TURN_ENVELOPE_SCHEMA_SHA256,
                "turn_output_modes_by_member": {
                    member_id: "json_schema" for member_id in member_ids
                },
            },
        )
        return formal_round

    def assertFrozenContext(
        self,
        context: dict,
        snapshot: dict,
        *,
        source_type: str,
        source_id: str,
    ) -> None:
        self.assertEqual(context["status"], "ready")
        self.assertTrue(context["integrity_ok"])
        self.assertTrue(context["exact_binding"])
        self.assertEqual(context["source_type"], source_type)
        self.assertEqual(context["source_id"], source_id)
        self.assertEqual(context["snapshot"], snapshot)
        self.assertEqual(
            context["snapshot_sha256"],
            snapshot["registry_snapshot_sha256"],
        )

    def assertFailedContextKeepsCoreArtifact(
        self,
        artifact: dict,
        *,
        artifact_id: str,
        title: str,
    ) -> None:
        self.assertEqual(artifact["id"], artifact_id)
        self.assertEqual(artifact["title"], title)
        self.assertEqual(artifact["content"]["summary"], title)
        context = artifact["plugin_registry_context"]
        self.assertEqual(context["status"], "integrity_failed")
        self.assertFalse(context["integrity_ok"])
        self.assertFalse(context["exact_binding"])
        self.assertEqual(context["snapshot"], {})
        self.assertEqual(context["snapshot_sha256"], "")
        self.assertTrue(context["integrity_issues"])
        for internal_field in (
            "plugin_registry_snapshot_json",
            "plugin_registry_snapshot_sha256",
            "plugin_registry_source_type",
            "plugin_registry_source_id",
            "_plugin_registry_snapshot_json",
            "_plugin_registry_snapshot_sha256",
            "_plugin_registry_source_type",
            "_plugin_registry_source_id",
        ):
            self.assertNotIn(internal_field, artifact)

    def test_roundless_artifact_freezes_current_room_registry(self) -> None:
        room = self._create_room()
        artifact = self._create_artifact(room)

        self.assertEqual(artifact["round_id"], "")
        self.assertFrozenContext(
            artifact["plugin_registry_context"],
            room["plugin_registry_snapshot"],
            source_type="room",
            source_id=room["id"],
        )

    def test_round_bound_artifact_copies_formal_round_and_ignores_later_room_change(
        self,
    ) -> None:
        room = self._create_room()
        formal_round = self._formal_round(room)
        artifact = self._create_artifact(room, round_id=formal_round["id"])
        frozen_snapshot = copy.deepcopy(formal_round["plugin_registry_snapshot"])

        changed_room = self.store.update_room(
            room["id"],
            {
                "capability_pack_ids": ["storage_research_readonly"],
                "expected_settings_version": room["settings_version"],
            },
        )
        self.assertIsNotNone(changed_room)
        self.assertNotEqual(
            changed_room["plugin_registry_snapshot_sha256"],
            frozen_snapshot["registry_snapshot_sha256"],
        )

        reread = self.store.get_artifact(room["id"], artifact["id"])
        self.assertIsNotNone(reread)
        self.assertFrozenContext(
            (reread or {})["plugin_registry_context"],
            frozen_snapshot,
            source_type="round",
            source_id=formal_round["id"],
        )
        self.assertEqual(
            (reread or {})["plugin_registry_context"]["snapshot"],
            self.store.get_round(room["id"], formal_round["id"])[
                "plugin_registry_snapshot"
            ],
        )

    def test_old_artifact_does_not_gain_new_storage_ui_contribution(self) -> None:
        room = self._create_room([])
        artifact = self._create_artifact(room)
        original_snapshot = copy.deepcopy(
            artifact["plugin_registry_context"]["snapshot"]
        )

        changed_room = self.store.update_room(
            room["id"],
            {
                "capability_pack_ids": ["storage_research_readonly"],
                "expected_settings_version": room["settings_version"],
            },
        )
        self.assertIsNotNone(changed_room)
        self.assertIn(
            "storage_research.artifact_workspace/v1",
            {
                row["contribution_id"]
                for row in changed_room["plugin_registry_snapshot"][
                    "ui_contributions"
                ]
            },
        )

        reread = self.store.get_artifact(room["id"], artifact["id"])
        self.assertIsNotNone(reread)
        frozen = (reread or {})["plugin_registry_context"]["snapshot"]
        self.assertEqual(frozen, original_snapshot)
        self.assertNotIn(
            "storage_research.artifact_workspace/v1",
            {row["contribution_id"] for row in frozen["ui_contributions"]},
        )

    def test_legacy_artifact_is_not_backfilled_from_current_room(self) -> None:
        room = self._create_room()
        artifact = self._create_artifact(room)
        with closing(self.store._connect()) as connection:
            connection.execute(
                """UPDATE artifacts
                      SET plugin_registry_snapshot_json='{}',
                          plugin_registry_snapshot_sha256='',
                          plugin_registry_source_type='',
                          plugin_registry_source_id=''
                    WHERE id=? AND room_id=?""",
                (artifact["id"], room["id"]),
            )
            connection.commit()

        reread = self.store.get_artifact(room["id"], artifact["id"])
        self.assertIsNotNone(reread)
        context = (reread or {})["plugin_registry_context"]
        self.assertEqual(context["status"], "legacy_unversioned")
        self.assertEqual(context["source_type"], "legacy_unversioned")
        self.assertFalse(context["integrity_ok"])
        self.assertFalse(context["exact_binding"])
        self.assertEqual(context["snapshot"], {})
        self.assertEqual(context["snapshot_sha256"], "")
        self.assertEqual(
            context["integrity_issues"],
            ["ARTIFACT_PLUGIN_REGISTRY_LEGACY_UNVERSIONED"],
        )
        self.assertNotEqual(room["plugin_registry_snapshot"], {})

    def test_artifact_registry_tamper_hides_context_but_keeps_core_artifact(
        self,
    ) -> None:
        tamper_cases = {
            "snapshot": ("plugin_registry_snapshot_json", '{"tampered":true}'),
            "hash": ("plugin_registry_snapshot_sha256", "0" * 64),
            "source_type": ("plugin_registry_source_type", "round"),
            "source_id": ("plugin_registry_source_id", "room_wrong"),
        }
        for case_name, (column, value) in tamper_cases.items():
            with self.subTest(case=case_name):
                room = self._create_room()
                title = f"P24 tamper {case_name}"
                artifact = self._create_artifact(room, title=title)
                with closing(self.store._connect()) as connection:
                    connection.execute(
                        f"UPDATE artifacts SET {column}=? WHERE id=? AND room_id=?",
                        (value, artifact["id"], room["id"]),
                    )
                    connection.commit()

                reread = self.store.get_artifact(room["id"], artifact["id"])
                self.assertIsNotNone(reread)
                self.assertFailedContextKeepsCoreArtifact(
                    reread or {},
                    artifact_id=artifact["id"],
                    title=title,
                )

    def test_round_registry_mirror_tamper_hides_artifact_context_only(self) -> None:
        room = self._create_room()
        formal_round = self._formal_round(room)
        title = "P24 tamper formal round mirror"
        artifact = self._create_artifact(
            room,
            round_id=formal_round["id"],
            title=title,
        )
        with closing(self.store._connect()) as connection:
            connection.execute(
                """UPDATE rounds
                      SET plugin_registry_snapshot_sha256=?
                    WHERE id=? AND room_id=?""",
                ("0" * 64, formal_round["id"], room["id"]),
            )
            connection.commit()

        reread = self.store.get_artifact(room["id"], artifact["id"])
        self.assertIsNotNone(reread)
        self.assertFailedContextKeepsCoreArtifact(
            reread or {},
            artifact_id=artifact["id"],
            title=title,
        )
        self.assertIn(
            "ARTIFACT_PLUGIN_REGISTRY_ROUND_BINDING_MISMATCH",
            (reread or {})["plugin_registry_context"]["integrity_issues"],
        )

    def test_artifact_version_detail_uses_its_original_frozen_context(self) -> None:
        room = self._create_room()
        artifact = self._create_artifact(room)
        original_snapshot = copy.deepcopy(
            artifact["plugin_registry_context"]["snapshot"]
        )

        changed_room = self.store.update_room(
            room["id"],
            {
                "capability_pack_ids": ["storage_research_readonly"],
                "expected_settings_version": room["settings_version"],
            },
        )
        self.assertIsNotNone(changed_room)
        revised = self.store.update_artifact(
            room["id"],
            artifact["id"],
            {
                "expected_version": artifact["version"],
                "content": self._content("P24 revised after room pack change"),
            },
        )
        self.assertIsNotNone(revised)

        version_one = self.store.get_artifact_version(
            room["id"], artifact["id"], 1
        )
        self.assertIsNotNone(version_one)
        version_detail = (version_one or {})["artifact_version"]
        self.assertFrozenContext(
            version_detail["plugin_registry_context"],
            original_snapshot,
            source_type="room",
            source_id=room["id"],
        )
        self.assertNotEqual(
            version_detail["plugin_registry_context"]["snapshot_sha256"],
            changed_room["plugin_registry_snapshot_sha256"],
        )

    def test_new_artifact_versions_are_sealed_and_public_snapshot_is_redacted(
        self,
    ) -> None:
        room = self._create_room()
        artifact = self._create_artifact(room)
        revised = self.store.update_artifact(
            room["id"],
            artifact["id"],
            {
                "expected_version": artifact["version"],
                "content": self._content("P24 sealed artifact revision"),
            },
        )
        self.assertIsNotNone(revised)

        with closing(self.store._connect()) as connection:
            rows = connection.execute(
                """SELECT version,snapshot_json,snapshot_sha256
                     FROM artifact_versions
                    WHERE room_id=? AND artifact_id=?
                    ORDER BY version""",
                (room["id"], artifact["id"]),
            ).fetchall()
        self.assertEqual([int(row["version"]) for row in rows], [1, 2])
        for row in rows:
            stored_snapshot = json.loads(str(row["snapshot_json"]))
            stored_sha256 = str(row["snapshot_sha256"] or "")
            self.assertEqual(len(stored_sha256), 64)
            self.assertEqual(stored_sha256, canonical_sha256(stored_snapshot))

        detail = self.store.get_artifact_version(
            room["id"], artifact["id"], 2
        )
        self.assertIsNotNone(detail)
        version_record = (detail or {})["artifact_version"]
        self.assertEqual(version_record["snapshot_storage_status"], "sealed")
        self.assertTrue(version_record["snapshot_storage_integrity_ok"])
        self.assertEqual(
            version_record["stored_snapshot_sha256"],
            version_record["snapshot_sha256"],
        )
        public_snapshot = version_record["snapshot"]
        for field in (
            "plugin_registry_snapshot_json",
            "plugin_registry_snapshot_sha256",
            "plugin_registry_source_type",
            "plugin_registry_source_id",
            "_plugin_registry_snapshot_json",
            "_plugin_registry_snapshot_sha256",
            "_plugin_registry_source_type",
            "_plugin_registry_source_id",
        ):
            self.assertNotIn(field, public_snapshot)

    def test_artifact_version_snapshot_tamper_fails_closed_with_value_error(
        self,
    ) -> None:
        room = self._create_room()
        artifact = self._create_artifact(room)
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                """SELECT snapshot_json FROM artifact_versions
                    WHERE room_id=? AND artifact_id=? AND version=1""",
                (room["id"], artifact["id"]),
            ).fetchone()
            tampered_snapshot = json.loads(str(row["snapshot_json"]))
            tampered_snapshot["title"] = "tampered without resealing"
            connection.execute(
                """UPDATE artifact_versions SET snapshot_json=?
                    WHERE room_id=? AND artifact_id=? AND version=1""",
                (
                    json.dumps(tampered_snapshot, ensure_ascii=False),
                    room["id"],
                    artifact["id"],
                ),
            )
            connection.commit()

        with self.assertRaises(ValueError) as raised:
            self.store.get_artifact_version(room["id"], artifact["id"], 1)
        self.assertIn(
            "ARTIFACT_VERSION_SNAPSHOT_SEAL_MISMATCH",
            str(raised.exception),
        )
        listed = self.store.list_artifact_versions(room["id"], artifact["id"])
        self.assertIsNotNone(listed)
        version_record = (listed or {})["versions"][0]
        self.assertFalse(version_record["integrity_ok"])
        self.assertFalse(version_record["snapshot_storage_integrity_ok"])
        self.assertIn(
            "ARTIFACT_VERSION_SNAPSHOT_SEAL_MISMATCH",
            version_record["integrity_issues"],
        )

    def test_legacy_empty_artifact_version_seal_is_explicit_and_not_backfilled(
        self,
    ) -> None:
        room = self._create_room()
        artifact = self._create_artifact(room)
        with closing(self.store._connect()) as connection:
            connection.execute(
                """UPDATE artifact_versions SET snapshot_sha256=''
                    WHERE room_id=? AND artifact_id=? AND version=1""",
                (room["id"], artifact["id"]),
            )
            connection.commit()

        reopened = StudioStore(self.store.path)
        listed = reopened.list_artifact_versions(room["id"], artifact["id"])
        self.assertIsNotNone(listed)
        summary = (listed or {})["versions"][0]
        self.assertEqual(summary["snapshot_storage_status"], "legacy_unsealed")
        self.assertFalse(summary["snapshot_storage_integrity_ok"])
        self.assertEqual(summary["stored_snapshot_sha256"], "")

        detail = reopened.get_artifact_version(room["id"], artifact["id"], 1)
        self.assertIsNotNone(detail)
        record = (detail or {})["artifact_version"]
        self.assertEqual(record["snapshot_storage_status"], "legacy_unsealed")
        self.assertFalse(record["snapshot_storage_integrity_ok"])
        self.assertEqual(record["stored_snapshot_sha256"], "")
        with closing(reopened._connect()) as connection:
            persisted = connection.execute(
                """SELECT snapshot_sha256 FROM artifact_versions
                    WHERE room_id=? AND artifact_id=? AND version=1""",
                (room["id"], artifact["id"]),
            ).fetchone()
        self.assertEqual(str(persisted["snapshot_sha256"] or ""), "")


class RoundRegistryTransactionConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-p24-round-transaction-"
        )
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.round_store = StudioStore(self.db_path)
        self.update_store = StudioStore(self.db_path)
        self.room = self.round_store.create_room(
            "P24 transactional round",
            "Bind the room registry without a read-write race",
            capability_pack_ids=["structured_project_research"],
        )["room"]
        self.old_snapshot = copy.deepcopy(
            self.room["plugin_registry_snapshot"]
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _normalized_sql(sql: str) -> str:
        return " ".join(str(sql).upper().split())

    def _create_expected_round(self) -> dict:
        return self.round_store.create_round(
            self.room["id"],
            "P24 deterministic transaction race",
            plugin_registry_snapshot=self.old_snapshot,
            expected_settings_version=self.room["settings_version"],
            expected_plugin_registry_snapshot_sha256=self.room[
                "plugin_registry_snapshot_sha256"
            ],
        )

    def _update_room_pack(self) -> dict | None:
        return self.update_store.update_room(
            self.room["id"],
            {
                "capability_pack_ids": ["storage_research_readonly"],
                "expected_settings_version": self.room["settings_version"],
            },
        )

    def test_round_write_lock_serializes_competing_room_update(self) -> None:
        round_lock_acquired = threading.Event()
        allow_round_to_continue = threading.Event()
        update_write_started = threading.Event()
        update_finished = threading.Event()
        results: dict[str, object] = {}
        errors: list[tuple[str, Exception]] = []
        original_round_connect = self.round_store._connect
        original_update_connect = self.update_store._connect

        def round_after_execute(sql: str) -> None:
            if self._normalized_sql(sql) == "BEGIN IMMEDIATE":
                round_lock_acquired.set()
                if not allow_round_to_continue.wait(5):
                    raise AssertionError("round transaction gate timed out")

        def update_before_execute(sql: str) -> None:
            if self._normalized_sql(sql) == "BEGIN IMMEDIATE":
                update_write_started.set()

        def round_connect():
            return _ExecuteHookConnection(
                original_round_connect(),
                after_execute=round_after_execute,
            )

        def update_connect():
            return _ExecuteHookConnection(
                original_update_connect(),
                before_execute=update_before_execute,
            )

        def create_round_worker() -> None:
            try:
                results["round"] = self._create_expected_round()
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(("round", exc))

        def update_room_worker() -> None:
            try:
                results["room"] = self._update_room_pack()
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(("room", exc))
            finally:
                update_finished.set()

        round_thread = threading.Thread(
            target=create_round_worker,
            name="p24-round-writer",
            daemon=True,
        )
        update_thread = threading.Thread(
            target=update_room_worker,
            name="p24-room-updater",
            daemon=True,
        )
        with patch.object(
            self.round_store,
            "_connect",
            side_effect=round_connect,
        ), patch.object(
            self.update_store,
            "_connect",
            side_effect=update_connect,
        ):
            round_thread.start()
            self.assertTrue(round_lock_acquired.wait(5))
            update_thread.start()
            try:
                self.assertTrue(update_write_started.wait(5))
                self.assertFalse(
                    update_finished.wait(0.2),
                    "room update crossed the round's BEGIN IMMEDIATE lock",
                )
            finally:
                allow_round_to_continue.set()
            round_thread.join(10)
            update_thread.join(10)

        self.assertFalse(round_thread.is_alive())
        self.assertFalse(update_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertIsInstance(results.get("round"), dict)
        self.assertIsInstance(results.get("room"), dict)

        persisted_round = self.round_store.get_round(
            self.room["id"], str((results["round"] or {}).get("id") or "")
        )
        current_room = self.update_store.room_snapshot(self.room["id"])["room"]
        self.assertIsNotNone(persisted_round)
        self.assertEqual(
            persisted_round["plugin_registry_snapshot"],
            self.old_snapshot,
        )
        self.assertEqual(
            persisted_round["plugin_registry_snapshot_sha256"],
            self.room["plugin_registry_snapshot_sha256"],
        )
        self.assertEqual(
            current_room["capability_pack_ids"],
            ["storage_research_readonly"],
        )
        self.assertNotEqual(
            current_room["plugin_registry_snapshot_sha256"],
            persisted_round["plugin_registry_snapshot_sha256"],
        )

    def test_round_rejects_drift_committed_before_begin_immediate(self) -> None:
        round_begin_reached = threading.Event()
        allow_round_begin = threading.Event()
        update_finished = threading.Event()
        results: dict[str, object] = {}
        original_round_connect = self.round_store._connect

        def round_before_execute(sql: str) -> None:
            if self._normalized_sql(sql) == "BEGIN IMMEDIATE":
                round_begin_reached.set()
                if not allow_round_begin.wait(5):
                    raise AssertionError("round begin gate timed out")

        def round_connect():
            return _ExecuteHookConnection(
                original_round_connect(),
                before_execute=round_before_execute,
            )

        def create_round_worker() -> None:
            try:
                results["round"] = self._create_expected_round()
            except Exception as exc:
                results["round_error"] = exc

        def update_room_worker() -> None:
            try:
                results["room"] = self._update_room_pack()
            except Exception as exc:  # pragma: no cover - asserted below
                results["room_error"] = exc
            finally:
                update_finished.set()

        round_thread = threading.Thread(
            target=create_round_worker,
            name="p24-round-drift-check",
            daemon=True,
        )
        update_thread = threading.Thread(
            target=update_room_worker,
            name="p24-room-drift-writer",
            daemon=True,
        )
        with patch.object(
            self.round_store,
            "_connect",
            side_effect=round_connect,
        ):
            round_thread.start()
            self.assertTrue(round_begin_reached.wait(5))
            update_thread.start()
            try:
                self.assertTrue(update_finished.wait(10))
                self.assertNotIn("room_error", results)
                self.assertIsInstance(results.get("room"), dict)
            finally:
                allow_round_begin.set()
            round_thread.join(10)
            update_thread.join(10)

        self.assertFalse(round_thread.is_alive())
        self.assertFalse(update_thread.is_alive())
        self.assertNotIn("round", results)
        self.assertIsInstance(results.get("round_error"), PluginRegistryError)
        current_room = self.update_store.room_snapshot(self.room["id"])["room"]
        self.assertEqual(
            current_room["capability_pack_ids"],
            ["storage_research_readonly"],
        )
        with closing(self.update_store._connect()) as connection:
            round_count = connection.execute(
                "SELECT COUNT(*) FROM rounds WHERE room_id=?",
                (self.room["id"],),
            ).fetchone()[0]
        self.assertEqual(round_count, 0)


class PluginRegistryResolutionSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-p24-registry-resolution-"
        )
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_unrelated_catalog_pack_does_not_change_selected_closure_snapshot(
        self,
    ) -> None:
        room = self.store.create_room(
            "P24 closure seal",
            "Unrelated catalog growth must not rewrite a frozen room",
            capability_pack_ids=["structured_project_research"],
        )["room"]
        original_snapshot = copy.deepcopy(room["plugin_registry_snapshot"])
        unrelated = copy.deepcopy(CAPABILITY_PACKS["structured_project_research"])
        unrelated.update(
            {
                "id": "unrelated_static_pack",
                "name": "Unrelated static pack",
                "capabilities": ["research.unrelated.static"],
                "dependencies": [],
                "domain_adapter_ids": [],
                "ui_contribution_ids": [],
            }
        )

        with patch.dict(
            CAPABILITY_PACKS,
            {"unrelated_static_pack": unrelated},
            clear=False,
        ):
            rebuilt = build_room_plugin_registry_snapshot(
                ["structured_project_research"]
            )
            reread = self.store.room_snapshot(room["id"])["room"]

        self.assertEqual(rebuilt, original_snapshot)
        self.assertEqual(reread["plugin_registry_snapshot"], original_snapshot)
        self.assertTrue(reread["plugin_registry_integrity_ok"])

    def test_incompatible_core_range_fails_closed_before_room_write(self) -> None:
        incompatible = copy.deepcopy(
            CAPABILITY_PACKS["structured_project_research"]
        )
        incompatible["core_protocol_range"] = ">=2.0.0 <3.0.0"
        with closing(self.store._connect()) as connection:
            before = connection.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]

        with patch.dict(
            CAPABILITY_PACKS,
            {"structured_project_research": incompatible},
            clear=False,
        ):
            with self.assertRaises(PluginRegistryError):
                build_room_plugin_registry_snapshot(
                    ["structured_project_research"]
                )
            with self.assertRaises(PluginRegistryError):
                self.store.create_room(
                    "P24 incompatible pack",
                    "Do not silently downgrade an incompatible plugin",
                    capability_pack_ids=["structured_project_research"],
                )

        with closing(self.store._connect()) as connection:
            after = connection.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
        self.assertEqual(after, before)

    def test_structured_project_research_is_static_and_has_no_adapter_market_or_provider(
        self,
    ) -> None:
        manifest = next(
            pack
            for pack in capability_pack_catalog()
            if pack["id"] == "structured_project_research"
        )
        snapshot = build_room_plugin_registry_snapshot(
            ["structured_project_research"]
        )

        self.assertEqual(manifest["domain_adapter_ids"], [])
        self.assertFalse(
            any(
                capability.startswith(("market.", "provider."))
                for capability in manifest["capabilities"]
            )
        )
        self.assertNotIn("market_data_policy", manifest)
        self.assertNotIn("provider_call_budget", manifest)
        self.assertEqual(snapshot["domain_adapters"], [])
        self.assertIn(
            "project_research.artifact_workspace/v1",
            {
                row["contribution_id"]
                for row in snapshot["ui_contributions"]
            },
        )
        self.assertFalse(snapshot["resolution"]["dynamic_code_loading"])
        self.assertEqual(snapshot["safety"]["execution_capability"], "none")
        self.assertFalse(snapshot["safety"]["live_trading_allowed"])


if __name__ == "__main__":
    unittest.main()
