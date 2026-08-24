from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path


from scripts import generate_dependency_inventory as inventory


class DependencyInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_root = Path(__file__).resolve().parents[1]

    def _write_fixture(
        self,
        root: Path,
        *,
        python_line: str | None = None,
        npm_integrity: str | None = None,
    ) -> None:
        frontend = root / "frontend"
        frontend.mkdir(parents=True)
        python_digest = "a" * 64
        (root / "requirements-lock-win-py314.txt").write_text(
            python_line or f"example==1.0.0 --hash=sha256:{python_digest}\n",
            encoding="utf-8",
        )
        if npm_integrity is None:
            npm_integrity = "sha512-" + base64.b64encode(b"b" * 64).decode("ascii")
        lock = {
            "name": "fixture-project",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "": {
                    "name": "fixture-project",
                    "version": "1.0.0",
                    "dependencies": {"example": "1.0.0"},
                },
                "node_modules/example": {
                    "version": "1.0.0",
                    "integrity": npm_integrity,
                },
            },
        }
        (frontend / "package-lock.json").write_text(
            json.dumps(lock),
            encoding="utf-8",
        )

    def test_current_locks_produce_deterministic_closed_inventory(self) -> None:
        first = inventory.build_inventory(self.project_root)
        second = inventory.build_inventory(self.project_root)
        self.assertEqual(first, second)
        inventory.validate_inventory(first)
        self.assertEqual(first["project"], {
            "id": "ai-collaboration-studio",
            "version": "0.1.0",
        })
        self.assertEqual(first["summary"]["python_components"], 10)
        self.assertEqual(first["summary"]["npm_components"], 155)
        self.assertEqual(first["summary"]["components"], 165)
        self.assertEqual(first["summary"]["npm_direct_runtime"], 3)
        self.assertEqual(first["summary"]["npm_direct_development"], 3)
        self.assertEqual(first["summary"]["npm_transitive"], 149)
        self.assertEqual(first["inputs"], [
            {
                "ecosystem": "pypi",
                "path": "requirements-lock-win-py314.txt",
                "sha256": "22e149f601a83f26833cff20d32cbf5aaa01242906de0d8cf9d53a6e569ea699",
            },
            {
                "ecosystem": "npm",
                "lockfile_version": 3,
                "path": "frontend/package-lock.json",
                "sha256": "875e3714c1a69ec74d3510afa73a82e98c0d9cac6ea168587d0ba631a7116ffc",
            },
        ])
        self.assertFalse(first["boundaries"]["vulnerabilities_evaluated"])
        self.assertFalse(first["boundaries"]["licenses_evaluated"])
        self.assertFalse(first["boundaries"]["sbom_standard_conformance_claimed"])
        rendered = json.dumps(first, ensure_ascii=False)
        self.assertNotIn(str(self.project_root), rendered)

    def test_unhashed_python_and_unhashed_npm_entries_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-inventory-invalid-python-"
        ) as temp_dir:
            root = Path(temp_dir)
            self._write_fixture(root, python_line="example==1.0.0\n")
            with self.assertRaises(inventory.DependencyInventoryError):
                inventory.build_inventory(root)

        with tempfile.TemporaryDirectory(
            prefix="ai-studio-inventory-invalid-npm-"
        ) as temp_dir:
            root = Path(temp_dir)
            self._write_fixture(root, npm_integrity="")
            with self.assertRaises(inventory.DependencyInventoryError):
                inventory.build_inventory(root)

    def test_output_is_temp_only_exclusive_and_outside_source(self) -> None:
        source_output = self.project_root / "dependency-inventory.json"
        self.assertFalse(source_output.exists())
        with self.assertRaises(inventory.DependencyInventoryError):
            inventory.write_inventory(self.project_root, source_output)
        self.assertFalse(source_output.exists())

        with tempfile.TemporaryDirectory(
            prefix="ai-studio-inventory-output-"
        ) as temp_dir:
            output = Path(temp_dir) / "dependency-inventory.json"
            report = inventory.write_inventory(self.project_root, output)
            self.assertTrue(output.is_file())
            stored = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(stored, report)
            self.assertEqual(
                inventory.verify_inventory_file(self.project_root, output),
                report,
            )
            with self.assertRaises(inventory.DependencyInventoryError):
                inventory.write_inventory(self.project_root, output)

    def test_verify_rejects_tampering_and_authoritative_lock_drift(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-inventory-tamper-"
        ) as temp_dir:
            root = Path(temp_dir)
            valid = root / "valid.json"
            inventory.write_inventory(self.project_root, valid)
            tampered = json.loads(valid.read_text(encoding="utf-8"))
            tampered["project"]["version"] = "tampered"
            tampered_path = root / "tampered.json"
            tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(inventory.DependencyInventoryError):
                inventory.verify_inventory_file(self.project_root, tampered_path)

        with tempfile.TemporaryDirectory(
            prefix="ai-studio-inventory-lock-drift-"
        ) as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            self._write_fixture(source)
            report_path = root / "inventory.json"
            inventory.write_inventory(source, report_path)
            python_lock = source / "requirements-lock-win-py314.txt"
            python_lock.write_text(
                "example==1.0.1 --hash=sha256:" + "c" * 64 + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(inventory.DependencyInventoryError):
                inventory.verify_inventory_file(source, report_path)


if __name__ == "__main__":
    unittest.main()
