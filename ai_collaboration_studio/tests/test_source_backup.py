from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from scripts.create_versioned_source_backup import (
    CONTENT_HASH_VERSION,
    MANIFEST_NAME,
    MANIFEST_VERSION,
    SourceBackupError,
    create_backup,
    preflight_backup,
    verify_backup,
)


CREATED_AT_UTC = "2026-08-12T03:45:00Z"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _read_manifest(archive_path: Path) -> dict[str, object]:
    with zipfile.ZipFile(archive_path, "r") as archive:
        return json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class SourceBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-source-backup-",
            ignore_cleanup_errors=True,
        )
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "fixture-source"
        self.source.mkdir()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create(self, destination_name: str = "backups") -> Path:
        return create_backup(
            source_root=self.source,
            destination_root=self.root / destination_name,
            source_root_label="fixture_source",
            created_at_utc=CREATED_AT_UTC,
        )

    def test_deterministic_closed_manifest_and_offline_verification(self) -> None:
        _write(self.source / "README.md", "fixture source\n")
        _write(self.source / "backend" / "service.py", "VALUE = 7\n")

        with patch(
            "socket.create_connection",
            side_effect=AssertionError("source backup must remain offline"),
        ):
            first = self._create("backups-a")
            second = self._create("backups-b")
            report = verify_backup(first)

        self.assertEqual(first.name, second.name)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first.stat().st_nlink, 1)
        self.assertEqual(second.stat().st_nlink, 1)
        self.assertFalse(any(first.parent.glob(".*.tmp")))

        manifest = _read_manifest(first)
        self.assertEqual(
            set(manifest),
            {
                "version",
                "backup_version",
                "created_at_utc",
                "source_root_label",
                "file_count",
                "total_size",
                "files",
                "total_sha256",
            },
        )
        self.assertEqual(manifest["version"], MANIFEST_VERSION)
        self.assertEqual(manifest["created_at_utc"], CREATED_AT_UTC)
        self.assertEqual(manifest["source_root_label"], "fixture_source")
        self.assertEqual(
            [row["path"] for row in manifest["files"]],
            ["README.md", "backend/service.py"],
        )
        for row in manifest["files"]:
            self.assertEqual(set(row), {"path", "size", "sha256"})
            source_bytes = (self.source / row["path"]).read_bytes()
            self.assertEqual(row["size"], len(source_bytes))
            self.assertEqual(row["sha256"], hashlib.sha256(source_bytes).hexdigest())
        expected_total = _canonical_sha256({
            "version": CONTENT_HASH_VERSION,
            "files": manifest["files"],
        })
        self.assertEqual(manifest["total_sha256"], expected_total)
        self.assertEqual(
            manifest["backup_version"],
            f"20260812T034500Z-{expected_total[:12]}",
        )
        self.assertEqual(report, {
            "ok": True,
            "version": MANIFEST_VERSION,
            "backup_version": manifest["backup_version"],
            "archive_size": first.stat().st_size,
            "archive_sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
            "source_root_label": "fixture_source",
            "file_count": 2,
            "total_size": manifest["total_size"],
            "total_sha256": expected_total,
        })

    def test_generated_dependencies_and_secret_material_are_excluded(self) -> None:
        included = {
            "app.py": "print('safe')\n",
            ".env.example": "API_KEY=replace-me\n",
        }
        excluded = {
            ".env": "REAL_SECRET=one\n",
            ".env.local": "REAL_SECRET=two\n",
            ".env.production": "REAL_SECRET=three\n",
            "api_key.txt": "secret\n",
            "credentials.yaml": "secret\n",
            "secrets.yml": "secret\n",
            "token.json": "secret\n",
            "service.secret": "secret\n",
            "private.pem": "private\n",
            "密钥说明.txt": "secret\n",
            "runtime/studio.sqlite3": "database\n",
            ".git/config": "git config\n",
            "node_modules/package/index.js": "generated\n",
            "dist/bundle.js": "generated\n",
            "frontend/.npm-cache/_logs/debug.log": "generated npm trace\n",
            "__pycache__/app.cpython-313.pyc": "cache\n",
            "nested/cache.pyc": "cache\n",
            "SeCrEtS/provider.txt": "provider-secret\n",
        }
        for relative, content in {**included, **excluded}.items():
            _write(self.source / relative, content)

        archive_path = self._create()
        manifest = _read_manifest(archive_path)
        self.assertEqual(
            [row["path"] for row in manifest["files"]],
            sorted(included),
        )
        with zipfile.ZipFile(archive_path, "r") as archive:
            self.assertEqual(
                set(archive.namelist()),
                {MANIFEST_NAME, *included},
            )
        self.assertTrue(verify_backup(archive_path)["ok"])

    def test_tampered_member_is_rejected_without_restoring_files(self) -> None:
        _write(self.source / "app.py", "ORIGINAL = True\n")
        original = self._create()
        tampered = self.root / "tampered.zip"
        with zipfile.ZipFile(original, "r") as source_archive:
            with zipfile.ZipFile(tampered, "w") as target_archive:
                for info in source_archive.infolist():
                    payload = source_archive.read(info)
                    if info.filename == "app.py":
                        payload = payload.replace(b"ORIGINAL", b"TAMPERED")
                    target_archive.writestr(info, payload)

        with self.assertRaisesRegex(SourceBackupError, "hash differs"):
            verify_backup(tampered)
        self.assertFalse((self.root / "app.py").exists())

    def test_conflict_explicit_destination_and_source_containment_fail_closed(self) -> None:
        _write(self.source / "app.py", "VALUE = 1\n")
        first = self._create()
        with self.assertRaisesRegex(SourceBackupError, "already exists"):
            self._create()
        self.assertTrue(first.is_file())
        self.assertFalse(any(first.parent.glob(".*.tmp")))

        inside = self.source / "backups"
        with self.assertRaisesRegex(SourceBackupError, "outside the source root"):
            create_backup(
                source_root=self.source,
                destination_root=inside,
                source_root_label="fixture_source",
                created_at_utc=CREATED_AT_UTC,
            )
        self.assertFalse(inside.exists())
        with self.assertRaisesRegex(SourceBackupError, "must be explicit"):
            create_backup(
                source_root=self.source,
                destination_root="",
                source_root_label="fixture_source",
                created_at_utc=CREATED_AT_UTC,
            )

    def test_preflight_is_read_only_and_binds_the_future_archive_version(self) -> None:
        _write(self.source / "app.py", "VALUE = 11\n")
        destination = self.root / "not-created" / "nested-backups"
        report = preflight_backup(
            source_root=self.source,
            destination_root=destination,
            source_root_label="fixture_source",
            created_at_utc=CREATED_AT_UTC,
        )

        self.assertTrue(report["ok"])
        self.assertTrue(report["ready"])
        self.assertFalse(report["destination_exists"])
        self.assertTrue(report["destination_requires_creation"])
        self.assertFalse(report["archive_exists"])
        self.assertEqual(report["source_file_count"], 1)
        self.assertIn("fixture_source-source-20260812T034500Z-", report["archive_path"])
        self.assertFalse(destination.exists())
        self.assertFalse((self.root / "not-created").exists())

        self._create("not-created/nested-backups")
        occupied = preflight_backup(
            source_root=self.source,
            destination_root=destination,
            source_root_label="fixture_source",
            created_at_utc=CREATED_AT_UTC,
        )
        self.assertFalse(occupied["ready"])
        self.assertTrue(occupied["destination_exists"])
        self.assertTrue(occupied["archive_exists"])

    def test_preflight_rejects_a_file_in_the_destination_chain(self) -> None:
        _write(self.source / "app.py", "VALUE = 12\n")
        blocked_parent = self.root / "blocked-parent"
        blocked_parent.write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(SourceBackupError, "non-directory path component"):
            preflight_backup(
                source_root=self.source,
                destination_root=blocked_parent / "nested",
                source_root_label="fixture_source",
                created_at_utc=CREATED_AT_UTC,
            )
        self.assertEqual(blocked_parent.read_text(encoding="utf-8"), "not a directory")

    def test_source_hard_links_are_rejected(self) -> None:
        hardlink_source = self.source / "original.txt"
        _write(hardlink_source, "one identity\n")
        hardlink_alias = self.source / "alias.txt"
        try:
            os.link(hardlink_source, hardlink_alias)
        except OSError as exc:  # pragma: no cover - unusual temporary filesystem
            self.skipTest(f"hard links unavailable in system temp: {exc}")
        with self.assertRaisesRegex(SourceBackupError, "hard links"):
            self._create()

    def test_source_symlinks_are_rejected(self) -> None:
        symlink_source = self.root / "symlink-source"
        symlink_source.mkdir()
        _write(symlink_source / "real.txt", "real\n")
        try:
            os.symlink(
                symlink_source / "real.txt",
                symlink_source / "alias.txt",
            )
        except OSError as exc:  # pragma: no cover - Windows without symlink rights
            self.skipTest(f"symlinks unavailable in system temp: {exc}")
        with self.assertRaisesRegex(SourceBackupError, "symlink or reparse point"):
            create_backup(
                source_root=symlink_source,
                destination_root=self.root / "symlink-backups",
                source_root_label="symlink_source",
                created_at_utc=CREATED_AT_UTC,
            )

    def test_destination_symlink_chain_is_rejected_before_creation(self) -> None:
        _write(self.source / "app.py", "VALUE = 3\n")
        real_destination = self.root / "real-destination"
        real_destination.mkdir()
        linked_destination = self.root / "linked-destination"
        try:
            os.symlink(
                real_destination,
                linked_destination,
                target_is_directory=True,
            )
        except OSError as exc:  # pragma: no cover - Windows without symlink rights
            self.skipTest(f"symlinks unavailable in system temp: {exc}")
        with self.assertRaisesRegex(SourceBackupError, "symlink or reparse point"):
            create_backup(
                source_root=self.source,
                destination_root=linked_destination / "nested",
                source_root_label="fixture_source",
                created_at_utc=CREATED_AT_UTC,
            )
        self.assertFalse((real_destination / "nested").exists())

    def test_verify_rejects_escape_and_ambiguous_archive_identity(self) -> None:
        for member_name in ("../escaped.txt", "C:/escaped.txt"):
            with self.subTest(member_name=member_name):
                malicious = self.root / (hashlib.sha256(
                    member_name.encode("utf-8")
                ).hexdigest()[:8] + ".zip")
                with zipfile.ZipFile(malicious, "w") as archive:
                    archive.writestr(member_name, "must not escape")
                with self.assertRaisesRegex(SourceBackupError, "escapes its root"):
                    verify_backup(malicious)
        self.assertFalse((self.root.parent / "escaped.txt").exists())
        self.assertFalse((self.root / "escaped.txt").exists())

        _write(self.source / "app.py", "VALUE = 2\n")
        original = self._create()
        alias = self.root / "archive-hardlink.zip"
        try:
            os.link(original, alias)
        except OSError as exc:  # pragma: no cover - unusual temporary filesystem
            self.skipTest(f"hard links unavailable in system temp: {exc}")
        with self.assertRaisesRegex(SourceBackupError, "ambiguous file identity"):
            verify_backup(alias)


if __name__ == "__main__":
    unittest.main()
