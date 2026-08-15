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
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from backend import http_server
from backend.artifact_service import ArtifactService
from backend.convergence import ConvergenceService
from backend.provider_call_ledger import ProviderCallLedger
from backend.providers.base import ProviderResponse
from backend.store import (
    ARTIFACT_EVIDENCE_SOURCE_DETAIL_MAX_BYTES,
    ARTIFACT_EVIDENCE_SOURCE_DETAIL_VERSION,
    ARTIFACT_EVIDENCE_SOURCE_PREVIEW_MAX_CHARS,
    ARTIFACT_EVIDENCE_SOURCE_TOTAL_PREVIEW_MAX_CHARS,
    ARTIFACT_EVIDENCE_SOURCES_VERSION,
    StudioStore,
)


def audited_evidence(
    source_type: str,
    source_id: str,
    *,
    role: str = "support",
    status: str = "source_checked",
    note: str = "",
) -> dict[str, str]:
    return {
        "type": source_type,
        "id": source_id,
        "evidence_role": role,
        "verification_status": status,
        "review_note": note,
    }


class MinutesProvider:
    def __init__(
        self,
        message_id: str,
        material_id: str,
        *,
        configured: bool = True,
        extra_message_id: str = "",
        provider_id: str = "openai",
    ) -> None:
        self.message_id = message_id
        self.material_id = material_id
        self.configured = configured
        self.extra_message_id = extra_message_id
        self.provider_id = provider_id
        self.calls = 0
        self.last_input = ""
        self.last_instructions = ""
        self.last_model = ""

    def status(self):
        return {"configured": self.configured}

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        self.calls += 1
        self.last_input = input_text
        self.last_instructions = instructions
        self.last_model = model
        content = {
            "summary": "讨论同意先验证低成本方案。",
            "summary_evidence": [
                {"type": "message", "id": self.message_id},
                *(
                    [{"type": "message", "id": self.extra_message_id}]
                    if self.extra_message_id
                    else []
                ),
            ],
            "conclusions": [{
                "text": "先做低成本原型。",
                "evidence": [
                    {"type": "material", "id": self.material_id},
                    {"type": "message", "id": "msg_fabricated"},
                ],
            }],
            "disagreements": [{
                "text": "是否立即扩大范围",
                "positions": ["先验证", "先扩展"],
                "evidence": [{"type": "message", "id": self.message_id}],
            }],
            "unknowns": [],
            "actions": [{
                "text": "准备原型验证",
                "owner": "方案架构师",
                "due": "",
                "state": "open",
                "evidence": [{"type": "material", "id": self.material_id}],
            }],
            "decision": {
                "status": "candidate",
                "options": [
                    {
                        "id": "option_low_cost",
                        "title": "低成本原型",
                        "description": "先验证关键假设。",
                        "benefits": ["更快获得反馈"],
                        "risks": ["覆盖面有限"],
                        "evidence": [{"type": "material", "id": self.material_id}],
                    },
                    {
                        "id": "option_full_scope",
                        "title": "完整范围",
                        "description": "一次性覆盖全部需求。",
                        "benefits": ["范围完整"],
                        "risks": ["成本较高"],
                        "evidence": [{"type": "message", "id": self.message_id}],
                    },
                ],
                "preferred_option_id": "option_low_cost",
                "rationale": "现有证据更支持先验证。",
                "evidence": [{"type": "message", "id": self.message_id}],
            },
        }
        return ProviderResponse(ok=True, provider=self.provider_id, model=model or "fake", content=json.dumps(content, ensure_ascii=False))


class UnsafeFailureMinutesProvider(MinutesProvider):
    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        self.calls += 1
        self.last_input = input_text
        return ProviderResponse(
            ok=False,
            provider="untrusted-upstream-provider",
            model="untrusted-upstream-model",
            error="raw sensitive upstream diagnostics",
            error_code="http_status",
        )


class UsageMinutesProvider(MinutesProvider):
    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        response = super().generate(
            instructions=instructions,
            input_text=input_text,
            model=model,
        )
        response.usage = {
            "input_tokens": 31,
            "output_tokens": 9,
            "cached": {"read_tokens": 4},
            "prompt": "artifact-prompt-secret-must-not-persist",
            "not_finite": float("nan"),
        }
        return response


class ExceptionMinutesProvider(MinutesProvider):
    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        self.calls += 1
        self.last_input = input_text
        raise RuntimeError("artifact-provider-exception-secret")


class FinalizeFailLedger:
    def __init__(self, delegate: ProviderCallLedger) -> None:
        self.delegate = delegate
        self.finish_calls = 0

    def reserve(self, **fields: object) -> dict[str, object]:
        return self.delegate.reserve(**fields)

    def finish(self, *_args: object, **_fields: object) -> dict[str, object]:
        self.finish_calls += 1
        raise RuntimeError("ledger-finalize-secret")


class StructuredMinutesProvider(MinutesProvider):
    def __init__(self, message_id: str, material_id: str) -> None:
        super().__init__(message_id, material_id)
        self.json_calls = 0

    def generate_json(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        self.json_calls += 1
        return super().generate(
            instructions=instructions,
            input_text=input_text,
            model=model,
        )


class MarketSnapshotMinutesProvider(MinutesProvider):
    def __init__(self, message_id: str, material_id: str, snapshot_id: str) -> None:
        super().__init__(message_id, material_id)
        self.snapshot_id = snapshot_id

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        response = super().generate(instructions=instructions, input_text=input_text, model=model)
        content = json.loads(response.content)
        content["summary_evidence"].append({
            "type": "round_market_snapshot",
            "id": self.snapshot_id,
            "evidence_role": "support",
            "verification_status": "corroborated",
            "source_snapshot_sha256": "0" * 64,
        })
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model or "fake",
            content=json.dumps(content, ensure_ascii=False),
        )


class MinutesRegistry:
    def __init__(self, provider: MinutesProvider) -> None:
        self.provider = provider

    def get(self, provider_id: str) -> MinutesProvider:
        self.provider.provider_id = provider_id
        return self.provider


class MappingMinutesRegistry:
    def __init__(self, providers: dict[str, MinutesProvider]) -> None:
        self.providers = providers
        self.requested_provider_ids: list[str] = []

    def get(self, provider_id: str) -> MinutesProvider | None:
        self.requested_provider_ids.append(provider_id)
        return self.providers.get(provider_id)


class CapturingArtifactService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.last_skip_provider_ids: set[str] = set()
        self.last_ledger: ProviderCallLedger | None = None
        self.last_frozen_synthesizer_route: dict = {}

    def generate_minutes(
        self,
        room_id: str,
        round_id: str = "",
        synthesizer_member_id: str = "",
        *,
        skip_provider_ids: set[str] | None = None,
        ledger: ProviderCallLedger | None = None,
        frozen_synthesizer_route: dict | None = None,
    ) -> dict:
        self.calls.append((room_id, round_id, synthesizer_member_id))
        self.last_skip_provider_ids = set(skip_provider_ids or set())
        self.last_ledger = ledger
        self.last_frozen_synthesizer_route = dict(frozen_synthesizer_route or {})
        return {"id": "artifact_http_fixture", "room_id": room_id}


class ArtifactWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.db_path)
        self.message = self.store.room_snapshot("room_plan")["messages"][0]
        self.ledger_sequence = 0
        self.material = self.store.add_material("room_plan", {
            "title": "验证约束",
            "kind": "note",
            "content": "首期必须使用低成本原型验证。",
        })

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_ledger(self, *, max_calls: int = 4) -> ProviderCallLedger:
        self.ledger_sequence += 1
        request_id = f"artifact-ledger-{self.ledger_sequence}"
        return ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="artifact_generation",
            client_request_id=request_id,
            plan={"test_request_id": request_id},
            max_calls=max_calls,
        )

    def test_corrupted_round_objective_is_not_reused_as_artifact_title(self) -> None:
        self.assertEqual(
            ArtifactService._clean_title_suffix("???????????????? MU SNDK WDC STX"),
            "",
        )
        self.assertEqual(
            ArtifactService._clean_title_suffix("比较 MU、SNDK、WDC、STX 的风险条件"),
            "比较 MU、SNDK、WDC、STX 的风险条件",
        )

    def create_frozen_round(self) -> tuple[dict, dict]:
        round_row = self.store.create_round("room_plan", "只使用本轮冻结证据整理会议纪要")
        round_message = self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id="member_round",
            sender_name="本轮研究员",
            content="本轮只讨论冻结的第一版资料。",
            round_id=round_row["id"],
        )
        material_context, manifest = self.store.material_prompt_bundle("room_plan")
        manifest = self.store.finalize_round_evidence_manifest(
            manifest,
            shared_context=material_context,
            market_snapshot=None,
        )
        self.store.save_round_checkpoint("room_plan", round_row["id"], {
            "member_ids": [],
            "spoken_counts": {},
            "spoken_stances": [],
            "successful_member_ids": [],
            "failed_member_ids": [],
            "previous_name": "本轮研究员",
            "completed": 1,
            "failures": 0,
            "skipped": 0,
            "proposals_created": 0,
            "next_order": 2,
            "max_turns": 1,
            "shared_context": material_context,
            "market_snapshot": None,
            "frozen_market": None,
            "round_evidence_manifest": manifest,
        })
        self.store.complete_round(round_row["id"], "COMPLETED")
        return round_row, round_message

    def create_frozen_route_round(self, member_id: str) -> dict:
        round_row = self.store.create_round("room_plan", "验证冻结会议整理路由")
        material_context, manifest = self.store.material_prompt_bundle("room_plan")
        manifest = self.store.finalize_round_evidence_manifest(
            manifest,
            shared_context=material_context,
            market_snapshot=None,
        )
        self.store.save_round_checkpoint("room_plan", round_row["id"], {
            "member_ids": [member_id],
            "spoken_counts": {},
            "spoken_stances": [],
            "successful_member_ids": [],
            "failed_member_ids": [],
            "previous_name": "系统",
            "completed": 0,
            "failures": 0,
            "skipped": 0,
            "proposals_created": 0,
            "next_order": 1,
            "max_turns": 1,
            "shared_context": material_context,
            "market_snapshot": None,
            "frozen_market": None,
            "round_evidence_manifest": manifest,
        })
        self.store.complete_round(round_row["id"], "COMPLETED")
        return round_row

    def create_frozen_market_round(
        self,
        snapshot_id: str,
    ) -> tuple[dict, dict]:
        round_row = self.store.create_round("room_plan", f"冻结市场快照 {snapshot_id}")
        room_snapshot = self.store.room_snapshot("room_plan")
        members = room_snapshot["members"]
        for member in members:
            self.store.add_message(
                "room_plan",
                sender_type="ai",
                sender_id=member["id"],
                sender_name=member["name"],
                identity=member["identity"],
                provider=member["provider"],
                model=member["model"],
                content=f"{member['name']} 已按冻结身份完成市场快照复核。",
                round_id=round_row["id"],
                member_version=member["version"],
            )
        market_snapshot = {
            "snapshot_id": snapshot_id,
            "captured_at": "2026-07-31T20:00:00Z",
            "state": "ready",
            "source": "futu_opend_readonly",
            "rows": [{
                "symbol": "US.MU",
                "last": 120.5,
                "market_time": "2026-07-31 16:00:00",
                "quality": "ready",
            }],
            "evidence": {
                "version": "storage_market_evidence_v6",
                "state": "ready",
                "fundamental": {"source": "futu_market_snapshot", "rows": []},
            },
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        material_context, manifest = self.store.material_prompt_bundle("room_plan")
        manifest = self.store.finalize_round_evidence_manifest(
            manifest,
            shared_context=material_context,
            market_snapshot=market_snapshot,
        )
        self.store.save_round_checkpoint("room_plan", round_row["id"], {
            "member_ids": [member["id"] for member in members],
            "spoken_counts": {member["id"]: 1 for member in members},
            "spoken_stances": [member["stance"] for member in members],
            "successful_member_ids": [member["id"] for member in members],
            "failed_member_ids": [],
            "previous_name": "市场数据官",
            "completed": len(members),
            "failures": 0,
            "skipped": 0,
            "proposals_created": 0,
            "next_order": len(members) + 1,
            "max_turns": len(members),
            "workflow_policy": room_snapshot["room"]["workflow_policy"],
            "capability_pack_ids": room_snapshot["room"].get("capability_pack_ids") or [],
            "room_capabilities": room_snapshot["room"].get("capabilities") or [],
            "shared_context": material_context,
            "market_snapshot": market_snapshot,
            "frozen_market": {
                "present": True,
                "ready": True,
                "state": "ready",
                "snapshot_id": snapshot_id,
                "captured_at": market_snapshot["captured_at"],
            },
            "round_evidence_manifest": manifest,
        })
        self.store.complete_round(round_row["id"], "COMPLETED")
        return round_row, market_snapshot

    def create_confirmed_candidate_artifact(self, *, round_id: str = "") -> dict:
        evidence = [audited_evidence("message", self.message["id"])]
        artifact = self.store.create_artifact(
            "room_plan",
            round_id=round_id,
            title="可交用户决定的候选方案",
            content={
                "summary": "比较两个方案后，建议先做可逆验证。",
                "summary_evidence": evidence,
                "requirements": [],
                "risks": [],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
                "decision": {
                    "status": "candidate",
                    "options": [
                        {
                            "id": "option_small",
                            "title": "小范围验证",
                            "description": "先验证核心假设。",
                            "benefits": ["可逆"],
                            "risks": ["覆盖有限"],
                            "evidence": evidence,
                        },
                        {
                            "id": "option_full",
                            "title": "完整范围",
                            "description": "一次完成全部范围。",
                            "benefits": ["覆盖完整"],
                            "risks": ["成本较高"],
                            "evidence": evidence,
                        },
                    ],
                    "preferred_option_id": "option_small",
                    "rationale": "当前证据更支持可逆验证。",
                    "evidence": evidence,
                },
            },
        )
        return self.store.confirm_artifact(
            "room_plan",
            artifact["id"],
            expected_version=artifact["version"],
            confirmed_by="user",
        )

    def test_artifact_draft_confirm_and_revision_lifecycle(self) -> None:
        artifact = self.store.create_artifact(
            "room_plan",
            title="阶段纪要",
            content={
                "summary": "先做验证。",
                "summary_evidence": [audited_evidence("message", self.message["id"])],
                "conclusions": [{
                    "text": "使用低成本原型。",
                    "evidence": [audited_evidence("material", self.material["id"])],
                }],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        )
        confirmed = self.store.confirm_artifact(
            "room_plan", artifact["id"], expected_version=artifact["version"], confirmed_by="user",
        )
        revised = self.store.update_artifact("room_plan", artifact["id"], {
            "expected_version": confirmed["version"],
            "title": "阶段纪要修订",
            "content": {**confirmed["content"], "summary": "修改后需要重新确认。"},
        })

        self.assertEqual(artifact["status"], "DRAFT")
        self.assertEqual(confirmed["status"], "CONFIRMED")
        self.assertEqual(confirmed["version"], 2)
        self.assertEqual(revised["status"], "DRAFT")
        self.assertEqual(revised["version"], 3)
        self.assertEqual(revised["confirmed_at"], 0)
        with closing(sqlite3.connect(self.db_path)) as connection:
            version_count = connection.execute(
                "SELECT COUNT(*) FROM artifact_versions WHERE artifact_id=?", (artifact["id"],),
            ).fetchone()[0]
            evidence_count = connection.execute(
                "SELECT COUNT(*) FROM artifact_evidence WHERE artifact_id=?", (artifact["id"],),
            ).fetchone()[0]
            evidence_audit = connection.execute(
                "SELECT DISTINCT evidence_role,verification_status FROM artifact_evidence WHERE artifact_id=?",
                (artifact["id"],),
            ).fetchall()
        self.assertEqual(version_count, 3)
        self.assertEqual(evidence_count, 2)
        self.assertEqual(
            evidence_audit,
            [("support", "source_checked"), ("support", "unreviewed")],
        )

    def test_artifact_exposes_evidence_review_progress_and_confirmation_issues(self) -> None:
        artifact = self.store.create_artifact(
            "room_plan",
            title="证据复核进度",
            content={
                "summary": "这是一条仍待用户核验的讨论摘要。",
                "summary_evidence": [{"type": "message", "id": self.message["id"]}],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        )
        review = artifact["evidence_review"]
        self.assertEqual(review["relation_count"], 1)
        self.assertEqual(review["unique_source_count"], 1)
        self.assertEqual(review["unreviewed_relation_count"], 1)
        self.assertFalse(review["confirmation_ready"])
        self.assertGreaterEqual(review["confirmation_issue_count"], 2)

        updated = self.store.update_artifact("room_plan", artifact["id"], {
            "expected_version": artifact["version"],
            "title": artifact["title"],
            "content": {
                **artifact["content"],
                "summary_evidence": [audited_evidence("message", self.message["id"])],
            },
        })
        self.assertEqual(updated["evidence_review"]["reviewed_relation_count"], 1)
        self.assertEqual(updated["evidence_review"]["unreviewed_relation_count"], 0)
        self.assertTrue(updated["evidence_review"]["confirmation_ready"])

    def test_legacy_confirmed_artifact_with_unreviewed_evidence_cannot_receive_user_decision(self) -> None:
        artifact = self.store.create_artifact(
            "room_plan",
            title="旧版已确认但未复核的候选",
            content={
                "summary": "旧版流程曾把这份草稿标记为已确认。",
                "summary_evidence": [{"type": "message", "id": self.message["id"]}],
                "requirements": [],
                "risks": [],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
                "decision": {"status": "undecided", "options": []},
            },
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                """UPDATE artifacts
                   SET status='CONFIRMED',confirmed_by='legacy_user',confirmed_at=updated_at
                   WHERE id=? AND room_id='room_plan'""",
                (artifact["id"],),
            )
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id=?",
                (artifact["id"],),
            ).fetchone()
            snapshot = dict(row)
            snapshot["content"] = json.loads(snapshot.pop("content_json"))
            connection.execute(
                """UPDATE artifact_versions SET snapshot_json=?
                   WHERE artifact_id=? AND room_id='room_plan' AND version=?""",
                (
                    json.dumps(snapshot, ensure_ascii=False),
                    artifact["id"],
                    artifact["version"],
                ),
            )
            before_artifact = tuple(row)
            before_version = connection.execute(
                """SELECT snapshot_json FROM artifact_versions
                   WHERE artifact_id=? AND version=?""",
                (artifact["id"], artifact["version"]),
            ).fetchone()[0]
            before_evidence = connection.execute(
                """SELECT evidence_role,verification_status,review_note
                   FROM artifact_evidence WHERE artifact_id=? ORDER BY rowid""",
                (artifact["id"],),
            ).fetchall()

        legacy = self.store.get_artifact("room_plan", artifact["id"])
        self.assertEqual(legacy["status"], "CONFIRMED")
        self.assertFalse(legacy["evidence_review"]["confirmation_ready"])
        self.assertGreater(legacy["evidence_review"]["unreviewed_relation_count"], 0)
        with self.assertRaisesRegex(
            ValueError,
            "现行证据与确认有效性校验.*尚未核验",
        ):
            self.store.create_artifact_user_decision(
                "room_plan",
                artifact["id"],
                expected_version=artifact["version"],
                action="hold",
                rationale="先保留，等待重新完成证据复核。",
            )

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            after_artifact = tuple(connection.execute(
                "SELECT * FROM artifacts WHERE id=?",
                (artifact["id"],),
            ).fetchone())
            after_version = connection.execute(
                """SELECT snapshot_json FROM artifact_versions
                   WHERE artifact_id=? AND version=?""",
                (artifact["id"], artifact["version"]),
            ).fetchone()[0]
            after_evidence = connection.execute(
                """SELECT evidence_role,verification_status,review_note
                   FROM artifact_evidence WHERE artifact_id=? ORDER BY rowid""",
                (artifact["id"],),
            ).fetchall()
            decision_count = connection.execute(
                "SELECT COUNT(*) FROM artifact_user_decisions WHERE artifact_id=?",
                (artifact["id"],),
            ).fetchone()[0]
        self.assertEqual(after_artifact, before_artifact)
        self.assertEqual(after_version, before_version)
        self.assertEqual(after_evidence, before_evidence)
        self.assertEqual(decision_count, 0)

    def test_user_decision_is_immutable_version_bound_and_becomes_stale_after_revision(self) -> None:
        confirmed = self.create_confirmed_candidate_artifact()

        supported = self.store.create_artifact_user_decision(
            "room_plan",
            confirmed["id"],
            expected_version=confirmed["version"],
            action="support",
            rationale="支持先做可逆验证，并保留停止条件。",
            selected_option_id="option_small",
        )
        duplicate = self.store.create_artifact_user_decision(
            "room_plan",
            confirmed["id"],
            expected_version=confirmed["version"],
            action="support",
            rationale="支持先做可逆验证，并保留停止条件。",
            selected_option_id="option_small",
        )
        held = self.store.create_artifact_user_decision(
            "room_plan",
            confirmed["id"],
            expected_version=confirmed["version"],
            action="hold",
            rationale="等待补齐资源上限后再判断。",
        )
        current = self.store.get_artifact("room_plan", confirmed["id"])

        self.assertEqual(supported["id"], duplicate["id"])
        self.assertEqual(supported["preferred_option_id"], "option_small")
        self.assertEqual(len(supported["artifact_snapshot_sha256"]), 64)
        self.assertEqual(supported["execution_capability"], "none")
        self.assertFalse(supported["live_trading_allowed"])
        self.assertFalse(supported["can_autonomously_decide"])
        self.assertEqual(current["user_decision"]["id"], held["id"])
        self.assertTrue(current["user_decision"]["is_current"])
        self.assertEqual(len(current["user_decision_history"]), 2)
        self.assertFalse(current["user_decision_history"][1]["is_current"])
        self.assertIn("更新", current["user_decision_history"][1]["stale_reason"])

        revised = self.store.update_artifact("room_plan", confirmed["id"], {
            "expected_version": confirmed["version"],
            "title": confirmed["title"],
            "content": {
                **confirmed["content"],
                "summary": "修订后必须重新确认再决定。",
            },
        })
        self.assertEqual(revised["status"], "DRAFT")
        self.assertFalse(revised["user_decision"]["is_current"])
        self.assertIn("修订", revised["user_decision"]["stale_reason"])
        with self.assertRaisesRegex(ValueError, "完成证据确认"):
            self.store.create_artifact_user_decision(
                "room_plan",
                revised["id"],
                expected_version=revised["version"],
                action="return",
                rationale="仍需补证。",
            )

        reviewed_revision = self.store.update_artifact("room_plan", revised["id"], {
            "expected_version": revised["version"],
            "title": revised["title"],
            "content": {
                **revised["content"],
                "summary_evidence": [{
                    **reference,
                    "verification_status": "source_checked",
                    "review_note": "rechecked after the summary changed",
                } for reference in revised["content"]["summary_evidence"]],
            },
        })
        reconfirmed = self.store.confirm_artifact(
            "room_plan",
            reviewed_revision["id"],
            expected_version=reviewed_revision["version"],
            confirmed_by="user",
        )
        with self.assertRaisesRegex(ValueError, "版本已变化"):
            self.store.create_artifact_user_decision(
                "room_plan",
                reconfirmed["id"],
                expected_version=confirmed["version"],
                action="hold",
                rationale="使用旧版本决定。",
            )
        returned = self.store.create_artifact_user_decision(
            "room_plan",
            reconfirmed["id"],
            expected_version=reconfirmed["version"],
            action="return",
            rationale="退回补充资源约束与验收条件。",
        )
        latest = self.store.get_artifact("room_plan", reconfirmed["id"])
        self.assertEqual(latest["user_decision"]["id"], returned["id"])
        self.assertTrue(latest["user_decision"]["is_current"])
        self.assertEqual(len(latest["user_decision_history"]), 3)

    def test_user_decision_rejects_invalid_action_reason_and_support_without_candidate(self) -> None:
        confirmed = self.create_confirmed_candidate_artifact()
        with self.assertRaisesRegex(ValueError, "只能"):
            self.store.create_artifact_user_decision(
                "room_plan",
                confirmed["id"],
                expected_version=confirmed["version"],
                action="execute",
                rationale="不允许的动作。",
            )
        with self.assertRaisesRegex(ValueError, "理由"):
            self.store.create_artifact_user_decision(
                "room_plan",
                confirmed["id"],
                expected_version=confirmed["version"],
                action="hold",
                rationale="",
            )

        evidence = [audited_evidence("message", self.message["id"])]
        no_candidate = self.store.create_artifact(
            "room_plan",
            title="没有首选候选",
            content={
                "summary": "只确认记录，不支持具体候选。",
                "summary_evidence": evidence,
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        )
        no_candidate = self.store.confirm_artifact(
            "room_plan",
            no_candidate["id"],
            expected_version=no_candidate["version"],
        )
        with self.assertRaisesRegex(ValueError, "selected_option_id"):
            self.store.create_artifact_user_decision(
                "room_plan",
                no_candidate["id"],
                expected_version=no_candidate["version"],
                action="support",
                rationale="不能支持不存在的候选。",
            )
        held = self.store.create_artifact_user_decision(
            "room_plan",
            no_candidate["id"],
            expected_version=no_candidate["version"],
            action="hold",
            rationale="保留记录，等待形成候选。",
        )
        self.assertEqual(held["action"], "hold")

    def test_stale_artifact_update_is_rejected(self) -> None:
        artifact = self.store.create_artifact(
            "room_plan",
            title="纪要",
            content={"summary": "草稿", "conclusions": [], "disagreements": [], "unknowns": [], "actions": []},
        )
        self.store.update_artifact("room_plan", artifact["id"], {
            "expected_version": 1,
            "content": {**artifact["content"], "summary": "新版本"},
        })
        with self.assertRaisesRegex(ValueError, "刷新"):
            self.store.update_artifact("room_plan", artifact["id"], {
                "expected_version": 1,
                "content": artifact["content"],
            })

    def test_project_artifact_normalizes_and_confirms_structured_workspace(self) -> None:
        message = self.store.add_message(
            "room_project",
            sender_type="user",
            sender_id="user",
            sender_name="我",
            content="首期需要在两周内验证核心用户是否愿意持续使用。",
        )
        material = self.store.add_material("room_project", {
            "title": "项目资源约束",
            "kind": "note",
            "content": "首期仅允许两名开发参与，预算必须可撤回。",
        })
        message_evidence = [audited_evidence("message", message["id"])]
        material_evidence = [audited_evidence("material", material["id"])]
        artifact = self.store.create_artifact(
            "room_project",
            title="结构化项目研究纪要",
            content={
                "summary": "先验证需求，再决定是否扩大投入。",
                "summary_evidence": message_evidence,
                "requirements": [{
                    "id": "requirement_retention",
                    "text": "核心用户愿意持续使用首期产品。",
                    "status": "confirmed",
                    "owner": "产品负责人",
                    "acceptance_criteria": "连续两周至少 5 名目标用户完成三次核心任务。",
                    "evidence": message_evidence,
                }],
                "risks": [{
                    "id": "risk_capacity",
                    "text": "两名开发无法同时覆盖完整范围。",
                    "probability": "high",
                    "impact": "high",
                    "blocking": False,
                    "trigger": "首周排期超过十个开发日。",
                    "mitigation": "保留低成本原型并推迟非核心模块。",
                    "owner": "项目经理",
                    "status": "mitigated",
                    "evidence": material_evidence,
                }],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
                "decision": {
                    "status": "candidate",
                    "options": [
                        {
                            "id": "option_prototype",
                            "title": "低成本原型",
                            "description": "先验证一条核心任务链。",
                            "benefits": ["反馈更快"],
                            "risks": ["覆盖有限"],
                            "value": "验证核心需求",
                            "cost": "两名开发，两周",
                            "timeline": "两周",
                            "dependencies": ["目标用户招募"],
                            "reversibility": "high",
                            "evidence": message_evidence,
                        },
                        {
                            "id": "option_full",
                            "title": "完整范围",
                            "description": "一次性实现全部规划模块。",
                            "benefits": ["覆盖完整"],
                            "risks": ["资源超载"],
                            "value": "覆盖全部场景",
                            "cost": "资源暂不明确",
                            "timeline": "至少六周",
                            "dependencies": ["新增开发资源"],
                            "reversibility": "low",
                            "evidence": material_evidence,
                        },
                    ],
                    "preferred_option_id": "option_prototype",
                    "rationale": "当前资源与可逆性更支持先验证。",
                    "evidence": message_evidence,
                },
            },
        )
        confirmed = self.store.confirm_artifact(
            "room_project",
            artifact["id"],
            expected_version=artifact["version"],
            confirmed_by="user",
        )

        self.assertEqual(confirmed["status"], "CONFIRMED")
        self.assertEqual(confirmed["content"]["requirements"][0]["status"], "confirmed")
        self.assertIn("三次核心任务", confirmed["content"]["requirements"][0]["acceptance_criteria"])
        self.assertEqual(confirmed["content"]["risks"][0]["probability"], "high")
        self.assertFalse(confirmed["content"]["risks"][0]["blocking"])
        preferred = confirmed["content"]["decision"]["options"][0]
        self.assertEqual(preferred["value"], "验证核心需求")
        self.assertEqual(preferred["dependencies"], ["目标用户招募"])
        self.assertEqual(preferred["reversibility"], "high")
        with closing(sqlite3.connect(self.db_path)) as connection:
            item_keys = {
                row[0]
                for row in connection.execute(
                    "SELECT item_key FROM artifact_evidence WHERE artifact_id=?",
                    (artifact["id"],),
                ).fetchall()
            }
        self.assertIn("requirements:requirement_retention", item_keys)
        self.assertIn("risks:risk_capacity", item_keys)

    def test_project_artifact_confirmation_requires_acceptance_and_risk_treatment_fields(self) -> None:
        evidence = [audited_evidence("message", self.message["id"])]
        missing_acceptance = self.store.create_artifact(
            "room_plan",
            title="缺少验收条件",
            content={
                "summary": "记录需求。",
                "summary_evidence": evidence,
                "requirements": [{
                    "text": "这是一项已确认需求。",
                    "status": "confirmed",
                    "acceptance_criteria": "",
                    "evidence": evidence,
                }],
                "risks": [],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        )
        with self.assertRaisesRegex(ValueError, "验收条件"):
            self.store.confirm_artifact(
                "room_plan",
                missing_acceptance["id"],
                expected_version=missing_acceptance["version"],
                confirmed_by="user",
            )

        missing_treatment = self.store.create_artifact(
            "room_plan",
            title="缺少风险处置",
            content={
                "summary": "记录风险。",
                "summary_evidence": evidence,
                "requirements": [],
                "risks": [{
                    "text": "资源中断风险。",
                    "status": "accepted",
                    "trigger": "",
                    "mitigation": "",
                    "evidence": evidence,
                }],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        )
        with self.assertRaisesRegex(ValueError, "触发信号"):
            self.store.confirm_artifact(
                "room_plan",
                missing_treatment["id"],
                expected_version=missing_treatment["version"],
                confirmed_by="user",
            )

    def test_project_minutes_instruction_is_capability_driven(self) -> None:
        project_room = self.store.room_snapshot("room_project")["room"]
        generic_room = self.store.room_snapshot("room_plan")["room"]

        project_prompt = ArtifactService._instructions(project_room)
        generic_prompt = ArtifactService._instructions(generic_room)

        self.assertIn("需求证据地图", project_prompt)
        self.assertIn("价值、成本、周期、依赖和可逆性", project_prompt)
        self.assertIn("requirements和risks输出空数组", generic_prompt)

    def test_round_minutes_use_frozen_project_capability_after_room_pack_changes(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_updated_at": room["updated_at"],
            "capability_pack_ids": ["structured_project_research"],
        })
        round_row, round_message = self.create_frozen_round()
        changed_room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_updated_at": changed_room["updated_at"],
            "capability_pack_ids": [],
        })
        provider = MinutesProvider(round_message["id"], self.material["id"])

        ArtifactService(self.store, MinutesRegistry(provider)).generate_minutes(
            "room_plan",
            round_row["id"],
        )

        self.assertIn("需求证据地图", provider.last_instructions)
        self.assertNotIn("requirements和risks输出空数组", provider.last_instructions)

    def test_round_minutes_generation_is_idempotent_before_second_provider_call(self) -> None:
        round_row, round_message = self.create_frozen_round()
        provider = MinutesProvider(round_message["id"], self.material["id"])
        service = ArtifactService(self.store, MinutesRegistry(provider))
        ledger = self.create_ledger(max_calls=1)

        first = service.generate_minutes("room_plan", round_row["id"], ledger=ledger)
        second = service.generate_minutes("room_plan", round_row["id"], ledger=ledger)

        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(provider.calls, 1)
        self.assertEqual(ledger.snapshot()["reserved_calls"], 1)
        self.assertEqual(len(ledger.attempts()), 1)
        self.assertEqual(ledger.attempts()[0]["status"], "RESPONDED")
        round_artifacts = [
            artifact
            for artifact in self.store.list_artifacts("room_plan")
            if artifact["round_id"] == round_row["id"]
        ]
        self.assertEqual(len(round_artifacts), 1)
        self.assertEqual(
            round_artifacts[0]["generation_key"],
            self.store.artifact_generation_key("room_plan", round_row["id"]),
        )

    def test_model_minutes_keep_only_real_room_evidence(self) -> None:
        provider = MinutesProvider(self.message["id"], self.material["id"])
        service = ArtifactService(self.store, MinutesRegistry(provider))

        artifact = service.generate_minutes("room_plan")

        conclusion_evidence = artifact["content"]["conclusions"][0]["evidence"]
        self.assertEqual(provider.calls, 1)
        self.assertEqual(artifact["status"], "DRAFT")
        self.assertEqual([item["id"] for item in conclusion_evidence], [self.material["id"]])
        self.assertEqual(conclusion_evidence[0]["verification_status"], "unreviewed")
        self.assertEqual(conclusion_evidence[0]["evidence_role"], "context")
        facilitator = next(
            member
            for member in self.store.room_snapshot("room_plan")["members"]
            if member["stance"] == "facilitator"
        )
        self.assertEqual(
            artifact["generation_source"],
            f"{facilitator['provider']}:{facilitator['model'] or 'default'}",
        )

    def test_round_market_snapshot_is_round_bound_hash_pinned_and_unreviewed(self) -> None:
        round_row, market_snapshot = self.create_frozen_market_round("futu_round_a")
        artifact = self.store.create_artifact(
            "room_plan",
            round_id=round_row["id"],
            title="冻结市场快照证据",
            content={
                "summary": "会议引用了本轮冻结的富途只读快照。",
                "summary_evidence": [{
                    "type": "round_market_snapshot",
                    "id": market_snapshot["snapshot_id"],
                    "source_revision": "forged_revision",
                    "source_snapshot_sha256": "0" * 64,
                }],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        )

        reference = artifact["content"]["summary_evidence"][0]
        expected_sha = self.store._canonical_sha256(market_snapshot)
        self.assertEqual(reference["type"], "round_market_snapshot")
        self.assertEqual(reference["id"], market_snapshot["snapshot_id"])
        self.assertEqual(reference["round_id"], round_row["id"])
        self.assertEqual(reference["source_revision"], "storage_market_evidence_v6")
        self.assertEqual(reference["source_snapshot_sha256"], expected_sha)
        self.assertEqual(reference["evidence_role"], "context")
        self.assertEqual(reference["verification_status"], "unreviewed")
        self.assertEqual(reference["execution_capability"], "none")
        self.assertFalse(reference["live_trading_allowed"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            stored = connection.execute(
                """SELECT source_type,source_id,source_version,source_revision,
                          source_snapshot_sha256,verification_status
                     FROM artifact_evidence WHERE artifact_id=? AND item_key='summary'""",
                (artifact["id"],),
            ).fetchone()
        self.assertEqual(stored, (
            "round_market_snapshot",
            market_snapshot["snapshot_id"],
            0,
            "storage_market_evidence_v6",
            expected_sha,
            "unreviewed",
        ))
        with self.assertRaisesRegex(ValueError, "尚未核验"):
            self.store.confirm_artifact(
                "room_plan",
                artifact["id"],
                expected_version=artifact["version"],
            )

        revised = self.store.update_artifact("room_plan", artifact["id"], {
            "expected_version": artifact["version"],
            "content": {
                **artifact["content"],
                "summary_evidence": [{
                    **reference,
                    "evidence_role": "support",
                    "verification_status": "source_checked",
                }],
            },
        })
        confirmed = self.store.confirm_artifact(
            "room_plan",
            revised["id"],
            expected_version=revised["version"],
        )
        self.assertEqual(confirmed["status"], "CONFIRMED")

    def test_round_market_snapshot_valid_rewrite_invalidates_saved_pin_and_review(self) -> None:
        round_row, market_snapshot = self.create_frozen_market_round("futu_pin_drift")
        artifact = self.store.create_artifact(
            "room_plan",
            round_id=round_row["id"],
            title="冻结市场快照 pin 漂移",
            content={
                "summary": "该摘要引用了本轮冻结市场快照。",
                "summary_evidence": [{
                    "type": "round_market_snapshot",
                    "id": market_snapshot["snapshot_id"],
                }],
            },
        )
        pinned = artifact["content"]["summary_evidence"][0]
        reviewed = self.store.update_artifact("room_plan", artifact["id"], {
            "expected_version": artifact["version"],
            "content": {
                **artifact["content"],
                "summary_evidence": [{
                    **pinned,
                    "evidence_role": "support",
                    "verification_status": "source_checked",
                }],
            },
        })
        confirmed = self.store.confirm_artifact(
            "room_plan",
            reviewed["id"],
            expected_version=reviewed["version"],
        )
        original_revision = confirmed["content"]["summary_evidence"][0]["source_revision"]
        original_sha256 = confirmed["content"]["summary_evidence"][0]["source_snapshot_sha256"]

        with closing(sqlite3.connect(self.db_path)) as connection:
            encoded = connection.execute(
                "SELECT state_json FROM round_checkpoints WHERE round_id=?",
                (round_row["id"],),
            ).fetchone()[0]
            state = json.loads(encoded)
            rewritten_snapshot = state["market_snapshot"]
            rewritten_snapshot["rows"][0]["last"] = 121.75
            rewritten_snapshot["evidence"]["version"] = "storage_market_evidence_v7"
            state["round_evidence_manifest"] = self.store.finalize_round_evidence_manifest(
                state["round_evidence_manifest"],
                shared_context=state["shared_context"],
                market_snapshot=rewritten_snapshot,
            )
            connection.execute(
                "UPDATE round_checkpoints SET state_json=? WHERE round_id=?",
                (json.dumps(state, ensure_ascii=False), round_row["id"]),
            )
            connection.commit()

        rewritten_sha256 = self.store._canonical_sha256(rewritten_snapshot)
        self.assertNotEqual(rewritten_sha256, original_sha256)
        drifted = self.store.get_artifact("room_plan", confirmed["id"])
        drifted_ref = drifted["content"]["summary_evidence"][0]
        self.assertEqual(drifted_ref["source_revision"], original_revision)
        self.assertEqual(drifted_ref["source_snapshot_sha256"], original_sha256)
        self.assertEqual(drifted_ref["version_status"], "unavailable")
        self.assertEqual(drifted_ref["version_decision"], "review_required")
        self.assertEqual(drifted_ref["verification_status"], "unreviewed")
        self.assertFalse(drifted["evidence_review"]["confirmation_ready"])

        convergence_gate = ConvergenceService(self.store)._evidence_gate(drifted)
        self.assertFalse(convergence_gate["ready"])
        self.assertEqual(convergence_gate["unreviewed_evidence_count"], 1)
        self.assertEqual(convergence_gate["stale_evidence_count"], 1)
        with self.assertRaisesRegex(ValueError, "完整性"):
            self.store.confirm_artifact(
                "room_plan",
                confirmed["id"],
                expected_version=confirmed["version"],
            )

        with self.assertRaisesRegex(ValueError, "完整性"):
            self.store.update_artifact("room_plan", confirmed["id"], {
                "expected_version": confirmed["version"],
                "content": confirmed["content"],
            })

    def test_model_minutes_can_reference_only_the_frozen_round_snapshot_and_never_preverify_it(self) -> None:
        room = self.store.room_snapshot("room_plan")["room"]
        self.store.update_room("room_plan", {
            "expected_settings_version": room["settings_version"],
            "capability_pack_ids": [
                *room.get("capability_pack_ids", []),
                "storage_research_readonly",
            ],
        })
        round_row, market_snapshot = self.create_frozen_market_round("futu_model_round")
        provider = MarketSnapshotMinutesProvider(
            self.message["id"],
            self.material["id"],
            market_snapshot["snapshot_id"],
        )

        artifact = ArtifactService(self.store, MinutesRegistry(provider)).generate_minutes(
            "room_plan",
            round_row["id"],
        )

        reference = next(
            item
            for item in artifact["content"]["summary_evidence"]
            if item["type"] == "round_market_snapshot"
        )
        self.assertIn(market_snapshot["snapshot_id"], provider.last_instructions)
        self.assertEqual(reference["id"], market_snapshot["snapshot_id"])
        self.assertEqual(reference["round_id"], round_row["id"])
        self.assertEqual(reference["source_snapshot_sha256"], self.store._canonical_sha256(market_snapshot))
        self.assertEqual(reference["evidence_role"], "context")
        self.assertEqual(reference["verification_status"], "unreviewed")

    def test_round_market_snapshot_rejects_cross_round_or_forged_identity(self) -> None:
        round_a, snapshot_a = self.create_frozen_market_round("futu_round_a")
        _round_b, snapshot_b = self.create_frozen_market_round("futu_round_b")

        with self.assertRaisesRegex(ValueError, "不属于产物绑定的轮次"):
            self.store.create_artifact(
                "room_plan",
                round_id=round_a["id"],
                title="跨轮快照应被拒绝",
                content={
                    "summary": "伪造了其他轮次的快照。",
                    "summary_evidence": [{
                        "type": "round_market_snapshot",
                        "id": snapshot_b["snapshot_id"],
                    }],
                },
            )

        with self.assertRaisesRegex(ValueError, "不属于产物绑定的轮次"):
            self.store.create_artifact(
                "room_plan",
                round_id=round_a["id"],
                title="伪造快照 ID 应被拒绝",
                content={
                    "summary": "伪造了不存在的快照 ID。",
                    "summary_evidence": [{
                        "type": "round_market_snapshot",
                        "id": "futu_forged",
                    }],
                },
            )
        self.assertNotEqual(snapshot_a["snapshot_id"], snapshot_b["snapshot_id"])

    def test_round_market_snapshot_tamper_blocks_confirmation(self) -> None:
        round_row, market_snapshot = self.create_frozen_market_round("futu_tamper")
        artifact = self.store.create_artifact(
            "room_plan",
            round_id=round_row["id"],
            title="篡改快照不得确认",
            content={
                "summary": "该摘要使用本轮快照。",
                "summary_evidence": [{
                    "type": "round_market_snapshot",
                    "id": market_snapshot["snapshot_id"],
                    "evidence_role": "support",
                    "verification_status": "source_checked",
                }],
            },
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            encoded = connection.execute(
                "SELECT state_json FROM round_checkpoints WHERE round_id=?",
                (round_row["id"],),
            ).fetchone()[0]
            state = json.loads(encoded)
            state["market_snapshot"]["rows"][0]["last"] = 999.0
            connection.execute(
                "UPDATE round_checkpoints SET state_json=? WHERE round_id=?",
                (json.dumps(state, ensure_ascii=False), round_row["id"]),
            )
            connection.commit()

        with self.assertRaisesRegex(ValueError, "完整性|市场快照校验失败"):
            self.store.confirm_artifact(
                "room_plan",
                artifact["id"],
                expected_version=artifact["version"],
            )
        self.assertEqual(self.store.get_artifact("room_plan", artifact["id"])["status"], "DRAFT")

    def test_round_market_snapshot_rejects_execution_capability_even_with_matching_hash(self) -> None:
        round_row, market_snapshot = self.create_frozen_market_round("futu_unsafe")
        with closing(sqlite3.connect(self.db_path)) as connection:
            encoded = connection.execute(
                "SELECT state_json FROM round_checkpoints WHERE round_id=?",
                (round_row["id"],),
            ).fetchone()[0]
            state = json.loads(encoded)
            unsafe = state["market_snapshot"]
            unsafe["execution_capability"] = "place_order"
            unsafe["live_trading_allowed"] = True
            state["round_evidence_manifest"]["market_snapshot"] = (
                self.store._market_snapshot_manifest_entry(unsafe)
            )
            connection.execute(
                "UPDATE round_checkpoints SET state_json=? WHERE round_id=?",
                (json.dumps(state, ensure_ascii=False), round_row["id"]),
            )
            connection.commit()

        with self.assertRaisesRegex(ValueError, "完整性|违反只读边界"):
            self.store.create_artifact(
                "room_plan",
                round_id=round_row["id"],
                title="不安全快照应被拒绝",
                content={
                    "summary": "不得绑定具有执行能力的快照。",
                    "summary_evidence": [{
                        "type": "round_market_snapshot",
                        "id": market_snapshot["snapshot_id"],
                    }],
                },
            )

    def test_explicit_synthesizer_uses_only_selected_enabled_member_route(self) -> None:
        members = self.store.room_snapshot("room_plan")["members"]
        facilitator = next(member for member in members if member["stance"] == "facilitator")
        selected = next(member for member in members if member["id"] != facilitator["id"])
        selected = self.store.update_member(
            "room_plan",
            selected["id"],
            {"provider": "doubao", "model": "doubao-minutes-fixture"},
        )
        facilitator_provider = MinutesProvider(self.message["id"], self.material["id"])
        selected_provider = MinutesProvider(
            self.message["id"],
            self.material["id"],
            provider_id="doubao",
        )
        registry = MappingMinutesRegistry({
            facilitator["provider"]: facilitator_provider,
            selected["provider"]: selected_provider,
        })

        artifact = ArtifactService(self.store, registry).generate_minutes(
            "room_plan",
            synthesizer_member_id=selected["id"],
        )

        self.assertEqual(registry.requested_provider_ids, ["doubao"])
        self.assertEqual(facilitator_provider.calls, 0)
        self.assertEqual(selected_provider.calls, 1)
        self.assertEqual(selected_provider.last_model, "doubao-minutes-fixture")
        self.assertEqual(artifact["generation_source"], "doubao:doubao-minutes-fixture")

    def test_frozen_synthesizer_route_uses_historical_version_after_current_edit(self) -> None:
        selected = next(
            member
            for member in self.store.room_snapshot("room_plan")["members"]
            if member["workflow_stage"] == "decision"
        )
        approved = self.store.update_member(
            "room_plan",
            selected["id"],
            {
                "provider": "deepseek",
                "model": "deepseek-approved-minutes",
                "enabled": True,
            },
        )
        round_row = self.create_frozen_route_round(approved["id"])
        self.store.update_member(
            "room_plan",
            approved["id"],
            {
                "provider": "doubao",
                "model": "doubao-current-must-not-run",
                "enabled": False,
            },
        )
        approved_provider = MinutesProvider(
            self.message["id"],
            self.material["id"],
            provider_id="deepseek",
        )
        current_provider = MinutesProvider(
            self.message["id"],
            self.material["id"],
            provider_id="doubao",
        )
        registry = MappingMinutesRegistry({
            "deepseek": approved_provider,
            "doubao": current_provider,
        })
        ledger = self.create_ledger(max_calls=1)

        artifact = ArtifactService(self.store, registry).generate_minutes(
            "room_plan",
            round_row["id"],
            synthesizer_member_id=approved["id"],
            frozen_synthesizer_route={
                "member_id": approved["id"],
                "member_version": approved["version"],
                "provider": approved["provider"],
                "model": approved["model"],
            },
            ledger=ledger,
        )

        self.assertEqual(registry.requested_provider_ids, ["deepseek"])
        self.assertEqual(approved_provider.calls, 1)
        self.assertEqual(current_provider.calls, 0)
        self.assertEqual(approved_provider.last_model, "deepseek-approved-minutes")
        self.assertEqual(
            artifact["generation_source"],
            "deepseek:deepseek-approved-minutes",
        )
        attempts = ledger.attempts()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["member_id"], approved["id"])
        self.assertEqual(attempts[0]["member_version"], approved["version"])
        self.assertEqual(attempts[0]["provider"], "deepseek")
        self.assertEqual(attempts[0]["model"], "deepseek-approved-minutes")

    def test_frozen_synthesizer_route_pins_resolved_model_for_blank_member_model(self) -> None:
        class ResolvedModelRegistry(MappingMinutesRegistry):
            @staticmethod
            def resolved_model(provider_id: str, configured_model: str = "") -> str:
                self.assertEqual(provider_id, "deepseek")
                return configured_model or "deepseek-approved-default"

        selected = self.store.room_snapshot("room_plan")["members"][0]
        approved = self.store.update_member(
            "room_plan",
            selected["id"],
            {"provider": "deepseek", "model": "", "enabled": True},
        )
        self.assertEqual(approved["model"], "")
        provider = MinutesProvider(
            self.message["id"],
            self.material["id"],
            provider_id="deepseek",
        )
        registry = ResolvedModelRegistry({"deepseek": provider})

        artifact = ArtifactService(self.store, registry).generate_minutes(
            "room_plan",
            frozen_synthesizer_route={
                "member_id": approved["id"],
                "member_version": approved["version"],
                "provider": "deepseek",
                "model": "deepseek-approved-default",
            },
        )

        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.last_model, "deepseek-approved-default")
        self.assertEqual(
            artifact["generation_source"],
            "deepseek:deepseek-approved-default",
        )

    def test_frozen_synthesizer_route_tampering_fails_before_provider_or_ledger(self) -> None:
        approved = self.store.room_snapshot("room_plan")["members"][0]
        provider = MinutesProvider(self.message["id"], self.material["id"])
        registry = MappingMinutesRegistry({"openai": provider, "doubao": provider})
        ledger = self.create_ledger(max_calls=1)
        artifact_count = len(self.store.room_snapshot("room_plan")["artifacts"])

        tampered_routes = [
            (
                {
                    "member_id": approved["id"],
                    "member_version": approved["version"],
                    "provider": "doubao",
                    "model": approved["model"],
                },
                "Provider 与成员版本不一致",
            ),
            (
                {
                    "member_id": approved["id"],
                    "member_version": approved["version"],
                    "provider": approved["provider"],
                    "model": "tampered-model-must-not-run",
                },
                "模型与成员版本不一致",
            ),
            (
                {
                    "member_id": approved["id"],
                    "member_version": approved["version"] + 10_000,
                    "provider": approved["provider"],
                    "model": approved["model"],
                },
                "成员版本不存在",
            ),
        ]
        for route, expected_error in tampered_routes:
            with self.subTest(route=route), self.assertRaisesRegex(
                ValueError,
                expected_error,
            ):
                ArtifactService(self.store, registry).generate_minutes(
                    "room_plan",
                    frozen_synthesizer_route=route,
                    ledger=ledger,
                )

        self.assertEqual(registry.requested_provider_ids, [])
        self.assertEqual(provider.calls, 0)
        self.assertEqual(ledger.attempts(), [])
        self.assertEqual(
            len(self.store.room_snapshot("room_plan")["artifacts"]),
            artifact_count,
        )

    def test_frozen_synthesizer_route_rejects_conflicting_explicit_member(self) -> None:
        members = self.store.room_snapshot("room_plan")["members"]
        approved = members[0]
        conflicting = members[1]
        provider = MinutesProvider(self.message["id"], self.material["id"])
        registry = MappingMinutesRegistry({approved["provider"]: provider})

        with self.assertRaisesRegex(ValueError, "与冻结路由不一致"):
            ArtifactService(self.store, registry).generate_minutes(
                "room_plan",
                synthesizer_member_id=conflicting["id"],
                frozen_synthesizer_route={
                    "id": approved["id"],
                    "version": approved["version"],
                    "provider": approved["provider"],
                    "model": approved["model"],
                },
            )

        self.assertEqual(registry.requested_provider_ids, [])
        self.assertEqual(provider.calls, 0)

    def test_explicit_synthesizer_cannot_bypass_skipped_provider(self) -> None:
        selected = self.store.room_snapshot("room_plan")["members"][0]
        selected = self.store.update_member(
            "room_plan",
            selected["id"],
            {"provider": "openai", "model": "must-not-run"},
        )
        provider = MinutesProvider(self.message["id"], self.material["id"])
        registry = MappingMinutesRegistry({"openai": provider})
        service = ArtifactService(self.store, registry)
        artifact_count = len(self.store.room_snapshot("room_plan")["artifacts"])

        with self.assertRaisesRegex(ValueError, "已跳过"):
            service.generate_minutes(
                "room_plan",
                synthesizer_member_id=selected["id"],
                skip_provider_ids={"openai"},
            )

        self.assertEqual(registry.requested_provider_ids, [])
        self.assertEqual(provider.calls, 0)
        self.assertEqual(len(self.store.room_snapshot("room_plan")["artifacts"]), artifact_count)

    def test_default_synthesizer_chooses_enabled_non_skipped_provider(self) -> None:
        members = self.store.room_snapshot("room_plan")["members"]
        facilitator = next(member for member in members if member["stance"] == "facilitator")
        self.store.update_member(
            "room_plan",
            facilitator["id"],
            {"provider": "openai", "model": "must-not-run"},
        )
        selected = next(
            member for member in members
            if member["workflow_stage"] == "decision"
        )
        selected = self.store.update_member(
            "room_plan",
            selected["id"],
            {"provider": "deepseek", "model": "deepseek-minutes-fixture"},
        )
        openai_provider = MinutesProvider(self.message["id"], self.material["id"])
        deepseek_provider = MinutesProvider(
            self.message["id"],
            self.material["id"],
            provider_id="deepseek",
        )
        registry = MappingMinutesRegistry({
            "openai": openai_provider,
            "deepseek": deepseek_provider,
        })

        artifact = ArtifactService(self.store, registry).generate_minutes(
            "room_plan",
            skip_provider_ids={"openai"},
        )

        self.assertEqual(registry.requested_provider_ids, ["deepseek"])
        self.assertEqual(openai_provider.calls, 0)
        self.assertEqual(deepseek_provider.calls, 1)
        self.assertEqual(artifact["generation_source"], "deepseek:deepseek-minutes-fixture")

    def test_default_synthesizer_prefers_decision_stage_member(self) -> None:
        members = self.store.room_snapshot("room_plan")["members"]
        facilitator = next(member for member in members if member["stance"] == "facilitator")
        decision_member = next(
            member for member in members
            if member["workflow_stage"] == "decision"
        )
        facilitator = self.store.update_member(
            "room_plan",
            facilitator["id"],
            {"provider": "deepseek", "model": "facilitator-model"},
        )
        decision_member = self.store.update_member(
            "room_plan",
            decision_member["id"],
            {"provider": "doubao", "model": "decision-json-model"},
        )
        facilitator_provider = MinutesProvider(
            self.message["id"],
            self.material["id"],
            provider_id="deepseek",
        )
        decision_provider = MinutesProvider(
            self.message["id"],
            self.material["id"],
            provider_id="doubao",
        )
        registry = MappingMinutesRegistry({
            facilitator["provider"]: facilitator_provider,
            decision_member["provider"]: decision_provider,
        })

        artifact = ArtifactService(self.store, registry).generate_minutes("room_plan")

        self.assertEqual(registry.requested_provider_ids, ["doubao"])
        self.assertEqual(facilitator_provider.calls, 0)
        self.assertEqual(decision_provider.calls, 1)
        self.assertEqual(artifact["generation_source"], "doubao:decision-json-model")

    def test_explicit_synthesizer_provider_mismatch_fails_closed_without_switching(self) -> None:
        members = self.store.room_snapshot("room_plan")["members"]
        facilitator = next(member for member in members if member["stance"] == "facilitator")
        selected = next(member for member in members if member["id"] != facilitator["id"])
        selected = self.store.update_member(
            "room_plan",
            selected["id"],
            {"provider": "doubao", "model": "doubao-minutes-fixture"},
        )
        facilitator_provider = MinutesProvider(self.message["id"], self.material["id"])
        mismatched_provider = MinutesProvider(
            self.message["id"],
            self.material["id"],
            provider_id="openai",
        )
        registry = MappingMinutesRegistry({
            facilitator["provider"]: facilitator_provider,
            selected["provider"]: mismatched_provider,
        })

        artifact = ArtifactService(self.store, registry).generate_minutes(
            "room_plan",
            synthesizer_member_id=selected["id"],
        )

        self.assertEqual(registry.requested_provider_ids, ["doubao"])
        self.assertEqual(mismatched_provider.calls, 1)
        self.assertEqual(facilitator_provider.calls, 0)
        self.assertEqual(artifact["generation_source"], "template_fallback")
        self.assertIn(
            "提供商身份与所选成员不一致",
            artifact["content"]["generation_notes"],
        )

    def test_disabled_explicit_synthesizer_fails_without_provider_call_or_artifact(self) -> None:
        selected = self.store.room_snapshot("room_plan")["members"][1]
        selected = self.store.update_member(
            "room_plan",
            selected["id"],
            {"enabled": False},
        )
        provider = MinutesProvider(self.message["id"], self.material["id"])
        service = ArtifactService(self.store, MinutesRegistry(provider))
        artifact_count = len(self.store.room_snapshot("room_plan")["artifacts"])

        with self.assertRaisesRegex(ValueError, "已禁用"):
            service.generate_minutes(
                "room_plan",
                synthesizer_member_id=selected["id"],
            )

        self.assertEqual(provider.calls, 0)
        self.assertEqual(len(self.store.room_snapshot("room_plan")["artifacts"]), artifact_count)

    def test_missing_explicit_synthesizer_fails_without_provider_call_or_artifact(self) -> None:
        provider = MinutesProvider(self.message["id"], self.material["id"])
        service = ArtifactService(self.store, MinutesRegistry(provider))
        artifact_count = len(self.store.room_snapshot("room_plan")["artifacts"])

        with self.assertRaisesRegex(ValueError, "不存在或不属于当前房间"):
            service.generate_minutes(
                "room_plan",
                synthesizer_member_id="member_not_in_room",
            )

        self.assertEqual(provider.calls, 0)
        self.assertEqual(len(self.store.room_snapshot("room_plan")["artifacts"]), artifact_count)

    def test_minutes_prefer_provider_structured_generation_when_available(self) -> None:
        provider = StructuredMinutesProvider(self.message["id"], self.material["id"])
        service = ArtifactService(self.store, MinutesRegistry(provider))
        ledger = self.create_ledger(max_calls=1)

        artifact = service.generate_minutes("room_plan", ledger=ledger)

        self.assertEqual(provider.json_calls, 1)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(artifact["content"]["decision"]["status"], "candidate")
        self.assertEqual(len(ledger.attempts()), 1)
        self.assertEqual(ledger.attempts()[0]["status"], "RESPONDED")

    def test_round_minutes_use_only_frozen_messages_and_material_versions(self) -> None:
        round_row, round_message = self.create_frozen_round()
        self.store.update_material("room_plan", self.material["id"], {
            **self.material,
            "expected_version": self.material["version"],
            "content": "第二版资料在会议结束后才出现，不得进入旧轮次纪要。",
        })
        provider = MinutesProvider(
            round_message["id"],
            self.material["id"],
            extra_message_id=self.message["id"],
        )
        service = ArtifactService(self.store, MinutesRegistry(provider))

        artifact = service.generate_minutes("room_plan", round_row["id"])

        self.assertEqual(
            [ref["id"] for ref in artifact["content"]["summary_evidence"]],
            [round_message["id"]],
        )
        material_ref = artifact["content"]["conclusions"][0]["evidence"][0]
        self.assertEqual(material_ref["version"], 1)
        self.assertEqual(material_ref["version_status"], "superseded")
        self.assertIn("首期必须使用低成本原型验证", provider.last_input)
        self.assertNotIn("第二版资料在会议结束后才出现", provider.last_input)
        self.assertIn("冻结轮次草稿", artifact["content"]["generation_notes"])

    def test_round_bound_artifact_drops_cross_round_and_post_round_evidence(self) -> None:
        round_a, message_a = self.create_frozen_round()
        _round_b, message_b = self.create_frozen_round()
        post_round_material = self.store.add_material("room_plan", {
            "title": "会后新增材料",
            "kind": "note",
            "content": "这份材料没有进入 round A 的冻结证据清单。",
        })

        artifact = self.store.create_artifact(
            "room_plan",
            round_id=round_a["id"],
            title="严格冻结来源范围",
            content={
                "summary": "只保留本轮消息。",
                "summary_evidence": [
                    audited_evidence("message", message_a["id"]),
                    audited_evidence("message", message_b["id"]),
                ],
                "conclusions": [{
                    "text": "只保留清单中钉住的资料版本。",
                    "evidence": [
                        audited_evidence("material", self.material["id"]),
                        audited_evidence("material", post_round_material["id"]),
                    ],
                }],
            },
        )

        self.assertEqual(
            [ref["id"] for ref in artifact["content"]["summary_evidence"]],
            [message_a["id"]],
        )
        self.assertEqual(
            [ref["id"] for ref in artifact["content"]["conclusions"][0]["evidence"]],
            [self.material["id"]],
        )

    def test_round_minutes_fail_closed_without_valid_frozen_checkpoint(self) -> None:
        round_row = self.store.create_round("room_plan", "缺少检查点")
        self.store.complete_round(round_row["id"], "COMPLETED")
        provider = MinutesProvider(self.message["id"], self.material["id"])
        service = ArtifactService(self.store, MinutesRegistry(provider))

        with self.assertRaisesRegex(ValueError, "冻结证据检查点"):
            service.generate_minutes("room_plan", round_row["id"])

        self.assertEqual(provider.calls, 0)

    def test_minutes_cannot_be_generated_while_round_is_running(self) -> None:
        round_row = self.store.create_round("room_plan", "运行中的讨论")
        provider = MinutesProvider(self.message["id"], self.material["id"])
        service = ArtifactService(self.store, MinutesRegistry(provider))

        with self.assertRaisesRegex(ValueError, "讨论正在运行"):
            service.generate_minutes("room_plan")
        with self.assertRaisesRegex(ValueError, "运行中或已取消"):
            service.generate_minutes("room_plan", round_row["id"])

        self.assertEqual(provider.calls, 0)

    def test_artifact_provider_error_metadata_is_not_persisted(self) -> None:
        provider = UnsafeFailureMinutesProvider(self.message["id"], self.material["id"])
        service = ArtifactService(self.store, MinutesRegistry(provider))
        ledger = self.create_ledger(max_calls=1)

        artifact = service.generate_minutes("room_plan", ledger=ledger)
        encoded = json.dumps(artifact, ensure_ascii=False)

        self.assertEqual(artifact["generation_source"], "template_fallback")
        self.assertIn("会议整理模型 请求失败", artifact["content"]["generation_notes"])
        self.assertNotIn("raw sensitive upstream diagnostics", encoded)
        self.assertNotIn("untrusted-upstream-provider", encoded)
        self.assertNotIn("untrusted-upstream-model", encoded)
        attempts = ledger.attempts()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["kind"], "artifact_generation")
        self.assertEqual(attempts[0]["status"], "FAILED")
        self.assertEqual(attempts[0]["error_code"], "http_status")

    def test_successful_minutes_call_records_safe_usage_once(self) -> None:
        provider = UsageMinutesProvider(self.message["id"], self.material["id"])
        ledger = self.create_ledger(max_calls=1)

        artifact = ArtifactService(
            self.store,
            MinutesRegistry(provider),
        ).generate_minutes("room_plan", ledger=ledger)

        self.assertNotEqual(artifact["generation_source"], "template_fallback")
        self.assertEqual(provider.calls, 1)
        attempts = ledger.attempts()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["kind"], "artifact_generation")
        self.assertEqual(attempts[0]["status"], "RESPONDED")
        self.assertTrue(attempts[0]["member_id"])
        self.assertGreater(attempts[0]["member_version"], 0)
        self.assertEqual(
            attempts[0]["usage"],
            {
                "cached.read_tokens": 4,
                "input_tokens": 31,
                "output_tokens": 9,
            },
        )
        self.assertNotIn(
            "artifact-prompt-secret",
            json.dumps(attempts, ensure_ascii=False),
        )

    def test_provider_exception_is_terminalized_without_leaking_text(self) -> None:
        provider = ExceptionMinutesProvider(self.message["id"], self.material["id"])
        ledger = self.create_ledger(max_calls=1)

        artifact = ArtifactService(
            self.store,
            MinutesRegistry(provider),
        ).generate_minutes("room_plan", ledger=ledger)

        attempts = ledger.attempts()
        self.assertEqual(provider.calls, 1)
        self.assertEqual(artifact["generation_source"], "template_fallback")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "FAILED")
        self.assertEqual(attempts[0]["error_code"], "provider_error")
        self.assertNotIn(
            "artifact-provider-exception-secret",
            json.dumps({"artifact": artifact, "attempts": attempts}, ensure_ascii=False),
        )

    def test_exhausted_minutes_budget_skips_provider_and_creates_honest_fallback(self) -> None:
        provider = MinutesProvider(self.message["id"], self.material["id"])
        ledger = self.create_ledger(max_calls=1)
        spent = ledger.reserve(
            kind="artifact_generation",
            provider="openai",
            model="earlier-artifact-model",
        )
        ledger.finish(
            str(spent["id"]),
            str(spent["attempt_token"]),
            status="CANCELLED",
        )

        artifact = ArtifactService(
            self.store,
            MinutesRegistry(provider),
        ).generate_minutes("room_plan", ledger=ledger)

        self.assertEqual(provider.calls, 0)
        self.assertEqual(artifact["generation_source"], "template_fallback")
        self.assertIn(
            "PROVIDER_CALL_BUDGET_EXCEEDED",
            artifact["content"]["generation_notes"],
        )
        self.assertIn(
            "Provider 调用次数上限已用尽",
            artifact["content"]["generation_notes"],
        )
        self.assertEqual(len(ledger.attempts()), 1)

    def test_ledger_finalize_failure_does_not_persist_artifact(self) -> None:
        provider = MinutesProvider(self.message["id"], self.material["id"])
        persisted_ledger = self.create_ledger(max_calls=2)
        failing_ledger = FinalizeFailLedger(persisted_ledger)
        artifact_count = len(self.store.list_artifacts("room_plan"))

        with self.assertRaisesRegex(RuntimeError, "终态写入失败"):
            ArtifactService(
                self.store,
                MinutesRegistry(provider),
            ).generate_minutes("room_plan", ledger=failing_ledger)

        self.assertEqual(provider.calls, 1)
        self.assertEqual(failing_ledger.finish_calls, 1)
        self.assertEqual(len(self.store.list_artifacts("room_plan")), artifact_count)
        attempts = persisted_ledger.attempts()
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "STARTED")

    def test_twelve_role_transcript_keeps_every_message_in_prompt(self) -> None:
        messages = [
            {
                "id": f"msg_role_{index}",
                "sender_name": f"角色 {index}",
                "content": f"ROLE_{index}_START " + ("证据与反证 " * 220) + f" ROLE_{index}_END",
            }
            for index in range(1, 13)
        ]

        context = ArtifactService._message_prompt_context(messages)

        self.assertLessEqual(len(context), 26000)
        for index in range(1, 13):
            self.assertIn(f"[消息:msg_role_{index}]", context)
            self.assertIn(f"ROLE_{index}_START", context)
            self.assertIn(f"ROLE_{index}_END", context)

    def test_unconfigured_provider_creates_honest_empty_framework(self) -> None:
        provider = MinutesProvider(self.message["id"], self.material["id"], configured=False)
        service = ArtifactService(self.store, MinutesRegistry(provider))

        artifact = service.generate_minutes("room_plan")

        self.assertEqual(provider.calls, 0)
        self.assertEqual(artifact["generation_source"], "template_fallback")
        self.assertEqual(artifact["content"]["conclusions"], [])
        self.assertIn("没有自动推断", artifact["content"]["summary"])

    def test_artifact_without_evidence_cannot_be_confirmed(self) -> None:
        artifact = self.store.create_artifact(
            "room_plan",
            title="无证据纪要",
            content={
                "summary": "这是未经证据支持的摘要。",
                "conclusions": [{"text": "直接确认这个结论。", "evidence": []}],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        )
        with self.assertRaisesRegex(ValueError, "会议摘要"):
            self.store.confirm_artifact(
                "room_plan", artifact["id"], expected_version=artifact["version"], confirmed_by="user",
            )

    def test_candidate_decision_requires_comparison_selection_reason_and_reviewed_evidence(self) -> None:
        evidence = [audited_evidence("message", self.message["id"])]
        artifact = self.store.create_artifact(
            "room_plan",
            title="候选方案比较",
            content={
                "summary": "先比较两个候选方案。",
                "summary_evidence": evidence,
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
                "decision": {
                    "status": "candidate",
                    "options": [{
                        "id": "option_a",
                        "title": "方案 A",
                        "description": "先做小范围验证。",
                        "benefits": ["成本低"],
                        "risks": ["覆盖较少"],
                        "evidence": evidence,
                    }],
                    "preferred_option_id": "option_a",
                    "rationale": "先控制验证成本。",
                    "evidence": evidence,
                },
            },
        )

        with self.assertRaisesRegex(ValueError, "至少需要两个"):
            self.store.confirm_artifact(
                "room_plan", artifact["id"], expected_version=artifact["version"], confirmed_by="user",
            )

        revised = self.store.update_artifact("room_plan", artifact["id"], {
            "expected_version": artifact["version"],
            "content": {
                **artifact["content"],
                "decision": {
                    **artifact["content"]["decision"],
                    "options": [
                        artifact["content"]["decision"]["options"][0],
                        {
                            "id": "option_b",
                            "title": "方案 B",
                            "description": "直接覆盖完整范围。",
                            "benefits": ["覆盖完整"],
                            "risks": ["成本高"],
                            "evidence": evidence,
                        },
                    ],
                },
            },
        })
        confirmed = self.store.confirm_artifact(
            "room_plan", revised["id"], expected_version=revised["version"], confirmed_by="user",
        )

        self.assertEqual(confirmed["status"], "CONFIRMED")
        self.assertEqual(confirmed["content"]["decision"]["preferred_option_id"], "option_a")
        with closing(sqlite3.connect(self.db_path)) as connection:
            item_keys = {
                row[0]
                for row in connection.execute(
                    "SELECT item_key FROM artifact_evidence WHERE artifact_id=?",
                    (artifact["id"],),
                ).fetchall()
            }
        self.assertTrue({"decision", "decision_options:option_a", "decision_options:option_b"}.issubset(item_keys))

    def test_model_preferred_option_survives_server_id_normalization(self) -> None:
        artifact = self.store.create_artifact(
            "room_plan",
            title="模型方案 ID 规范化",
            content={
                "summary": "模型使用了非 ASCII 方案 ID。",
                "summary_evidence": [],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
                "decision": {
                    "status": "candidate",
                    "options": [
                        {"id": "方案甲", "title": "方案甲", "description": "先验证。"},
                        {"id": "方案乙", "title": "方案乙", "description": "直接扩展。"},
                    ],
                    "preferred_option_id": "方案甲",
                    "rationale": "先验证风险更低。",
                    "evidence": [],
                },
            },
        )

        decision = artifact["content"]["decision"]
        option_ids = {item["id"] for item in decision["options"]}
        self.assertEqual(len(option_ids), 2)
        self.assertIn(decision["preferred_option_id"], option_ids)
        self.assertNotIn("方案甲", option_ids)

    def test_empty_artifact_cannot_be_confirmed(self) -> None:
        artifact = self.store.create_artifact(
            "room_plan",
            title="空纪要",
            content={
                "summary": "",
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        )

        with self.assertRaisesRegex(ValueError, "会议摘要不能为空"):
            self.store.confirm_artifact(
                "room_plan",
                artifact["id"],
                expected_version=artifact["version"],
                confirmed_by="user",
            )

    def test_handled_disagreement_requires_resolution(self) -> None:
        artifact = self.store.create_artifact(
            "room_plan",
            title="分歧处理纪要",
            content={
                "summary": "会议保留一项已处理分歧。",
                "summary_evidence": [audited_evidence("message", self.message["id"])],
                "conclusions": [],
                "disagreements": [{
                    "text": "是否扩大验证范围",
                    "positions": ["先验证", "立即扩大"],
                    "status": "accepted_risk",
                    "blocking": True,
                    "owner": "用户",
                    "resolution": "",
                    "evidence": [audited_evidence("message", self.message["id"])],
                }],
                "unknowns": [],
                "actions": [],
            },
        )

        with self.assertRaisesRegex(ValueError, "决议说明"):
            self.store.confirm_artifact(
                "room_plan",
                artifact["id"],
                expected_version=artifact["version"],
                confirmed_by="user",
            )

        revised = self.store.update_artifact("room_plan", artifact["id"], {
            "expected_version": artifact["version"],
            "content": {
                **artifact["content"],
                "disagreements": [{
                    **artifact["content"]["disagreements"][0],
                    "resolution": "用户接受先用小样本验证的剩余风险，并保留停止条件。",
                }],
            },
        })
        rereviewed_content = json.loads(json.dumps(revised["content"]))
        rereviewed_content["disagreements"][0]["evidence"][0].update({
            "verification_status": "source_checked",
            "review_note": "已按新增决议重新核对原发言。",
        })
        revised = self.store.update_artifact("room_plan", revised["id"], {
            "expected_version": revised["version"],
            "content": rereviewed_content,
        })
        confirmed = self.store.confirm_artifact(
            "room_plan",
            revised["id"],
            expected_version=revised["version"],
            confirmed_by="user",
        )

        disagreement = confirmed["content"]["disagreements"][0]
        self.assertEqual(disagreement["status"], "accepted_risk")
        self.assertTrue(disagreement["blocking"])
        self.assertEqual(disagreement["owner"], "用户")
        self.assertIn("停止条件", disagreement["resolution"])

    def test_already_confirmed_artifact_is_revalidated_against_current_gate(self) -> None:
        artifact = self.store.create_artifact(
            "room_plan",
            title="旧确认纪要",
            content={
                "summary": "先做验证。",
                "summary_evidence": [audited_evidence("message", self.message["id"])],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        )
        confirmed = self.store.confirm_artifact(
            "room_plan",
            artifact["id"],
            expected_version=artifact["version"],
            confirmed_by="user",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE artifacts SET content_json=? WHERE id=?",
                (
                    json.dumps({
                        "summary": "",
                        "conclusions": [],
                        "disagreements": [],
                        "unknowns": [],
                        "actions": [],
                    }, ensure_ascii=False),
                    artifact["id"],
                ),
            )

        with self.assertRaisesRegex(ValueError, "会议摘要不能为空"):
            self.store.confirm_artifact(
                "room_plan",
                artifact["id"],
                expected_version=confirmed["version"],
                confirmed_by="user",
            )

    def test_existing_reference_is_not_verified_until_user_reviews_role_and_status(self) -> None:
        artifact = self.store.create_artifact(
            "room_plan",
            title="待核验纪要",
            content={
                "summary": "资料已经绑定，但尚未核验。",
                "summary_evidence": [{"type": "material", "id": self.material["id"]}],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        )

        with self.assertRaisesRegex(ValueError, "尚未核验"):
            self.store.confirm_artifact(
                "room_plan", artifact["id"], expected_version=artifact["version"], confirmed_by="user",
            )

        reviewed = self.store.update_artifact("room_plan", artifact["id"], {
            "expected_version": artifact["version"],
            "content": {
                **artifact["content"],
                "summary_evidence": [audited_evidence("material", self.material["id"])],
            },
        })
        confirmed = self.store.confirm_artifact(
            "room_plan", reviewed["id"], expected_version=reviewed["version"], confirmed_by="user",
        )

        self.assertEqual(confirmed["status"], "CONFIRMED")
        self.assertEqual(confirmed["content"]["summary_evidence"][0]["verification_status"], "source_checked")

    def test_counter_or_disputed_evidence_requires_an_explicit_review_note(self) -> None:
        artifact = self.store.create_artifact(
            "room_plan",
            title="反证纪要",
            content={
                "summary": "存在反方材料。",
                "summary_evidence": [
                    audited_evidence("message", self.message["id"]),
                    audited_evidence("material", self.material["id"], role="counter"),
                ],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        )

        with self.assertRaisesRegex(ValueError, "反证说明"):
            self.store.confirm_artifact(
                "room_plan", artifact["id"], expected_version=artifact["version"], confirmed_by="user",
            )

        revised = self.store.update_artifact("room_plan", artifact["id"], {
            "expected_version": artifact["version"],
            "content": {
                **artifact["content"],
                "summary_evidence": [
                    audited_evidence("message", self.message["id"]),
                    audited_evidence(
                        "material",
                        self.material["id"],
                        role="counter",
                        status="disputed",
                        note="该资料与会议主张相冲突，保留为反证。",
                    ),
                ],
            },
        })
        confirmed = self.store.confirm_artifact(
            "room_plan", revised["id"], expected_version=revised["version"], confirmed_by="user",
        )

        counter = confirmed["content"]["summary_evidence"][1]
        self.assertEqual(counter["evidence_role"], "counter")
        self.assertEqual(counter["verification_status"], "disputed")
        self.assertIn("相冲突", counter["review_note"])

    def test_existing_evidence_table_is_migrated_without_upgrading_old_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            legacy_path = Path(temp_dir) / "legacy.sqlite3"
            with closing(sqlite3.connect(legacy_path)) as connection:
                connection.execute(
                    """CREATE TABLE artifact_evidence(
                        artifact_id TEXT NOT NULL,item_key TEXT NOT NULL,source_type TEXT NOT NULL,
                        source_id TEXT NOT NULL,source_version INTEGER NOT NULL DEFAULT 0,
                        created_at INTEGER NOT NULL,
                        PRIMARY KEY(artifact_id,item_key,source_type,source_id)
                    )"""
                )
                connection.execute(
                    "INSERT INTO artifact_evidence VALUES(?,?,?,?,?,?)",
                    ("artifact_old", "summary", "material", "mat_old", 1, 1),
                )
                connection.commit()

            StudioStore(legacy_path)

            with closing(sqlite3.connect(legacy_path)) as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(artifact_evidence)")}
                migrated = connection.execute(
                    """SELECT evidence_role,verification_status,review_note,version_decision,
                              source_revision,source_snapshot_sha256
                         FROM artifact_evidence"""
                ).fetchone()

        self.assertTrue({
            "evidence_role",
            "verification_status",
            "review_note",
            "version_decision",
            "source_revision",
            "source_snapshot_sha256",
        }.issubset(columns))
        self.assertEqual(migrated, ("context", "unreviewed", "", "current", "", ""))

    def test_material_version_change_requires_migration_or_explained_historical_snapshot(self) -> None:
        artifact = self.store.create_artifact(
            "room_plan",
            title="版本漂移纪要",
            content={
                "summary": "基于第一版资料。",
                "summary_evidence": [audited_evidence("material", self.material["id"])],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
            },
        )
        updated_material = self.store.update_material("room_plan", self.material["id"], {
            **self.material,
            "expected_version": self.material["version"],
            "content": "第二版资料修正了原始限制。",
        })
        drifted_ref = self.store.get_artifact("room_plan", artifact["id"])["content"]["summary_evidence"][0]

        self.assertEqual(drifted_ref["version"], 1)
        self.assertEqual(drifted_ref["latest_version"], 2)
        self.assertEqual(drifted_ref["version_status"], "superseded")
        self.assertEqual(drifted_ref["version_decision"], "review_required")

        with self.assertRaisesRegex(ValueError, "版本变化"):
            self.store.confirm_artifact(
                "room_plan", artifact["id"], expected_version=artifact["version"], confirmed_by="user",
            )

        keep_old = self.store.update_artifact("room_plan", artifact["id"], {
            "expected_version": artifact["version"],
            "content": {
                **artifact["content"],
                "summary_evidence": [{
                    **audited_evidence(
                        "material",
                        self.material["id"],
                        note="本纪要记录当时决策，保留会前看到的第一版快照。",
                    ),
                    "version": 1,
                    "version_decision": "keep_snapshot",
                }],
            },
        })
        confirmed_old = self.store.confirm_artifact(
            "room_plan", keep_old["id"], expected_version=keep_old["version"], confirmed_by="user",
        )

        old_ref = confirmed_old["content"]["summary_evidence"][0]
        self.assertEqual(updated_material["version"], 2)
        self.assertEqual(old_ref["version"], 1)
        self.assertEqual(old_ref["latest_version"], 2)
        self.assertEqual(old_ref["version_status"], "superseded")
        self.assertEqual(old_ref["version_decision"], "keep_snapshot")

        migrated_draft = self.store.update_artifact("room_plan", confirmed_old["id"], {
            "expected_version": confirmed_old["version"],
            "content": {
                **confirmed_old["content"],
                "summary_evidence": [{
                    **audited_evidence("material", self.material["id"]),
                    "version": 2,
                    "version_decision": "current",
                }],
            },
        })
        migrated_ref = migrated_draft["content"]["summary_evidence"][0]
        self.assertEqual(migrated_ref["version"], 2)
        self.assertEqual(migrated_ref["version_status"], "current")

    def create_evidence_source_fixture(self) -> tuple[dict, dict, dict, dict]:
        round_row = self.store.create_round("room_plan", "冻结证据来源目录")
        secret_marker = "sk-proj-TESTONLYabcdefghijklmnop"
        round_message = self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id="member_evidence",
            sender_name="证据研究员",
            identity="核验冻结来源",
            content=f"OPENAI_API_KEY={secret_marker}\n第一版判断。" + ("长文本" * 1200),
            round_id=round_row["id"],
            member_version=3,
        )
        market_snapshot = {
            "snapshot_id": "futu_store_frozen",
            "captured_at": "2026-08-01T12:00:00Z",
            "state": "ready",
            "source": "futu_opend_readonly",
            "rows": [{"symbol": "US.MU", "last": 121.5, "quality": "ready"}],
            "evidence": {
                "version": "storage_market_evidence_v6",
                "state": "ready",
            },
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        context, manifest = self.store.material_prompt_bundle("room_plan")
        manifest = self.store.finalize_round_evidence_manifest(
            manifest,
            shared_context=context,
            market_snapshot=market_snapshot,
        )
        self.store.save_round_checkpoint("room_plan", round_row["id"], {
            "member_ids": [],
            "spoken_counts": {},
            "spoken_stances": [],
            "successful_member_ids": [],
            "failed_member_ids": [],
            "completed": 1,
            "next_order": 2,
            "max_turns": 1,
            "shared_context": context,
            "market_snapshot": market_snapshot,
            "round_evidence_manifest": manifest,
        })
        self.store.complete_round(round_row["id"], "COMPLETED")
        updated_material = self.store.update_material("room_plan", self.material["id"], {
            **self.material,
            "expected_version": self.material["version"],
            "content": "第二版内容，不得替代本轮冻结的第一版。",
        })
        other_round = self.store.create_round("room_plan", "另一个轮次")
        other_message = self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id="member_other",
            sender_name="跨轮成员",
            content="这条跨轮消息不得进入来源目录。",
            round_id=other_round["id"],
        )
        artifact = self.store.create_artifact(
            "room_plan",
            round_id=round_row["id"],
            title="冻结证据来源目录",
            content={
                "summary": "只引用本轮冻结来源。",
                "summary_evidence": [
                    {"type": "material", "id": self.material["id"]},
                    {"type": "message", "id": round_message["id"]},
                    {"type": "round_market_snapshot", "id": market_snapshot["snapshot_id"]},
                ],
            },
        )
        self.assertEqual(updated_material["version"], 2)
        return artifact, round_message, other_message, market_snapshot

    def test_evidence_sources_use_exact_historical_material_and_same_round_messages(self) -> None:
        artifact, round_message, other_message, market_snapshot = (
            self.create_evidence_source_fixture()
        )

        payload = self.store.artifact_evidence_sources("room_plan", artifact["id"])

        self.assertEqual(payload["version"], ARTIFACT_EVIDENCE_SOURCES_VERSION)
        self.assertEqual(payload["artifact_id"], artifact["id"])
        self.assertEqual(payload["round_id"], artifact["round_id"])
        self.assertTrue(payload["authoritative"])
        self.assertEqual(payload["unresolved"], [])
        material_source = next(
            source for source in payload["sources"] if source["type"] == "material"
        )
        self.assertEqual(material_source["version"], 1)
        self.assertEqual(material_source["status"], "superseded")
        self.assertTrue(material_source["exact"])
        self.assertTrue(material_source["source_identity_exact"])
        self.assertTrue(material_source["preview_complete"])
        self.assertTrue(material_source["preview_exact"])
        self.assertIn("首期必须使用低成本原型验证", material_source["preview"])
        self.assertNotIn("第二版内容", material_source["preview"])
        self.assertEqual(material_source["locator"], {
            "material_id": self.material["id"],
            "material_version": 1,
        })
        message_sources = [
            source for source in payload["sources"] if source["type"] == "message"
        ]
        self.assertEqual([source["id"] for source in message_sources], [round_message["id"]])
        self.assertNotIn(other_message["id"], {source["id"] for source in payload["sources"]})
        self.assertEqual(message_sources[0]["sender_name"], "证据研究员")
        self.assertEqual(message_sources[0]["locator"], {"message_id": round_message["id"]})
        self.assertTrue(message_sources[0]["exact"])
        self.assertTrue(message_sources[0]["source_identity_exact"])
        self.assertFalse(message_sources[0]["preview_complete"])
        self.assertFalse(message_sources[0]["preview_exact"])
        self.assertTrue(message_sources[0]["preview_truncated"])
        self.assertTrue(message_sources[0]["preview_redacted"])
        market_sources = [
            source
            for source in payload["sources"]
            if source["type"] == "round_market_snapshot"
        ]
        self.assertEqual(len(market_sources), 1)
        self.assertEqual(market_sources[0]["id"], market_snapshot["snapshot_id"])
        self.assertEqual(
            market_sources[0]["locator"],
            {"snapshot_id": market_snapshot["snapshot_id"]},
        )
        self.assertTrue(market_sources[0]["source_identity_exact"])
        self.assertTrue(market_sources[0]["preview_complete"])
        self.assertNotIn("payload", market_sources[0])
        self.assertLessEqual(
            max(len(str(source.get("preview") or "")) for source in payload["sources"]),
            ARTIFACT_EVIDENCE_SOURCE_PREVIEW_MAX_CHARS,
        )
        self.assertLessEqual(
            sum(len(str(source.get("preview") or "")) for source in payload["sources"]),
            ARTIFACT_EVIDENCE_SOURCE_TOTAL_PREVIEW_MAX_CHARS,
        )
        self.assertNotIn(
            "sk-proj-TESTONLYabcdefghijklmnop",
            json.dumps(payload, ensure_ascii=False),
        )
        self.assertIsNone(
            self.store.artifact_evidence_sources("room_project", artifact["id"])
        )

    def test_evidence_source_preview_fails_closed_when_total_budget_is_exhausted(self) -> None:
        artifact, _round_message, _other_message, _market_snapshot = (
            self.create_evidence_source_fixture()
        )

        with patch("backend.store.ARTIFACT_EVIDENCE_SOURCE_TOTAL_PREVIEW_MAX_CHARS", 0):
            payload = self.store.artifact_evidence_sources("room_plan", artifact["id"])

        market_source = next(
            source
            for source in payload["sources"]
            if source["type"] == "round_market_snapshot"
        )
        self.assertTrue(market_source["exact"])
        self.assertTrue(market_source["source_identity_exact"])
        self.assertEqual(market_source["preview"], "")
        self.assertFalse(market_source["preview_complete"])
        self.assertFalse(market_source["preview_exact"])
        self.assertTrue(market_source["preview_truncated"])
        self.assertTrue(market_source["preview_budget_exhausted"])

    def test_evidence_source_url_redacts_tokenized_sensitive_query_names(self) -> None:
        safe_url = self.store._artifact_evidence_source_url(
            "https://example.com/source?token=TOPSECRET&auth=AUTHSECRET&key=KEYSECRET"
            "&sig=SIGSECRET&session=SESSIONSECRET&jwt=JWTSECRET&code=CODESECRET"
            "&X-Amz-Credential=AWSSECRET&monkey=banana&tokenization=model"
            "&postcode=100000&codec=h264&sessionize=true"
        )

        query = parse_qs(urlsplit(safe_url).query, keep_blank_values=True)
        self.assertEqual(query["token"], ["[REDACTED]"])
        self.assertEqual(query["auth"], ["[REDACTED]"])
        self.assertEqual(query["key"], ["[REDACTED]"])
        self.assertEqual(query["sig"], ["[REDACTED]"])
        self.assertEqual(query["session"], ["[REDACTED]"])
        self.assertEqual(query["jwt"], ["[REDACTED]"])
        self.assertEqual(query["code"], ["[REDACTED]"])
        self.assertEqual(query["X-Amz-Credential"], ["[REDACTED]"])
        self.assertEqual(query["monkey"], ["banana"])
        self.assertEqual(query["tokenization"], ["model"])
        self.assertEqual(query["postcode"], ["100000"])
        self.assertEqual(query["codec"], ["h264"])
        self.assertEqual(query["sessionize"], ["true"])
        self.assertNotIn("TOPSECRET", safe_url)

    def test_full_frozen_source_detail_is_artifact_bound_bounded_and_fail_closed(self) -> None:
        round_row = self.store.create_round("room_plan", "完整冻结来源")
        long_message = self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id="member_long_detail",
            sender_name="长文研究员",
            content="可核验长消息" * 1500,
            round_id=round_row["id"],
            member_version=4,
        )
        oversized_message = self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id="member_oversized_detail",
            sender_name="超长研究员",
            content="x" * (ARTIFACT_EVIDENCE_SOURCE_DETAIL_MAX_BYTES + 100),
            round_id=round_row["id"],
            member_version=2,
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE messages SET content=? WHERE id=?",
                (
                    "x" * (ARTIFACT_EVIDENCE_SOURCE_DETAIL_MAX_BYTES + 100),
                    oversized_message["id"],
                ),
            )
            connection.commit()
        redacted_message = self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id="member_secret_detail",
            sender_name="脱敏研究员",
            content="OPENAI_API_KEY=sk-proj-TESTONLYabcdefghijklmnop\n需要安全显示。",
            round_id=round_row["id"],
            member_version=1,
        )
        market_snapshot = {
            "snapshot_id": "futu_large_frozen_detail",
            "captured_at": "2026-08-03T09:30:00Z",
            "state": "ready",
            "source": "futu_opend_readonly",
            "detail_blob": "m" * 159_000,
            "rows": [{"symbol": "US.MU", "last": 123.45, "quality": "ready"}],
            "evidence": {"version": "storage_market_evidence_v6", "state": "ready"},
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        context, manifest = self.store.material_prompt_bundle("room_plan")
        manifest = self.store.finalize_round_evidence_manifest(
            manifest,
            shared_context=context,
            market_snapshot=market_snapshot,
        )
        self.store.save_round_checkpoint("room_plan", round_row["id"], {
            "member_ids": [],
            "spoken_counts": {},
            "spoken_stances": [],
            "successful_member_ids": [],
            "failed_member_ids": [],
            "completed": 1,
            "next_order": 4,
            "max_turns": 3,
            "shared_context": context,
            "market_snapshot": market_snapshot,
            "round_evidence_manifest": manifest,
        })
        self.store.complete_round(round_row["id"], "COMPLETED")
        artifact = self.store.create_artifact(
            "room_plan",
            round_id=round_row["id"],
            title="完整冻结来源",
            content={"summary": "验证完整冻结来源。", "summary_evidence": []},
        )

        directory = self.store.artifact_evidence_sources("room_plan", artifact["id"])
        market_preview = next(
            source for source in directory["sources"]
            if source["id"] == market_snapshot["snapshot_id"]
        )
        self.assertFalse(market_preview["preview_complete"])
        market_detail = self.store.artifact_evidence_source_detail(
            "room_plan",
            artifact["id"],
            "round_market_snapshot",
            market_snapshot["snapshot_id"],
        )
        self.assertEqual(market_detail["version"], ARTIFACT_EVIDENCE_SOURCE_DETAIL_VERSION)
        self.assertTrue(market_detail["authoritative"])
        self.assertTrue(market_detail["source"]["source_identity_exact"])
        self.assertTrue(market_detail["source"]["preview_complete"])
        self.assertGreater(market_detail["source"]["detail_bytes"], 159_000)
        self.assertLessEqual(
            market_detail["source"]["detail_bytes"],
            ARTIFACT_EVIDENCE_SOURCE_DETAIL_MAX_BYTES,
        )

        long_detail = self.store.artifact_evidence_source_detail(
            "room_plan", artifact["id"], "message", long_message["id"],
        )["source"]
        self.assertTrue(long_detail["preview_complete"])
        self.assertEqual(long_detail["preview"], "可核验长消息" * 1500)

        oversized_detail = self.store.artifact_evidence_source_detail(
            "room_plan", artifact["id"], "message", oversized_message["id"],
        )["source"]
        self.assertFalse(oversized_detail["preview_complete"])
        self.assertTrue(oversized_detail["preview_truncated"])
        self.assertLessEqual(
            len(oversized_detail["preview"].encode("utf-8")),
            ARTIFACT_EVIDENCE_SOURCE_DETAIL_MAX_BYTES,
        )

        redacted_detail = self.store.artifact_evidence_source_detail(
            "room_plan", artifact["id"], "message", redacted_message["id"],
        )["source"]
        self.assertFalse(redacted_detail["preview_complete"])
        self.assertTrue(redacted_detail["preview_redacted"])
        self.assertNotIn("sk-proj-TESTONLY", redacted_detail["preview"])

        with self.assertRaisesRegex(LookupError, "不属于"):
            self.store.artifact_evidence_source_detail(
                "room_plan", artifact["id"], "message", "msg_not_authoritative",
            )
        with self.assertRaisesRegex(ValueError, "仅支持"):
            self.store.artifact_evidence_source_detail(
                "room_plan", artifact["id"], "material", self.material["id"],
            )

        late_message = self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id="member_late_detail",
            sender_name="迟到研究员",
            content="产物创建后的消息不得读取。",
            round_id=round_row["id"],
            member_version=1,
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE messages SET created_at=? WHERE id=?",
                (int(artifact["created_at"]) + 1, late_message["id"]),
            )
            connection.commit()
        with self.assertRaisesRegex(LookupError, "不属于"):
            self.store.artifact_evidence_source_detail(
                "room_plan", artifact["id"], "message", late_message["id"],
            )

    def test_evidence_sources_report_missing_frozen_version_without_current_fallback(self) -> None:
        artifact, _round_message, _other_message, _market_snapshot = (
            self.create_evidence_source_fixture()
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "DELETE FROM material_versions WHERE room_id=? AND material_id=? AND version=1",
                ("room_plan", self.material["id"]),
            )
            connection.commit()

        payload = self.store.artifact_evidence_sources("room_plan", artifact["id"])

        self.assertFalse(any(
            source["type"] == "material" and source["id"] == self.material["id"]
            for source in payload["sources"]
        ))
        unresolved = next(
            source
            for source in payload["unresolved"]
            if source["type"] == "material" and source["id"] == self.material["id"]
        )
        self.assertEqual(unresolved["version"], 1)
        self.assertEqual(unresolved["code"], "MATERIAL_VERSION_MISSING")
        self.assertFalse(unresolved["exact"])

    def test_evidence_sources_fail_closed_for_duplicate_exact_material_version(self) -> None:
        artifact, _round_message, _other_message, _market_snapshot = (
            self.create_evidence_source_fixture()
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            # Simulate a pre-migration database. Current databases reject this
            # duplicate identity before it can be persisted.
            connection.execute("DROP INDEX uq_material_versions_identity")
            connection.execute("DROP TRIGGER trg_material_versions_identity_insert")
            connection.execute("DROP TRIGGER trg_material_versions_identity_update")
            original = connection.execute(
                """SELECT snapshot_json,changed_at FROM material_versions
                   WHERE room_id=? AND material_id=? AND version=1""",
                ("room_plan", self.material["id"]),
            ).fetchone()
            connection.execute(
                """INSERT INTO material_versions(
                       id,material_id,room_id,version,snapshot_json,changed_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    "material_version_duplicate_fixture",
                    self.material["id"],
                    "room_plan",
                    1,
                    original[0],
                    int(original[1]) + 1,
                ),
            )
            connection.commit()

        payload = self.store.artifact_evidence_sources("room_plan", artifact["id"])

        self.assertFalse(any(
            source["type"] == "material" and source["id"] == self.material["id"]
            for source in payload["sources"]
        ))
        unresolved = next(
            source
            for source in payload["unresolved"]
            if source["type"] == "material" and source["id"] == self.material["id"]
        )
        self.assertEqual(unresolved["version"], 1)
        self.assertEqual(unresolved["code"], "MATERIAL_VERSION_AMBIGUOUS")
        self.assertFalse(unresolved["exact"])


class ArtifactHttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_artifacts = http_server.ARTIFACTS
        self.original_store = http_server.STORE
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "http-studio.sqlite3")
        self.artifacts = CapturingArtifactService()
        http_server.ARTIFACTS = self.artifacts
        http_server.STORE = self.store
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        http_server.ARTIFACTS = self.original_artifacts
        http_server.STORE = self.original_store
        self.temp_dir.cleanup()

    def test_generate_route_reuses_frozen_authorized_synthesizer(self) -> None:
        round_row = self.store.create_round("room_plan", "HTTP artifact source")
        self.store.complete_round(round_row["id"], "PARTIAL")
        approved = self.store.enabled_members("room_plan")[0]
        artifact_route = {
            "member_id": str(approved["id"]),
            "member_version": int(approved["version"]),
            "provider": str(approved.get("provider") or "openai").lower(),
            "model": str(approved.get("model") or "") or "resolved-test-model",
        }
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id=f"artifact-http-{round_row['id']}",
            plan_hash="a" * 64,
            max_calls=4,
            skip_provider_ids={"openai"},
            artifact_route=artifact_route,
        )
        ledger.bind_round(round_row["id"])
        request = Request(
            f"{self.base_url}/api/rooms/room_plan/artifacts/generate",
            data=json.dumps({
                "round_id": round_row["id"],
                "synthesizer_member_id": artifact_route["member_id"],
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            method="POST",
        )

        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 201)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            self.artifacts.calls,
            [("room_plan", round_row["id"], artifact_route["member_id"])],
        )
        self.assertEqual(self.artifacts.last_skip_provider_ids, {"openai"})
        self.assertEqual(self.artifacts.last_ledger.run_id, ledger.run_id)
        self.assertEqual(
            self.artifacts.last_frozen_synthesizer_route,
            artifact_route,
        )

    def test_user_decision_response_uses_post_write_snapshot_for_convergence_and_acceptance(self) -> None:
        class DecisionStore:
            def __init__(self, action: str) -> None:
                self.action = action
                self.snapshot = None
                self.paper_portfolio_creations = 0

            def create_artifact_user_decision(
                self,
                room_id: str,
                artifact_id: str,
                *,
                expected_version,
                action,
                rationale,
                created_by: str,
                selected_option_id=None,
                **_tokens,
            ):
                self.action = action
                self.snapshot = {
                    "room": {"id": room_id},
                    "revision": f"post-write-{action}",
                    "decision_action": action,
                    "artifacts": [{
                        "id": artifact_id,
                        "version": expected_version,
                        "user_decision": {"action": action, "rationale": rationale},
                    }],
                }
                return {
                    "id": f"decision-{action}",
                    "artifact_id": artifact_id,
                    "artifact_version": expected_version,
                    "action": action,
                    "rationale": rationale,
                    "created_by": created_by,
                }

            def room_snapshot(self, room_id: str):
                if self.snapshot is None:
                    raise AssertionError("room_snapshot must be read after the user decision write")
                if self.snapshot["room"]["id"] != room_id:
                    raise AssertionError("room_snapshot received the wrong room")
                return self.snapshot

            def create_paper_portfolio(self, *_args, **_kwargs):
                self.paper_portfolio_creations += 1
                raise AssertionError("a user decision must not create a paper portfolio")

        class ConvergenceProbe:
            def __init__(self, store: DecisionStore) -> None:
                self.store = store
                self.result = None

            def evaluate(self, room_id: str, *, snapshot=None):
                if snapshot is not self.store.snapshot:
                    raise AssertionError("convergence did not receive the post-write snapshot")
                self.result = {
                    "room_id": room_id,
                    "decision_action": snapshot["decision_action"],
                    "snapshot_revision": snapshot["revision"],
                }
                return self.result

        class OrchestratorProbe:
            def __init__(self, convergence: ConvergenceProbe) -> None:
                self.convergence = convergence

        class ForbiddenDependency:
            def __getattr__(self, name: str):
                raise AssertionError(f"user-decision route must not access {name}")

        original_orchestrator = http_server.ORCHESTRATOR
        original_acceptance = http_server.storage_sample_acceptance
        original_providers = http_server.PROVIDERS
        original_market = http_server.STORAGE_MARKET
        try:
            for action in ("support", "hold", "return"):
                with self.subTest(action=action):
                    store = DecisionStore(action)
                    convergence = ConvergenceProbe(store)
                    acceptance_calls = []

                    def acceptance_probe(room_id, *, snapshot, convergence_state):
                        if snapshot is not store.snapshot:
                            raise AssertionError("acceptance did not receive the post-write snapshot")
                        if convergence_state is not convergence.result:
                            raise AssertionError("acceptance did not receive the matching convergence result")
                        acceptance_calls.append((snapshot, convergence_state))
                        return {
                            "version": "storage_sample_acceptance_v2",
                            "state": {
                                "support": "review_required",
                                "hold": "deferred",
                                "return": "returned",
                            }[action],
                            "user_decision_action": action,
                            "snapshot_revision": snapshot["revision"],
                            "execution_capability": "none",
                            "live_trading_allowed": False,
                        }

                    http_server.STORE = store
                    http_server.ORCHESTRATOR = OrchestratorProbe(convergence)
                    http_server.storage_sample_acceptance = acceptance_probe
                    http_server.PROVIDERS = ForbiddenDependency()
                    http_server.STORAGE_MARKET = ForbiddenDependency()

                    request = Request(
                        f"{self.base_url}/api/rooms/room_plan/artifacts/artifact-fixture/user-decision",
                        data=json.dumps({
                            "expected_version": 7,
                            "action": action,
                            "rationale": f"{action} rationale",
                            **(
                                {"selected_option_id": "option_fixture"}
                                if action == "support"
                                else {}
                            ),
                        }).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json",
                            "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
                        },
                        method="POST",
                    )
                    with urlopen(request, timeout=3) as response:
                        payload = json.loads(response.read().decode("utf-8"))

                    self.assertEqual(response.status, 200)
                    self.assertTrue(payload["ok"])
                    self.assertEqual(payload["user_decision"]["action"], action)
                    self.assertEqual(payload["convergence"]["decision_action"], action)
                    self.assertEqual(
                        payload["storage_sample_acceptance"]["user_decision_action"],
                        action,
                    )
                    self.assertEqual(
                        payload["storage_sample_acceptance"]["snapshot_revision"],
                        f"post-write-{action}",
                    )
                    self.assertEqual(len(acceptance_calls), 1)
                    self.assertEqual(store.paper_portfolio_creations, 0)
        finally:
            http_server.STORE = self.store
            http_server.ORCHESTRATOR = original_orchestrator
            http_server.storage_sample_acceptance = original_acceptance
            http_server.PROVIDERS = original_providers
            http_server.STORAGE_MARKET = original_market

    def test_evidence_sources_route_reads_frozen_checkpoint_without_live_market_call(self) -> None:
        round_row = self.store.create_round("room_plan", "只读冻结快照接口")
        market_snapshot = {
            "snapshot_id": "futu_http_frozen",
            "captured_at": "2026-07-31T20:00:00Z",
            "state": "ready",
            "source": "futu_opend_readonly",
            "rows": [{"symbol": "US.MU", "last": 120.5, "quality": "ready"}],
            "evidence": {
                "version": "storage_market_evidence_v6",
                "state": "ready",
            },
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        context, manifest = self.store.material_prompt_bundle("room_plan")
        manifest = self.store.finalize_round_evidence_manifest(
            manifest,
            shared_context=context,
            market_snapshot=market_snapshot,
        )
        self.store.save_round_checkpoint("room_plan", round_row["id"], {
            "member_ids": [],
            "spoken_counts": {},
            "spoken_stances": [],
            "successful_member_ids": [],
            "failed_member_ids": [],
            "completed": 1,
            "next_order": 2,
            "max_turns": 1,
            "shared_context": context,
            "market_snapshot": market_snapshot,
            "round_evidence_manifest": manifest,
        })
        self.store.complete_round(round_row["id"], "COMPLETED")
        artifact = self.store.create_artifact(
            "room_plan",
            round_id=round_row["id"],
            title="HTTP 冻结来源",
            content={
                "summary": "读取该轮已冻结的市场证据。",
                "summary_evidence": [{
                    "type": "round_market_snapshot",
                    "id": market_snapshot["snapshot_id"],
                }],
            },
        )

        class NeverCallLiveMarket:
            def __init__(self) -> None:
                self.calls = 0

            def snapshot(self, **_kwargs):
                self.calls += 1
                raise AssertionError("冻结来源接口不得请求实时市场")

        original_market = http_server.STORAGE_MARKET
        never_live = NeverCallLiveMarket()
        http_server.STORAGE_MARKET = never_live
        try:
            request = Request(
                f"{self.base_url}/api/rooms/room_plan/artifacts/{artifact['id']}/evidence-sources",
                headers={"X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN},
                method="GET",
            )
            with urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            http_server.STORAGE_MARKET = original_market

        self.assertEqual(response.status, 200)
        self.assertEqual(never_live.calls, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"], ARTIFACT_EVIDENCE_SOURCES_VERSION)
        self.assertEqual(payload["artifact_id"], artifact["id"])
        self.assertEqual(payload["round_id"], round_row["id"])
        self.assertTrue(payload["authoritative"])
        self.assertEqual(payload["unresolved"], [])
        self.assertEqual(len(payload["sources"]), 1)
        source = payload["sources"][0]
        self.assertEqual(source["type"], "round_market_snapshot")
        self.assertEqual(source["id"], market_snapshot["snapshot_id"])
        self.assertEqual(source["snapshot_id"], market_snapshot["snapshot_id"])
        self.assertEqual(source["round_id"], round_row["id"])
        self.assertEqual(source["source_revision"], "storage_market_evidence_v6")
        self.assertEqual(
            source["source_snapshot_sha256"],
            self.store._canonical_sha256(market_snapshot),
        )
        self.assertTrue(source["exact"])
        self.assertTrue(source["preview_exact"])
        self.assertEqual(source["locator"], {"snapshot_id": market_snapshot["snapshot_id"]})
        self.assertNotIn("payload", source)
        self.assertIn(market_snapshot["snapshot_id"], source["preview"])
        self.assertEqual(source["execution_capability"], "none")
        self.assertFalse(source["live_trading_allowed"])

    def test_full_frozen_market_source_route_never_calls_live_market(self) -> None:
        round_row = self.store.create_round("room_plan", "HTTP 完整冻结来源")
        market_snapshot = {
            "snapshot_id": "futu_http_large_frozen",
            "captured_at": "2026-08-03T09:30:00Z",
            "state": "ready",
            "source": "futu_opend_readonly",
            "detail_blob": "m" * 159_000,
            "rows": [{"symbol": "US.MU", "last": 123.45, "quality": "ready"}],
            "evidence": {"version": "storage_market_evidence_v6", "state": "ready"},
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        context, manifest = self.store.material_prompt_bundle("room_plan")
        manifest = self.store.finalize_round_evidence_manifest(
            manifest,
            shared_context=context,
            market_snapshot=market_snapshot,
        )
        self.store.save_round_checkpoint("room_plan", round_row["id"], {
            "member_ids": [],
            "spoken_counts": {},
            "spoken_stances": [],
            "successful_member_ids": [],
            "failed_member_ids": [],
            "completed": 1,
            "next_order": 1,
            "max_turns": 1,
            "shared_context": context,
            "market_snapshot": market_snapshot,
            "round_evidence_manifest": manifest,
        })
        self.store.complete_round(round_row["id"], "COMPLETED")
        artifact = self.store.create_artifact(
            "room_plan",
            round_id=round_row["id"],
            title="HTTP 完整冻结来源",
            content={"summary": "只读完整冻结快照。", "summary_evidence": []},
        )

        class NeverCallLiveMarket:
            def __init__(self) -> None:
                self.calls = 0

            def snapshot(self, **_kwargs):
                self.calls += 1
                raise AssertionError("完整冻结来源接口不得请求实时市场")

        original_market = http_server.STORAGE_MARKET
        never_live = NeverCallLiveMarket()
        http_server.STORAGE_MARKET = never_live
        try:
            request = Request(
                f"{self.base_url}/api/rooms/room_plan/artifacts/{artifact['id']}"
                f"/evidence-sources/round_market_snapshot/{market_snapshot['snapshot_id']}",
                headers={"X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN},
                method="GET",
            )
            with urlopen(request, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            http_server.STORAGE_MARKET = original_market

        self.assertEqual(response.status, 200)
        self.assertEqual(never_live.calls, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["version"], ARTIFACT_EVIDENCE_SOURCE_DETAIL_VERSION)
        self.assertEqual(payload["artifact_id"], artifact["id"])
        self.assertEqual(payload["round_id"], round_row["id"])
        self.assertTrue(payload["source"]["preview_complete"])
        self.assertGreater(payload["source"]["detail_bytes"], 159_000)
        self.assertIn('"snapshot_id":"futu_http_large_frozen"', payload["source"]["preview"])


if __name__ == "__main__":
    unittest.main()
