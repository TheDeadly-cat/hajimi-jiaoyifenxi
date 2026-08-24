from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4


if __package__:
    from scripts import bootstrap_ai_collaboration_studio as bootstrap
    from scripts import create_versioned_source_backup as source_backup
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import bootstrap_ai_collaboration_studio as bootstrap
    from scripts import create_versioned_source_backup as source_backup


SECURITY_SCAN_VERSION = "static_security_scan_v1"
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_HASHED_LOCK_RE = re.compile(
    r"[A-Za-z0-9_.-]+==[^\s]+ --hash=sha256:[0-9a-f]{64}"
)
_USES_RE = re.compile(
    r"^\s*(?:-\s*)?uses:\s*([^\s#]+)",
    flags=re.MULTILINE,
)
_TEXT_SUFFIXES = frozenset({
    ".bat",
    ".cfg",
    ".cmd",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".log",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".svg",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
})
_MAX_TEXT_BYTES = 8 * 1024 * 1024
_PLACEHOLDER_MARKERS = (
    "example",
    "fixture",
    "must-not-leak",
    "not-a-real",
    "placeholder",
    "redacted",
)
_SECRET_RULES = (
    (
        "private_key_block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "github_access_token",
        re.compile(
            r"(?:github_pat_[A-Za-z0-9_]{40,}|gh[pousr]_[A-Za-z0-9]{30,})"
        ),
    ),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    (
        "openai_api_key",
        re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    ),
    (
        "slack_access_token",
        re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    ),
)
_PROVIDER_ENV_NAMES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "FUTU_PASSWORD_MD5",
)


def _check(
    check_id: str,
    ok: bool,
    summary: str,
    **evidence: Any,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": check_id,
        "ok": bool(ok),
        "summary": summary,
    }
    if evidence:
        result["evidence"] = evidence
    return result


def source_inventory(project_root: Path) -> list[Any]:
    root = Path(os.path.abspath(os.fspath(project_root)))
    source_backup._assert_existing_chain_has_no_links(root)
    if not root.is_dir():
        raise source_backup.SourceBackupError("source root is not a directory")
    return source_backup._scan_source(root)


def check_source_boundary(records: list[Any]) -> dict[str, Any]:
    required_directories = {
        ".git",
        ".npm-cache",
        "__pycache__",
        "dist",
        "node_modules",
        "runtime",
        "secrets",
    }
    secret_examples = (
        ".env",
        ".env.production",
        "credentials.json",
        "id_rsa",
        "private.pem",
        "service.key",
    )
    boundary_ok = required_directories.issubset(
        source_backup._EXCLUDED_DIRECTORY_NAMES
    ) and all(source_backup._is_secret_filename(name) for name in secret_examples)
    return _check(
        "publishable_source_boundary",
        boundary_ok,
        "Source inventory reuses the versioned-backup exclusion contract.",
        files_scanned=len(records),
        required_excluded_directories=len(required_directories),
        sensitive_filename_examples=len(secret_examples),
    )


def _workflow_files(project_root: Path) -> list[Path]:
    workflow_root = project_root / ".github" / "workflows"
    return sorted(
        {
            *workflow_root.glob("*.yml"),
            *workflow_root.glob("*.yaml"),
        },
        key=lambda path: path.name.casefold(),
    )


def check_workflow_action_pins(project_root: Path) -> dict[str, Any]:
    workflows = _workflow_files(project_root)
    issues: list[dict[str, Any]] = []
    references = 0
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        for match in _USES_RE.finditer(text):
            reference = match.group(1)
            references += 1
            if reference.startswith("./"):
                continue
            action, separator, revision = reference.rpartition("@")
            if not separator or not action or _FULL_SHA_RE.fullmatch(revision) is None:
                issues.append({
                    "path": workflow.relative_to(project_root).as_posix(),
                    "line": text.count("\n", 0, match.start()) + 1,
                    "rule": "EXTERNAL_ACTION_NOT_FULL_SHA",
                })
    ok = bool(workflows) and references > 0 and not issues
    return _check(
        "workflow_action_pins",
        ok,
        "Every external workflow action must use a reviewed 40-character commit.",
        workflow_count=len(workflows),
        action_references=references,
        issues=issues,
    )


