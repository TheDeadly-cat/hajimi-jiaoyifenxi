from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import backend.store as store_module
from backend.store import (
    StudioStore,
    _LazyStudioStore,
    _initialize_migration_shadow,
    _startup_identity,
    new_id,
    now_ms,
)


class StudioStoreInitializationHardeningTests(unittest.TestCase):
    def test_migration_shadow_initializes_system_temp_without_skip_env(self) -> None:
        migration_epoch_ms = 1_950_000_000_000
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-migration-shadow-"
        ) as temp_dir:
            database_path = Path(temp_dir) / "shadow.sqlite3"
            database_path.touch()
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AI_STUDIO_SKIP_LOCAL_ENV", None)
                shadow = _initialize_migration_shadow(
                    database_path,
                    migration_epoch_ms,
                )

            self.assertEqual(shadow.path, database_path.resolve())
            self.assertTrue(database_path.is_file())
            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
                applied_at_values = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT applied_at FROM schema_migrations"
                    ).fetchall()
                }
            self.assertEqual(applied_at_values, {migration_epoch_ms})
        self.assertIsNone(store_module._INITIALIZATION_EPOCH_MS.get())
        self.assertIsNone(store_module._INITIALIZATION_ID_COUNTER.get())

    def test_migration_shadow_rejects_non_system_temp_path(self) -> None:
        database_path = (
            Path(__file__).resolve().parent
            / f"must-not-create-shadow-{uuid.uuid4().hex}.sqlite3"
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_STUDIO_SKIP_LOCAL_ENV", None)
            with self.assertRaisesRegex(RuntimeError, "Migration shadows.*system temp"):
                _initialize_migration_shadow(database_path, 1_950_000_000_000)
        self.assertFalse(database_path.exists())

    def test_migration_shadow_rejects_parent_path_alias(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-migration-shadow-parent-") as temp_dir:
            temp_path = Path(temp_dir)
            parent_alias = temp_path / "parent-alias"
            try:
                os.symlink(temp_path, parent_alias, target_is_directory=True)
            except OSError as exc:  # pragma: no cover - Windows without symlink rights
                self.skipTest(f"directory symlinks unavailable in system temp: {exc}")
            database_path = parent_alias / "shadow.sqlite3"
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "path may not contain a symlink or reparse point",
                ):
                    _initialize_migration_shadow(database_path, 1_950_000_000_000)
                self.assertFalse((temp_path / "shadow.sqlite3").exists())
            finally:
                parent_alias.unlink()

    def test_public_constructor_still_rejects_system_temp_without_skip_env(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-public-constructor-guard-"
        ) as temp_dir:
            database_path = Path(temp_dir) / "public.sqlite3"
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AI_STUDIO_SKIP_LOCAL_ENV", None)
                with self.assertRaisesRegex(RuntimeError, "isolated tests"):
                    StudioStore(database_path, migration_epoch_ms=1_950_000_000_000)
            self.assertFalse(database_path.exists())

    def test_migration_shadow_uses_deterministic_context_and_resets_it(self) -> None:
        observed: list[tuple[int, str, str]] = []

        def probe_initialize(_store: StudioStore) -> None:
            observed.append((now_ms(), new_id("probe"), new_id("probe")))

        migration_epoch_ms = 1_960_000_000_000
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-migration-shadow-context-"
        ) as temp_dir:
            temp_path = Path(temp_dir)
            first_path = temp_path / "first.sqlite3"
            second_path = temp_path / "second.sqlite3"
            first_path.touch()
            second_path.touch()
            with (
                patch.dict(os.environ, {}, clear=False),
                patch.object(StudioStore, "_initialize", probe_initialize),
            ):
                os.environ.pop("AI_STUDIO_SKIP_LOCAL_ENV", None)
                _initialize_migration_shadow(
                    first_path,
                    migration_epoch_ms,
                )
                _initialize_migration_shadow(
                    second_path,
                    migration_epoch_ms,
                )

        self.assertEqual(len(observed), 2)
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[0][0], migration_epoch_ms)
        self.assertIsNone(store_module._INITIALIZATION_EPOCH_MS.get())
        self.assertIsNone(store_module._INITIALIZATION_ID_COUNTER.get())

    def test_schema_initialization_rejects_hard_link_aliases(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-hard-link-initialization-guard-"
        ) as temp_dir:
            temp_path = Path(temp_dir)
            victim_path = temp_path / "victim.sqlite3"
            alias_path = temp_path / "alias.sqlite3"
            StudioStore(victim_path, migration_epoch_ms=1_900_000_000_000)
            with closing(sqlite3.connect(victim_path)) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            os.link(victim_path, alias_path)
            victim_sha256 = hashlib.sha256(victim_path.read_bytes()).hexdigest()

            with self.assertRaisesRegex(RuntimeError, "hard-linked"):
                StudioStore(alias_path, migration_epoch_ms=1_900_000_000_001)
            with self.assertRaisesRegex(RuntimeError, "hard-linked"):
                _initialize_migration_shadow(alias_path, 1_900_000_000_001)

            self.assertEqual(
                hashlib.sha256(victim_path.read_bytes()).hexdigest(),
                victim_sha256,
            )

    def test_public_constructor_has_no_schema_bypass_arguments(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-store-signature-") as temp_dir:
            database_path = Path(temp_dir) / "signature.sqlite3"

            with self.assertRaises(TypeError):
                StudioStore(database_path, initialize_schema=False)  # type: ignore[call-arg]
            with self.assertRaises(TypeError):
                StudioStore(  # type: ignore[call-arg]
                    database_path,
                    schema_change_authorized=True,
                )

            self.assertFalse(database_path.exists())

    def test_initialization_requires_skip_env_and_system_temp_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-store-guard-") as temp_dir:
            database_path = Path(temp_dir) / "skip-env-required.sqlite3"
            for invalid_value in ("", "true", "yes"):
                with self.subTest(skip_local_env=invalid_value):
                    with patch.dict(
                        os.environ,
                        {"AI_STUDIO_SKIP_LOCAL_ENV": invalid_value},
                    ):
                        with self.assertRaisesRegex(RuntimeError, "isolated tests"):
                            StudioStore(database_path)
            self.assertFalse(database_path.exists())

        outside_temp = (
            Path(__file__).resolve().parent
            / f"must-not-create-{uuid.uuid4().hex}.sqlite3"
        )
        with patch.dict(os.environ, {"AI_STUDIO_SKIP_LOCAL_ENV": "1"}):
            with self.assertRaisesRegex(RuntimeError, "system temp"):
                StudioStore(outside_temp)
        self.assertFalse(outside_temp.exists())

    def test_migration_epoch_is_frozen_only_while_initialize_runs(self) -> None:
        observed: list[int] = []

        class ProbeStore(StudioStore):
            def _initialize(self) -> None:
                observed.extend((now_ms(), now_ms()))

        with tempfile.TemporaryDirectory(prefix="ai-studio-store-epoch-") as temp_dir:
            with patch.object(store_module.time, "time", return_value=98.765):
                ProbeStore(
                    Path(temp_dir) / "epoch.sqlite3",
                    migration_epoch_ms=1_234_567,
                )
                self.assertEqual(observed, [1_234_567, 1_234_567])
                self.assertEqual(now_ms(), 98_765)
                self.assertIsNone(store_module._INITIALIZATION_ID_COUNTER.get())

    def test_migration_epoch_context_is_reset_when_initialize_fails(self) -> None:
        class FailingStore(StudioStore):
            def _initialize(self) -> None:
                self.observed_epoch = now_ms()
                raise RuntimeError("expected initialize failure")

        with tempfile.TemporaryDirectory(prefix="ai-studio-store-epoch-failure-") as temp_dir:
            with patch.object(store_module.time, "time", return_value=45.678):
                with self.assertRaisesRegex(RuntimeError, "expected initialize failure"):
                    FailingStore(
                        Path(temp_dir) / "epoch-failure.sqlite3",
                        migration_epoch_ms=9_876,
                    )
                self.assertEqual(now_ms(), 45_678)
                self.assertIsNone(store_module._INITIALIZATION_ID_COUNTER.get())

    def test_initialization_ids_are_canonical_and_repeatable(self) -> None:
        class IdProbeStore(StudioStore):
            def _initialize(self) -> None:
                self.generated_ids = [
                    new_id("room_version"),
                    new_id("room_version"),
                    new_id("member"),
                ]

        epoch_ms = 1_725_000_123_456
        expected_payload = json.dumps(
            {
                "counter": 0,
                "epoch_ms": epoch_ms,
                "prefix": "room_version",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        expected_first = (
            "room_version_" + hashlib.sha256(expected_payload).hexdigest()[:12]
        )

        with tempfile.TemporaryDirectory(prefix="ai-studio-store-id-repeat-") as temp_dir:
            first = IdProbeStore(
                Path(temp_dir) / "first.sqlite3",
                migration_epoch_ms=epoch_ms,
            )
            second = IdProbeStore(
                Path(temp_dir) / "second.sqlite3",
                migration_epoch_ms=epoch_ms,
            )
            different_epoch = IdProbeStore(
                Path(temp_dir) / "different.sqlite3",
                migration_epoch_ms=epoch_ms + 1,
            )

        self.assertEqual(first.generated_ids, second.generated_ids)
        self.assertEqual(first.generated_ids[0], expected_first)
        self.assertNotEqual(first.generated_ids, different_epoch.generated_ids)

    def test_room_version_backfill_ids_repeat_for_same_migration_epoch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-room-version-repeat-") as temp_dir:
            temp_path = Path(temp_dir)
            legacy_path = temp_path / "legacy.sqlite3"
            StudioStore(legacy_path, migration_epoch_ms=1_700_000_000_000)
            with closing(sqlite3.connect(legacy_path)) as connection, connection:
                connection.execute("DELETE FROM room_versions")
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            first_path = temp_path / "first.sqlite3"
            second_path = temp_path / "second.sqlite3"
            shutil.copyfile(legacy_path, first_path)
            shutil.copyfile(legacy_path, second_path)
            migration_epoch_ms = 1_800_000_000_000
            StudioStore(first_path, migration_epoch_ms=migration_epoch_ms)
            StudioStore(second_path, migration_epoch_ms=migration_epoch_ms)

            def room_versions(path: Path) -> list[tuple[object, ...]]:
                with closing(sqlite3.connect(path)) as connection:
                    return connection.execute(
                        """SELECT id,room_id,version,snapshot_json,
                                  snapshot_sha256,changed_at
                             FROM room_versions
                         ORDER BY room_id,version"""
                    ).fetchall()

            first_versions = room_versions(first_path)
            second_versions = room_versions(second_path)

        self.assertTrue(first_versions)
        self.assertEqual(first_versions, second_versions)

    def test_nested_and_concurrent_initialization_counters_are_isolated(self) -> None:
        class LeafStore(StudioStore):
            def _initialize(self) -> None:
                self.generated_ids = [new_id("probe"), new_id("probe")]

        class OuterStore(StudioStore):
            def __init__(
                self,
                path: Path,
                *,
                migration_epoch_ms: int,
                nested_path: Path | None = None,
                barrier: threading.Barrier | None = None,
            ) -> None:
                self.nested_path = nested_path
                self.barrier = barrier
                super().__init__(path, migration_epoch_ms=migration_epoch_ms)

            def _initialize(self) -> None:
                self.generated_ids = [new_id("probe")]
                if self.barrier is not None:
                    self.barrier.wait(timeout=5)
                if self.nested_path is not None:
                    self.nested_store = LeafStore(
                        self.nested_path,
                        migration_epoch_ms=now_ms() + 1,
                    )
                self.generated_ids.append(new_id("probe"))

        epoch_ms = 1_900_000_000_000
        with tempfile.TemporaryDirectory(prefix="ai-studio-store-id-context-") as temp_dir:
            temp_path = Path(temp_dir)
            plain = OuterStore(
                temp_path / "plain.sqlite3",
                migration_epoch_ms=epoch_ms,
            )
            nested = OuterStore(
                temp_path / "nested.sqlite3",
                migration_epoch_ms=epoch_ms,
                nested_path=temp_path / "nested-leaf.sqlite3",
            )
            self.assertEqual(plain.generated_ids, nested.generated_ids)

            barrier = threading.Barrier(2)
            concurrent_ids: dict[int, list[str]] = {}
            errors: list[BaseException] = []

            def initialize(index: int) -> None:
                try:
                    probe = OuterStore(
                        temp_path / f"concurrent-{index}.sqlite3",
                        migration_epoch_ms=epoch_ms,
                        barrier=barrier,
                    )
                    concurrent_ids[index] = probe.generated_ids
                except BaseException as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            threads = [
                threading.Thread(target=initialize, args=(index,))
                for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

        self.assertFalse(errors)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(concurrent_ids, {0: plain.generated_ids, 1: plain.generated_ids})

    def test_new_id_returns_to_uuid4_after_success_and_failure(self) -> None:
        class SuccessfulStore(StudioStore):
            def _initialize(self) -> None:
                self.initialization_id = new_id("probe")

        class FailingStore(StudioStore):
            def _initialize(self) -> None:
                self.initialization_id = new_id("probe")
                raise RuntimeError("expected deterministic failure")

        with tempfile.TemporaryDirectory(prefix="ai-studio-store-id-reset-") as temp_dir:
            SuccessfulStore(
                Path(temp_dir) / "success.sqlite3",
                migration_epoch_ms=123,
            )
            with self.assertRaisesRegex(RuntimeError, "deterministic failure"):
                FailingStore(
                    Path(temp_dir) / "failure.sqlite3",
                    migration_epoch_ms=456,
                )

        self.assertIsNone(store_module._INITIALIZATION_EPOCH_MS.get())
        self.assertIsNone(store_module._INITIALIZATION_ID_COUNTER.get())
        with patch.object(store_module.uuid, "uuid4") as uuid4:
            uuid4.return_value.hex = "0123456789abcdef0123456789abcdef"
            self.assertEqual(new_id("runtime"), "runtime_0123456789ab")
            uuid4.assert_called_once_with()


class LazyStudioStoreStartupIdentityTests(unittest.TestCase):
    def _initialized_database(self, temp_dir: str, name: str = "studio.sqlite3") -> Path:
        database_path = Path(temp_dir) / name
        StudioStore(database_path)
        return database_path

    def test_verified_startup_requires_complete_file_family_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-startup-identity-") as temp_dir:
            database_path = self._initialized_database(temp_dir)
            identity = _startup_identity(database_path)
            identity.pop("journal")
            lazy = _LazyStudioStore()

            with patch.object(store_module, "DATABASE_PATH", database_path):
                with self.assertRaisesRegex(RuntimeError, "journal component"):
                    lazy.configure_verified_startup(database_path, identity)

    def test_verified_startup_uses_private_existing_schema_constructor(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-startup-private-") as temp_dir:
            database_path = self._initialized_database(temp_dir)
            lazy = _LazyStudioStore()

            with patch.object(store_module, "DATABASE_PATH", database_path):
                lazy.configure_verified_startup(
                    database_path,
                    _startup_identity(database_path),
                )
                with patch.object(
                    StudioStore,
                    "__init__",
                    side_effect=AssertionError("public constructor was used"),
                ):
                    resolved = lazy._resolve()

            self.assertEqual(resolved.path, database_path.resolve())

    def test_verified_startup_rejects_parent_path_alias_before_resolution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-startup-parent-alias-") as temp_dir:
            database_path = self._initialized_database(temp_dir)
            parent_alias = Path(temp_dir) / "parent-alias"
            try:
                os.symlink(Path(temp_dir), parent_alias, target_is_directory=True)
            except OSError as exc:  # pragma: no cover - Windows without symlink rights
                self.skipTest(f"directory symlinks unavailable in system temp: {exc}")
            alias_path = parent_alias / database_path.name
            lazy = _LazyStudioStore()
            try:
                with patch.object(store_module, "DATABASE_PATH", database_path):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "Verified startup path may not contain a symlink or reparse point",
                    ):
                        lazy.configure_verified_startup(
                            alias_path,
                            _startup_identity(database_path),
                        )
            finally:
                parent_alias.unlink()

    def test_verified_startup_rejects_sidecar_identity_before_open(self) -> None:
        for component, suffix in (
            ("wal", "-wal"),
            ("shm", "-shm"),
            ("journal", "-journal"),
        ):
            with self.subTest(component=component):
                with tempfile.TemporaryDirectory(
                    prefix=f"ai-studio-startup-reject-{component}-"
                ) as temp_dir:
                    database_path = self._initialized_database(temp_dir)
                    sidecar = Path(f"{database_path}{suffix}")
                    sidecar.write_bytes(b"sidecar")
                    lazy = _LazyStudioStore()
                    with patch.object(store_module, "DATABASE_PATH", database_path):
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "Verified startup refuses SQLite sidecars",
                        ):
                            lazy.configure_verified_startup(
                                database_path,
                                _startup_identity(database_path),
                            )
                    self.assertEqual(sidecar.read_bytes(), b"sidecar")

    def test_startup_identity_rejects_dangling_symlink_components(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-startup-dangling-") as temp_dir:
            database_path = self._initialized_database(temp_dir)
            sidecar = Path(f"{database_path}-shm")
            missing_target = Path(temp_dir) / "missing-shm-target"
            try:
                os.symlink(missing_target, sidecar)
            except OSError as exc:  # pragma: no cover - Windows without symlink rights
                self.skipTest(f"symlinks unavailable in system temp: {exc}")
            with self.assertRaisesRegex(
                RuntimeError,
                "may not be a symlink",
            ):
                _startup_identity(database_path)
            self.assertTrue(sidecar.is_symlink())
            sidecar.unlink()

    def test_startup_identity_rejects_hardlinked_main_component(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-startup-hardlink-") as temp_dir:
            database_path = self._initialized_database(temp_dir)
            alias_path = Path(temp_dir) / "alias.sqlite3"
            try:
                os.link(database_path, alias_path)
            except OSError as exc:  # pragma: no cover - unusual temporary filesystem
                self.skipTest(f"hard links unavailable in system temp: {exc}")
            with self.assertRaisesRegex(
                RuntimeError,
                "hard-linked",
            ):
                _startup_identity(database_path)
            self.assertEqual(database_path.stat().st_nlink, 2)
            alias_path.unlink()

    def test_every_sidecar_is_reverified_before_existing_schema_open(self) -> None:
        for component, suffix in (
            ("wal", "-wal"),
            ("shm", "-shm"),
            ("journal", "-journal"),
        ):
            with self.subTest(component=component):
                with tempfile.TemporaryDirectory(
                    prefix=f"ai-studio-startup-{component}-"
                ) as temp_dir:
                    database_path = self._initialized_database(temp_dir)
                    lazy = _LazyStudioStore()
                    with patch.object(store_module, "DATABASE_PATH", database_path):
                        lazy.configure_verified_startup(
                            database_path,
                            _startup_identity(database_path),
                        )
                        with Path(f"{database_path}{suffix}").open("ab") as sidecar:
                            sidecar.write(b"changed-after-preflight")
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "changed after startup preflight",
                        ):
                            lazy._resolve()

    def test_file_family_is_reverified_after_private_open(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ai-studio-startup-race-") as temp_dir:
            database_path = self._initialized_database(temp_dir)
            lazy = _LazyStudioStore()
            original_startup_identity = store_module._startup_identity
            resolve_identity_calls = 0

            with patch.object(store_module, "DATABASE_PATH", database_path):
                lazy.configure_verified_startup(
                    database_path,
                    original_startup_identity(database_path),
                )

                def capture_then_change(path: Path) -> dict[str, dict[str, object]]:
                    nonlocal resolve_identity_calls
                    identity = original_startup_identity(path)
                    resolve_identity_calls += 1
                    if resolve_identity_calls == 1:
                        Path(f"{path}-journal").write_bytes(b"race")
                    return identity

                with patch.object(
                    store_module,
                    "_startup_identity",
                    side_effect=capture_then_change,
                ):
                    with self.assertRaisesRegex(RuntimeError, "while opening"):
                        lazy._resolve()

            self.assertEqual(resolve_identity_calls, 2)

    def test_unverified_default_fallback_is_rejected_outside_isolated_temp(self) -> None:
        database_path = (
            Path(__file__).resolve().parent
            / f"must-not-fallback-{uuid.uuid4().hex}.sqlite3"
        )
        lazy = _LazyStudioStore()
        with (
            patch.object(store_module, "DATABASE_PATH", database_path),
            patch.dict(os.environ, {"AI_STUDIO_SKIP_LOCAL_ENV": "1"}),
        ):
            with self.assertRaisesRegex(RuntimeError, "verified database identity"):
                lazy._resolve()
        self.assertFalse(database_path.exists())


if __name__ == "__main__":
    unittest.main()
