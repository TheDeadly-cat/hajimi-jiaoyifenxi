"""Fail-closed operator CLI for one fixed 24-hour official-source soak.

Only ``preview``, ``start`` and ``verify`` are public commands.  Argument and
confirmation failures are handled before importing configuration or any
runtime/DB module.  The production entry point has no injectable duration,
clock, database path, runner, or source mode.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import sys
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence, TextIO


sys.dont_write_bytecode = True

SOURCE_MONITORING_SOAK_CLI_VERSION = "source_monitoring_soak_cli_v1"
SOURCE_MONITORING_SOAK_CONFIRMATION = "START_24H_SOURCE_MONITORING_SOAK"
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_CODE_FILE_COUNT = 512
_MAX_CODE_FILE_BYTES = 8 * 1024 * 1024
_MAX_CODE_TOTAL_BYTES = 64 * 1024 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")
_VERDICT_ISSUE_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,99}\Z")
_ID_RE = re.compile(r"source_soak_(?:campaign|session)_[0-9a-f]{32}\Z")
_RUNTIME_ID_RE = re.compile(r"source_monitor_runtime_[0-9a-f]{32}\Z")
_FIXED_BUNDLE_NAMES = frozenset(
    {"plan.json", "baseline-inventory.json", "ledger.jsonl", "final-inventory.json"}
)
_START_BUNDLE_NAMES = frozenset({"plan.json", "baseline-inventory.json"})
_PRODUCTION_BINDING_FIELDS = (
    "settings_sha256",
    "registry_sha256",
    "code_identity_sha256",
    "db_startup_identity_sha256",
    "db_schema_sha256",
    "preview_sha256",
    "enabled_adapter_keys_sha256",
)
_VERDICT_FIELDS = frozenset(
    {
        "version",
        "overall_status",
        "continuity_verdict",
        "production_binding_verdict",
        "database_verdict",
        "source_acceptance_verdict",
        "overall_acceptance",
        "identity",
        "counts",
        "timing",
        "bindings",
        "issue_count",
        "issues",
        "issues_truncated",
        "verdict_sha256",
        "safety",
    }
)
_VERDICT_COUNT_FIELDS = frozenset(
    {
        "ledger_record_count",
        "runtime_sample_count",
        "run_terminal_count",
        "unique_terminal_run_count",
        "expected_adapter_count",
        "covered_adapter_count",
    }
)
_VERDICT_TIMING_FIELDS = frozenset(
    {
        "required_duration_ns",
        "declared_sample_interval_ns",
        "declared_maximum_sample_gap_ns",
        "observed_elapsed_ns",
        "maximum_observed_sample_gap_ns",
    }
)
_VERDICT_BINDING_FIELDS = frozenset(
    {
        "ledger_terminal",
        "last_record_sha256",
        "baseline_inventory_sha256",
        "final_inventory_sha256",
        "database_delta_verdict_sha256",
        "expected_production_bindings_sha256",
        "observed_production_bindings_sha256",
    }
)
_VERIFIER_SAFETY = {
    "database_reads_performed": 0,
    "database_writes_performed": 0,
    "network_requests_performed": 0,
    "provider_calls_performed": 0,
    "model_calls_performed": 0,
    "market_calls_performed": 0,
    "formal_rounds_created": 0,
    "execution_capability": "none",
    "live_trading_allowed": False,
}
_RETAINED_LIVE_OWNERS: list[tuple[Any, Any]] = []


class _CliFailure(RuntimeError):
    def __init__(self, code: str, *, exit_code: int = 2) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _CliFailure("SOURCE_MONITORING_SOAK_ARGUMENT_INVALID")


@dataclass(frozen=True, slots=True)
class _SoakCliDependencies:
    database_path_loader: Callable[[], Any] | None = None
    owner_acquirer: Callable[[Any], Any] | None = None
    readiness_checker: Callable[[Any], Any] | None = None
    store_opener: Callable[[Path], Any] | None = None
    settings_loader: Callable[[], Any] | None = None
    registry_builder: Callable[[Any], Any] | None = None
    repository_builder: Callable[[Any], Any] | None = None
    inventory_builder: Callable[[Any], Any] | None = None
    inventory_writer: Callable[..., Any] | None = None
    inventory_loader: Callable[[Any], Any] | None = None
    plan_builder: Callable[..., Any] | None = None
    plan_writer: Callable[..., Any] | None = None
    plan_loader: Callable[[Any], Any] | None = None
    observer_factory: Callable[[Any], Any] | None = None
    runtime_builder: Callable[[Any, Any, Any], Any] | None = None
    runner_factory: Callable[..., Any] | None = None
    verifier: Callable[..., Any] | None = None
    canonical_sha256: Callable[[Any], str] | None = None
    code_identity_builder: Callable[[], str] | None = None
    id_factory: Callable[[str], str] | None = None


@dataclass(slots=True)
class _ExecutionState:
    owner_acquired: bool = False
    database_reads_possible: bool = False
    runtime_boundary_entered: bool = False
    artifact_writes: int | None = 0


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(add_help=False, prog="run_source_monitoring_soak.py")
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", add_help=False)
    preview.add_argument("--bundle", required=True)
    preview.add_argument("--mode", required=True)
    start = commands.add_parser("start", add_help=False)
    start.add_argument("--bundle", required=True)
    start.add_argument("--confirm", required=True)
    start.add_argument("--preview-sha256", required=True)
    verify = commands.add_parser("verify", add_help=False)
    verify.add_argument("--bundle", required=True)
    return parser


def _help_payload() -> dict[str, Any]:
    return {
        "version": SOURCE_MONITORING_SOAK_CLI_VERSION,
        "command": "help",
        "ok": True,
        "scope": "official_sources_only",
        "required_duration_hours": 24,
        "commands": [
            "preview --bundle <existing-empty-directory> --mode official",
            (
                "start --bundle <directory> --confirm "
                "START_24H_SOURCE_MONITORING_SOAK --preview-sha256 <sha256>"
            ),
            "verify --bundle <directory>",
        ],
        "safety": _safety(_ExecutionState()),
    }


def _safety(state: _ExecutionState) -> dict[str, Any]:
    runtime_unknown = state.runtime_boundary_entered
    return {
        "database_reads_performed": (
            None if state.database_reads_possible else 0
        ),
        "database_writes_performed": None if runtime_unknown else 0,
        "network_requests_performed": None if runtime_unknown else 0,
        "artifact_writes_performed": state.artifact_writes,
        "provider_calls_performed": 0,
        "model_calls_performed": 0,
        "market_calls_performed": 0,
        "formal_rounds_created": 0,
        "database_owner_acquired": state.owner_acquired,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def _error_payload(command: str, code: str, state: _ExecutionState) -> dict[str, Any]:
    clean = code if _ERROR_CODE_RE.fullmatch(code) else "SOURCE_MONITORING_SOAK_FAILED"
    return {
        "version": SOURCE_MONITORING_SOAK_CLI_VERSION,
        "command": command if command in {"preview", "start", "verify"} else "invalid",
        "ok": False,
        "status": "indeterminate" if state.runtime_boundary_entered else "failed",
        "error_code": clean,
        "safety": _safety(state),
    }


def _emit(stream: TextIO, payload: dict[str, Any]) -> None:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        encoded = json.dumps(
            _error_payload("invalid", "SOURCE_MONITORING_SOAK_OUTPUT_INVALID", _ExecutionState()),
            sort_keys=True,
            separators=(",", ":"),
        )
    if len(encoded.encode("utf-8")) > _MAX_OUTPUT_BYTES:
        encoded = json.dumps(
            _error_payload("invalid", "SOURCE_MONITORING_SOAK_OUTPUT_TOO_LARGE", _ExecutionState()),
            sort_keys=True,
            separators=(",", ":"),
        )
    stream.write(encoded + "\n")


def _safe_error(exc: BaseException) -> tuple[str, int]:
    code = getattr(exc, "code", "")
    if type(code) is str and _ERROR_CODE_RE.fullmatch(code):
        exit_code = getattr(exc, "exit_code", 2)
        return code, exit_code if type(exit_code) is int and exit_code in {1, 2, 3} else 2
    return "SOURCE_MONITORING_SOAK_UNEXPECTED", 1


def _reject_duplicate_options(argv: Sequence[str]) -> None:
    for option in ("--bundle", "--mode", "--confirm", "--preview-sha256"):
        count = sum(token == option or token.startswith(option + "=") for token in argv)
        if count > 1:
            raise _CliFailure("SOURCE_MONITORING_SOAK_ARGUMENT_INVALID")


def _is_reparse(metadata: os.stat_result) -> bool:
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    return bool(flag and attributes & flag)


def _assert_unaliased_path(path: Path) -> None:
    absolute = path.absolute()
    candidates = list(reversed(absolute.parents)) + [absolute]
    for candidate in candidates:
        if not os.path.lexists(os.fspath(candidate)):
            continue
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise _CliFailure("SOURCE_MONITORING_SOAK_BUNDLE_PATH_INVALID")


def _bundle(value: Any, *, expected_names: frozenset[str]) -> Path:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise _CliFailure("SOURCE_MONITORING_SOAK_BUNDLE_PATH_INVALID")
    requested = Path(value).expanduser()
    try:
        _assert_unaliased_path(requested)
        resolved = requested.resolve(strict=True)
        before = resolved.lstat()
        if not stat.S_ISDIR(before.st_mode) or _is_reparse(before):
            raise OSError("not an independent directory")
        with os.scandir(resolved) as entries:
            names = frozenset(entry.name for entry in entries)
        after = resolved.lstat()
    except _CliFailure:
        raise
    except (OSError, RuntimeError):
        raise _CliFailure("SOURCE_MONITORING_SOAK_BUNDLE_PATH_INVALID") from None
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise _CliFailure("SOURCE_MONITORING_SOAK_BUNDLE_IDENTITY_CHANGED")
    if names != expected_names:
        raise _CliFailure("SOURCE_MONITORING_SOAK_BUNDLE_CONTENTS_INVALID")
    return resolved


def _database_path(deps: _SoakCliDependencies) -> Any:
    if deps.database_path_loader is not None:
        return deps.database_path_loader()
    from .config import DATABASE_PATH

    return DATABASE_PATH


def _acquire_owner(database_path: Any, deps: _SoakCliDependencies) -> Any:
    if deps.owner_acquirer is not None:
        return deps.owner_acquirer(database_path)
    from .instance_ownership import DatabaseInstanceOwner, InstanceAlreadyRunning

    try:
        return DatabaseInstanceOwner(database_path).acquire(
            metadata={"role": "source_monitoring_soak"}
        )
    except InstanceAlreadyRunning as exc:
        raise _CliFailure("SOURCE_MONITORING_INSTANCE_ACTIVE", exit_code=3) from exc


def _readiness(database_path: Any, deps: _SoakCliDependencies) -> dict[str, Any]:
    if deps.readiness_checker is not None:
        value = deps.readiness_checker(database_path)
    else:
        from .database_migration import (
            DatabaseMigrationRequired,
            assert_database_ready_for_startup,
        )

        try:
            value = assert_database_ready_for_startup(database_path)
        except DatabaseMigrationRequired as exc:
            raise _CliFailure(
                "SOURCE_MONITORING_DATABASE_MIGRATION_REQUIRED"
            ) from exc
    if type(value) is not dict or type(value.get("startup_identity")) is not dict:
        raise _CliFailure("SOURCE_MONITORING_DATABASE_READINESS_INVALID")
    schema = value.get("schema_sha256")
    if type(schema) is not str or _SHA256_RE.fullmatch(schema) is None:
        raise _CliFailure("SOURCE_MONITORING_DATABASE_READINESS_INVALID")
    return value


def _open_store(database_path: Any, deps: _SoakCliDependencies) -> Any:
    if deps.store_opener is not None:
        return deps.store_opener(Path(database_path))
    from .store import StudioStore

    return StudioStore._open_existing_schema(Path(database_path))


def _settings(deps: _SoakCliDependencies) -> Any:
    if deps.settings_loader is not None:
        value = deps.settings_loader()
    else:
        from .source_monitoring.settings import load_source_monitoring_settings

        value = load_source_monitoring_settings()
    if (
        getattr(value, "enabled", None) is not True
        or getattr(value, "auto_start", None) is not True
        or getattr(value, "official_only", None) is not True
        or getattr(value, "allow_readonly_market", None) is not False
        or getattr(value, "dry_run", None) is not False
        or getattr(value, "trading_impact_rules_enabled", None) is not False
        or not callable(getattr(value, "to_dict", None))
    ):
        raise _CliFailure("SOURCE_MONITORING_SOAK_SETTINGS_UNSAFE")
    return value


def _registry(settings: Any, deps: _SoakCliDependencies) -> Any:
    if deps.registry_builder is not None:
        value = deps.registry_builder(settings)
    else:
        from .source_monitoring.default_registry import build_official_source_registry

        profile_id = getattr(settings, "source_profile", "")
        value = build_official_source_registry(**({"source_profile": profile_id} if profile_id else {}))
    if (
        getattr(value, "official_only", None) is not True
        or type(getattr(value, "adapter_keys", None)) is not tuple
        or not callable(getattr(value, "metadata_for", None))
        or not callable(getattr(value, "to_dict", None))
    ):
        raise _CliFailure("SOURCE_MONITORING_SOAK_REGISTRY_UNSAFE")
    from .source_monitoring.profiles import require_profile_registry

    require_profile_registry(value, getattr(settings, "source_profile", ""))
    return value


def _repository(store: Any, deps: _SoakCliDependencies) -> Any:
    if deps.repository_builder is not None:
        value = deps.repository_builder(store)
    else:
        from .source_monitoring.state_repository import SourceMonitoringStateRepository

        value = SourceMonitoringStateRepository(store)
    if not callable(getattr(value, "get_state", None)):
        raise _CliFailure("SOURCE_MONITORING_SOAK_REPOSITORY_INVALID")
    return value


def _inventory(database_path: Any, deps: _SoakCliDependencies) -> dict[str, Any]:
    if deps.inventory_builder is not None:
        value = deps.inventory_builder(database_path)
    else:
        from .source_monitoring.soak_db_inventory import build_soak_db_inventory

        value = build_soak_db_inventory(database_path)
    _inventory_binding(value)
    return value


def _inventory_binding(value: Any) -> tuple[int, str]:
    if type(value) is not dict or type(value.get("runs")) is not list:
        raise _CliFailure("SOURCE_MONITORING_SOAK_INVENTORY_INVALID")
    count = value.get("run_count")
    digest = value.get("inventory_sha256")
    if (
        type(count) is not int
        or count < 0
        or len(value["runs"]) != count
        or type(digest) is not str
        or _SHA256_RE.fullmatch(digest) is None
    ):
        raise _CliFailure("SOURCE_MONITORING_SOAK_INVENTORY_INVALID")
    return count, digest


def _write_inventory(value: dict[str, Any], path: Path, deps: _SoakCliDependencies) -> Any:
    if deps.inventory_writer is not None:
        return deps.inventory_writer(inventory=value, artifact_path=path)
    from .source_monitoring.soak_db_inventory import write_soak_db_inventory_exclusive

    return write_soak_db_inventory_exclusive(inventory=value, artifact_path=path)


def _load_inventory(path: Path, deps: _SoakCliDependencies) -> dict[str, Any]:
    if deps.inventory_loader is not None:
        value = deps.inventory_loader(path)
    else:
        from .source_monitoring.soak_db_inventory import load_soak_db_inventory

        value = load_soak_db_inventory(path)
    _inventory_binding(value)
    return value


def _build_plan(deps: _SoakCliDependencies, **values: Any) -> dict[str, Any]:
    if deps.plan_builder is not None:
        value = deps.plan_builder(**values)
    else:
        from .source_monitoring.soak_plan import build_source_monitoring_soak_plan

        value = build_source_monitoring_soak_plan(**values)
    from .source_monitoring.soak_plan import validate_source_monitoring_soak_plan

    return validate_source_monitoring_soak_plan(value)


def _write_plan(value: dict[str, Any], path: Path, deps: _SoakCliDependencies) -> Any:
    if deps.plan_writer is not None:
        return deps.plan_writer(artifact_path=path, plan=value)
    from .source_monitoring.soak_plan import write_source_monitoring_soak_plan_exclusive

    return write_source_monitoring_soak_plan_exclusive(artifact_path=path, plan=value)


def _load_plan(path: Path, deps: _SoakCliDependencies) -> dict[str, Any]:
    if deps.plan_loader is not None:
        value = deps.plan_loader(path)
    else:
        from .source_monitoring.soak_plan import load_source_monitoring_soak_plan

        value = load_source_monitoring_soak_plan(path)
    from .source_monitoring.soak_plan import validate_source_monitoring_soak_plan

    value = validate_source_monitoring_soak_plan(value)
    if value.get("mode") != "official":
        raise _CliFailure("SOURCE_MONITORING_SOAK_PLAN_INVALID")
    return value


def _canonical(value: Any, deps: _SoakCliDependencies) -> str:
    if deps.canonical_sha256 is not None:
        digest = deps.canonical_sha256(value)
    else:
        from .source_monitoring.contracts import canonical_sha256

        digest = canonical_sha256(value)
    if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
        raise _CliFailure("SOURCE_MONITORING_SOAK_HASH_INVALID")
    return digest


def _adapter_descriptors(registry: Any, repository: Any) -> tuple[dict[str, Any], ...]:
    keys = registry.adapter_keys
    if (
        not 1 <= len(keys) <= 50
        or tuple(sorted(keys)) != keys
        or len(set(keys)) != len(keys)
    ):
        raise _CliFailure("SOURCE_MONITORING_SOAK_REGISTRY_UNSAFE")
    descriptors: list[dict[str, Any]] = []
    for key in keys:
        if type(key) is not str:
            raise _CliFailure("SOURCE_MONITORING_SOAK_REGISTRY_UNSAFE")
        metadata = registry.metadata_for(key)
        if (
            getattr(metadata, "adapter_key", None) != key
            or getattr(metadata, "official_source", None) is not True
            or type(getattr(metadata, "max_market_calls_per_poll", None)) is not int
            or getattr(metadata, "max_market_calls_per_poll", None) != 0
            or getattr(metadata, "execution_capability", None) != "none"
            or getattr(metadata, "live_trading_allowed", None) is not False
        ):
            raise _CliFailure("SOURCE_MONITORING_SOAK_REGISTRY_UNSAFE")
        state_value = repository.get_state(key)
        if state_value is None or state_value.get("enabled") is not True:
            continue
        config_version = getattr(metadata, "config_version", None)
        state_version = state_value.get("state_version")
        checkpoint_sha256 = state_value.get("checkpoint_sha256")
        if (
            state_value.get("adapter_key") != key
            or type(config_version) is not str
            or state_value.get("config_version") != config_version
            or type(state_version) is not int
            or state_version < 1
            or type(checkpoint_sha256) is not str
            or _SHA256_RE.fullmatch(checkpoint_sha256) is None
        ):
            raise _CliFailure("SOURCE_MONITORING_SOAK_ADAPTER_STATE_INVALID")
        descriptors.append(
            {
                "adapter_key": key,
                "config_version": config_version,
                "state_version": state_version,
                "checkpoint_sha256": checkpoint_sha256,
            }
        )
    if not descriptors:
        raise _CliFailure("SOURCE_MONITORING_SOAK_NO_ENABLED_ADAPTERS")
    return tuple(descriptors)


def _id(kind: str, deps: _SoakCliDependencies) -> str:
    value = (
        deps.id_factory(kind)
        if deps.id_factory is not None
        else f"source_soak_{kind}_{uuid.uuid4().hex}"
    )
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        raise _CliFailure("SOURCE_MONITORING_SOAK_IDENTITY_INVALID")
    return value


def _code_identity(deps: _SoakCliDependencies) -> str:
    if deps.code_identity_builder is not None:
        value = deps.code_identity_builder()
    else:
        value = _build_code_identity()
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise _CliFailure("SOURCE_MONITORING_SOAK_CODE_IDENTITY_INVALID")
    return value


def _build_code_identity() -> str:
    raw_module = Path(__file__).absolute()
    _assert_unaliased_path(raw_module)
    project = raw_module.resolve(strict=True).parents[1]
    source_root = project / "backend" / "source_monitoring"
    host_relatives = {
        "backend/config.py",
        "backend/database_migration.py",
        "backend/instance_ownership.py",
        "backend/market/__init__.py",
        "backend/market/futu_readonly.py",
        "backend/market/ir_releases.py",
        "backend/market/official_http.py",
        "backend/market/official_macro.py",
        "backend/market/sec_edgar.py",
        "backend/path_identity.py",
        "backend/source_inbox_contracts.py",
        "backend/source_inbox_import_ux.py",
        "backend/source_inbox_service.py",
        "backend/source_inbox_trading_impact.py",
        "backend/source_monitoring_soak_cli.py",
        "backend/store.py",
        "backend/structured_logging.py",
        "scripts/run_source_monitoring_soak.py",
    }
    candidates = {path.resolve(strict=True) for path in source_root.rglob("*.py")}
    candidates.update((project / relative).resolve(strict=True) for relative in host_relatives)
    if not 1 <= len(candidates) <= _MAX_CODE_FILE_COUNT:
        raise _CliFailure("SOURCE_MONITORING_SOAK_CODE_IDENTITY_INVALID")
    rows: list[tuple[str, str]] = []
    total = 0
    for path in sorted(candidates, key=lambda item: item.relative_to(project).as_posix()):
        try:
            path.relative_to(project)
            _assert_unaliased_path(path)
            before = path.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or int(before.st_nlink) != 1
                or _is_reparse(before)
                or not 0 < before.st_size <= _MAX_CODE_FILE_BYTES
            ):
                raise OSError("unsafe code identity input")
            flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0)
            flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                remaining = before.st_size
                digest = hashlib.sha256()
                while remaining:
                    chunk = os.read(descriptor, min(1024 * 1024, remaining))
                    if not chunk:
                        raise OSError("short code identity read")
                    digest.update(chunk)
                    remaining -= len(chunk)
                after_fd = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            after_path = path.lstat()
        except (OSError, RuntimeError, ValueError):
            raise _CliFailure("SOURCE_MONITORING_SOAK_CODE_IDENTITY_INVALID") from None
        before_sig = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_fd_sig = (
            after_fd.st_dev,
            after_fd.st_ino,
            after_fd.st_size,
            after_fd.st_mtime_ns,
        )
        after_path_sig = (
            after_path.st_dev,
            after_path.st_ino,
            after_path.st_size,
            after_path.st_mtime_ns,
        )
        if before_sig != after_fd_sig or after_fd_sig != after_path_sig:
            raise _CliFailure("SOURCE_MONITORING_SOAK_CODE_IDENTITY_CHANGED")
        total += before.st_size
        if total > _MAX_CODE_TOTAL_BYTES:
            raise _CliFailure("SOURCE_MONITORING_SOAK_CODE_IDENTITY_INVALID")
        rows.append((path.relative_to(project).as_posix(), digest.hexdigest()))
    seal = hashlib.sha256()
    seal.update(b"source_monitoring_soak_code_identity_v1\0")
    for relative, digest in rows:
        seal.update(relative.encode("utf-8"))
        seal.update(b"\0")
        seal.update(digest.encode("ascii"))
        seal.update(b"\n")
    return seal.hexdigest()


def _plan_values(
    *,
    plan_identity: dict[str, str],
    readiness: dict[str, Any],
    settings: Any,
    registry: Any,
    descriptors: tuple[dict[str, Any], ...],
    baseline: dict[str, Any],
    code_identity_sha256: str,
    deps: _SoakCliDependencies,
) -> dict[str, Any]:
    baseline_count, baseline_sha = _inventory_binding(baseline)
    return {
        "campaign_id": plan_identity["campaign_id"],
        "session_id": plan_identity["session_id"],
        "settings_sha256": _canonical(settings.to_dict(), deps),
        "registry_sha256": _canonical(registry.to_dict(), deps),
        "code_identity_sha256": code_identity_sha256,
        "db_startup_identity_sha256": _canonical(readiness["startup_identity"], deps),
        "db_schema_sha256": readiness["schema_sha256"],
        "baseline_run_count": baseline_count,
        "baseline_run_inventory_sha256": baseline_sha,
        "enabled_adapters": descriptors,
    }


def _expected_bindings(plan: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in _PRODUCTION_BINDING_FIELDS:
        value = plan.get(field)
        if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
            raise _CliFailure("SOURCE_MONITORING_SOAK_PLAN_INVALID")
        result[field] = value
    return result


def _expected_descriptors(plan: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    value = plan.get("enabled_adapters")
    if type(value) is not list or not value:
        raise _CliFailure("SOURCE_MONITORING_SOAK_PLAN_INVALID")
    return tuple(dict(row) for row in value)


def _observer(database_path: Any, deps: _SoakCliDependencies) -> Any:
    if deps.observer_factory is not None:
        return deps.observer_factory(database_path)
    from .source_monitoring.soak_runner import SoakRuntimeObserver

    return SoakRuntimeObserver(database_path)


def _runtime(store: Any, settings: Any, observer: Any, deps: _SoakCliDependencies) -> Any:
    if deps.runtime_builder is not None:
        return deps.runtime_builder(store, settings, observer)
    from .source_monitoring.runtime import build_source_monitoring_runtime

    return build_source_monitoring_runtime(
        store,
        settings,
        cycle_observer=observer,
        start_gate=observer.await_activation,
    )


def _runner(deps: _SoakCliDependencies, **values: Any) -> Any:
    if deps.runner_factory is not None:
        return deps.runner_factory(**values)
    from .source_monitoring.soak_runner import SourceMonitoringSoakRunner

    return SourceMonitoringSoakRunner(**values)


def _verify(deps: _SoakCliDependencies, ledger_path: Path, **values: Any) -> dict[str, Any]:
    if deps.verifier is not None:
        result = deps.verifier(ledger_path, **values)
    else:
        from .source_monitoring.soak_verifier import verify_soak_evidence

        result = verify_soak_evidence(ledger_path, **values)
    if type(result) is not dict:
        raise _CliFailure("SOURCE_MONITORING_SOAK_VERDICT_INVALID")
    return result


def _runtime_descriptors(runtime: Any) -> tuple[Any, Any, tuple[dict[str, Any], ...]]:
    scheduler = getattr(runtime, "scheduler", None)
    registry = getattr(scheduler, "registry", None)
    repository = getattr(getattr(scheduler, "supervisor", None), "repository", None)
    if registry is None or repository is None:
        raise _CliFailure("SOURCE_MONITORING_SOAK_RUNTIME_INVALID")
    return registry, repository, _adapter_descriptors(registry, repository)


def _prove_quiescent(runtime: Any) -> bool:
    try:
        snapshot = runtime.snapshot()
        if type(snapshot) is dict and snapshot.get("thread_alive") is False:
            return True
    except BaseException:
        pass
    try:
        runtime.stop()
    except BaseException:
        pass
    waiter = getattr(runtime, "wait_until_stopped", None)
    if not callable(waiter):
        return False
    try:
        return waiter() is True
    except BaseException:
        return False


@contextmanager
def _stop_signal_scope(stop_event: threading.Event) -> Iterator[None]:
    installed: list[tuple[int, Any]] = []

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    if threading.current_thread() is threading.main_thread():
        signals = [signal.SIGINT]
        if hasattr(signal, "SIGTERM"):
            signals.append(signal.SIGTERM)
        try:
            for number in signals:
                previous = signal.getsignal(number)
                signal.signal(number, request_stop)
                installed.append((number, previous))
        except (OSError, RuntimeError, ValueError):
            for number, previous in reversed(installed):
                signal.signal(number, previous)
            raise _CliFailure("SOURCE_MONITORING_SOAK_SIGNAL_HANDLER_FAILED") from None
    try:
        yield
    finally:
        for number, previous in reversed(installed):
            signal.signal(number, previous)


def _project_verdict(verdict: dict[str, Any]) -> dict[str, Any]:
    identity = verdict.get("identity")
    counts = verdict.get("counts")
    timing = verdict.get("timing")
    bindings = verdict.get("bindings")
    issues = verdict.get("issues")
    if (
        set(verdict) != _VERDICT_FIELDS
        or verdict.get("version") != "source_monitoring_soak_verdict_v1"
        or verdict.get("overall_status")
        not in {"EVIDENCE_VERIFIED", "FAILED", "INVALID_LEDGER", "INCOMPLETE_UNSEALED"}
        or verdict.get("continuity_verdict") not in {"PASS", "FAIL", "INCOMPLETE_UNSEALED"}
        or verdict.get("production_binding_verdict")
        not in {"PASS", "FAIL", "NOT_EVALUATED"}
        or verdict.get("database_verdict") not in {"PASS", "FAIL", "NOT_EVALUATED"}
        or type(identity) is not dict
        or set(identity) != {"campaign_id", "session_id", "runtime_id"}
        or type(counts) is not dict
        or set(counts) != _VERDICT_COUNT_FIELDS
        or any(type(value) is not int or value < 0 for value in counts.values())
        or type(timing) is not dict
        or set(timing) != _VERDICT_TIMING_FIELDS
        or any(type(value) is not int or value < 0 for value in timing.values())
        or type(bindings) is not dict
        or set(bindings) != _VERDICT_BINDING_FIELDS
        or type(bindings.get("ledger_terminal")) is not bool
        or any(
            type(bindings[field]) is not str
            or (
                bindings[field] != ""
                and _SHA256_RE.fullmatch(bindings[field]) is None
            )
            for field in _VERDICT_BINDING_FIELDS - {"ledger_terminal"}
        )
        or type(issues) is not list
        or len(issues) > 64
        or type(verdict.get("issue_count")) is not int
        or verdict["issue_count"] < len(issues)
        or type(verdict.get("issues_truncated")) is not bool
        or type(verdict.get("safety")) is not dict
        or set(verdict["safety"]) != set(_VERIFIER_SAFETY)
        or any(
            type(verdict["safety"][field]) is not type(expected)
            or verdict["safety"][field] != expected
            for field, expected in _VERIFIER_SAFETY.items()
        )
        or verdict.get("source_acceptance_verdict") != "NOT_EVALUATED"
        or verdict.get("overall_acceptance") != "NOT_CLAIMED"
        or type(verdict.get("verdict_sha256")) is not str
        or _SHA256_RE.fullmatch(verdict["verdict_sha256"]) is None
    ):
        raise _CliFailure("SOURCE_MONITORING_SOAK_VERDICT_INVALID")
    campaign_id = identity.get("campaign_id")
    session_id = identity.get("session_id")
    runtime_id = identity.get("runtime_id")
    if (
        type(campaign_id) is not str
        or type(session_id) is not str
        or type(runtime_id) is not str
        or (campaign_id != "" and _ID_RE.fullmatch(campaign_id) is None)
        or (session_id != "" and _ID_RE.fullmatch(session_id) is None)
        or (runtime_id != "" and _RUNTIME_ID_RE.fullmatch(runtime_id) is None)
    ):
        raise _CliFailure("SOURCE_MONITORING_SOAK_VERDICT_INVALID")
    projected_issues: list[dict[str, Any]] = []
    for issue in issues:
        if (
            type(issue) is not dict
            or set(issue) != {"scope", "code", "sequence_no", "run_id"}
            or issue.get("scope") not in {"ledger", "continuity", "binding", "database"}
            or type(issue.get("code")) is not str
            or _VERDICT_ISSUE_CODE_RE.fullmatch(issue["code"]) is None
            or type(issue.get("sequence_no")) is not int
            or issue["sequence_no"] < 0
        ):
            raise _CliFailure("SOURCE_MONITORING_SOAK_VERDICT_INVALID")
        projected_issues.append(
            {
                "scope": issue["scope"],
                "code": issue["code"],
                "sequence_no": issue["sequence_no"],
            }
        )
    return {
        "version": verdict["version"],
        "overall_status": verdict.get("overall_status"),
        "continuity_verdict": verdict.get("continuity_verdict"),
        "production_binding_verdict": verdict.get("production_binding_verdict"),
        "database_verdict": verdict.get("database_verdict"),
        "source_acceptance_verdict": "NOT_EVALUATED",
        "overall_acceptance": "NOT_CLAIMED",
        "identity": dict(identity),
        "counts": dict(counts),
        "timing": dict(timing),
        "bindings": dict(bindings),
        "issue_count": verdict["issue_count"],
        "issues": projected_issues,
        "issues_truncated": verdict["issues_truncated"],
        "verdict_sha256": verdict["verdict_sha256"],
    }


def _verdict_payload(command: str, plan: dict[str, Any], verdict: dict[str, Any], state: _ExecutionState) -> tuple[int, dict[str, Any]]:
    projected = _project_verdict(verdict)
    identity = projected["identity"]
    if (
        identity.get("campaign_id") not in {"", plan["campaign_id"]}
        or identity.get("session_id") not in {"", plan["session_id"]}
    ):
        raise _CliFailure("SOURCE_MONITORING_SOAK_VERDICT_BINDING_INVALID")
    verified = projected.get("overall_status") == "EVIDENCE_VERIFIED"
    return (
        0 if verified else 2,
        {
            "version": SOURCE_MONITORING_SOAK_CLI_VERSION,
            "command": command,
            "ok": verified,
            "mode": "official",
            "preview_sha256": plan["preview_sha256"],
            "verdict": projected,
            "safety": _safety(state),
        },
    )


def _preview(bundle_value: str, deps: _SoakCliDependencies, state: _ExecutionState) -> tuple[int, dict[str, Any]]:
    bundle = _bundle(bundle_value, expected_names=frozenset())
    database_path = _database_path(deps)
    owner = _acquire_owner(database_path, deps)
    state.owner_acquired = True
    try:
        state.database_reads_possible = True
        ready = _readiness(database_path, deps)
        store = _open_store(database_path, deps)
        settings = _settings(deps)
        registry = _registry(settings, deps)
        repository = _repository(store, deps)
        descriptors = _adapter_descriptors(registry, repository)
        baseline = _inventory(database_path, deps)
        if any(type(row) is dict and row.get("status") == "RUNNING" for row in baseline["runs"]):
            raise _CliFailure("SOURCE_MONITORING_SOAK_RUNNING_ROWS_PRESENT")
        code_sha = _code_identity(deps)
        plan = _build_plan(
            deps,
            **_plan_values(
                plan_identity={
                    "campaign_id": _id("campaign", deps),
                    "session_id": _id("session", deps),
                },
                readiness=ready,
                settings=settings,
                registry=registry,
                descriptors=descriptors,
                baseline=baseline,
                code_identity_sha256=code_sha,
                deps=deps,
            ),
        )
        state.artifact_writes = None
        _write_inventory(baseline, bundle / "baseline-inventory.json", deps)
        state.artifact_writes = 1
        state.artifact_writes = None
        _write_plan(plan, bundle / "plan.json", deps)
        state.artifact_writes = 2
        _bundle(bundle_value, expected_names=_START_BUNDLE_NAMES)
        if (
            _load_plan(bundle / "plan.json", deps) != plan
            or _load_inventory(bundle / "baseline-inventory.json", deps) != baseline
        ):
            raise _CliFailure("SOURCE_MONITORING_SOAK_ARTIFACT_DRIFT")
        return 0, {
            "version": SOURCE_MONITORING_SOAK_CLI_VERSION,
            "command": "preview",
            "ok": True,
            "mode": "official",
            "required_duration_hours": 24,
            "required_duration_ns": plan["required_duration_ns"],
            "sample_interval_ns": plan["sample_interval_ns"],
            "maximum_sample_gap_ns": plan["maximum_sample_gap_ns"],
            "campaign_id": plan["campaign_id"],
            "session_id": plan["session_id"],
            "preview_sha256": plan["preview_sha256"],
            "enabled_adapter_count": plan["enabled_adapter_count"],
            "enabled_adapters": plan["enabled_adapters"],
            "confirmation_required": SOURCE_MONITORING_SOAK_CONFIRMATION,
            "safety": _safety(state),
        }
    finally:
        owner.release()


def _start(bundle_value: str, supplied_sha: str, deps: _SoakCliDependencies, state: _ExecutionState) -> tuple[int, dict[str, Any]]:
    bundle = _bundle(bundle_value, expected_names=_START_BUNDLE_NAMES)
    plan = _load_plan(bundle / "plan.json", deps)
    baseline = _load_inventory(bundle / "baseline-inventory.json", deps)
    if supplied_sha != plan.get("preview_sha256"):
        raise _CliFailure("SOURCE_MONITORING_SOAK_PREVIEW_HASH_MISMATCH")
    baseline_count, baseline_sha = _inventory_binding(baseline)
    if (
        baseline_count != plan.get("baseline_run_count")
        or baseline_sha != plan.get("baseline_run_inventory_sha256")
        or any(type(row) is dict and row.get("status") == "RUNNING" for row in baseline["runs"])
    ):
        raise _CliFailure("SOURCE_MONITORING_SOAK_BASELINE_DRIFT")

    database_path = _database_path(deps)
    owner = _acquire_owner(database_path, deps)
    state.owner_acquired = True
    runtime: Any | None = None
    retain_owner = False
    try:
        state.database_reads_possible = True
        ready = _readiness(database_path, deps)
        store = _open_store(database_path, deps)
        settings = _settings(deps)
        registry = _registry(settings, deps)
        repository = _repository(store, deps)
        descriptors = _adapter_descriptors(registry, repository)
        current_baseline = _inventory(database_path, deps)
        if current_baseline != baseline:
            raise _CliFailure("SOURCE_MONITORING_SOAK_BASELINE_DRIFT")
        code_sha = _code_identity(deps)
        rebuilt = _build_plan(
            deps,
            **_plan_values(
                plan_identity={
                    "campaign_id": plan["campaign_id"],
                    "session_id": plan["session_id"],
                },
                readiness=ready,
                settings=settings,
                registry=registry,
                descriptors=descriptors,
                baseline=current_baseline,
                code_identity_sha256=code_sha,
                deps=deps,
            ),
        )
        if rebuilt != plan:
            raise _CliFailure("SOURCE_MONITORING_SOAK_PLAN_DRIFT")

        observer = _observer(database_path, deps)
        runtime = _runtime(store, settings, observer, deps)
        actual_registry, _actual_repository, actual_descriptors = _runtime_descriptors(runtime)
        if (
            actual_descriptors != descriptors
            or actual_descriptors != _expected_descriptors(plan)
            or _canonical(actual_registry.to_dict(), deps) != plan["registry_sha256"]
            or _canonical(runtime.settings.to_dict(), deps) != plan["settings_sha256"]
        ):
            raise _CliFailure("SOURCE_MONITORING_SOAK_RUNTIME_BINDING_DRIFT")

        def compare_baseline(actual: dict[str, Any]) -> None:
            if actual != baseline:
                raise _CliFailure("SOURCE_MONITORING_SOAK_BASELINE_DRIFT")

        def write_final(actual: dict[str, Any]) -> None:
            _write_inventory(actual, bundle / "final-inventory.json", deps)

        stop_event = threading.Event()
        runner = _runner(
            deps,
            runtime=runtime,
            observer=observer,
            database_owner=owner,
            database_path=database_path,
            ledger_path=bundle / "ledger.jsonl",
            campaign_id=plan["campaign_id"],
            session_id=plan["session_id"],
            preview_sha256=plan["preview_sha256"],
            expected_enabled_adapters=_expected_descriptors(plan),
            code_identity_sha256=plan["code_identity_sha256"],
            code_identity_checker=lambda: _code_identity(deps),
            db_startup_identity_sha256=plan["db_startup_identity_sha256"],
            db_schema_sha256=plan["db_schema_sha256"],
            stop_event=stop_event,
            baseline_inventory_sink=compare_baseline,
            final_inventory_sink=write_final,
        )
        state.runtime_boundary_entered = True
        state.artifact_writes = None
        with _stop_signal_scope(stop_event):
            runner_result = runner.run()
        if type(runner_result) is not dict:
            raise _CliFailure("SOURCE_MONITORING_SOAK_RUNNER_RESULT_INVALID")
        if not _prove_quiescent(runtime):
            retain_owner = True
            _RETAINED_LIVE_OWNERS.append((owner, runtime))
            raise _CliFailure(
                "SOURCE_MONITORING_SOAK_RUNTIME_NOT_QUIESCENT", exit_code=1
            )

        _bundle(bundle_value, expected_names=_FIXED_BUNDLE_NAMES)
        sealed_plan = _load_plan(bundle / "plan.json", deps)
        sealed_baseline = _load_inventory(bundle / "baseline-inventory.json", deps)
        final = _load_inventory(bundle / "final-inventory.json", deps)
        if sealed_plan != plan or sealed_baseline != baseline:
            raise _CliFailure("SOURCE_MONITORING_SOAK_ARTIFACT_DRIFT")
        verdict = _verify(
            deps,
            bundle / "ledger.jsonl",
            baseline_inventory=sealed_baseline,
            final_inventory=final,
            expected_bindings=_expected_bindings(plan),
            expected_enabled_adapter_keys=tuple(
                row["adapter_key"] for row in _expected_descriptors(plan)
            ),
        )
        return _verdict_payload("start", plan, verdict, state)
    finally:
        if runtime is not None and not retain_owner and not _prove_quiescent(runtime):
            retain_owner = True
            _RETAINED_LIVE_OWNERS.append((owner, runtime))
            raise _CliFailure(
                "SOURCE_MONITORING_SOAK_RUNTIME_NOT_QUIESCENT", exit_code=1
            )
        if not retain_owner:
            owner.release()


def _verify_bundle(bundle_value: str, deps: _SoakCliDependencies, state: _ExecutionState) -> tuple[int, dict[str, Any]]:
    bundle = _bundle(bundle_value, expected_names=_FIXED_BUNDLE_NAMES)
    plan = _load_plan(bundle / "plan.json", deps)
    baseline = _load_inventory(bundle / "baseline-inventory.json", deps)
    final = _load_inventory(bundle / "final-inventory.json", deps)
    baseline_count, baseline_sha = _inventory_binding(baseline)
    if (
        baseline_count != plan.get("baseline_run_count")
        or baseline_sha != plan.get("baseline_run_inventory_sha256")
    ):
        raise _CliFailure("SOURCE_MONITORING_SOAK_BASELINE_DRIFT")
    verdict = _verify(
        deps,
        bundle / "ledger.jsonl",
        baseline_inventory=baseline,
        final_inventory=final,
        expected_bindings=_expected_bindings(plan),
        expected_enabled_adapter_keys=tuple(
            row["adapter_key"] for row in _expected_descriptors(plan)
        ),
    )
    return _verdict_payload("verify", plan, verdict, state)


def _main_with_dependencies(
    argv: Sequence[str] | None,
    *,
    output: TextIO,
    dependencies: _SoakCliDependencies,
) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    state = _ExecutionState()
    command = raw[0] if raw and raw[0] in {"preview", "start", "verify"} else "invalid"
    if any(token in {"-h", "--help"} for token in raw):
        _emit(output, _help_payload())
        return 0
    try:
        _reject_duplicate_options(raw)
        arguments = _parser().parse_args(raw)
        command = arguments.command
        if command == "preview":
            if arguments.mode != "official":
                raise _CliFailure("SOURCE_MONITORING_SOAK_MODE_INVALID")
            exit_code, payload = _preview(arguments.bundle, dependencies, state)
        elif command == "start":
            if arguments.confirm != SOURCE_MONITORING_SOAK_CONFIRMATION:
                raise _CliFailure("SOURCE_MONITORING_SOAK_CONFIRMATION_REQUIRED")
            if (
                type(arguments.preview_sha256) is not str
                or _SHA256_RE.fullmatch(arguments.preview_sha256) is None
            ):
                raise _CliFailure("SOURCE_MONITORING_SOAK_PREVIEW_HASH_INVALID")
            exit_code, payload = _start(
                arguments.bundle,
                arguments.preview_sha256,
                dependencies,
                state,
            )
        else:
            exit_code, payload = _verify_bundle(arguments.bundle, dependencies, state)
    except BaseException as exc:
        code, exit_code = _safe_error(exc)
        payload = _error_payload(command, code, state)
    _emit(output, payload)
    return exit_code


def main(argv: Sequence[str] | None = None, *, output: TextIO | None = None) -> int:
    """Run the sealed production CLI; no production dependency seam is public."""

    return _main_with_dependencies(
        argv,
        output=output or sys.stdout,
        dependencies=_SoakCliDependencies(),
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
