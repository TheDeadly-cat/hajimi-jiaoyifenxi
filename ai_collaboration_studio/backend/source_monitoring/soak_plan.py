"""Canonical, path-free plans for one official-source monitoring soak bundle.

This module only validates values and reads or writes the fixed ``plan.json``
artifact.  It does not discover a Studio database, inspect an adapter registry,
create directories, start a runtime, or open the network.  The plan binds the
inputs that a separate owner-scoped runner must independently recheck.
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat as statlib
from pathlib import Path
from typing import Any

from ..path_identity import first_reparse_component
from .contracts import MAX_NATIVE_INTEGER, canonical_json, canonical_sha256


SOURCE_MONITORING_SOAK_PLAN_VERSION = "source_monitoring_soak_plan_v1"
SOURCE_MONITORING_SOAK_PLAN_WRITE_VERSION = "source_monitoring_soak_plan_write_v1"

SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS = 86_400_000_000_000
SOURCE_MONITORING_SOAK_SAMPLE_INTERVAL_NS = 5_000_000_000
SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS = 120_000_000_000

SOAK_PLAN_FILENAME = "plan.json"
SOAK_BASELINE_INVENTORY_FILENAME = "baseline-inventory.json"
SOAK_LEDGER_FILENAME = "ledger.jsonl"
SOAK_FINAL_INVENTORY_FILENAME = "final-inventory.json"

MAX_SOAK_PLAN_BYTES = 64 * 1024
MAX_SOAK_PLAN_ADAPTERS = 50

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CAMPAIGN_ID_RE = re.compile(r"source_soak_campaign_[0-9a-f]{32}\Z")
_SESSION_ID_RE = re.compile(r"source_soak_session_[0-9a-f]{32}\Z")
_ADAPTER_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_CONFIG_VERSION_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")

_PLAN_FIELDS = frozenset(
    {
        "version",
        "campaign_id",
        "session_id",
        "mode",
        "required_duration_ns",
        "sample_interval_ns",
        "maximum_sample_gap_ns",
        "settings_sha256",
        "registry_sha256",
        "code_identity_sha256",
        "db_startup_identity_sha256",
        "db_schema_sha256",
        "baseline_run_count",
        "baseline_run_inventory_sha256",
        "enabled_adapter_count",
        "enabled_adapters",
        "enabled_adapter_keys_sha256",
        "artifacts",
        "preview_sha256",
    }
)
_ADAPTER_FIELDS = frozenset(
    {"adapter_key", "config_version", "state_version", "checkpoint_sha256"}
)
_ARTIFACT_FIELDS = frozenset(
    {"plan", "baseline_inventory", "ledger", "final_inventory"}
)
_FIXED_ARTIFACTS = {
    "plan": SOAK_PLAN_FILENAME,
    "baseline_inventory": SOAK_BASELINE_INVENTORY_FILENAME,
    "ledger": SOAK_LEDGER_FILENAME,
    "final_inventory": SOAK_FINAL_INVENTORY_FILENAME,
}


class SourceMonitoringSoakPlanError(ValueError):
    """A bounded, machine-readable failure in the soak plan contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _raise(code: str, message: str) -> None:
    raise SourceMonitoringSoakPlanError(code, message)


def _exact_fields(value: Any, expected: frozenset[str], *, field: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_SCHEMA_INVALID",
            f"{field} must contain exactly the closed v1 field set",
        )
    return value


def _sha256(value: Any, *, field: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_HASH_INVALID",
            f"{field} must be a lowercase SHA-256 digest",
        )
    return value


def _identity(value: Any, pattern: re.Pattern[str], *, field: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_IDENTITY_INVALID",
            f"{field} is not a canonical soak identity",
        )
    return value


def _native_non_negative(value: Any, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_NATIVE_INTEGER:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_INTEGER_INVALID",
            f"{field} must be a non-negative native signed 64-bit integer",
        )
    return value