def check_isolated_ci_entrypoints(project_root: Path) -> dict[str, Any]:
    workflows = _workflow_files(project_root)
    combined = "\n".join(path.read_text(encoding="utf-8") for path in workflows)
    required = (
        "AI_STUDIO_SKIP_LOCAL_ENV",
        "npm.cmd --prefix frontend test",
        "scripts/bootstrap_ai_collaboration_studio.py",
        "scripts/run_backend_tests_isolated.py",
        "scripts/run_fresh_source_smoke.py",
        "scripts/generate_dependency_inventory.py",
        "scripts/run_isolated_release_drill.py",
        "scripts/run_static_security_checks.py",
    )
    forbidden = {
        "DIRECT_NODE_TEST": re.compile(r"\bnode(?:\.exe)?\s+--test\b", re.I),
        "DIRECT_UNITTEST": re.compile(
            r"\bpython(?:\.exe)?\s+-m\s+unittest\b",
            re.I,
        ),
        "DIRECT_SERVER_START": re.compile(r"\bserver\.py\b", re.I),
        "FORMAL_OR_PROVIDER_PORT": re.compile(
            r"(?<!\d)(?:8770|11111|18787)(?!\d)"
        ),
    }
    missing = [value for value in required if value not in combined]
    blocked = [rule for rule, pattern in forbidden.items() if pattern.search(combined)]
    provider_env_issues = [
        name
        for name in _PROVIDER_ENV_NAMES
        if re.search(
            rf'^\s+{re.escape(name)}:\s*""\s*$',
            combined,
            flags=re.MULTILINE,
        )
        is None
    ]
    return _check(
        "isolated_ci_entrypoints",
        bool(workflows) and not missing and not blocked and not provider_env_issues,
        "CI must remain isolated and use only guarded test and smoke entrypoints.",
        missing_requirements=missing,
        forbidden_rules=blocked,
        provider_env_issues=provider_env_issues,
    )


def _named_function(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    return next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        ),
        None,
    )


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        candidate
        for candidate in ast.walk(node)
        if isinstance(candidate, ast.Call) and _call_name(candidate) == name
    ]


def _is_active_socket_call(call: ast.Call) -> bool:
    name = _call_name(call)
    if name not in {"connect", "connect_ex", "create_connection"}:
        return False
    if (
        name == "connect"
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "sqlite3"
    ):
        return False
    return True


