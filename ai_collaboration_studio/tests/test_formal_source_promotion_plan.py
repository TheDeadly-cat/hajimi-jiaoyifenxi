from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import plan_formal_source_promotion as promotion


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class FormalSourcePromotionPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-promotion-plan-",
            ignore_cleanup_errors=True,
        )
        self.root = Path(self.temp_dir.name)
        self.empty_hooks = self.root / "empty-hooks"
        self.empty_templates = self.root / "empty-templates"
        self.empty_hooks.mkdir()
        self.empty_templates.mkdir()
        self.repository = self.root / "repository"
        self.project = self.repository / "ai_collaboration_studio"
        self.formal = self.root / "formal-source"
        self.project.mkdir(parents=True)
        self.formal.mkdir()
        self._git("init")
        self._git("config", "user.name", "Promotion Plan Test")
        self._git("config", "user.email", "promotion-plan@example.invalid")
        self._git("config", "core.autocrlf", "false")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _git(self, *arguments: str) -> str:
        return self._git_at(self.repository, *arguments)

    def _git_at(self, cwd: Path, *arguments: str) -> str:
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.upper().startswith("GIT_"):
                environment.pop(key, None)
        environment.update({
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
        })
        result = subprocess.run(
            [
                os.fspath(promotion._git_executable_path()),
                "--no-pager",
                "-c",
                f"core.hooksPath={self.empty_hooks}",
                "-c",
                f"init.templateDir={self.empty_templates}",
                "-c",
                "commit.gpgSign=false",
                *arguments,
            ],
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if result.returncode != 0:
            self.fail(
                f"git {' '.join(arguments)} failed: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    @staticmethod
    def _write(root: Path, relative: str, payload: bytes) -> None:
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

    def _commit(self, message: str) -> str:
        self._git("add", "-A")
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD")

    def _plan(self, base: str, tip: str) -> dict[str, object]:
        with mock.patch.object(promotion, "REPOSITORY_ROOT", self.repository):
            return promotion.build_formal_source_promotion_plan(
                formal_source_root=self.formal,
                base_commit=base,
                tip_commit=tip,
            )

    def test_classifies_crlf_add_existing_tip_and_manual_merge_deterministically(self) -> None:
        self._write(self.project, ".gitattributes", b"*.txt text\n")
        self._write(self.project, "clean.txt", b"base clean\n")
        self._write(self.project, "manual.txt", b"base manual\n")
        self._write(self.project, "already.txt", b"base already\n")
        base = self._commit("base")

        self._write(self.project, "clean.txt", b"tip clean\n")
        self._write(self.project, "manual.txt", b"tip manual\n")
        self._write(self.project, "already.txt", b"tip already\n")
        self._write(self.project, "new.txt", b"tip new\n")
        tip = self._commit("tip")

        self._write(self.formal, "clean.txt", b"base clean\r\n")
        self._write(self.formal, "manual.txt", b"formal manual\n")
        self._write(self.formal, "already.txt", b"tip already\r\n")
        first = self._plan(base, tip)
        second = self._plan(base, tip)

        self.assertEqual(first, second)
        self.assertEqual(first["counts"], {
            "changes": {"added": 1, "modified": 3},
            "classifications": {
                "already_tip": 1,
                "clean_add": 1,
                "clean_apply": 1,
                "manual_merge_required": 1,
            },
        })
        by_path = {row["path"]: row for row in first["entries"]}
        self.assertEqual(by_path["clean.txt"]["classification"], "clean_apply")
        self.assertEqual(by_path["manual.txt"]["classification"], "manual_merge_required")
        self.assertEqual(by_path["already.txt"]["classification"], "already_tip")
        self.assertEqual(by_path["new.txt"]["classification"], "clean_add")
        self.assertEqual(first["separate_write_review"], {
            "eligible": False,
            "atomic_snapshot": False,
            "valid_as_write_precondition": False,
            "requires_fresh_locked_preview": True,
        })
        self.assertFalse(first["writes_authorized"])
        self.assertRegex(first["formal_root_binding"]["path_sha256"], r"^[0-9a-f]{64}$")
        unsigned = {key: value for key, value in first.items() if key != "plan_sha256"}
        self.assertEqual(first["plan_sha256"], _canonical_sha256(unsigned))

    def test_plan_binds_formal_root_and_missing_parent_identity(self) -> None:
        self._write(self.project, "nested/new.py", b"base\n")
        base = self._commit("base")
        self._write(self.project, "nested/new.py", b"tip\n")
        tip = self._commit("tip")
        first = self._plan(base, tip)

        second_root = self.root / "other-formal-source"
        second_root.mkdir()
        with mock.patch.object(promotion, "REPOSITORY_ROOT", self.repository):
            second = promotion.build_formal_source_promotion_plan(
                formal_source_root=second_root,
                base_commit=base,
                tip_commit=tip,
            )
        self.assertNotEqual(first["formal_root_binding"], second["formal_root_binding"])
        self.assertNotEqual(first["plan_sha256"], second["plan_sha256"])

        (self.formal / "nested").mkdir()
        third = self._plan(base, tip)
        self.assertNotEqual(first["entries"][0]["target_parent_chain"], third["entries"][0]["target_parent_chain"])
        self.assertNotEqual(first["plan_sha256"], third["plan_sha256"])

    def test_current_shape_fixture_is_exactly_106_34_3_and_not_ready(self) -> None:
        for index in range(37):
            relative = f"modified/file_{index:03d}.py"
            self._write(self.project, relative, f"BASE_{index}\n".encode())
        base = self._commit("base")
        for index in range(37):
            relative = f"modified/file_{index:03d}.py"
            self._write(self.project, relative, f"TIP_{index}\n".encode())
            formal_payload = (
                f"FORMAL_{index}\n" if index < 3 else f"BASE_{index}\n"
            ).encode()
            self._write(self.formal, relative, formal_payload)
        for index in range(106):
            self._write(
                self.project,
                f"added/file_{index:03d}.py",
                f"ADDED_{index}\n".encode(),
            )
        tip = self._commit("tip")

        report = self._plan(base, tip)
        self.assertEqual(report["scope"]["changed_path_count"], 143)
        self.assertEqual(report["counts"]["changes"], {"added": 106, "modified": 37})
        self.assertEqual(report["counts"]["classifications"], {
            "already_tip": 0,
            "clean_add": 106,
            "clean_apply": 34,
            "manual_merge_required": 3,
        })
        self.assertFalse(report["separate_write_review"]["eligible"])

    def test_formal_extras_are_not_enumerated_or_opened_and_nothing_is_written(self) -> None:
        self._write(self.project, "app.py", b"base\n")
        base = self._commit("base")
        self._write(self.project, "app.py", b"tip\n")
        tip = self._commit("tip")
        self._write(self.formal, "app.py", b"base\n")
        self._write(self.formal, ".env.local", b"CANARY=must-not-open\n")
        self._write(self.formal, "runtime/studio.sqlite3-wal", b"must-not-open")
        self._write(
            self.formal,
            "backend/telegram_intelligence/canary.py",
            b"must-not-open",
        )
        before = {
            path.relative_to(self.formal).as_posix(): (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
            for path in self.formal.rglob("*")
            if path.is_file()
        }
        original_open = promotion.os.open
        opened: list[Path] = []

        def recording_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            candidate = Path(path)  # type: ignore[arg-type]
            try:
                candidate.resolve().relative_to(self.formal.resolve())
            except (OSError, ValueError):
                pass
            else:
                opened.append(candidate)
            return original_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        with (
            mock.patch.object(promotion.os, "scandir", side_effect=AssertionError("no target enumeration")),
            mock.patch.object(promotion.os, "open", side_effect=recording_open),
        ):
            report = self._plan(base, tip)
        after = {
            path.relative_to(self.formal).as_posix(): (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
            for path in self.formal.rglob("*")
            if path.is_file()
        }
        self.assertEqual(before, after)
        self.assertEqual(
            {path.resolve() for path in opened},
            {(self.formal / "app.py").resolve()},
        )
        self.assertFalse(report["scope"]["target_extra_paths_evaluated"])
        self.assertFalse(report["safety"]["filesystem_writes"])

    def test_rejects_noncanonical_commits_and_non_linear_range(self) -> None:
        self._write(self.project, "app.py", b"base\n")
        base = self._commit("base")
        self._write(self.project, "app.py", b"tip\n")
        tip = self._commit("tip")
        with mock.patch.object(promotion, "REPOSITORY_ROOT", self.repository):
            for invalid in (base[:12], base.upper(), "g" * 40, True):
                with self.subTest(invalid=invalid):
                    with self.assertRaisesRegex(
                        promotion.FormalSourcePromotionPlanError,
                        "exact lowercase full commit",
                    ):
                        promotion.build_formal_source_promotion_plan(
                            formal_source_root=self.formal,
                            base_commit=invalid,  # type: ignore[arg-type]
                            tip_commit=tip,
                        )
            with self.assertRaisesRegex(
                promotion.FormalSourcePromotionPlanError,
                "must differ",
            ):
                promotion.build_formal_source_promotion_plan(
                    formal_source_root=self.formal,
                    base_commit=base,
                    tip_commit=base,
                )
            with self.assertRaisesRegex(
                promotion.FormalSourcePromotionPlanError,
                "must be an ancestor",
            ):
                promotion.build_formal_source_promotion_plan(
                    formal_source_root=self.formal,
                    base_commit=tip,
                    tip_commit=base,
                )

    def test_rejects_delete_before_any_formal_root_access(self) -> None:
        self._write(self.project, "deleted.py", b"base\n")
        base = self._commit("base")
        (self.project / "deleted.py").unlink()
        tip = self._commit("delete")
        with (
            mock.patch.object(promotion, "REPOSITORY_ROOT", self.repository),
            mock.patch.object(
                promotion,
                "_formal_root",
                side_effect=AssertionError("formal root must not be touched"),
            ),
        ):
            with self.assertRaisesRegex(
                promotion.FormalSourcePromotionPlanError,
                "Only added or modified",
            ):
                promotion.build_formal_source_promotion_plan(
                    formal_source_root=self.formal,
                    base_commit=base,
                    tip_commit=tip,
                )

    def test_rejects_secret_path_before_any_formal_root_access(self) -> None:
        self._write(self.project, ".env.local", b"SECRET=base\n")
        base = self._commit("base")
        self._write(self.project, ".env.local", b"SECRET=tip\n")
        tip = self._commit("tip")
        with (
            mock.patch.object(promotion, "REPOSITORY_ROOT", self.repository),
            mock.patch.object(
                promotion,
                "_formal_root",
                side_effect=AssertionError("formal root must not be touched"),
            ),
        ):
            with self.assertRaisesRegex(
                promotion.FormalSourcePromotionPlanError,
                "secret material",
            ):
                promotion.build_formal_source_promotion_plan(
                    formal_source_root=self.formal,
                    base_commit=base,
                    tip_commit=tip,
                )

    def test_custom_clean_filter_is_rejected_without_executing_it(self) -> None:
        self._write(self.project, "filtered.txt", b"base\n")
        base = self._commit("base")
        self._write(self.project, ".gitattributes", b"*.txt filter=evil\n")
        self._write(self.project, "filtered.txt", b"tip\n")
        tip = self._commit("tip")
        marker = self.root / "filter-executed"
        self._git("config", "filter.evil.clean", f"python -c \"open(r'{marker}','wb').write(b'x')\"")
        self._write(self.formal, "filtered.txt", b"base\n")

        with mock.patch.object(promotion, "REPOSITORY_ROOT", self.repository):
            with self.assertRaisesRegex(
                promotion.FormalSourcePromotionPlanError,
                "Custom filters",
            ):
                promotion.build_formal_source_promotion_plan(
                    formal_source_root=self.formal,
                    base_commit=base,
                    tip_commit=tip,
                )
        self.assertFalse(marker.exists())

    def test_ambient_git_routing_and_trace_variables_are_scrubbed(self) -> None:
        self._write(self.project, "app.py", b"base\n")
        base = self._commit("base")
        self._write(self.project, "app.py", b"tip\n")
        tip = self._commit("tip")
        self._write(self.formal, "app.py", b"base\n")

        other = self.root / "other-repository"
        other.mkdir()
        self._git_at(other, "init")
        trace_marker = self.root / "git-trace.log"
        poisoned = {
            "GIT_DIR": os.fspath(other / ".git"),
            "GIT_WORK_TREE": os.fspath(other),
            "GIT_TRACE": os.fspath(trace_marker),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": os.fspath(other / "objects"),
        }
        with mock.patch.dict(os.environ, poisoned, clear=False):
            report = self._plan(base, tip)
        self.assertEqual(
            report["counts"]["classifications"]["clean_apply"],
            1,
        )
        self.assertFalse(trace_marker.exists())

    def test_blob_summary_keeps_mode_when_two_paths_share_one_oid(self) -> None:
        self._write(self.project, "one.txt", b"base\n")
        self._write(self.project, "two.txt", b"base\n")
        self._git("add", "-A")
        self._git("update-index", "--chmod=+x", "ai_collaboration_studio/two.txt")
        self._git("commit", "-m", "base")
        base = self._git("rev-parse", "HEAD")
        self._write(self.project, "one.txt", b"tip\n")
        self._write(self.project, "two.txt", b"tip\n")
        self._git("add", "-A")
        self._git("update-index", "--chmod=+x", "ai_collaboration_studio/two.txt")
        self._git("commit", "-m", "tip")
        tip = self._git("rev-parse", "HEAD")
        self._write(self.formal, "one.txt", b"base\n")
        self._write(self.formal, "two.txt", b"base\n")

        report = self._plan(base, tip)
        by_path = {row["path"]: row for row in report["entries"]}
        self.assertEqual(by_path["one.txt"]["base"]["mode"], "100644")
        self.assertEqual(by_path["two.txt"]["base"]["mode"], "100755")
        self.assertEqual(by_path["one.txt"]["tip"]["mode"], "100644")
        self.assertEqual(by_path["two.txt"]["tip"]["mode"], "100755")

    def test_tree_lookup_treats_bracket_filename_as_one_literal_path(self) -> None:
        self._write(self.project, "a.py", b"unchanged\n")
        base = self._commit("base")
        self._write(self.project, "[a].py", b"tip\n")
        tip = self._commit("tip")

        report = self._plan(base, tip)

        self.assertEqual(report["scope"]["changed_path_count"], 1)
        self.assertEqual(report["entries"][0]["path"], "[a].py")
        self.assertEqual(report["entries"][0]["classification"], "clean_add")

    def test_git_control_output_limit_is_enforced_by_streaming_reader(self) -> None:
        with mock.patch.object(promotion, "REPOSITORY_ROOT", self.repository):
            with self.assertRaisesRegex(
                promotion.FormalSourcePromotionPlanError,
                "output exceeds its closed limit",
            ):
                promotion._run_git(
                    ["rev-parse", "--show-toplevel"],
                    max_stdout_bytes=1,
                )

    def test_repository_local_git_executable_is_rejected_before_launch(self) -> None:
        candidate = self.project / "git.exe"
        candidate.write_bytes(b"not an executable")
        trusted_git = promotion._git_executable_path()
        promotion._git_executable_binding.cache_clear()
        try:
            with (
                mock.patch.object(promotion, "REPOSITORY_ROOT", self.repository),
                mock.patch.dict(
                    os.environ,
                    {
                        "PATH": os.pathsep.join((
                            os.fspath(self.project),
                            os.fspath(trusted_git.parent),
                        )),
                    },
                    clear=False,
                ),
            ):
                selected, _signature = promotion._git_executable_binding()
                self.assertEqual(selected, trusted_git)
        finally:
            promotion._git_executable_binding.cache_clear()

    def test_repository_git_symlink_is_skipped_before_resolution(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows executable discovery regression")
        trusted_git = promotion._git_executable_path()
        external = self.root / "external-git.exe"
        external.write_bytes(b"not an executable")
        candidate = self.project / "git.exe"
        try:
            candidate.symlink_to(external)
        except OSError as exc:  # pragma: no cover - host privilege policy
            self.skipTest(f"symlinks unavailable: {exc}")
        promotion._git_executable_binding.cache_clear()
        try:
            with (
                mock.patch.object(promotion, "REPOSITORY_ROOT", self.repository),
                mock.patch.dict(
                    os.environ,
                    {
                        "PATH": os.pathsep.join((
                            os.fspath(self.project),
                            os.fspath(trusted_git.parent),
                        )),
                    },
                    clear=False,
                ),
            ):
                selected, _signature = promotion._git_executable_binding()
                self.assertEqual(selected, trusted_git)
        finally:
            promotion._git_executable_binding.cache_clear()

    def test_oversized_blob_is_rejected_before_content_process_starts(self) -> None:
        entry = promotion._TreeEntry(
            mode="100644",
            object_type="blob",
            oid="a" * 40,
            path="ai_collaboration_studio/large.bin",
        )
        size_result = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=str(promotion._MAX_FILE_BYTES + 1).encode("ascii"),
            stderr=b"",
        )
        with (
            mock.patch.object(promotion, "_run_git", return_value=size_result),
            mock.patch.object(
                promotion.subprocess,
                "Popen",
                side_effect=AssertionError("content process must not start"),
            ),
        ):
            with self.assertRaisesRegex(
                promotion.FormalSourcePromotionPlanError,
                "exceeds the promotion size limit",
            ):
                promotion._blob_record(entry)

    def test_hardlinked_formal_target_is_rejected(self) -> None:
        self._write(self.project, "app.py", b"base\n")
        base = self._commit("base")
        self._write(self.project, "app.py", b"tip\n")
        tip = self._commit("tip")
        source = self.formal / "app.py"
        source.write_bytes(b"base\n")
        alias = self.formal / "alias.py"
        try:
            os.link(source, alias)
        except OSError as exc:  # pragma: no cover - unusual temporary filesystem
            self.skipTest(f"hard links unavailable: {exc}")
        with self.assertRaisesRegex(
            promotion.FormalSourcePromotionPlanError,
            "independent regular file",
        ):
            self._plan(base, tip)

    def test_same_size_second_read_change_is_rejected(self) -> None:
        path = self.formal / "app.py"
        path.write_bytes(b"AAAA")
        original = promotion._read_descriptor_pass
        calls = 0

        def changed_second_pass(descriptor: int, *, collect: bool):
            nonlocal calls
            calls += 1
            result = original(descriptor, collect=collect)
            if calls == 2:
                return result[0], hashlib.sha256(b"BBBB").hexdigest(), result[2]
            return result

        with mock.patch.object(
            promotion,
            "_read_descriptor_pass",
            side_effect=changed_second_pass,
        ):
            with self.assertRaisesRegex(
                promotion.FormalSourcePromotionPlanError,
                "changed while it was inspected",
            ):
                promotion._read_formal_file_twice(path)

    def test_path_policy_rejects_aliases_devices_and_unsafe_templates(self) -> None:
        unsafe = (
            "ai_collaboration_studio/../escape.py",
            "ai_collaboration_studio/runtime/data.json",
            "ai_collaboration_studio/.env.local/credential.txt",
            "ai_collaboration_studio/credentials.json/value.txt",
            "ai_collaboration_studio/data.sqlite3/chunk.bin",
            "ai_collaboration_studio/.env.example/nested.txt",
            "ai_collaboration_studio/data.sqlite3-wal",
            "ai_collaboration_studio/CON.txt",
            "ai_collaboration_studio/LONGNA~1/file.py",
            "ai_collaboration_studio/e\u0301.py",
            "ai_collaboration_studio/trailing. ",
            "ai_collaboration_studio/name:stream",
            "ai_collaboration_studio/.env.production",
            'ai_collaboration_studio/bad"name.py',
            "ai_collaboration_studio/bad?.py",
            "ai_collaboration_studio/bad|.py",
            "ai_collaboration_studio/bad<.py",
            "ai_collaboration_studio/bad>.py",
            "ai_collaboration_studio/bad*.py",
        )
        for path in unsafe:
            with self.subTest(path=path):
                with self.assertRaises(promotion.FormalSourcePromotionPlanError):
                    promotion._validate_promotion_path(path)
        self.assertEqual(
            promotion._validate_promotion_path(
                "ai_collaboration_studio/.env.example"
            ),
            ".env.example",
        )
        with self.assertRaisesRegex(
            promotion.FormalSourcePromotionPlanError,
            "case-insensitive collision",
        ):
            promotion._validate_all_paths([
                ("A", "ai_collaboration_studio/Case.py"),
                ("A", "ai_collaboration_studio/case.py"),
            ])


if __name__ == "__main__":
    unittest.main()