def _normalize_adapter(value: Any, *, index: int) -> dict[str, Any]:
    descriptor = _exact_fields(
        value,
        _ADAPTER_FIELDS,
        field=f"enabled_adapters[{index}]",
    )
    adapter_key = descriptor["adapter_key"]
    if type(adapter_key) is not str or _ADAPTER_KEY_RE.fullmatch(adapter_key) is None:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_ADAPTER_INVALID",
            f"enabled_adapters[{index}].adapter_key is not canonical",
        )
    config_version = descriptor["config_version"]
    if (
        type(config_version) is not str
        or _CONFIG_VERSION_RE.fullmatch(config_version) is None
    ):
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_ADAPTER_INVALID",
            f"enabled_adapters[{index}].config_version is not canonical",
        )
    state_version = _native_non_negative(
        descriptor["state_version"],
        field=f"enabled_adapters[{index}].state_version",
    )
    if state_version == 0:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_ADAPTER_INVALID",
            f"enabled_adapters[{index}].state_version must be positive",
        )
    return {
        "adapter_key": adapter_key,
        "config_version": config_version,
        "state_version": state_version,
        "checkpoint_sha256": _sha256(
            descriptor["checkpoint_sha256"],
            field=f"enabled_adapters[{index}].checkpoint_sha256",
        ),
    }


def _normalize_enabled_adapters(
    value: Any,
    *,
    require_sorted: bool,
) -> list[dict[str, Any]]:
    if type(value) not in {list, tuple} or not 1 <= len(value) <= MAX_SOAK_PLAN_ADAPTERS:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_ADAPTERS_INVALID",
            "enabled_adapters must be a non-empty bounded native list or tuple",
        )
    adapters = [
        _normalize_adapter(descriptor, index=index)
        for index, descriptor in enumerate(value)
    ]
    keys = [descriptor["adapter_key"] for descriptor in adapters]
    if len(set(keys)) != len(keys):
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_ADAPTERS_INVALID",
            "enabled adapter keys must be unique",
        )
    if require_sorted and keys != sorted(keys):
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_ADAPTERS_INVALID",
            "enabled adapters must be sorted by adapter_key",
        )
    if not require_sorted:
        adapters.sort(key=lambda descriptor: descriptor["adapter_key"])
    return adapters


def _preview_hash_basis(plan: dict[str, Any]) -> dict[str, Any]:
    basis = copy.deepcopy(plan)
    basis.pop("preview_sha256", None)
    return basis


def validate_source_monitoring_soak_plan(value: Any) -> dict[str, Any]:
    """Return a defensive copy of one exact, internally sealed v1 plan."""

    plan = _exact_fields(value, _PLAN_FIELDS, field="plan")
    if plan["version"] != SOURCE_MONITORING_SOAK_PLAN_VERSION:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_VERSION_INVALID",
            "source-monitoring soak plan version is unsupported",
        )
    campaign_id = _identity(
        plan["campaign_id"],
        _CAMPAIGN_ID_RE,
        field="campaign_id",
    )
    session_id = _identity(
        plan["session_id"],
        _SESSION_ID_RE,
        field="session_id",
    )
    if plan["mode"] != "official":
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_MODE_INVALID",
            "v1 soak plans are official-only",
        )
    fixed_policy = {
        "required_duration_ns": SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS,
        "sample_interval_ns": SOURCE_MONITORING_SOAK_SAMPLE_INTERVAL_NS,
        "maximum_sample_gap_ns": SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS,
    }
    for field, expected in fixed_policy.items():
        if type(plan[field]) is not int or plan[field] != expected:
            _raise(
                "SOURCE_MONITORING_SOAK_PLAN_POLICY_INVALID",
                f"{field} does not match the fixed official soak policy",
            )

    hashes = {
        field: _sha256(plan[field], field=field)
        for field in (
            "settings_sha256",
            "registry_sha256",
            "code_identity_sha256",
            "db_startup_identity_sha256",
            "db_schema_sha256",
            "baseline_run_inventory_sha256",
            "enabled_adapter_keys_sha256",
            "preview_sha256",
        )
    }
    baseline_run_count = _native_non_negative(
        plan["baseline_run_count"],
        field="baseline_run_count",
    )
    adapters = _normalize_enabled_adapters(
        plan["enabled_adapters"],
        require_sorted=True,
    )
    if (
        type(plan["enabled_adapter_count"]) is not int
        or plan["enabled_adapter_count"] != len(adapters)
    ):
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_ADAPTERS_INVALID",
            "enabled_adapter_count does not match enabled_adapters",
        )
    adapter_keys = [descriptor["adapter_key"] for descriptor in adapters]
    if hashes["enabled_adapter_keys_sha256"] != canonical_sha256(adapter_keys):
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_SEAL_INVALID",
            "enabled adapter key seal does not match enabled_adapters",
        )

    artifacts = _exact_fields(plan["artifacts"], _ARTIFACT_FIELDS, field="artifacts")
    if artifacts != _FIXED_ARTIFACTS:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_ARTIFACTS_INVALID",
            "artifact names do not match the fixed path-free soak bundle",
        )

    normalized = {
        "version": SOURCE_MONITORING_SOAK_PLAN_VERSION,
        "campaign_id": campaign_id,
        "session_id": session_id,
        "mode": "official",
        **fixed_policy,
        "settings_sha256": hashes["settings_sha256"],
        "registry_sha256": hashes["registry_sha256"],
        "code_identity_sha256": hashes["code_identity_sha256"],
        "db_startup_identity_sha256": hashes["db_startup_identity_sha256"],
        "db_schema_sha256": hashes["db_schema_sha256"],
        "baseline_run_count": baseline_run_count,
        "baseline_run_inventory_sha256": hashes[
            "baseline_run_inventory_sha256"
        ],
        "enabled_adapter_count": len(adapters),
        "enabled_adapters": adapters,
        "enabled_adapter_keys_sha256": hashes["enabled_adapter_keys_sha256"],
        "artifacts": dict(_FIXED_ARTIFACTS),
        "preview_sha256": hashes["preview_sha256"],
    }
    if normalized["preview_sha256"] != canonical_sha256(
        _preview_hash_basis(normalized)
    ):
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_SEAL_INVALID",
            "preview_sha256 does not seal the complete plan",
        )
    return copy.deepcopy(normalized)