def check_passive_protected_port_inspection(project_root: Path) -> dict[str, Any]:
    fresh_path = project_root / "scripts" / "run_fresh_source_smoke.py"
    release_path = project_root / "scripts" / "run_isolated_release_drill.py"
    issues: list[str] = []
    evidence: dict[str, Any] = {
        "fresh_source_passive_checks": 0,
        "fresh_source_random_port_checks": 0,
        "fresh_source_listener_table_queries": 0,
        "release_passive_checks": 0,
        "release_listener_table_queries": 0,
        "release_active_socket_calls": 0,
    }
    if not fresh_path.is_file():
        issues.append("FRESH_SOURCE_SMOKE_MISSING")
    else:
        fresh_source = fresh_path.read_text(encoding="utf-8")
        try:
            fresh_tree = ast.parse(fresh_source, filename=str(fresh_path))
        except SyntaxError:
            issues.append("FRESH_SOURCE_SMOKE_PARSE_FAILED")
        else:
            run_smoke = _named_function(fresh_tree, "run_smoke")
            protected_state = _named_function(fresh_tree, "protected_port_state")
            if run_smoke is None:
                issues.append("FRESH_RUN_SMOKE_MISSING")
            else:
                passive_calls = _calls(run_smoke, "protected_port_state")
                random_calls = _calls(run_smoke, "port_open")
                evidence["fresh_source_passive_checks"] = len(passive_calls)
                evidence["fresh_source_random_port_checks"] = len(random_calls)
                if len(passive_calls) < 3:
                    issues.append("FRESH_PASSIVE_CHECK_COVERAGE_INCOMPLETE")
                random_only = len(random_calls) == 2 and all(
                    len(call.args) == 1
                    and not call.keywords
                    and isinstance(call.args[0], ast.Name)
                    and call.args[0].id == "port"
                    for call in random_calls
                )
                if not random_only:
                    issues.append("FRESH_RANDOM_PORT_PROBE_DRIFT")
            if protected_state is None:
                issues.append("FRESH_PROTECTED_STATE_MISSING")
            else:
                listener_calls = _calls(
                    protected_state,
                    "_windows_listener_ports",
                )
                evidence["fresh_source_listener_table_queries"] = len(
                    listener_calls
                )
                if len(listener_calls) < 2:
                    issues.append("FRESH_PASSIVE_LISTENER_QUERY_INCOMPLETE")
                if any(
                    _calls(protected_state, name)
                    for name in ("connect", "connect_ex", "create_connection")
                ):
                    issues.append("FRESH_PROTECTED_STATE_ACTIVE_PROBE")
            if "GetExtendedTcpTable" not in fresh_source:
                issues.append("FRESH_WINDOWS_IP_HELPER_MISSING")

    if not release_path.is_file():
        issues.append("RELEASE_DRILL_MISSING")
    else:
        release_source = release_path.read_text(encoding="utf-8")
        try:
            release_tree = ast.parse(release_source, filename=str(release_path))
        except SyntaxError:
            issues.append("RELEASE_DRILL_PARSE_FAILED")
        else:
            run_drill = _named_function(release_tree, "run_drill")
            protected_state = _named_function(
                release_tree,
                "_protected_port_state",
            )
            if run_drill is None:
                issues.append("RELEASE_RUN_DRILL_MISSING")
            else:
                passive_calls = _calls(run_drill, "_protected_port_state")
                evidence["release_passive_checks"] = len(passive_calls)
                if len(passive_calls) < 2:
                    issues.append("RELEASE_PASSIVE_CHECK_COVERAGE_INCOMPLETE")
            if protected_state is None:
                issues.append("RELEASE_PROTECTED_STATE_MISSING")
            else:
                listener_calls = _calls(
                    protected_state,
                    "_windows_listener_ports",
                )
                evidence["release_listener_table_queries"] = len(
                    listener_calls
                )
                if len(listener_calls) < 2:
                    issues.append("RELEASE_PASSIVE_LISTENER_QUERY_INCOMPLETE")
            active_calls = sum(
                1
                for candidate in ast.walk(release_tree)
                if isinstance(candidate, ast.Call)
                and _is_active_socket_call(candidate)
            )
            evidence["release_active_socket_calls"] = active_calls
            if active_calls:
                issues.append("RELEASE_ACTIVE_SOCKET_PROBE")
            if "GetExtendedTcpTable" not in release_source:
                issues.append("RELEASE_WINDOWS_IP_HELPER_MISSING")

    return _check(
        "passive_protected_port_inspection",
        not issues,
        "Protected ports must use passive Windows listener-table inspection.",
        inspection="windows_ip_helper_get_extended_tcp_table",
        issues=issues,
        **evidence,
    )


