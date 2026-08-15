from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.store import StudioStore


class MemberLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        created = self.store.create_room(
            "成员生命周期隔离测试",
            "验证版本、归档、恢复和顺序并发合同",
            template_id="open_collaboration",
        )
        self.room_id = str(created["room"]["id"])

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _rows_for_member(self, table: str, member_id: str) -> int:
        allowed = {
            "member_versions",
            "message_mentions",
            "chat_request_targets",
            "chat_request_attempts",
        }
        if table not in allowed:
            raise AssertionError(f"unexpected table: {table}")
        with closing(sqlite3.connect(self.store.path)) as connection:
            return int(connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE member_id=?",
                (member_id,),
            ).fetchone()[0])

    def _foreign_key_violations(self) -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(self.store.path)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            return [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]

    def test_expected_version_rejects_stale_overwrite_and_preserves_version_history(self) -> None:
        original = self.store.room_snapshot(self.room_id)["members"][0]

        updated = self.store.update_member(
            self.room_id,
            original["id"],
            {
                "identity": "版本二身份",
                "responsibilities": "只允许当前版本完成这次修改。",
            },
            expected_version=original["version"],
        )

        self.assertEqual(updated["version"], original["version"] + 1)
        self.assertEqual(updated["identity"], "版本二身份")
        versions_after_success = self._rows_for_member(
            "member_versions",
            original["id"],
        )
        with self.assertRaisesRegex(ValueError, "版本"):
            self.store.update_member(
                self.room_id,
                original["id"],
                {
                    "identity": "过期编辑不得覆盖",
                    "provider": "stale-provider",
                },
                expected_version=original["version"],
            )

        current = self.store.get_member(self.room_id, original["id"])
        frozen_v1 = self.store.get_member_version(
            self.room_id,
            original["id"],
            original["version"],
        )
        frozen_v2 = self.store.get_member_version(
            self.room_id,
            original["id"],
            updated["version"],
        )
        self.assertEqual(current["version"], updated["version"])
        self.assertEqual(current["identity"], "版本二身份")
        self.assertNotEqual(current["provider"], "stale-provider")
        self.assertEqual(frozen_v1["identity"], original["identity"])
        self.assertEqual(frozen_v2["identity"], "版本二身份")
        self.assertEqual(
            self._rows_for_member("member_versions", original["id"]),
            versions_after_success,
        )
        unchanged = self.store.update_member(
            self.room_id,
            original["id"],
            {"identity": "版本二身份"},
            expected_version=updated["version"],
        )
        self.assertEqual(unchanged["version"], updated["version"])
        self.assertEqual(
            self._rows_for_member("member_versions", original["id"]),
            versions_after_success,
        )
        self.assertEqual(self._foreign_key_violations(), [])

    def test_archive_and_restore_preserve_structured_mention_history_and_foreign_keys(self) -> None:
        target = self.store.room_snapshot(self.room_id)["members"][0]
        mention_version = int(target["version"])
        routed = self.store.create_user_message_request(
            self.room_id,
            content=f"@{target['name']} 请保留这条历史点名关系",
            mentions=[{
                "member_id": target["id"],
                "expected_member_version": mention_version,
            }],
            client_message_id="member-life-mention-0001",
        )
        request_id = str(routed["routing"]["request_id"])
        claim_token = self.store.claim_chat_target(
            self.room_id,
            request_id,
            target["id"],
            lease_owner="member-lifecycle-test",
        )
        self.assertTrue(claim_token)
        relation_counts = {
            table: self._rows_for_member(table, target["id"])
            for table in (
                "message_mentions",
                "chat_request_targets",
                "chat_request_attempts",
            )
        }
        self.assertEqual(relation_counts, {
            "message_mentions": 1,
            "chat_request_targets": 1,
            "chat_request_attempts": 1,
        })

        revised = self.store.update_member(
            self.room_id,
            target["id"],
            {"identity": "点名后更新的当前身份"},
            expected_version=mention_version,
        )
        archived = self.store.archive_member(
            self.room_id,
            target["id"],
            expected_version=revised["version"],
        )

        self.assertTrue(archived["archived"])
        self.assertGreater(archived["archived_at"], 0)
        self.assertFalse(archived["enabled"])
        self.assertEqual(archived["version"], revised["version"] + 1)
        snapshot = self.store.room_snapshot(self.room_id)
        self.assertNotIn(target["id"], [member["id"] for member in snapshot["members"]])
        self.assertIn(target["id"], [member["id"] for member in snapshot["archived_members"]])
        source_message = next(
            message for message in snapshot["messages"]
            if message["id"] == routed["message"]["id"]
        )
        self.assertEqual(len(source_message["mentions"]), 1)
        self.assertEqual(source_message["mentions"][0]["member_id"], target["id"])
        self.assertEqual(source_message["mentions"][0]["member_version"], mention_version)
        self.assertEqual(source_message["mentions"][0]["identity"], target["identity"])
        request_after_archive = self.store.get_chat_request(self.room_id, request_id)
        self.assertEqual(request_after_archive["targets"][0]["member_id"], target["id"])
        self.assertEqual(
            request_after_archive["targets"][0]["member_version"],
            mention_version,
        )
        for table, expected_count in relation_counts.items():
            self.assertEqual(
                self._rows_for_member(table, target["id"]),
                expected_count,
            )
        self.assertEqual(self._foreign_key_violations(), [])

        with self.assertRaisesRegex(ValueError, "版本"):
            self.store.restore_member(
                self.room_id,
                target["id"],
                expected_version=revised["version"],
            )
        restored = self.store.restore_member(
            self.room_id,
            target["id"],
            expected_version=archived["version"],
        )
        self.assertFalse(restored["archived"])
        self.assertEqual(restored["archived_at"], 0)
        self.assertTrue(restored["enabled"])
        self.assertEqual(restored["version"], archived["version"] + 1)
        restored_snapshot = self.store.room_snapshot(self.room_id)
        restored_source = next(
            message for message in restored_snapshot["messages"]
            if message["id"] == routed["message"]["id"]
        )
        self.assertEqual(restored_source["mentions"][0]["member_id"], target["id"])
        self.assertEqual(restored_source["mentions"][0]["member_version"], mention_version)
        self.assertEqual(restored_source["mentions"][0]["identity"], target["identity"])
        for table, expected_count in relation_counts.items():
            self.assertEqual(
                self._rows_for_member(table, target["id"]),
                expected_count,
            )
        self.assertEqual(self._foreign_key_violations(), [])

    def test_restore_preserves_the_pre_archive_participation_state(self) -> None:
        target = self.store.room_snapshot(self.room_id)["members"][1]
        disabled = self.store.update_member(
            self.room_id,
            target["id"],
            {"enabled": False},
            expected_version=target["version"],
        )
        archived = self.store.archive_member(
            self.room_id,
            target["id"],
            expected_version=disabled["version"],
        )
        restored = self.store.restore_member(
            self.room_id,
            target["id"],
            expected_version=archived["version"],
        )

        self.assertFalse(restored["archived"])
        self.assertFalse(restored["enabled"])
        self.assertEqual(restored["version"], archived["version"] + 1)
        self.assertEqual(self._foreign_key_violations(), [])

    def test_reorder_uses_compare_and_swap_and_rejects_duplicate_or_incomplete_sets(self) -> None:
        original_members = self.store.room_snapshot(self.room_id)["members"]
        original_ids = [str(member["id"]) for member in original_members]
        original_versions = {
            str(member["id"]): int(member["version"])
            for member in original_members
        }
        desired_ids = list(reversed(original_ids))

        reordered = self.store.reorder_members(
            self.room_id,
            desired_ids,
            expected_member_ids=original_ids,
        )
        self.assertEqual([member["id"] for member in reordered], desired_ids)
        self.assertEqual(
            {str(member["id"]): int(member["version"]) for member in reordered},
            original_versions,
        )

        with self.assertRaisesRegex(ValueError, "已变化"):
            self.store.reorder_members(
                self.room_id,
                original_ids,
                expected_member_ids=original_ids,
            )
        with self.assertRaisesRegex(ValueError, "重复"):
            self.store.reorder_members(
                self.room_id,
                [desired_ids[0], desired_ids[0], *desired_ids[2:]],
                expected_member_ids=desired_ids,
            )
        with self.assertRaisesRegex(ValueError, "完整"):
            self.store.reorder_members(
                self.room_id,
                desired_ids[:-1],
                expected_member_ids=desired_ids,
            )
        with self.assertRaisesRegex(ValueError, "完整"):
            self.store.reorder_members(
                self.room_id,
                [*desired_ids[:-1], "member_unknown"],
                expected_member_ids=desired_ids,
            )

        final_members = self.store.room_snapshot(self.room_id)["members"]
        self.assertEqual([member["id"] for member in final_members], desired_ids)
        self.assertEqual(
            {str(member["id"]): int(member["version"]) for member in final_members},
            original_versions,
        )
        self.assertEqual(self._foreign_key_violations(), [])

    def test_restore_appends_after_active_reorder_with_unique_positions(self) -> None:
        members = self.store.room_snapshot(self.room_id)["members"]
        target = members[1]
        archived = self.store.archive_member(
            self.room_id,
            target["id"],
            expected_version=target["version"],
        )
        active_ids = [
            member["id"]
            for member in self.store.room_snapshot(self.room_id)["members"]
        ]
        self.store.reorder_members(
            self.room_id,
            list(reversed(active_ids)),
            expected_member_ids=active_ids,
        )

        restored = self.store.restore_member(
            self.room_id,
            target["id"],
            expected_version=archived["version"],
        )
        final_members = self.store.room_snapshot(self.room_id)["members"]
        positions = [int(member["position"]) for member in final_members]

        self.assertEqual(final_members[-1]["id"], restored["id"])
        self.assertEqual(len(positions), len(set(positions)))
        self.assertEqual(positions, list(range(1, len(final_members) + 1)))
        self.assertEqual(self._foreign_key_violations(), [])

    def test_archive_and_restore_are_blocked_during_an_active_round(self) -> None:
        target = self.store.room_snapshot(self.room_id)["members"][0]
        active_round = self.store.create_round(self.room_id, "冻结成员生命周期")

        with self.assertRaisesRegex(ValueError, "当前轮次"):
            self.store.archive_member(
                self.room_id,
                target["id"],
                expected_version=target["version"],
            )

        self.store.complete_round(active_round["id"], "COMPLETED")
        archived = self.store.archive_member(
            self.room_id,
            target["id"],
            expected_version=target["version"],
        )
        with closing(sqlite3.connect(self.store.path)) as connection, connection:
            connection.execute(
                "UPDATE rounds SET status='PAUSED' WHERE id=?",
                (active_round["id"],),
            )
        with self.assertRaisesRegex(ValueError, "当前轮次"):
            self.store.restore_member(
                self.room_id,
                target["id"],
                expected_version=archived["version"],
            )
        self.assertEqual(self._foreign_key_violations(), [])

    def test_explicit_moderator_must_be_reassigned_before_pause_or_archive(self) -> None:
        snapshot = self.store.room_snapshot(self.room_id)
        moderator = snapshot["members"][0]
        room = self.store.update_room(self.room_id, {
            "expected_settings_version": snapshot["room"]["settings_version"],
            "moderator_member_id": moderator["id"],
        })

        with self.assertRaisesRegex(ValueError, "当前动态主持"):
            self.store.update_member(
                self.room_id,
                moderator["id"],
                {"enabled": False},
                expected_version=moderator["version"],
            )
        with self.assertRaisesRegex(ValueError, "当前动态主持"):
            self.store.archive_member(
                self.room_id,
                moderator["id"],
                expected_version=moderator["version"],
            )

        self.store.update_room(self.room_id, {
            "expected_settings_version": room["settings_version"],
            "moderator_member_id": "",
        })
        paused = self.store.update_member(
            self.room_id,
            moderator["id"],
            {"enabled": False},
            expected_version=moderator["version"],
        )
        archived = self.store.archive_member(
            self.room_id,
            moderator["id"],
            expected_version=paused["version"],
        )

        self.assertFalse(paused["enabled"])
        self.assertTrue(archived["archived"])
        self.assertEqual(self._foreign_key_violations(), [])


if __name__ == "__main__":
    unittest.main()