def build_source_monitoring_soak_plan(
    *,
    campaign_id: Any,
    session_id: Any,
    settings_sha256: Any,
    registry_sha256: Any,
    code_identity_sha256: Any,
    db_startup_identity_sha256: Any,
    db_schema_sha256: Any,
    baseline_run_count: Any,
    baseline_run_inventory_sha256: Any,
    enabled_adapters: Any,
) -> dict[str, Any]:
    """Build one path-free official soak plan from already observed bindings."""

    adapters = _normalize_enabled_adapters(enabled_adapters, require_sorted=False)
    adapter_keys = [descriptor["adapter_key"] for descriptor in adapters]
    plan: dict[str, Any] = {
        "version": SOURCE_MONITORING_SOAK_PLAN_VERSION,
        "campaign_id": _identity(
            campaign_id,
            _CAMPAIGN_ID_RE,
            field="campaign_id",
        ),
        "session_id": _identity(
            session_id,
            _SESSION_ID_RE,
            field="session_id",
        ),
        "mode": "official",
        "required_duration_ns": SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS,
        "sample_interval_ns": SOURCE_MONITORING_SOAK_SAMPLE_INTERVAL_NS,
        "maximum_sample_gap_ns": SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS,
        "settings_sha256": _sha256(settings_sha256, field="settings_sha256"),
        "registry_sha256": _sha256(registry_sha256, field="registry_sha256"),
        "code_identity_sha256": _sha256(
            code_identity_sha256,
            field="code_identity_sha256",
        ),
        "db_startup_identity_sha256": _sha256(
            db_startup_identity_sha256,
            field="db_startup_identity_sha256",
        ),
        "db_schema_sha256": _sha256(
            db_schema_sha256,
            field="db_schema_sha256",
        ),
        "baseline_run_count": _native_non_negative(
            baseline_run_count,
            field="baseline_run_count",
        ),
        "baseline_run_inventory_sha256": _sha256(
            baseline_run_inventory_sha256,
            field="baseline_run_inventory_sha256",
        ),
        "enabled_adapter_count": len(adapters),
        "enabled_adapters": adapters,
        "enabled_adapter_keys_sha256": canonical_sha256(adapter_keys),
        "artifacts": dict(_FIXED_ARTIFACTS),
        "preview_sha256": "",
    }
    plan["preview_sha256"] = canonical_sha256(_preview_hash_basis(plan))
    return validate_source_monitoring_soak_plan(plan)