def check_python_lock(project_root: Path) -> dict[str, Any]:
    lock_path = project_root / "requirements-lock-win-py314.txt"
    if not lock_path.is_file():
        return _check(
            "python_dependency_lock",
            False,
            "The Windows CPython 3.14 dependency lock is missing.",
        )
    raw = lock_path.read_bytes()
    lines = [
        line.strip()
        for line in raw.decode("ascii").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    try:
        requirements = bootstrap.parse_hashed_lock(lock_path)
    except (bootstrap.BootstrapError, OSError, UnicodeError):
        requirements = {}
    exact = bool(lines) and all(_HASHED_LOCK_RE.fullmatch(line) for line in lines)
    return _check(
        "python_dependency_lock",
        bool(requirements) and exact and len(lines) == len(requirements),
        "Every Python dependency must be exact and protected by SHA-256.",
        package_count=len(requirements),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def check_logging_contract(project_root: Path) -> dict[str, Any]:
    logging_path = project_root / "backend" / "structured_logging.py"
    http_path = project_root / "backend" / "http_server.py"
    server_path = project_root / "server.py"
    if not all(path.is_file() for path in (logging_path, http_path, server_path)):
        return _check(
            "structured_redacted_logging",
            False,
            "Structured host logging files are incomplete.",
        )
    logging_source = logging_path.read_text(encoding="utf-8")
    http_source = http_path.read_text(encoding="utf-8")
    server_source = server_path.read_text(encoding="utf-8")
    required = (
        "studio_log_event_v1",
        "def sanitize_fields",
        "def classify_request_target",
        "def log_request",
        "def log_error",
        "server_start_failed",
    )
    combined = logging_source + "\n" + http_source + "\n" + server_source
    missing = [value for value in required if value not in combined]
    direct_prints = [
        path
        for path, source in (
            ("backend/http_server.py", http_source),
            ("server.py", server_source),
        )
        if "print(" in source
    ]
    raw_exit = "raise SystemExit(str(" in server_source
    return _check(
        "structured_redacted_logging",
        not missing and not direct_prints and not raw_exit,
        "Host lifecycle and HTTP metadata must use bounded JSONL events.",
        missing_contracts=missing,
        direct_print_files=direct_prints,
        raw_exception_exit=raw_exit,
    )


def _is_explicit_test_placeholder(relative_path: str, value: str) -> bool:
    lowered = value.casefold()
    return relative_path.startswith("tests/") and any(
        marker in lowered for marker in _PLACEHOLDER_MARKERS
    )


def find_high_confidence_secrets(
    project_root: Path,
    records: list[Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    inventory = records if records is not None else source_inventory(project_root)
    findings: list[dict[str, Any]] = []
    scanned = 0
    for record in inventory:
        suffix = Path(record.relative_path).suffix.casefold()
        if suffix not in _TEXT_SUFFIXES:
            continue
        scanned += 1
        size = int(record.fingerprint[3])
        if size > _MAX_TEXT_BYTES:
            findings.append({
                "path": record.relative_path,
                "line": 0,
                "rule": "TEXT_SOURCE_EXCEEDS_SCAN_LIMIT",
            })
            continue
        try:
            text = record.absolute_path.read_text(encoding="utf-8")
        except UnicodeError:
            findings.append({
                "path": record.relative_path,
                "line": 0,
                "rule": "TEXT_SOURCE_IS_NOT_UTF8",
            })
            continue
        for rule, pattern in _SECRET_RULES:
            for match in pattern.finditer(text):
                if _is_explicit_test_placeholder(record.relative_path, match.group(0)):
                    continue
                findings.append({
                    "path": record.relative_path,
                    "line": text.count("\n", 0, match.start()) + 1,
                    "rule": rule,
                })
                if len(findings) >= 100:
                    return findings, scanned
    return findings, scanned


def check_high_confidence_secrets(
    project_root: Path,
    records: list[Any],
) -> dict[str, Any]:
    findings, scanned = find_high_confidence_secrets(project_root, records)
    return _check(
        "high_confidence_secret_patterns",
        not findings,
        "Publishable text must not contain high-confidence credential material.",
        text_files_scanned=scanned,
        findings=findings,
    )


def run_security_checks(project_root: Path) -> dict[str, Any]:
    root = Path(os.path.abspath(os.fspath(project_root)))
    try:
        records = source_inventory(root)
    except (source_backup.SourceBackupError, OSError):
        checks = [
            _check(
                "source_inventory",
                False,
                "The publishable source inventory could not be established safely.",
            )
        ]
    else:
        checks = [
            check_source_boundary(records),
            check_workflow_action_pins(root),
            check_isolated_ci_entrypoints(root),
            check_passive_protected_port_inspection(root),
            check_python_lock(root),
            check_logging_contract(root),
            check_high_confidence_secrets(root, records),
        ]
    passed = sum(1 for check in checks if check["ok"])
    failed = len(checks) - passed
    return {
        "schema_version": SECURITY_SCAN_VERSION,
        "ok": failed == 0,
        "summary": {
            "checks": len(checks),
            "passed": passed,
            "failed": failed,
        },
        "checks": checks,
        "boundaries": {
            "offline": True,
            "network_requests": 0,
            "sast_complete": False,
            "dependency_cve_audit": False,
            "penetration_test": False,
        },
    }


def write_report(path: Path, payload: dict[str, Any]) -> None:
    target = Path(os.path.abspath(os.fspath(path)))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        encoding="ascii",
    )
    os.replace(temporary, target)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run deterministic offline security checks on publishable source"
    )
    parser.add_argument("--project-root")
    parser.add_argument("--report")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    root = (
        Path(arguments.project_root)
        if arguments.project_root
        else Path(__file__).resolve().parents[1]
    )
    try:
        result = run_security_checks(root)
        if arguments.report:
            write_report(Path(arguments.report), result)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {
            "schema_version": SECURITY_SCAN_VERSION,
            "ok": False,
            "error_code": "STATIC_SECURITY_SCAN_FAILED",
            "exception_type": type(exc).__name__,
        }
    output = json.dumps(result, ensure_ascii=True, indent=2)
    print(output, file=sys.stdout if result.get("ok") else sys.stderr)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
