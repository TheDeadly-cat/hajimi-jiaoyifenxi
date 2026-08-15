from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
import os
from pathlib import Path

from backend.instance_ownership import DatabaseInstanceOwner, InstanceAlreadyRunning
from backend.store import StudioStore


class DatabaseInstanceOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "runtime" / "studio.sqlite3"
        self.database_path.parent.mkdir(parents=True)
        self.database_path.touch()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_same_database_has_one_owner_and_stale_lock_file_is_reusable(self) -> None:
        first = DatabaseInstanceOwner(self.database_path).acquire()
        second = DatabaseInstanceOwner(self.database_path.parent / "." / self.database_path.name)
        try:
            with self.assertRaises(InstanceAlreadyRunning):
                second.acquire()
            first.assert_held_for(self.database_path)
            with self.assertRaises(RuntimeError):
                first.assert_held_for(self.database_path.parent / "other.sqlite3")
        finally:
            first.release()

        self.assertTrue(first.lock_path.exists())
        replacement = DatabaseInstanceOwner(self.database_path).acquire()
        replacement.release()
        replacement.release()

    def test_owner_lock_rejects_symlink_or_hardlink_without_writing_target(self) -> None:
        target = self.database_path.parent / "unrelated-owner-target.bin"
        target.write_bytes(b"sentinel")
        lock_path = self.database_path.with_name(
            f"{self.database_path.name}.owner.lock"
        )

        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                if lock_path.exists() or lock_path.is_symlink():
                    lock_path.unlink()
                if kind == "symlink":
                    try:
                        os.symlink(target, lock_path)
                    except OSError as exc:  # pragma: no cover - Windows policy dependent
                        self.skipTest(f"symlinks unavailable in system temp: {exc}")
                else:
                    try:
                        os.link(target, lock_path)
                    except OSError as exc:  # pragma: no cover - unusual filesystem
                        self.skipTest(f"hard links unavailable in system temp: {exc}")

                with self.assertRaisesRegex(RuntimeError, "owner lock"):
                    DatabaseInstanceOwner(self.database_path).acquire()
                self.assertEqual(target.read_bytes(), b"sentinel")
                lock_path.unlink()

    def test_owner_rejects_database_path_alias_before_creating_owner_lock(self) -> None:
        target = self.database_path.parent / "real-database.sqlite3"
        target.write_bytes(b"database")
        for kind in ("symlink", "hardlink"):
            with self.subTest(kind=kind):
                alias = self.database_path.parent / f"alias-{kind}.sqlite3"
                if alias.exists() or alias.is_symlink():
                    alias.unlink()
                try:
                    if kind == "symlink":
                        os.symlink(target, alias)
                    else:
                        os.link(target, alias)
                except OSError as exc:  # pragma: no cover - filesystem dependent
                    self.skipTest(f"{kind} unavailable in system temp: {exc}")
                with self.assertRaisesRegex(RuntimeError, "Database path"):
                    DatabaseInstanceOwner(alias)
                self.assertFalse(
                    alias.with_name(f"{alias.name}.owner.lock").exists()
                )
                alias.unlink()

    def test_owner_rejects_database_parent_alias_before_creating_owner_lock(self) -> None:
        parent_alias = self.database_path.parent / "database-parent-symlink"
        try:
            os.symlink(self.database_path.parent, parent_alias, target_is_directory=True)
        except OSError as exc:  # pragma: no cover - Windows without symlink rights
            self.skipTest(f"directory symlinks unavailable in system temp: {exc}")
        alias = parent_alias / self.database_path.name
        try:
            with self.assertRaisesRegex(RuntimeError, "Database path"):
                DatabaseInstanceOwner(alias)
            self.assertFalse(
                alias.with_name(f"{alias.name}.owner.lock").exists()
            )
        finally:
            parent_alias.unlink()

    def test_abrupt_process_exit_releases_operating_system_lock(self) -> None:
        script = (
            "import sys,time; "
            "from backend.instance_ownership import DatabaseInstanceOwner; "
            "owner=DatabaseInstanceOwner(sys.argv[1]).acquire(); "
            "print('locked', flush=True); time.sleep(60)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(self.database_path)],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaises(InstanceAlreadyRunning):
                DatabaseInstanceOwner(self.database_path).acquire()
        finally:
            process.kill()
            process.communicate(timeout=10)

        replacement = DatabaseInstanceOwner(self.database_path).acquire()
        replacement.release()

    def test_orphan_recovery_requires_matching_live_owner(self) -> None:
        store = StudioStore(self.database_path)
        round_row = store.create_round("room_plan", "所有权测试中的运行轮次")
        snapshot = store.room_snapshot("room_plan")
        store.save_round_checkpoint("room_plan", round_row["id"], {
            "member_ids": [snapshot["members"][0]["id"]],
            "workflow_policy": snapshot["room"]["workflow_policy"],
            "capability_pack_ids": snapshot["room"]["capability_pack_ids"],
            "next_order": 1,
            "max_turns": 1,
        })
        moderator = store.enabled_members("room_plan")[0]
        store.begin_director_attempt(
            "room_plan",
            str(round_row["id"]),
            moderator_member_id=str(moderator["id"]),
            moderator_member_version=int(moderator["version"]),
            provider=str(moderator["provider"]),
            model=str(moderator["model"]),
        )
        wrong_path = self.database_path.parent / "other.sqlite3"
        wrong_path.touch()
        wrong_owner = DatabaseInstanceOwner(wrong_path).acquire()
        try:
            with self.assertRaises(RuntimeError):
                store.recover_orphaned_work(instance_owner=wrong_owner)
        finally:
            wrong_owner.release()
        self.assertEqual(store.get_round("room_plan", round_row["id"])["status"], "RUNNING")

        released_owner = DatabaseInstanceOwner(self.database_path).acquire()
        released_owner.release()
        with self.assertRaises(RuntimeError):
            store.recover_orphaned_work(instance_owner=released_owner)

        with DatabaseInstanceOwner(self.database_path) as owner:
            recovery = store.recover_orphaned_work(instance_owner=owner)
        self.assertEqual(recovery["paused_rounds"], 1)
        self.assertEqual(recovery["cancelled_rounds"], 0)
        self.assertEqual(store.get_round("room_plan", round_row["id"])["status"], "PAUSED")
        attempts = store.list_director_attempts(
            "room_plan", round_id=str(round_row["id"])
        )
        self.assertEqual([attempt["status"] for attempt in attempts], ["CANCELLED"])
        self.assertEqual(attempts[0]["error_code"], "director_attempt_abandoned")

    def test_orphan_without_recoverable_checkpoint_is_cancelled(self) -> None:
        store = StudioStore(self.database_path)
        round_row = store.create_round("room_plan", "检查点尚未建立时进程中断")
        moderator = store.enabled_members("room_plan")[0]
        store.begin_director_attempt(
            "room_plan",
            str(round_row["id"]),
            moderator_member_id=str(moderator["id"]),
            moderator_member_version=int(moderator["version"]),
            provider=str(moderator["provider"]),
            model=str(moderator["model"]),
        )

        with DatabaseInstanceOwner(self.database_path) as owner:
            recovery = store.recover_orphaned_work(instance_owner=owner)

        self.assertEqual(recovery["paused_rounds"], 0)
        self.assertEqual(recovery["cancelled_rounds"], 1)
        self.assertEqual(
            store.get_round("room_plan", round_row["id"])["status"],
            "CANCELLED",
        )
        attempts = store.list_director_attempts(
            "room_plan", round_id=str(round_row["id"])
        )
        self.assertEqual([attempt["status"] for attempt in attempts], ["CANCELLED"])
        self.assertEqual(attempts[0]["error_code"], "director_round_cancelled")


if __name__ == "__main__":
    unittest.main()