def _reparse_flag(metadata: os.stat_result) -> bool:
    flag = int(getattr(statlib, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    return bool(flag and attributes & flag)


def _require_independent_regular(metadata: os.stat_result) -> None:
    if (
        not statlib.S_ISREG(metadata.st_mode)
        or int(metadata.st_nlink) != 1
        or _reparse_flag(metadata)
    ):
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_PATH_INVALID",
            "plan artifact must be an independent non-reparse regular file",
        )


def _fsync_parent_directory(path: Path) -> None:
    """Persist a newly created directory entry where directory fsync exists."""

    if os.name == "nt":
        return
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0) or 0)
    descriptor = os.open(path.parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _new_plan_path(value: Any) -> Path:
    if isinstance(value, bool) or not isinstance(value, (str, os.PathLike)):
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_PATH_INVALID",
            "plan artifact path must be explicit",
        )
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw.strip():
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_PATH_INVALID",
            "plan artifact path must be explicit",
        )
    requested = Path(raw).expanduser()
    if requested.name != SOAK_PLAN_FILENAME:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_PATH_INVALID",
            f"plan artifact filename must be exactly {SOAK_PLAN_FILENAME}",
        )
    if first_reparse_component(requested) is not None:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_PATH_INVALID",
            "plan artifact path may not contain a symlink or reparse point",
        )
    try:
        parent = requested.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SourceMonitoringSoakPlanError(
            "SOURCE_MONITORING_SOAK_PLAN_PATH_INVALID",
            "plan artifact parent directory must already exist",
        ) from exc
    if not parent.is_dir():
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_PATH_INVALID",
            "plan artifact parent directory must already exist",
        )
    resolved = (parent / requested.name).resolve(strict=False)
    if first_reparse_component(resolved) is not None:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_PATH_INVALID",
            "plan artifact path may not contain a symlink or reparse point",
        )
    return resolved


def _existing_plan_path(value: Any) -> Path:
    path = _new_plan_path(value)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SourceMonitoringSoakPlanError(
            "SOURCE_MONITORING_SOAK_PLAN_READ_FAILED",
            "plan artifact is unavailable",
        ) from exc
    _require_independent_regular(metadata)
    if not 0 < metadata.st_size <= MAX_SOAK_PLAN_BYTES:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_SIZE_INVALID",
            "plan artifact is empty or exceeds the bounded file size",
        )
    return path


def write_source_monitoring_soak_plan_exclusive(
    artifact_path: str | os.PathLike[str],
    plan: Any,
) -> dict[str, Any]:
    """Create and fsync the fixed canonical ``plan.json`` without overwrite."""

    normalized = validate_source_monitoring_soak_plan(plan)
    payload = canonical_json(normalized).encode("utf-8")
    if not 0 < len(payload) <= MAX_SOAK_PLAN_BYTES:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_SIZE_INVALID",
            "plan artifact is empty or exceeds the bounded file size",
        )
    path = _new_plan_path(artifact_path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= int(getattr(os, "O_BINARY", 0) or 0)
    flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        _require_independent_regular(opened)
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if type(count) is not int or count <= 0:
                raise OSError("plan artifact write made no progress")
            written += count
        os.fsync(descriptor)
        final_fd = os.fstat(descriptor)
        final_path = path.lstat()
        _require_independent_regular(final_fd)
        _require_independent_regular(final_path)
        if (
            first_reparse_component(path) is not None
            or (final_fd.st_dev, final_fd.st_ino)
            != (final_path.st_dev, final_path.st_ino)
            or final_fd.st_size != len(payload)
            or final_path.st_size != len(payload)
        ):
            _raise(
                "SOURCE_MONITORING_SOAK_PLAN_IDENTITY_CHANGED",
                "plan artifact identity changed while writing",
            )
        _fsync_parent_directory(path)
    except FileExistsError as exc:
        raise SourceMonitoringSoakPlanError(
            "SOURCE_MONITORING_SOAK_PLAN_EXISTS",
            "plan artifact already exists and will not be overwritten",
        ) from exc
    except SourceMonitoringSoakPlanError:
        raise
    except OSError as exc:
        raise SourceMonitoringSoakPlanError(
            "SOURCE_MONITORING_SOAK_PLAN_WRITE_FAILED",
            "plan artifact could not be written durably",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return {
        "version": SOURCE_MONITORING_SOAK_PLAN_WRITE_VERSION,
        "filename": SOAK_PLAN_FILENAME,
        "preview_sha256": normalized["preview_sha256"],
        "bytes_written": len(payload),
    }


def _decode_canonical_plan(raw: bytes) -> dict[str, Any]:
    if not 0 < len(raw) <= MAX_SOAK_PLAN_BYTES:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_SIZE_INVALID",
            "plan artifact is empty or exceeds the bounded file size",
        )

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite constant: {value}")

    try:
        decoded = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise SourceMonitoringSoakPlanError(
            "SOURCE_MONITORING_SOAK_PLAN_FORMAT_INVALID",
            "plan artifact is not unique canonical JSON",
        ) from exc
    if type(decoded) is not dict:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_FORMAT_INVALID",
            "plan artifact root must be an object",
        )
    try:
        canonical = canonical_json(decoded).encode("utf-8")
    except Exception as exc:
        raise SourceMonitoringSoakPlanError(
            "SOURCE_MONITORING_SOAK_PLAN_FORMAT_INVALID",
            "plan artifact cannot be canonicalized",
        ) from exc
    if raw != canonical:
        _raise(
            "SOURCE_MONITORING_SOAK_PLAN_FORMAT_INVALID",
            "plan artifact bytes are not canonical",
        )
    return decoded


