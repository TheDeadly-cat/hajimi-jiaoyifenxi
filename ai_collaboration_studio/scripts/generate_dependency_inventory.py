from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote


INVENTORY_VERSION = "dependency_inventory_v1"
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PYTHON_LOCK_PATH = Path("requirements-lock-win-py314.txt")
_NPM_LOCK_PATH = Path("frontend/package-lock.json")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PYTHON_REQUIREMENT_RE = re.compile(
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"==(?P<version>[^\s]+)"
    r"\s+--hash=sha256:(?P<digest>[0-9a-f]{64})"
)
_SRI_RE = re.compile(
    r"(?P<algorithm>sha1|sha256|sha384|sha512)-"
    r"(?P<digest>[A-Za-z0-9+/]+={0,2})"
)
_SRI_BYTES = {"sha1": 20, "sha256": 32, "sha384": 48, "sha512": 64}
_MAX_INVENTORY_BYTES = 16 * 1024 * 1024


class DependencyInventoryError(RuntimeError):
    """Raised when a dependency lock or inventory boundary is invalid."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _value_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_regular_text(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise DependencyInventoryError(f"lock input is not a regular file: {path.name}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DependencyInventoryError(f"lock input is unreadable: {path.name}") from exc


def _canonical_python_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _parse_python_lock(path: Path) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(
        _read_regular_text(path).splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PYTHON_REQUIREMENT_RE.fullmatch(line)
        if match is None:
            raise DependencyInventoryError(
                f"Python lock line {line_number} is not exact and singly SHA-256 hashed"
            )
        name = _canonical_python_name(match.group("name"))
        if name in seen:
            raise DependencyInventoryError(f"duplicate Python package: {name}")
        seen.add(name)
        version = match.group("version")
        digest = match.group("digest")
        components.append({
            "component_id": f"pypi:{name}@{version}",
            "dependency_role": "locked_unspecified",
            "development": False,
            "ecosystem": "pypi",
            "integrity": [{"algorithm": "sha256", "digest": digest}],
            "locator": name,
            "name": name,
            "optional": False,
            "peer": False,
            "purl": f"pkg:pypi/{quote(name, safe='')}@{quote(version, safe='')}",
            "scope": "runtime",
            "version": version,
        })
    if not components:
        raise DependencyInventoryError("Python lock has no packages")
    return components


def _parse_sri(value: Any, *, locator: str) -> list[dict[str, str]]:
    if not isinstance(value, str) or not value.strip():
        raise DependencyInventoryError(f"npm package has no integrity: {locator}")
    result: list[dict[str, str]] = []
    for token in value.split():
        match = _SRI_RE.fullmatch(token)
        if match is None:
            raise DependencyInventoryError(f"npm package has invalid integrity: {locator}")
        algorithm = match.group("algorithm")
        digest = match.group("digest")
        try:
            decoded = base64.b64decode(digest, validate=True)
        except ValueError as exc:
            raise DependencyInventoryError(
                f"npm package has invalid integrity encoding: {locator}"
            ) from exc
        if len(decoded) != _SRI_BYTES[algorithm]:
            raise DependencyInventoryError(
                f"npm package has invalid {algorithm} digest length: {locator}"
            )
        result.append({"algorithm": algorithm, "digest": digest})
    return result


def _npm_package_name(locator: str) -> str:
    marker = "node_modules/"
    if marker not in locator:
        raise DependencyInventoryError(f"unsupported npm package locator: {locator}")
    name = locator.rsplit(marker, 1)[1]
    if not name or name.startswith("/") or "node_modules" in name:
        raise DependencyInventoryError(f"invalid npm package locator: {locator}")
    if name.startswith("@"):
        if name.count("/") != 1:
            raise DependencyInventoryError(f"invalid scoped npm package: {locator}")
    elif "/" in name:
        raise DependencyInventoryError(f"invalid npm package name: {locator}")
    return name


def _npm_purl(name: str, version: str) -> str:
    if name.startswith("@"):
        namespace, package = name[1:].split("/", 1)
        return (
            f"pkg:npm/{quote(namespace, safe='')}/{quote(package, safe='')}"
            f"@{quote(version, safe='')}"
        )
    return f"pkg:npm/{quote(name, safe='')}@{quote(version, safe='')}"


def _string_map(value: Any, *, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise DependencyInventoryError(f"npm root {field} is not an object")
    result: dict[str, str] = {}
    for raw_name, raw_range in value.items():
        if not isinstance(raw_name, str) or not isinstance(raw_range, str):
            raise DependencyInventoryError(f"npm root {field} is not textual")
        result[raw_name] = raw_range
    return result


def _parse_npm_lock(
    path: Path,
) -> tuple[dict[str, str], list[dict[str, Any]], int]:
    try:
        lock = json.loads(_read_regular_text(path))
    except json.JSONDecodeError as exc:
        raise DependencyInventoryError("npm lock is not valid JSON") from exc
    if not isinstance(lock, dict) or lock.get("lockfileVersion") != 3:
        raise DependencyInventoryError("npm lockfileVersion must be 3")
    packages = lock.get("packages")
    if not isinstance(packages, dict) or not isinstance(packages.get(""), dict):
        raise DependencyInventoryError("npm lock has no root package entry")
    root = packages[""]
    project_name = lock.get("name")
    project_version = lock.get("version")
    if (
        not isinstance(project_name, str)
        or not project_name
        or root.get("name", project_name) != project_name
        or not isinstance(project_version, str)
        or not project_version
        or root.get("version", project_version) != project_version
    ):
        raise DependencyInventoryError("npm lock project identity is inconsistent")
    runtime_dependencies = _string_map(
        root.get("dependencies"),
        field="dependencies",
    )
    development_dependencies = _string_map(
        root.get("devDependencies"),
        field="devDependencies",
    )
    overlap = set(runtime_dependencies).intersection(development_dependencies)
    if overlap:
        raise DependencyInventoryError(
            f"npm direct dependency scopes overlap: {sorted(overlap)[0]}"
        )
    direct_names = set(runtime_dependencies).union(development_dependencies)
    components: list[dict[str, Any]] = []
    direct_found: set[str] = set()
    for raw_locator, raw_entry in packages.items():
        if raw_locator == "":
            continue
        if not isinstance(raw_locator, str) or not isinstance(raw_entry, dict):
            raise DependencyInventoryError("npm package entry is invalid")
        if raw_entry.get("link") is True:
            raise DependencyInventoryError(f"npm link package is unsupported: {raw_locator}")
        name = _npm_package_name(raw_locator)
        version = raw_entry.get("version")
        if not isinstance(version, str) or not version:
            raise DependencyInventoryError(f"npm package has no exact version: {raw_locator}")
        integrity = _parse_sri(raw_entry.get("integrity"), locator=raw_locator)
        direct = raw_locator == f"node_modules/{name}" and name in direct_names
        if direct:
            direct_found.add(name)
        development = raw_entry.get("dev") is True or (
            direct and name in development_dependencies
        )
        components.append({
            "component_id": f"npm:{raw_locator}@{version}",
            "dependency_role": "direct" if direct else "transitive",
            "development": development,
            "ecosystem": "npm",
            "integrity": integrity,
            "locator": raw_locator,
            "name": name,
            "optional": raw_entry.get("optional") is True,
            "peer": raw_entry.get("peer") is True,
            "purl": _npm_purl(name, version),
            "scope": "development" if development else "runtime",
            "version": version,
        })
    missing_direct = sorted(direct_names.difference(direct_found))
    if missing_direct:
        raise DependencyInventoryError(
            f"npm direct dependency has no exact package entry: {missing_direct[0]}"
        )
    if not components:
        raise DependencyInventoryError("npm lock has no package components")
    return (
        {"id": project_name, "version": project_version},
        components,
        int(lock["lockfileVersion"]),
    )


def build_inventory(project_root: str | Path = _PROJECT_ROOT) -> dict[str, Any]:
    root = Path(os.path.abspath(os.fspath(project_root)))
    python_lock = root / _PYTHON_LOCK_PATH
    npm_lock = root / _NPM_LOCK_PATH
    python_components = _parse_python_lock(python_lock)
    project, npm_components, npm_lockfile_version = _parse_npm_lock(npm_lock)
    components = sorted(
        [*python_components, *npm_components],
        key=lambda row: (
            row["ecosystem"],
            row["name"],
            row["version"],
            row["locator"],
        ),
    )
    component_ids = [row["component_id"] for row in components]
    if len(component_ids) != len(set(component_ids)):
        raise DependencyInventoryError("dependency component identity is ambiguous")
    algorithms = Counter(
        digest["algorithm"]
        for component in components
        for digest in component["integrity"]
    )
    report: dict[str, Any] = {
        "schema_version": INVENTORY_VERSION,
        "ok": True,
        "project": project,
        "inputs": [
            {
                "ecosystem": "pypi",
                "path": _PYTHON_LOCK_PATH.as_posix(),
                "sha256": _file_sha256(python_lock),
            },
            {
                "ecosystem": "npm",
                "lockfile_version": npm_lockfile_version,
                "path": _NPM_LOCK_PATH.as_posix(),
                "sha256": _file_sha256(npm_lock),
            },
        ],
        "summary": {
            "components": len(components),
            "integrity_algorithms": dict(sorted(algorithms.items())),
            "npm_components": len(npm_components),
            "npm_direct_development": sum(
                row["ecosystem"] == "npm"
                and row["dependency_role"] == "direct"
                and row["scope"] == "development"
                for row in components
            ),
            "npm_direct_runtime": sum(
                row["ecosystem"] == "npm"
                and row["dependency_role"] == "direct"
                and row["scope"] == "runtime"
                for row in components
            ),
            "npm_transitive": sum(
                row["ecosystem"] == "npm"
                and row["dependency_role"] == "transitive"
                for row in components
            ),
            "python_components": len(python_components),
        },
        "components": components,
        "boundaries": {
            "absolute_source_paths_included": False,
            "licenses_evaluated": False,
            "network_requests": 0,
            "offline": True,
            "registry_metadata_queried": False,
            "sbom_standard_conformance_claimed": False,
            "vulnerabilities_evaluated": False,
        },
    }
    report["inventory_sha256"] = _value_sha256(report)
    return report


def validate_inventory(report: dict[str, Any]) -> None:
    expected_keys = {
        "schema_version",
        "ok",
        "project",
        "inputs",
        "summary",
        "components",
        "boundaries",
        "inventory_sha256",
    }
    if (
        not isinstance(report, dict)
        or set(report) != expected_keys
        or report.get("schema_version") != INVENTORY_VERSION
    ):
        raise DependencyInventoryError("dependency inventory is not closed and versioned")
    digest = report.get("inventory_sha256")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise DependencyInventoryError("dependency inventory digest is invalid")
    unsigned = dict(report)
    unsigned.pop("inventory_sha256")
    if _value_sha256(unsigned) != digest:
        raise DependencyInventoryError("dependency inventory digest mismatch")
    components = report.get("components")
    if not isinstance(components, list) or report.get("summary", {}).get(
        "components"
    ) != len(components):
        raise DependencyInventoryError("dependency inventory component count mismatch")


def verify_inventory_file(
    project_root: str | Path,
    report_path: str | Path,
) -> dict[str, Any]:
    root = Path(os.path.abspath(os.fspath(project_root)))
    path = Path(os.path.abspath(os.fspath(report_path)))
    if not path.is_file() or path.is_symlink():
        raise DependencyInventoryError("dependency inventory report is not a regular file")
    if path.stat().st_size > _MAX_INVENTORY_BYTES:
        raise DependencyInventoryError("dependency inventory report exceeds the size limit")
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DependencyInventoryError("dependency inventory report is unreadable") from exc
    if not isinstance(stored, dict):
        raise DependencyInventoryError("dependency inventory report is not an object")
    validate_inventory(stored)
    expected = build_inventory(root)
    if stored != expected:
        raise DependencyInventoryError(
            "dependency inventory does not match the authoritative locks"
        )
    return stored


def write_inventory(
    project_root: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    root = Path(os.path.abspath(os.fspath(project_root)))
    output = Path(os.path.abspath(os.fspath(output_path)))
    system_temp = Path(tempfile.gettempdir()).resolve()
    try:
        output.resolve(strict=False).relative_to(system_temp)
    except ValueError as exc:
        raise DependencyInventoryError("inventory output must remain in system temp") from exc
    try:
        output.resolve(strict=False).relative_to(root.resolve())
    except ValueError:
        pass
    else:
        raise DependencyInventoryError("inventory output must remain outside source")
    if not output.parent.is_dir() or output.parent.is_symlink():
        raise DependencyInventoryError("inventory output parent must be an existing directory")
    if output.exists():
        raise DependencyInventoryError("inventory output already exists")
    report = build_inventory(root)
    validate_inventory(report)
    descriptor: int | None = None
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            descriptor = None
            json.dump(report, target, ensure_ascii=False, indent=2, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        try:
            output.unlink()
        except FileNotFoundError:
            pass
        raise
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a deterministic offline dependency inventory"
    )
    parser.add_argument(
        "--project-root",
        default=str(_PROJECT_ROOT),
        help="source root containing the authoritative Python and npm locks",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--output",
        help="new JSON report path under the system temporary directory",
    )
    action.add_argument(
        "--verify",
        metavar="REPORT",
        help="verify an existing report against the authoritative locks",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.verify:
            report = verify_inventory_file(args.project_root, args.verify)
            output_path = Path(os.path.abspath(args.verify))
            operation = "verify"
        else:
            report = write_inventory(args.project_root, args.output)
            output_path = Path(os.path.abspath(args.output))
            operation = "generate"
    except DependencyInventoryError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({
        "component_count": report["summary"]["components"],
        "file_sha256": _file_sha256(output_path),
        "inventory_sha256": report["inventory_sha256"],
        "ok": True,
        "operation": operation,
        "output": str(output_path),
        "schema_version": INVENTORY_VERSION,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
