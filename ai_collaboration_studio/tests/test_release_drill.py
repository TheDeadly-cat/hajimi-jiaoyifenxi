from __future__ import annotations

import json
import os
import socket
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.create_versioned_source_backup import create_backup
from scripts.run_isolated_release_drill import (
    ReleaseDrillError,
    activate_release,
    build_synthetic_failure_receipt,
    install_release,
    read_activation_pointer,
    rollback_release,
    run_drill,
)


class ReleaseLifecycleContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-release-lifecycle-test-",
            ignore_cleanup_errors=True,
        )
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def make_archive(self, name: str, created_at: str, marker: str) -> Path:
        source = self.root / f"source-{name}"
        (source / "frontend").mkdir(parents=True)
        (source / "server.py").write_text("pass\n", encoding="utf-8")
        (source / "README.md").write_text(marker + "\n", encoding="utf-8")
        (source / "requirements-lock-win-py314.txt").write_text(
            "fixture==1 --hash=sha256:" + ("0" * 64) + "\n",
            encoding="ascii",
        )
        (source / "frontend" / "package.json").write_text(
            json.dumps({"name": "fixture", "version": name}) + "\n",
            encoding="utf-8",
        )
        return create_backup(
            source_root=source,
            destination_root=self.root / f"archive-{name}",
            source_root_label=f"fixture_{name}",
            created_at_utc=created_at,
        )

    def test_install_upgrade_and_explicit_rollback_publish_exact_generations(self) -> None:
        baseline = self.make_archive("baseline", "2026-08-20T00:00:00Z", "one")
        current = self.make_archive("current", "2026-08-20T00:00:02Z", "two")
        release_root = self.root / "release-root"
        baseline_receipt = install_release(baseline, release_root)
        current_receipt = install_release(current, release_root)

        first = activate_release(
            release_root,
            baseline_receipt["release_id"],
            expected_active_release_id=None,
        )
        upgraded = activate_release(
            release_root,
            current_receipt["release_id"],
            expected_active_release_id=baseline_receipt["release_id"],
        )
        rolled_back = rollback_release(
            release_root,
            failed_release_id=current_receipt["release_id"],
            target_release_id=baseline_receipt["release_id"],
            expected_generation=2,
            failure_receipt=build_synthetic_failure_receipt(
                current_receipt["release_id"]
            ),
        )

        self.assertEqual(first["generation"], 1)
        self.assertEqual(upgraded["generation"], 2)
        self.assertEqual(rolled_back["generation"], 3)
        self.assertEqual(
            read_activation_pointer(release_root),
            rolled_back,
        )
        self.assertEqual(
            rolled_back["active_release_id"],
            baseline_receipt["release_id"],
        )
        with self.assertRaisesRegex(ReleaseDrillError, "already exists"):
            install_release(baseline, release_root)

    def test_stale_activation_and_inexact_failure_receipt_fail_closed(self) -> None:
        baseline = self.make_archive("baseline", "2026-08-20T00:00:00Z", "one")
        current = self.make_archive("current", "2026-08-20T00:00:02Z", "two")
        release_root = self.root / "release-root"
        baseline_receipt = install_release(baseline, release_root)
        current_receipt = install_release(current, release_root)
        activate_release(
            release_root,
            baseline_receipt["release_id"],
            expected_active_release_id=None,
        )
        with self.assertRaisesRegex(ReleaseDrillError, "changed before upgrade"):
            activate_release(
                release_root,
                current_receipt["release_id"],
                expected_active_release_id=None,
            )
        upgraded = activate_release(
            release_root,
            current_receipt["release_id"],
            expected_active_release_id=baseline_receipt["release_id"],
        )
        wrong = build_synthetic_failure_receipt(baseline_receipt["release_id"])
        with self.assertRaisesRegex(ReleaseDrillError, "does not authorize"):
            rollback_release(
                release_root,
                failed_release_id=current_receipt["release_id"],
                target_release_id=baseline_receipt["release_id"],
                expected_generation=upgraded["generation"],
                failure_receipt=wrong,
            )
        self.assertEqual(
            read_activation_pointer(release_root)["active_release_id"],
            current_receipt["release_id"],
        )

    def test_project_drill_is_synthetic_offline_and_preserves_application_data(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        # This synthetic lifecycle fixture must not depend on unrelated host
        # listeners. Keep the real protected-port decision; replace only the
        # passive operating-system listener inventory for this test.
        with patch.dict(os.environ, {"AI_STUDIO_SKIP_LOCAL_ENV": "1"}), patch(
            "scripts.run_isolated_release_drill._windows_listener_ports",
            side_effect=lambda *_: set(),
        ) as listener_inventory:
            result = run_drill(project_root)

        self.assertEqual(listener_inventory.call_count, 4)
        self.assertEqual(
            [entry.args[0] for entry in listener_inventory.call_args_list],
            [socket.AF_INET, socket.AF_INET6] * 2,
        )
        expected_ports = {"8770": False, "11111": False, "18787": False}
        self.assertEqual(result["ports"], {
            "before": expected_ports, "after": expected_ports,
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["schema_version"], "isolated_release_drill_v1")
        self.assertTrue(result["install"]["reinstall_blocked"])
        self.assertTrue(result["activation"]["stale_activation_blocked"])
        self.assertEqual(result["activation"]["rollback_generation"], 3)
        self.assertTrue(result["application_data"]["family_unchanged"])
        boundaries = result["boundaries"]
        self.assertTrue(boundaries["system_temp_only"])
        self.assertTrue(boundaries["synthetic_baseline"])
        self.assertFalse(boundaries["historical_upgrade_compatibility_proven"])
        self.assertFalse(boundaries["application_started"])
        self.assertFalse(boundaries["database_migration_executed"])
        self.assertFalse(boundaries["formal_database_opened"])
        self.assertEqual(boundaries["external_network_requests"], 0)

    def test_project_drill_refuses_protected_listener_before_creating_artifacts(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        for port in (8770, 11111, 18787):
            for family in (socket.AF_INET, socket.AF_INET6):
                with self.subTest(port=port, family=family), patch.dict(
                    os.environ, {"AI_STUDIO_SKIP_LOCAL_ENV": "1"}
                ), patch(
                    "scripts.run_isolated_release_drill._windows_listener_ports",
                    side_effect=lambda address_family, _row_type: (
                        {port} if address_family == family else set()
                    ),
                ) as listener_inventory, patch(
                    "scripts.run_isolated_release_drill.source_backup.create_backup"
                ) as create_archive:
                    # Exercise run_drill's real refusal, without opening,
                    # stopping or querying any production socket.
                    with self.assertRaisesRegex(
                        ReleaseDrillError, "a protected port already has a listener"
                    ):
                        run_drill(project_root)
                    self.assertEqual(listener_inventory.call_count, 2)
                    create_archive.assert_not_called()


if __name__ == "__main__":
    unittest.main()
