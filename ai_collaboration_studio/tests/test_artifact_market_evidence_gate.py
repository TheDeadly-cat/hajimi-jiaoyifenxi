from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.artifact_service import ArtifactService
from backend.providers.base import ProviderResponse
from backend.store import StudioStore


class OmitMarketMinutesProvider:
    def __init__(self, message_id: str) -> None:
        self.message_id = message_id
        self.provider_id = ""

    @staticmethod
    def status() -> dict[str, bool]:
        return {"configured": True}

    def generate_json(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
    ) -> ProviderResponse:
        del instructions, input_text
        return ProviderResponse(
            ok=True,
            provider=self.provider_id,
            model=model or "fixture",
            content=json.dumps(
                {
                    "summary": "本轮形成了只读研究摘要。",
                    "summary_evidence": [
                        {"type": "message", "id": self.message_id}
                    ],
                    "conclusions": [],
                    "disagreements": [],
                    "unknowns": [],
                    "actions": [],
                    "decision": {
                        "status": "undecided",
                        "options": [],
                        "preferred_option_id": "",
                        "rationale": "",
                        "evidence": [],
                    },
                },
                ensure_ascii=False,
            ),
        )

    generate = generate_json


class SingleProviderRegistry:
    def __init__(self, provider: OmitMarketMinutesProvider) -> None:
        self.provider = provider

    def get(self, provider_id: str) -> OmitMarketMinutesProvider:
        self.provider.provider_id = provider_id
        return self.provider


class ArtifactMarketEvidenceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def _checked_message(message_id: str) -> dict[str, str]:
        return {
            "type": "message",
            "id": message_id,
            "evidence_role": "support",
            "verification_status": "source_checked",
        }

    def _artifact_content(self, message_id: str) -> dict:
        return {
            "summary": "只读研究使用本轮冻结证据形成摘要。",
            "summary_evidence": [self._checked_message(message_id)],
            "conclusions": [],
            "disagreements": [],
            "unknowns": [],
            "actions": [],
            "decision": {
                "status": "undecided",
                "options": [],
                "preferred_option_id": "",
                "rationale": "",
                "evidence": [],
            },
        }

    def _create_round(
        self,
        *,
        snapshot_id: str = "",
    ) -> tuple[dict, dict, dict | None]:
        round_row = self.store.create_round(
            "room_plan",
            "验证冻结市场证据确认门",
        )
        room_snapshot = self.store.room_snapshot("room_plan")
        members = room_snapshot["members"]
        messages = [
            self.store.add_message(
                "room_plan",
                sender_type="ai",
                sender_id=member["id"],
                sender_name=member["name"],
                identity=member["identity"],
                provider=member["provider"],
                model=member["model"],
                content=f"{member['name']} 已按冻结身份复核本轮输入。",
                round_id=round_row["id"],
                member_version=member["version"],
            )
            for member in members
        ]
        message = messages[0]
        market_snapshot = None
        if snapshot_id:
            market_snapshot = {
                "snapshot_id": snapshot_id,
                "captured_at": "2026-08-01T00:00:00Z",
                "state": "ready",
                "source": "futu_opend",
                "symbols": ["US.MU", "US.SNDK", "US.WDC", "US.STX"],
                "rows": [
                    {
                        "symbol": symbol,
                        "last": 100.0 + index,
                        "market_time": "2026-07-31 16:00:00",
                        "quality": "ready",
                    }
                    for index, symbol in enumerate(
                        ("US.MU", "US.SNDK", "US.WDC", "US.STX")
                    )
                ],
                "missing_symbols": [],
                "source_errors": [],
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
        self.store.save_round_checkpoint(
            "room_plan",
            round_row["id"],
            {
                "member_ids": [member["id"] for member in members],
                "spoken_counts": {member["id"]: 1 for member in members},
                "spoken_stances": [member["stance"] for member in members],
                "successful_member_ids": [member["id"] for member in members],
                "failed_member_ids": [],
                "previous_name": members[-1]["name"],
                "completed": len(members),
                "failures": 0,
                "skipped": 0,
                "proposals_created": 0,
                "next_order": len(members) + 1,
                "max_turns": len(members),
                "workflow_policy": room_snapshot["room"]["workflow_policy"],
                "capability_pack_ids": room_snapshot["room"].get("capability_pack_ids") or [],
                "shared_context": context,
                "market_snapshot": market_snapshot,
                "frozen_market": (
                    {
                        "present": True,
                        "ready": True,
                        "state": "ready",
                        "snapshot_id": snapshot_id,
                        "captured_at": market_snapshot["captured_at"],
                    }
                    if market_snapshot
                    else None
                ),
                "round_evidence_manifest": manifest,
            },
        )
        self.store.complete_round(round_row["id"], "COMPLETED")
        return round_row, message, market_snapshot

    @staticmethod
    def _market_refs(artifact: dict) -> list[dict]:
        return [
            ref
            for ref in artifact["content"]["summary_evidence"]
            if ref.get("type") == "round_market_snapshot"
        ]

    def _create_market_artifact(self, snapshot_id: str) -> tuple[dict, dict, dict]:
        round_row, message, market_snapshot = self._create_round(
            snapshot_id=snapshot_id,
        )
        artifact = self.store.create_artifact(
            "room_plan",
            round_id=round_row["id"],
            title="冻结市场证据门测试",
            content=self._artifact_content(message["id"]),
        )
        assert artifact is not None and market_snapshot is not None
        return artifact, round_row, market_snapshot

    def test_store_auto_injects_exact_unreviewed_round_market_snapshot(self) -> None:
        artifact, round_row, market_snapshot = self._create_market_artifact(
            "futu_gate_store",
        )

        refs = self._market_refs(artifact)
        self.assertEqual(len(refs), 1)
        reference = refs[0]
        self.assertEqual(reference["id"], market_snapshot["snapshot_id"])
        self.assertEqual(reference["round_id"], round_row["id"])
        self.assertEqual(reference["source_revision"], "storage_market_evidence_v6")
        self.assertEqual(
            reference["source_snapshot_sha256"],
            self.store._canonical_sha256(market_snapshot),
        )
        self.assertEqual(reference["evidence_role"], "context")
        self.assertEqual(reference["verification_status"], "unreviewed")
        self.assertEqual(reference["execution_capability"], "none")
        self.assertFalse(reference["live_trading_allowed"])
        self.assertIn(
            "本轮冻结市场快照证据尚未核验",
            artifact["evidence_review"]["confirmation_issues"],
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            stored = connection.execute(
                """SELECT source_id,source_revision,source_snapshot_sha256,
                          verification_status
                     FROM artifact_evidence
                    WHERE artifact_id=? AND source_type='round_market_snapshot'""",
                (artifact["id"],),
            ).fetchone()
        self.assertEqual(
            stored,
            (
                market_snapshot["snapshot_id"],
                "storage_market_evidence_v6",
                self.store._canonical_sha256(market_snapshot),
                "unreviewed",
            ),
        )

    def test_model_omission_is_filled_without_preverification(self) -> None:
        round_row, message, market_snapshot = self._create_round(
            snapshot_id="futu_gate_model",
        )
        provider = OmitMarketMinutesProvider(message["id"])
        artifact = ArtifactService(
            self.store,
            SingleProviderRegistry(provider),
        ).generate_minutes("room_plan", round_row["id"])

        refs = self._market_refs(artifact)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["id"], market_snapshot["snapshot_id"])
        self.assertEqual(refs[0]["verification_status"], "unreviewed")

    def test_removing_required_reference_reinjects_it_and_blocks_confirmation(self) -> None:
        artifact, _round_row, _market_snapshot = self._create_market_artifact(
            "futu_gate_remove",
        )
        content_without_market = {
            **artifact["content"],
            "summary_evidence": [
                ref
                for ref in artifact["content"]["summary_evidence"]
                if ref.get("type") != "round_market_snapshot"
            ],
        }
        revised = self.store.update_artifact(
            "room_plan",
            artifact["id"],
            {
                "expected_version": artifact["version"],
                "content": content_without_market,
            },
        )
        assert revised is not None

        refs = self._market_refs(revised)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["verification_status"], "unreviewed")
        with self.assertRaisesRegex(ValueError, "冻结市场快照证据尚未核验"):
            self.store.confirm_artifact(
                "room_plan",
                artifact["id"],
                expected_version=revised["version"],
            )
        unchanged = self.store.get_artifact("room_plan", artifact["id"])
        self.assertEqual(unchanged["status"], "DRAFT")
        self.assertEqual(unchanged["version"], revised["version"])

    def test_source_checked_or_corroborated_market_reference_can_confirm(self) -> None:
        for verification_status in ("source_checked", "corroborated"):
            with self.subTest(verification_status=verification_status):
                artifact, _round_row, _market_snapshot = self._create_market_artifact(
                    f"futu_gate_{verification_status}",
                )
                reviewed_content = {
                    **artifact["content"],
                    "summary_evidence": [
                        {
                            **ref,
                            "verification_status": verification_status,
                        }
                        if ref.get("type") == "round_market_snapshot"
                        else ref
                        for ref in artifact["content"]["summary_evidence"]
                    ],
                }
                revised = self.store.update_artifact(
                    "room_plan",
                    artifact["id"],
                    {
                        "expected_version": artifact["version"],
                        "content": reviewed_content,
                    },
                )
                confirmed = self.store.confirm_artifact(
                    "room_plan",
                    artifact["id"],
                    expected_version=revised["version"],
                )
                self.assertEqual(confirmed["status"], "CONFIRMED")

    def test_round_without_market_snapshot_is_unchanged(self) -> None:
        round_row, message, _market_snapshot = self._create_round()
        artifact = self.store.create_artifact(
            "room_plan",
            round_id=round_row["id"],
            title="通用冻结轮次",
            content=self._artifact_content(message["id"]),
        )
        assert artifact is not None

        self.assertEqual(self._market_refs(artifact), [])
        self.assertTrue(artifact["evidence_review"]["confirmation_ready"])
        confirmed = self.store.confirm_artifact(
            "room_plan",
            artifact["id"],
            expected_version=artifact["version"],
        )
        self.assertEqual(confirmed["status"], "CONFIRMED")

    def test_legacy_missing_reference_is_reported_without_read_time_rewrite(self) -> None:
        artifact, _round_row, _market_snapshot = self._create_market_artifact(
            "futu_gate_legacy",
        )
        legacy_content = {
            **artifact["content"],
            "summary_evidence": [
                ref
                for ref in artifact["content"]["summary_evidence"]
                if ref.get("type") != "round_market_snapshot"
            ],
        }
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE artifacts SET content_json=? WHERE id=?",
                (json.dumps(legacy_content, ensure_ascii=False), artifact["id"]),
            )
            connection.execute(
                "DELETE FROM artifact_evidence WHERE artifact_id=? AND source_type='round_market_snapshot'",
                (artifact["id"],),
            )
            connection.commit()
        before_bytes = self.db_path.read_bytes()

        loaded = self.store.get_artifact("room_plan", artifact["id"])

        self.assertEqual(before_bytes, self.db_path.read_bytes())
        self.assertEqual(loaded["status"], "DRAFT")
        self.assertEqual(loaded["version"], artifact["version"])
        self.assertEqual(self._market_refs(loaded), [])
        self.assertIn(
            "产物缺少本轮冻结市场快照证据",
            loaded["evidence_review"]["confirmation_issues"],
        )
        with self.assertRaisesRegex(ValueError, "冻结市场快照证据尚未核验"):
            self.store.confirm_artifact(
                "room_plan",
                artifact["id"],
                expected_version=artifact["version"],
            )
        with closing(sqlite3.connect(self.db_path)) as connection:
            stored = connection.execute(
                "SELECT status,version,content_json FROM artifacts WHERE id=?",
                (artifact["id"],),
            ).fetchone()
        self.assertEqual(stored[0], "DRAFT")
        self.assertEqual(stored[1], artifact["version"])
        self.assertEqual(json.loads(stored[2]), legacy_content)


if __name__ == "__main__":
    unittest.main()
