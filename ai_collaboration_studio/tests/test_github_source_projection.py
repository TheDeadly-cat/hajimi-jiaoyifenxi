from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.create_github_source_projection import (
    GitHubProjectionError,
    create_github_projection,
)


WORKFLOW = """name: Fixture

on:
  push:

jobs:
  test:
    runs-on: windows-latest
    defaults:
      run:
        shell: pwsh
        working-directory: ai_collaboration_studio
    steps:
      - uses: actions/checkout@1111111111111111111111111111111111111111
      - uses: actions/setup-node@2222222222222222222222222222222222222222
        with:
          cache-dependency-path: ai_collaboration_studio/frontend/package-lock.json
      - run: npm.cmd --prefix frontend test
"""


class GitHubSourceProjectionTests(unittest.TestCase):
    def create_fixture(self, root: Path) -> Path:
        source = root / "source"
        (source / ".github" / "workflows").mkdir(parents=True)
        (source / "delivery" / "repository-root").mkdir(parents=True)
        (source / "frontend").mkdir()
        (source / "runtime").mkdir()
        (source / ".github" / "workflows" / "isolated-validation.yml").write_text(
            WORKFLOW,
            encoding="utf-8",
        )
        (source / "delivery" / "repository-root" / "README.md").write_text(
            "# Fixture repository\n",
            encoding="utf-8",
        )
        (
            source
            / "delivery"
            / "repository-root"
            / "run_ai_collaboration_studio.cmd.template"
        ).write_text(
            "@powershell.exe -File \"%~dp0ai_collaboration_studio\\scripts\\start_ai_collaboration_studio.ps1\"\n",
            encoding="utf-8",
        )
        (source / "frontend" / "package-lock.json").write_text(
            '{"lockfileVersion":3}\n',
            encoding="utf-8",
        )
        (source / ".env.example").write_text("SAFE_TEMPLATE=\n", encoding="utf-8")
        (source / ".env.local").write_text("SECRET=not-for-projection\n", encoding="utf-8")
        (source / "runtime" / "formal.sqlite3").write_bytes(b"not-for-projection")
        return source

    def test_projection_is_deterministic_and_places_delivery_files_at_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = self.create_fixture(root)
            first = create_github_projection(
                source_root=source,
                destination_root=root / "projection-one",
            )
            second = create_github_projection(
                source_root=source,
                destination_root=root / "projection-two",
            )

            self.assertEqual(first["source_total_sha256"], second["source_total_sha256"])
            self.assertEqual(
                first["projected_total_sha256"],
                second["projected_total_sha256"],
            )
            projection = root / "projection-one"
            root_workflow = projection / ".github" / "workflows" / "isolated-validation.yml"
            nested_workflow = (
                projection
                / "ai_collaboration_studio"
                / ".github"
                / "workflows"
                / "isolated-validation.yml"
            )
            self.assertEqual(root_workflow.read_bytes(), nested_workflow.read_bytes())
            self.assertTrue((projection / "README.md").is_file())
            self.assertTrue((projection / "run_ai_collaboration_studio.cmd").is_file())
            self.assertTrue(
                (projection / "ai_collaboration_studio" / ".env.example").is_file()
            )
            self.assertFalse(
                (projection / "ai_collaboration_studio" / ".env.local").exists()
            )
            self.assertFalse(
                (projection / "ai_collaboration_studio" / "runtime").exists()
            )
            self.assertEqual(first["forbidden_paths"], [])

    def test_existing_or_nested_destination_is_rejected_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = self.create_fixture(root)
            existing = root / "existing"
            existing.mkdir()
            sentinel = existing / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(GitHubProjectionError, "must not already exist"):
                create_github_projection(
                    source_root=source,
                    destination_root=existing,
                )
            with self.assertRaisesRegex(GitHubProjectionError, "must be disjoint"):
                create_github_projection(
                    source_root=source,
                    destination_root=source / "projection",
                )
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_workflow_without_repository_root_paths_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            source = self.create_fixture(root)
            workflow = source / ".github" / "workflows" / "isolated-validation.yml"
            workflow.write_text(
                WORKFLOW.replace(
                    "ai_collaboration_studio/frontend/package-lock.json",
                    "frontend/package-lock.json",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                GitHubProjectionError,
                "repository-layout requirements",
            ):
                create_github_projection(
                    source_root=source,
                    destination_root=root / "projection",
                )


if __name__ == "__main__":
    unittest.main()
