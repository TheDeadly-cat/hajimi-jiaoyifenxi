from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from backend.decision_lineage import canonical_sha256
from backend.store import StudioStore
from backend.turn_contract import (
    CANDIDATE_RISK_REVIEW_VERSION,
    TURN_CONTRACT_VERSION,
)
from backend.turn_contract_artifact import project_turn_contract_artifact
from backend.turn_envelope import (
    TURN_ENVELOPE_SCHEMA_SHA256,
    TURN_ENVELOPE_VERSION,
)
from backend.workflow_policy import default_workflow_policy


ATTESTATION_VERSION = "artifact_governance_attestation_v1"
EVALUATOR_VERSION = "round_governance_evaluator_v1"
GOVERNANCE_VERSION = "artifact_governance_v1"


def _contract(candidate_updates: list[dict]) -> dict:
    return {
        "version": TURN_CONTRACT_VERSION,
        "claims": [],
        "responds_to": [],
        "candidate_updates": candidate_updates,
        "risks": [],
        "next_actions": [],
        "confidence": {
            "kind": "model_subjective",
            "value": None,
            "label": "unknown",
            "basis": "",
        },
        "confidence_is_not_win_rate": True,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def _candidate(
    candidate_id: str,
    action: str,
    *,
    title: str,
    thesis: str,
    evidence_id: str = "",
) -> dict:
    return {
        "id": candidate_id,
        "title": title,
        "action": action,
        "symbol": "US.MU",
        "direction": "LONG" if candidate_id == "candidate_a" else "HOLD",
        "horizon_days": 20,
        "thesis": thesis,
        "invalidation": f"{title} 的关键假设不再成立",
        "evidence": (
            [{"type": "message", "id": evidence_id, "role": "context"}]
            if evidence_id
            else []
        ),
    }


def _formal_message(
    message_id: str,
    sender_id: str,
    payload: dict,
) -> dict:
    return {
        "id": message_id,
        "sender_type": "ai",
        "sender_id": sender_id,
        "sender_name": sender_id,
        "member_version": 1,
        "is_formal_round_turn": True,
        "turn_contract_version": TURN_CONTRACT_VERSION,
        "turn_contract": payload,
        "turn_contract_qualified": True,
        "turn_contract_issues": [],
        "turn_contract_integrity_ok": True,
    }


def _ready_projection() -> dict:
    proposal = _formal_message(
        "governance_plan_message",
        "governance_planner",
        _contract([
            _candidate(
                "candidate_a",
                "propose",
                title="候选 A",
                thesis="先做可逆的小范围纸面验证",
            ),
            _candidate(
                "candidate_b",
                "propose",
                title="候选 B",
                thesis="保留现状并继续收集证据",
            ),
        ]),
    )
    risk_contract = _contract([
        _candidate(
            "candidate_a",
            "support",
            title="候选 A",
            thesis="先做可逆的小范围纸面验证",
        ),
        _candidate(
            "candidate_b",
            "challenge",
            title="候选 B",
            thesis="保留现状并继续收集证据",
        ),
    ])
    risk_contract["responds_to"] = [{
        "type": "message",
        "id": proposal["id"],
        "relation": "qualifies",
        "reason": "精确复核两个候选的当前版本。",
    }]
    risk_review = _formal_message(
        "governance_risk_message",
        "governance_risk",
        risk_contract,
    )
    decision = _formal_message(
        "governance_decision_message",
        "governance_decision",
        _contract([
            _candidate(
                "candidate_a",
                "select",
                title="候选 A",
                thesis="先做可逆的小范围纸面验证",
                evidence_id=risk_review["id"],
            ),
            _candidate(
                "candidate_b",
                "reject",
                title="候选 B",
                thesis="保留现状并继续收集证据",
            ),
        ]),
    )
    projection = project_turn_contract_artifact(
        [proposal, risk_review, decision],
        member_resolver=lambda member_id, _version: {
            "workflow_stage": (
                "risk"
                if member_id == "governance_risk"
                else "decision"
                if member_id == "governance_decision"
                else "plan"
            )
        },
        candidate_risk_review_required=True,
    )
    assert projection["candidate_lineage"]["ready"] is True
    assert projection["candidate_risk_reviews"]["ready"] is True
    assert projection["decision"]["status"] == "candidate"
    return projection


def _reviewed_message_evidence(message_id: str) -> list[dict]:
    return [{
        "type": "message",
        "id": message_id,
        "evidence_role": "support",
        "verification_status": "source_checked",
        "review_note": "已核对本地离线测试消息。",
    }]


class ArtifactGovernanceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "artifact-governance.sqlite3"
        self.store = StudioStore(self.db_path)

    @contextmanager
    def _governance_patches(self, context_or_factory):
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                StudioStore,
                "_artifact_round_confirmation_issues",
                return_value=[],
            ))
            stack.enter_context(patch.object(
                StudioStore,
                "_artifact_confirmation_issues",
                return_value=[],
            ))
            options = (
                {"side_effect": context_or_factory}
                if callable(context_or_factory)
                else {"return_value": context_or_factory}
            )
            stack.enter_context(patch.object(
                StudioStore,
                "_artifact_governance_round_context",
                create=True,
                **options,
            ))
            yield

    def _create_formal_round(self) -> tuple[dict, dict]:
        round_row = self.store.create_formal_round(
            "room_storage",
            "验证 P13 artifact governance 持久化",
        )
        source_message = self.store.add_message(
            "room_storage",
            sender_type="user",
            sender_id="user",
            sender_name="用户",
            content="只验证离线治理持久化，不调用模型。",
            round_id=round_row["id"],
        )
        shared_context, manifest = self.store.material_prompt_bundle("room_storage")
        manifest = self.store.finalize_round_evidence_manifest(
            manifest,
            shared_context=shared_context,
            market_snapshot=None,
        )
        members = self.store.enabled_members("room_storage")
        self.store.save_round_checkpoint(
            "room_storage",
            round_row["id"],
            {
                "version": 9,
                "member_ids": [str(member["id"]) for member in members],
                "moderator_member_id": str(members[0]["id"]),
                "spoken_counts": {},
                "spoken_stances": [],
                "successful_member_ids": [],
                "failed_member_ids": [],
                "previous_name": "",
                "completed": 0,
                "failures": 0,
                "skipped": 0,
                "proposals_created": 0,
                "next_order": 1,
                "max_turns": len(members),
                "shared_context": shared_context,
                "market_snapshot": None,
                "round_evidence_manifest": manifest,
                "skip_provider_ids": [],
                "workflow_policy": default_workflow_policy("open_collaboration"),
                "capability_pack_ids": [],
                "project_workspace": None,
                "turn_contract_version": TURN_CONTRACT_VERSION,
                "turn_contract_required": True,
                "candidate_risk_review_version": CANDIDATE_RISK_REVIEW_VERSION,
                "candidate_risk_review_required": True,
                "turn_envelope_version": TURN_ENVELOPE_VERSION,
                "turn_envelope_schema_sha256": TURN_ENVELOPE_SCHEMA_SHA256,
                "turn_output_modes_by_member": {
                    str(member["id"]): "json_schema"
                    for member in members
                },
            },
        )
        self.store.complete_round(round_row["id"], "COMPLETED")
        return self.store.get_round("room_storage", round_row["id"]), source_message

    def _artifact_content(self, message_id: str, *, mismatch: bool = False) -> dict:
        evidence = _reviewed_message_evidence(message_id)
        decision = copy.deepcopy(_ready_projection()["decision"])
        for option in decision["options"]:
            option["evidence"] = copy.deepcopy(evidence)
        decision["evidence"] = copy.deepcopy(evidence)
        if mismatch:
            decision["preferred_option_id"] = "candidate_b"
            decision["rationale"] = "故意选择未经治理投影选中的另一候选。"
        return {
            "summary": "本产物只用于验证治理证明的 SQLite 原子持久化。",
            "summary_evidence": copy.deepcopy(evidence),
            "requirements": [],
            "risks": [],
            "conclusions": [],
            "disagreements": [],
            "unknowns": [],
            "actions": [],
            "decision": decision,
        }

    def _create_governed_artifact(
        self,
        *,
        mismatch: bool = False,
    ) -> tuple[dict, dict]:
        round_row, source_message = self._create_formal_round()
        artifact = self.store.create_artifact(
            "room_storage",
            title="P13 治理产物",
            round_id=round_row["id"],
            content=self._artifact_content(source_message["id"], mismatch=mismatch),
            created_by="offline_governance_test",
        )
        projection = _ready_projection()
        # The Store intentionally strips server-only lineage fields from the
        # user-editable content. Governance compares the public decision fields
        # but still needs the authoritative lineage kept in the projection.
        artifact_decision = artifact["content"]["decision"]
        projected_options = {
            str(option.get("id") or ""): option
            for option in projection["decision"]["options"]
            if isinstance(option, dict)
        }
        for artifact_option in artifact_decision["options"]:
            projected_option = projected_options[str(artifact_option["id"])]
            projected_option.update({
                key: copy.deepcopy(value)
                for key, value in artifact_option.items()
                if key != "lineage"
            })
        projection["decision"].update({
            "status": str(artifact_decision["status"]),
            "preferred_option_id": str(artifact_decision["preferred_option_id"]),
            "rationale": str(artifact_decision["rationale"]),
            "evidence": copy.deepcopy(artifact_decision["evidence"]),
        })
        if mismatch:
            projection["decision"]["preferred_option_id"] = "candidate_a"
            projection["decision"]["rationale"] = (
                "选择依据：先做可逆的小范围纸面验证；"
                "失效条件：候选 A 的关键假设不再成立"
            )
        round_input = {
            "version": "round_governance_input_v1",
            "room_id": "room_storage",
            "round_id": round_row["id"],
            "round_status": "COMPLETED",
            "round_objective": round_row["objective"],
            "turn_contract_version": TURN_CONTRACT_VERSION,
            "candidate_risk_review_version": CANDIDATE_RISK_REVIEW_VERSION,
            "source_message_ids": list(projection.get("source_message_ids") or []),
            "turn_ledger_sha256": str(round_row.get("turn_ledger_sha256") or ""),
        }
        context = {
            "version": GOVERNANCE_VERSION,
            "applicable": True,
            "status": "ready",
            "round_id": round_row["id"],
            "round_status": "COMPLETED",
            "turn_contract_version": TURN_CONTRACT_VERSION,
            "candidate_risk_review_version": CANDIDATE_RISK_REVIEW_VERSION,
            "candidate_risk_review_required": True,
            "bundle_valid": True,
            "bundle_issues": [],
            "messages": [],
            "successful_member_ids": [],
            "round_governance_input": round_input,
            "round_governance_input_sha256": canonical_sha256(round_input),
            "projection": projection,
            "projection_issues": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        }
        return artifact, context

    def _artifact_state(self, artifact_id: str) -> tuple[str, int, list[int], int]:
        with closing(self.store._connect()) as connection:
            artifact = connection.execute(
                "SELECT status,version FROM artifacts WHERE id=?",
                (artifact_id,),
            ).fetchone()
            versions = connection.execute(
                "SELECT version FROM artifact_versions WHERE artifact_id=? ORDER BY version",
                (artifact_id,),
            ).fetchall()
            attestation_count = connection.execute(
                "SELECT COUNT(*) FROM artifact_governance_attestations WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()[0]
        return (
            str(artifact["status"]),
            int(artifact["version"]),
            [int(row["version"]) for row in versions],
            int(attestation_count),
        )

    def _confirm_governed(self) -> tuple[dict, dict, sqlite3.Row]:
        artifact, context = self._create_governed_artifact()
        with self._governance_patches(context):
            confirmed = self.store.confirm_artifact(
                "room_storage",
                artifact["id"],
                expected_version=artifact["version"],
                confirmed_by="user",
            )
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM artifact_governance_attestations
                   WHERE artifact_id=? AND artifact_version=?""",
                (artifact["id"], confirmed["version"]),
            ).fetchone()
        self.assertIsNotNone(row)
        return confirmed, context, row

    @staticmethod
    def _governed_support_tokens(
        context: dict,
        attestation_row: sqlite3.Row,
        selected_option_id: str = "candidate_a",
    ) -> dict:
        lineage = next(
            candidate
            for candidate in context["projection"]["candidate_lineage"]["candidates"]
            if candidate["id"] == selected_option_id
        )
        return {
            "selected_option_id": selected_option_id,
            "expected_candidate_revision": lineage["revision"],
            "expected_candidate_origin_message_id": lineage["origin_message_id"],
            "expected_candidate_latest_message_id": lineage["latest_message_id"],
            "expected_governance_attestation_sha256": attestation_row[
                "attestation_sha256"
            ],
        }

    def test_schema_migrates_legacy_decisions_without_backfilling_governance(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy-governance.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as connection, connection:
            connection.execute(
                """CREATE TABLE artifact_user_decisions (
                       id TEXT PRIMARY KEY,
                       room_id TEXT NOT NULL,
                       artifact_id TEXT NOT NULL,
                       artifact_version INTEGER NOT NULL,
                       action TEXT NOT NULL,
                       rationale TEXT NOT NULL,
                       preferred_option_id TEXT NOT NULL DEFAULT '',
                       artifact_snapshot_sha256 TEXT NOT NULL,
                       created_by TEXT NOT NULL DEFAULT 'user',
                       created_at INTEGER NOT NULL
                   )"""
            )
            connection.execute(
                """INSERT INTO artifact_user_decisions(
                       id,room_id,artifact_id,artifact_version,action,rationale,
                       preferred_option_id,artifact_snapshot_sha256,created_by,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    "legacy_decision",
                    "legacy_room",
                    "legacy_artifact",
                    3,
                    "hold",
                    "保留旧记录，但不追溯伪造治理证明。",
                    "",
                    "a" * 64,
                    "legacy_user",
                    1,
                ),
            )

        StudioStore(legacy_path)
        with closing(sqlite3.connect(legacy_path)) as connection:
            decision_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(artifact_user_decisions)"
                ).fetchall()
            }
            attestation_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(artifact_governance_attestations)"
                ).fetchall()
            }
            legacy = connection.execute(
                """SELECT id,artifact_version,governance_attestation_sha256
                   FROM artifact_user_decisions WHERE id='legacy_decision'"""
            ).fetchone()
            attestation_count = connection.execute(
                "SELECT COUNT(*) FROM artifact_governance_attestations"
            ).fetchone()[0]

        self.assertIn("governance_attestation_sha256", decision_columns)
        self.assertTrue({
            "artifact_id",
            "artifact_version",
            "room_id",
            "round_id",
            "attestation_version",
            "evaluator_version",
            "round_governance_input_sha256",
            "projection_json",
            "projection_sha256",
            "artifact_binding_sha256",
            "attestation_json",
            "attestation_sha256",
            "created_at",
        }.issubset(attestation_columns))
        self.assertEqual(legacy, ("legacy_decision", 3, None))
        self.assertEqual(attestation_count, 0)

    def test_mismatched_formal_decision_fails_without_persistent_writes(self) -> None:
        artifact, context = self._create_governed_artifact(mismatch=True)
        before = self._artifact_state(artifact["id"])

        with self._governance_patches(context):
            with self.assertRaises(ValueError):
                self.store.confirm_artifact(
                    "room_storage",
                    artifact["id"],
                    expected_version=artifact["version"],
                    confirmed_by="user",
                )

        self.assertEqual(before, ("DRAFT", 1, [1], 0))
        self.assertEqual(self._artifact_state(artifact["id"]), before)

    def test_matching_confirmation_atomically_persists_immutable_attestation(self) -> None:
        artifact, context = self._create_governed_artifact()
        target_version = int(artifact["version"]) + 1
        forged_hash = "f" * 64
        with closing(self.store._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO artifact_governance_attestations(
                       artifact_id,artifact_version,room_id,round_id,
                       attestation_version,evaluator_version,
                       round_governance_input_sha256,projection_json,
                       projection_sha256,artifact_binding_sha256,
                       attestation_json,attestation_sha256,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    artifact["id"],
                    target_version,
                    "room_storage",
                    artifact["round_id"],
                    ATTESTATION_VERSION,
                    EVALUATOR_VERSION,
                    forged_hash,
                    "{}",
                    forged_hash,
                    forged_hash,
                    "{}",
                    forged_hash,
                    1,
                ),
            )

        with self._governance_patches(context):
            with self.assertRaises((ValueError, sqlite3.IntegrityError)):
                self.store.confirm_artifact(
                    "room_storage",
                    artifact["id"],
                    expected_version=artifact["version"],
                    confirmed_by="user",
                )
        # The conflicting append-only attestation may remain, but artifact state
        # and its immutable version history must roll back together.
        self.assertEqual(
            self._artifact_state(artifact["id"]),
            ("DRAFT", 1, [1], 1),
        )

        with closing(self.store._connect()) as connection, connection:
            connection.execute(
                """DELETE FROM artifact_governance_attestations
                   WHERE artifact_id=? AND artifact_version=?""",
                (artifact["id"], target_version),
            )
        with self._governance_patches(context):
            confirmed = self.store.confirm_artifact(
                "room_storage",
                artifact["id"],
                expected_version=artifact["version"],
                confirmed_by="user",
            )

        self.assertEqual(confirmed["status"], "CONFIRMED")
        self.assertEqual(confirmed["version"], target_version)
        self.assertTrue(confirmed["governance_snapshot"]["applicable"])
        self.assertEqual(confirmed["governance_snapshot"]["status"], "ready")
        self.assertTrue(
            confirmed["governance_snapshot"]["attestation_integrity_ok"]
        )
        self.assertEqual(
            self._artifact_state(artifact["id"]),
            ("CONFIRMED", target_version, [1, target_version], 1),
        )
        with closing(self.store._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM artifact_governance_attestations
                   WHERE artifact_id=? AND artifact_version=?""",
                (artifact["id"], target_version),
            ).fetchone()
        self.assertEqual(row["attestation_version"], ATTESTATION_VERSION)
        self.assertEqual(row["evaluator_version"], EVALUATOR_VERSION)
        self.assertEqual(row["round_governance_input_sha256"], context["round_governance_input_sha256"])
        self.assertRegex(row["projection_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(row["artifact_binding_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(row["attestation_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(json.loads(row["projection_json"]), context["projection"])
        attestation = json.loads(row["attestation_json"])
        self.assertEqual(attestation["artifact_id"], artifact["id"])
        self.assertEqual(attestation["artifact_version"], target_version)
        self.assertEqual(attestation["round_id"], artifact["round_id"])
        self.assertEqual(attestation["execution_capability"], "none")
        self.assertFalse(attestation["live_trading_allowed"])
        self.assertFalse(attestation["can_autonomously_decide"])

    def test_confirmation_cas_rejects_stale_version_without_new_attestation(self) -> None:
        artifact, context = self._create_governed_artifact()
        revised_content = copy.deepcopy(artifact["content"])
        revised_content["summary"] = "另一个写入者已经保存了新草稿版本。"
        revised = self.store.update_artifact(
            "room_storage",
            artifact["id"],
            {
                "expected_version": artifact["version"],
                "title": artifact["title"],
                "content": revised_content,
            },
        )
        context["projection"]["decision"] = copy.deepcopy(
            revised["content"]["decision"]
        )

        with self._governance_patches(context):
            with self.assertRaises(ValueError):
                self.store.confirm_artifact(
                    "room_storage",
                    artifact["id"],
                    expected_version=artifact["version"],
                    confirmed_by="stale_user",
                )

        self.assertEqual(
            self._artifact_state(artifact["id"]),
            ("DRAFT", revised["version"], [1, revised["version"]], 0),
        )

    def test_new_user_decision_binds_exact_governance_attestation(self) -> None:
        confirmed, context, attestation_row = self._confirm_governed()
        with self._governance_patches(context):
            decision = self.store.create_artifact_user_decision(
                "room_storage",
                confirmed["id"],
                expected_version=confirmed["version"],
                action="support",
                rationale="支持继续进行无实盘能力的纸面验证。",
                **self._governed_support_tokens(context, attestation_row),
            )
            current = self.store.get_artifact("room_storage", confirmed["id"])
            evidence_graph = self.store.artifact_evidence_graph(
                "room_storage",
                confirmed["id"],
            )

        self.assertEqual(
            decision["governance_attestation_sha256"],
            attestation_row["attestation_sha256"],
        )
        self.assertTrue(decision["governance_attestation_integrity_ok"])
        self.assertTrue(decision["integrity_ok"])
        self.assertTrue(current["governance_snapshot"]["integrity_ok"])
        self.assertEqual(
            current["governance_snapshot"]["user_decision_state"]["decision_id"],
            decision["id"],
        )
        self.assertTrue(current["user_decision"]["is_current"])
        self.assertTrue(any(
            edge.get("edge_type") == "decides_on"
            for edge in evidence_graph["edges"]
        ))
        with closing(self.store._connect()) as connection:
            persisted = connection.execute(
                "SELECT * FROM artifact_user_decisions WHERE id=?",
                (decision["id"],),
            ).fetchone()
        self.assertEqual(
            persisted["governance_attestation_sha256"],
            attestation_row["attestation_sha256"],
        )

    def test_historical_version_replays_its_attestation_and_user_decision(self) -> None:
        confirmed, context, attestation_row = self._confirm_governed()
        with self._governance_patches(context):
            decision = self.store.create_artifact_user_decision(
                "room_storage",
                confirmed["id"],
                expected_version=confirmed["version"],
                action="support",
                rationale="历史版本继续保留用户决定与治理证明。",
                **self._governed_support_tokens(context, attestation_row),
            )
            detail = self.store.get_artifact_version(
                "room_storage",
                confirmed["id"],
                confirmed["version"],
            )

        version = detail["artifact_version"]
        governance = version["governance_snapshot"]
        self.assertTrue(governance["integrity_ok"])
        self.assertEqual(
            governance["attestation_sha256"],
            attestation_row["attestation_sha256"],
        )
        self.assertEqual(
            governance["user_decision_state"]["decision_id"],
            decision["id"],
        )
        self.assertEqual(
            version["snapshot"]["governance_snapshot"]["snapshot_sha256"],
            governance["snapshot_sha256"],
        )

    def test_attestation_projection_and_round_input_tamper_fail_closed(self) -> None:
        for corruption in ("attestation", "projection", "round_input"):
            with self.subTest(corruption=corruption):
                confirmed, context, _attestation_row = self._confirm_governed()

                def current_context(*args, **_kwargs):
                    candidate = copy.deepcopy(context)
                    connection = next(
                        (arg for arg in args if isinstance(arg, sqlite3.Connection)),
                        None,
                    )
                    owns_connection = connection is None
                    if owns_connection:
                        connection = self.store._connect()
                    try:
                        round_row = connection.execute(
                            "SELECT objective FROM rounds WHERE id=? AND room_id=?",
                            (confirmed["round_id"], "room_storage"),
                        ).fetchone()
                    finally:
                        if owns_connection:
                            connection.close()
                    candidate["round_governance_input"]["round_objective"] = str(
                        round_row["objective"] or ""
                    )
                    candidate["round_governance_input_sha256"] = canonical_sha256(
                        candidate["round_governance_input"]
                    )
                    return candidate

                with closing(self.store._connect()) as connection, connection:
                    if corruption == "attestation":
                        connection.execute(
                            """UPDATE artifact_governance_attestations
                               SET attestation_json='{}'
                               WHERE artifact_id=? AND artifact_version=?""",
                            (confirmed["id"], confirmed["version"]),
                        )
                    elif corruption == "projection":
                        connection.execute(
                            """UPDATE artifact_governance_attestations
                               SET projection_json='{}'
                               WHERE artifact_id=? AND artifact_version=?""",
                            (confirmed["id"], confirmed["version"]),
                        )
                    else:
                        connection.execute(
                            "UPDATE rounds SET objective=objective || ' tampered' WHERE id=?",
                            (confirmed["round_id"],),
                        )
                    before_count = connection.execute(
                        """SELECT COUNT(*) FROM artifact_user_decisions
                           WHERE artifact_id=?""",
                        (confirmed["id"],),
                    ).fetchone()[0]

                with self._governance_patches(current_context):
                    with self.assertRaises(ValueError):
                        self.store.create_artifact_user_decision(
                            "room_storage",
                            confirmed["id"],
                            expected_version=confirmed["version"],
                            action="hold",
                            rationale="损坏后必须失败关闭。",
                        )

                with closing(self.store._connect()) as connection:
                    after_count = connection.execute(
                        """SELECT COUNT(*) FROM artifact_user_decisions
                           WHERE artifact_id=?""",
                        (confirmed["id"],),
                    ).fetchone()[0]
                self.assertEqual(after_count, before_count)

    def test_unbound_artifact_and_null_marker_round_remain_compatible(self) -> None:
        room_message = self.store.room_snapshot("room_plan")["messages"][0]
        unbound = self.store.create_artifact(
            "room_plan",
            title="通用未绑定产物",
            content=self._artifact_content(room_message["id"]),
            created_by="offline_governance_test",
        )
        unbound = self.store.confirm_artifact(
            "room_plan",
            unbound["id"],
            expected_version=unbound["version"],
            confirmed_by="user",
        )
        unbound_decision = self.store.create_artifact_user_decision(
            "room_plan",
            unbound["id"],
            expected_version=unbound["version"],
            action="support",
            rationale="通用产物继续沿用证据门，不伪装成 P13 风险复核。",
            selected_option_id="candidate_a",
        )

        legacy_round = self.store.create_round("room_plan", "旧 NULL marker 轮次")
        legacy_message = self.store.add_message(
            "room_plan",
            sender_type="user",
            sender_id="user",
            sender_name="用户",
            content="旧轮次兼容证据。",
            round_id=legacy_round["id"],
        )
        shared_context, manifest = self.store.material_prompt_bundle("room_plan")
        manifest = self.store.finalize_round_evidence_manifest(
            manifest,
            shared_context=shared_context,
            market_snapshot=None,
        )
        self.store.save_round_checkpoint(
            "room_plan",
            legacy_round["id"],
            {
                "member_ids": [],
                "spoken_counts": {},
                "spoken_stances": [],
                "successful_member_ids": [],
                "failed_member_ids": [],
                "previous_name": "用户",
                "completed": 0,
                "failures": 0,
                "skipped": 0,
                "proposals_created": 0,
                "next_order": 1,
                "max_turns": 1,
                "shared_context": shared_context,
                "market_snapshot": None,
                "round_evidence_manifest": manifest,
            },
        )
        self.store.complete_round(legacy_round["id"], "COMPLETED")
        legacy = self.store.create_artifact(
            "room_plan",
            title="旧 NULL marker 产物",
            round_id=legacy_round["id"],
            content=self._artifact_content(legacy_message["id"]),
            created_by="offline_governance_test",
        )
        with patch.object(
            StudioStore,
            "_artifact_round_confirmation_issues",
            return_value=[],
        ):
            legacy = self.store.confirm_artifact(
                "room_plan",
                legacy["id"],
                expected_version=legacy["version"],
                confirmed_by="user",
            )
            legacy_decision = self.store.create_artifact_user_decision(
                "room_plan",
                legacy["id"],
                expected_version=legacy["version"],
                action="support",
                rationale="保留 legacy 语义，不追溯宣称 P13 已完成。",
                selected_option_id="candidate_a",
            )

        with closing(self.store._connect()) as connection:
            attestation_count = connection.execute(
                """SELECT COUNT(*) FROM artifact_governance_attestations
                   WHERE artifact_id IN (?,?)""",
                (unbound["id"], legacy["id"]),
            ).fetchone()[0]
        self.assertEqual(attestation_count, 0)
        self.assertEqual(unbound_decision["governance_attestation_sha256"], "")
        self.assertEqual(legacy_decision["governance_attestation_sha256"], "")
        self.assertFalse(unbound["governance_snapshot"]["applicable"])
        self.assertEqual(
            unbound["governance_snapshot"]["status"],
            "not_round_bound",
        )
        self.assertFalse(legacy["governance_snapshot"]["applicable"])
        self.assertEqual(
            legacy["governance_snapshot"]["status"],
            "legacy_unavailable",
        )
        self.assertIsNone(
            self.store.get_round("room_plan", legacy_round["id"])[
                "candidate_risk_review_version"
            ]
        )

    def test_user_can_select_non_ai_preferred_governed_candidate(self) -> None:
        confirmed, context, attestation_row = self._confirm_governed()
        with self._governance_patches(context):
            decision = self.store.create_artifact_user_decision(
                "room_storage",
                confirmed["id"],
                expected_version=confirmed["version"],
                action="support",
                rationale="The user explicitly selects candidate B after review.",
                **self._governed_support_tokens(
                    context,
                    attestation_row,
                    "candidate_b",
                ),
            )
            current = self.store.get_artifact("room_storage", confirmed["id"])

        self.assertEqual(decision["decision_version"], "artifact_user_decision_v2")
        self.assertEqual(decision["ai_preferred_option_id"], "candidate_a")
        self.assertEqual(decision["selected_option_id"], "candidate_b")
        self.assertEqual(decision["preferred_option_id"], "candidate_b")
        self.assertFalse(decision["selected_is_ai_preferred"])
        self.assertTrue(decision["selected_option_risk_review_required"])
        self.assertTrue(decision["candidate_binding_integrity_ok"])
        self.assertTrue(decision["decision_record_integrity_ok"])
        self.assertTrue(current["user_decision"]["is_current"])
        self.assertEqual(
            current["governance_snapshot"]["user_decision_state"][
                "selected_option_id"
            ],
            "candidate_b",
        )

    def test_v2_candidate_binding_tampering_fails_closed(self) -> None:
        corruptions = {
            "ai_preferred_option_id": "candidate_b",
            "selected_option_id": "candidate_b",
            "selected_option_revision": 99,
            "selected_option_origin_message_id": "tampered_origin",
            "selected_option_latest_message_id": "tampered_latest",
            "selected_option_snapshot_sha256": "0" * 64,
            "selected_option_risk_review_required": 0,
            "decision_record_sha256": "f" * 64,
        }
        for field, value in corruptions.items():
            with self.subTest(field=field):
                confirmed, context, attestation_row = self._confirm_governed()
                with self._governance_patches(context):
                    decision = self.store.create_artifact_user_decision(
                        "room_storage",
                        confirmed["id"],
                        expected_version=confirmed["version"],
                        action="support",
                        rationale="Persist an exact decision before tampering.",
                        **self._governed_support_tokens(context, attestation_row),
                    )
                with closing(self.store._connect()) as connection, connection:
                    connection.execute(
                        f"UPDATE artifact_user_decisions SET {field}=? WHERE id=?",
                        (value, decision["id"]),
                    )
                with self._governance_patches(context):
                    current = self.store.get_artifact(
                        "room_storage",
                        confirmed["id"],
                    )
                self.assertFalse(current["user_decision"]["integrity_ok"])
                self.assertFalse(
                    current["user_decision"]["candidate_binding_integrity_ok"]
                )
                self.assertFalse(current["user_decision"]["is_current"])

    def test_governed_selection_can_record_risk_review_not_applicable(self) -> None:
        artifact, context = self._create_governed_artifact()
        context["projection"]["candidate_risk_reviews"] = {
            "version": CANDIDATE_RISK_REVIEW_VERSION,
            "applicable": False,
            "ready": True,
            "status": "not_applicable",
            "reviews": [],
            "issues": [],
            "review_actions_are_dispositions_only": True,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        }
        with self._governance_patches(context):
            confirmed = self.store.confirm_artifact(
                "room_storage",
                artifact["id"],
                expected_version=artifact["version"],
                confirmed_by="user",
            )
        with closing(self.store._connect()) as connection:
            attestation_row = connection.execute(
                """SELECT * FROM artifact_governance_attestations
                   WHERE artifact_id=? AND artifact_version=?""",
                (confirmed["id"], confirmed["version"]),
            ).fetchone()
        with self._governance_patches(context):
            decision = self.store.create_artifact_user_decision(
                "room_storage",
                confirmed["id"],
                expected_version=confirmed["version"],
                action="support",
                rationale="No risk-review layer applies in this general room.",
                **self._governed_support_tokens(context, attestation_row),
            )

        self.assertFalse(decision["selected_option_risk_review_required"])
        self.assertTrue(decision["candidate_binding_integrity_ok"])

    def test_store_preserves_omitted_fields_and_strict_integer_tokens(self) -> None:
        room_message = self.store.room_snapshot("room_plan")["messages"][0]
        artifact = self.store.create_artifact(
            "room_plan",
            title="Selection presence contract",
            content=self._artifact_content(room_message["id"]),
            created_by="offline_test",
        )
        artifact = self.store.confirm_artifact(
            "room_plan",
            artifact["id"],
            expected_version=artifact["version"],
            confirmed_by="user",
        )
        held = self.store.create_artifact_user_decision(
            "room_plan",
            artifact["id"],
            expected_version=artifact["version"],
            action="hold",
            rationale="Hold without any selection field.",
        )
        self.assertEqual(held["selected_option_id"], "")
        self.assertEqual(held["preferred_option_id"], "")
        returned = self.store.create_artifact_user_decision(
            "room_plan",
            artifact["id"],
            expected_version=artifact["version"],
            action="return",
            rationale="Return without any selection field.",
        )
        self.assertEqual(returned["selected_option_id"], "")
        self.assertEqual(returned["preferred_option_id"], "")
        self.assertEqual(returned["selected_option_snapshot_sha256"], "")
        graph = self.store.artifact_evidence_graph("room_plan", artifact["id"])
        self.assertFalse(any(
            edge["edge_type"] == "selects" for edge in graph["edges"]
        ))
        for field, value in (
            ("selected_option_id", ""),
            ("expected_candidate_revision", 0),
            ("expected_candidate_origin_message_id", None),
            ("expected_candidate_latest_message_id", ""),
            ("expected_governance_attestation_sha256", None),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.store.create_artifact_user_decision(
                        "room_plan",
                        artifact["id"],
                        expected_version=artifact["version"],
                        action="return",
                        rationale="Presence must be rejected.",
                        **{field: value},
                    )
        for invalid_version in (True, 1.5, "1.0"):
            with self.subTest(expected_version=invalid_version):
                with self.assertRaises(ValueError):
                    self.store.create_artifact_user_decision(
                        "room_plan",
                        artifact["id"],
                        expected_version=invalid_version,
                        action="support",
                        rationale="Invalid version token.",
                        selected_option_id="candidate_a",
                    )
        for invalid_revision in (True, 1.5, "1.0"):
            with self.subTest(expected_candidate_revision=invalid_revision):
                with self.assertRaises(ValueError):
                    self.store.create_artifact_user_decision(
                        "room_plan",
                        artifact["id"],
                        expected_version=artifact["version"],
                        action="support",
                        rationale="Invalid candidate revision token.",
                        selected_option_id="candidate_a",
                        expected_candidate_revision=invalid_revision,
                    )


if __name__ == "__main__":
    unittest.main()