def load_source_monitoring_soak_plan(
    artifact_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Load and revalidate one fixed, independent canonical ``plan.json``."""

    path = _existing_plan_path(artifact_path)
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0)
    flags |= int(getattr(os, "O_NOFOLLOW", 0) or 0)
    descriptor = -1
    try:
        before_path = path.lstat()
        descriptor = os.open(path, flags)
        before_fd = os.fstat(descriptor)
        _require_independent_regular(before_path)
        _require_independent_regular(before_fd)
        if (
            (before_fd.st_dev, before_fd.st_ino)
            != (before_path.st_dev, before_path.st_ino)
            or not 0 < before_fd.st_size <= MAX_SOAK_PLAN_BYTES
        ):
            _raise(
                "SOURCE_MONITORING_SOAK_PLAN_IDENTITY_CHANGED",
                "plan artifact identity changed before reading",
            )
        chunks: list[bytes] = []
        remaining = MAX_SOAK_PLAN_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after_fd = os.fstat(descriptor)
        after_path = path.lstat()
        _require_independent_regular(after_fd)
        _require_independent_regular(after_path)
        before_signature = (
            before_fd.st_dev,
            before_fd.st_ino,
            before_fd.st_size,
            before_fd.st_mtime_ns,
            before_fd.st_ctime_ns,
        )
        after_signature = (
            after_fd.st_dev,
            after_fd.st_ino,
            after_fd.st_size,
            after_fd.st_mtime_ns,
            after_fd.st_ctime_ns,
        )
        if (
            first_reparse_component(path) is not None
            or (after_fd.st_dev, after_fd.st_ino)
            != (after_path.st_dev, after_path.st_ino)
            or before_signature != after_signature
            or len(raw) != after_fd.st_size
        ):
            _raise(
                "SOURCE_MONITORING_SOAK_PLAN_IDENTITY_CHANGED",
                "plan artifact identity or size changed while reading",
            )
    except SourceMonitoringSoakPlanError:
        raise
    except OSError as exc:
        raise SourceMonitoringSoakPlanError(
            "SOURCE_MONITORING_SOAK_PLAN_READ_FAILED",
            "plan artifact could not be read",
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return validate_source_monitoring_soak_plan(_decode_canonical_plan(raw))


__all__ = [
    "MAX_SOAK_PLAN_ADAPTERS",
    "MAX_SOAK_PLAN_BYTES",
    "SOAK_BASELINE_INVENTORY_FILENAME",
    "SOAK_FINAL_INVENTORY_FILENAME",
    "SOAK_LEDGER_FILENAME",
    "SOAK_PLAN_FILENAME",
    "SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS",
    "SOURCE_MONITORING_SOAK_PLAN_VERSION",
    "SOURCE_MONITORING_SOAK_PLAN_WRITE_VERSION",
    "SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS",
    "SOURCE_MONITORING_SOAK_SAMPLE_INTERVAL_NS",
    "SourceMonitoringSoakPlanError",
    "build_source_monitoring_soak_plan",
    "load_source_monitoring_soak_plan",
    "validate_source_monitoring_soak_plan",
    "write_source_monitoring_soak_plan_exclusive",
]
