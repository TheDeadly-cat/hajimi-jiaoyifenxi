"""Operational-only acceptance for one sealed official-source soak bundle.

This v2 verifier is layered on top of, and does not modify, the v1 soak
verifier.  It reads only the four fixed evidence artifacts.  It never
discovers or opens a database, performs a network request, calls a Provider or
model, touches Futu, creates a formal round, or exposes trading capability.

A pass means only that the sealed 24-hour runtime evidence contains at least
one clean ``SUCCEEDED`` terminal run for each of the six required official
adapter paths.  The ledger is not an independently anchored network witness,
and a pass does not attest content truth, migration, Provider/Futu behavior,
trading, merge, release, or public deployment.

As with the v1 bundle verifier, the operator must protect the bundle directory
against concurrent writes and entry replacement by another local principal.
The verifier detects observable identity drift, but does not claim an
open-relative Windows directory-handle sandbox against an attacker that can
replace entries and restore them between observations.
"""

from __future__ import annotations

import ntpath
import os
import re
import stat as statlib
from pathlib import Path
from typing import Any

from ..path_identity import first_reparse_component
from .contracts import canonical_sha256
from .soak_db_inventory import (
    SoakDbInventoryError,
    load_soak_db_inventory,
)
from .soak_evidence import (
    SOAK_EVENT_RUN_TERMINAL,
    SourceMonitoringSoakEvidenceError,
)
from .soak_plan import (
    SOAK_BASELINE_INVENTORY_FILENAME,
    SOAK_FINAL_INVENTORY_FILENAME,
    SOAK_LEDGER_FILENAME,
    SOAK_PLAN_FILENAME,
    SourceMonitoringSoakPlanError,
    load_source_monitoring_soak_plan,
)
from .soak_verifier import (
    SOURCE_MONITORING_SOAK_VERDICT_VERSION,
    SourceMonitoringSoakVerifierError,
    _verify_soak_evidence_with_observer,
)


SOURCE_MONITORING_SOAK_OPERATIONAL_ACCEPTANCE_VERSION = (
    "source_monitoring_soak_operational_acceptance_v2"
)
MAX_SOAK_OPERATIONAL_ACCEPTANCE_ISSUES = 64

# Canonical byte-order is also the order required by soak-plan v1.
REQUIRED_OFFICIAL_ADAPTER_KEYS = (
    "bls_releases",
    "company_ir",
    "federal_reserve",
    "official_macro_calendar",
    "sec_filings",
    "treasury_releases",
)

_FIXED_BUNDLE_NAMES = frozenset(
    {
        SOAK_PLAN_FILENAME,
        SOAK_BASELINE_INVENTORY_FILENAME,
        SOAK_LEDGER_FILENAME,
        SOAK_FINAL_INVENTORY_FILENAME,
    }
)
_NATIVE_PATH_TYPE = type(Path())
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ISSUE_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,119}\Z")
_ADAPTER_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_RUN_ID_RE = re.compile(r"(?:source_run_[0-9a-f]{32})?\Z")
_CAMPAIGN_ID_RE = re.compile(r"(?:source_soak_campaign_[0-9a-f]{32})?\Z")
_SESSION_ID_RE = re.compile(r"(?:source_soak_session_[0-9a-f]{32})?\Z")
_RUNTIME_ID_RE = re.compile(r"(?:source_monitor_runtime_[0-9a-f]{32})?\Z")

# Windows drive classes from GetDriveTypeW.  Network and indeterminate drive
# classes are deliberately excluded so a user-controlled bundle path cannot
# make the verifier dereference SMB/WebDAV storage while claiming zero network
# requests.
_WINDOWS_LOCAL_DRIVE_TYPES = frozenset({2, 3, 5, 6})

_RESULT_FIELDS = frozenset(
    {
        "version",
        "overall_status",
        "source_acceptance_verdict",
        "overall_acceptance",
        "operational_only",
        "content_truth_attested",
        "independent_network_witness",
        "required_adapter_keys",
        "per_adapter_counts",
        "v1_evidence_verdict",
        "issue_count",
        "issues",
        "issues_truncated",
        "verdict_sha256",
        "safety",
    }
)
_PER_ADAPTER_COUNT_FIELDS = frozenset(
    {
        "terminal_run_count",
        "succeeded_run_count",
        "disallowed_status_run_count",
        "rejected_item_count",
        "source_error_run_count",
        "config_mismatch_run_count",
        "market_activity_run_count",
        "missing_import_receipt_run_count",
    }
)
_V1_SUMMARY_FIELDS = frozenset(
    {
        "version",
        "overall_status",
        "continuity_verdict",
        "production_binding_verdict",
        "database_verdict",
        "source_acceptance_verdict",
        "overall_acceptance",
        "verdict_sha256",
    }
)
_ISSUE_FIELDS = frozenset(
    {"scope", "code", "sequence_no", "adapter_key", "run_id"}
)
_SAFETY_FIELDS = frozenset(
    {
        "database_discovery_performed",
        "database_reads_performed",
        "database_writes_performed",
        "network_requests_performed",
        "provider_calls_performed",
        "model_calls_performed",
        "market_calls_performed",
        "formal_rounds_created",
        "execution_capability",
        "live_trading_allowed",
    }
)
_SAFETY = {
    "database_discovery_performed": False,
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
_V1_RESULT_FIELDS = frozenset(
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
_V1_IDENTITY_FIELDS = frozenset({"campaign_id", "session_id", "runtime_id"})
_V1_COUNT_FIELDS = frozenset(
    {
        "ledger_record_count",
        "runtime_sample_count",
        "run_terminal_count",
        "unique_terminal_run_count",
        "expected_adapter_count",
        "covered_adapter_count",
    }
)
_V1_TIMING_FIELDS = frozenset(
    {
        "required_duration_ns",
        "declared_sample_interval_ns",
        "declared_maximum_sample_gap_ns",
        "observed_elapsed_ns",
        "maximum_observed_sample_gap_ns",
    }
)
_V1_BINDING_FIELDS = frozenset(
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
_V1_ISSUE_FIELDS = frozenset({"scope", "code", "sequence_no", "run_id"})
_V1_SAFETY = {
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


class SourceMonitoringSoakAcceptanceError(ValueError):
    """A bounded path/input error or a broken closed v2 output contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _IssueCollector:
    def __init__(self) -> None:
        self.total = 0
        self.rows: list[dict[str, Any]] = []

    def add(
        self,
        scope: str,
        code: str,
        *,
        sequence_no: int = 0,
        adapter_key: str = "",
        run_id: str = "",
    ) -> None:
        clean_scope = (
            scope
            if scope in {"bundle", "artifact", "v1", "operational"}
            else "operational"
        )
        clean_code = (
            code
            if type(code) is str and _ISSUE_CODE_RE.fullmatch(code)
            else "SOURCE_MONITORING_SOAK_OPERATIONAL_ACCEPTANCE_FAILED"
        )
        clean_sequence = (
            sequence_no
            if type(sequence_no) is int and sequence_no >= 0
            else 0
        )
        clean_adapter = (
            adapter_key
            if type(adapter_key) is str
            and (
                adapter_key == ""
                or _ADAPTER_KEY_RE.fullmatch(adapter_key) is not None
            )
            else ""
        )
        clean_run_id = (
            run_id
            if type(run_id) is str and _RUN_ID_RE.fullmatch(run_id) is not None
            else ""
        )
        self.total += 1
        if len(self.rows) < MAX_SOAK_OPERATIONAL_ACCEPTANCE_ISSUES:
            self.rows.append(
                {
                    "scope": clean_scope,
                    "code": clean_code,
                    "sequence_no": clean_sequence,
                    "adapter_key": clean_adapter,
                    "run_id": clean_run_id,
                }
            )


def _blank_adapter_counts() -> dict[str, dict[str, int]]:
    return {
        adapter_key: {field: 0 for field in sorted(_PER_ADAPTER_COUNT_FIELDS)}
        for adapter_key in REQUIRED_OFFICIAL_ADAPTER_KEYS
    }


def _blank_v1_summary() -> dict[str, str]:
    return {
        "version": SOURCE_MONITORING_SOAK_VERDICT_VERSION,
        "overall_status": "NOT_EVALUATED",
        "continuity_verdict": "NOT_EVALUATED",
        "production_binding_verdict": "NOT_EVALUATED",
        "database_verdict": "NOT_EVALUATED",
        "source_acceptance_verdict": "NOT_EVALUATED",
        "overall_acceptance": "NOT_CLAIMED",
        "verdict_sha256": "",
    }


def _blank_result() -> dict[str, Any]:
    return {
        "version": SOURCE_MONITORING_SOAK_OPERATIONAL_ACCEPTANCE_VERSION,
        "overall_status": "FAILED",
        "source_acceptance_verdict": "FAIL",
        "overall_acceptance": "NOT_CLAIMED",
        "operational_only": True,
        "content_truth_attested": False,
        "independent_network_witness": False,
        "required_adapter_keys": list(REQUIRED_OFFICIAL_ADAPTER_KEYS),
        "per_adapter_counts": _blank_adapter_counts(),
        "v1_evidence_verdict": _blank_v1_summary(),
        "issue_count": 0,
        "issues": [],
        "issues_truncated": False,
        "verdict_sha256": "",
        "safety": dict(_SAFETY),
    }


def _native_non_negative(value: Any) -> bool:
    return type(value) is int and value >= 0


def _seal_result(
    result: dict[str, Any],
    issues: _IssueCollector,
) -> dict[str, Any]:
    result["issue_count"] = issues.total
    result["issues"] = list(issues.rows)
    result["issues_truncated"] = issues.total > len(issues.rows)
    counts = result.get("per_adapter_counts")
    v1_summary = result.get("v1_evidence_verdict")
    valid = (
        type(result) is dict
        and set(result) == _RESULT_FIELDS
        and result.get("version")
        == SOURCE_MONITORING_SOAK_OPERATIONAL_ACCEPTANCE_VERSION
        and result.get("overall_status")
        in {"OPERATIONAL_SOURCE_PATH_ACCEPTED", "FAILED"}
        and result.get("source_acceptance_verdict") in {"PASS", "FAIL"}
        and result.get("overall_acceptance") == "NOT_CLAIMED"
        and result.get("operational_only") is True
        and result.get("content_truth_attested") is False
        and result.get("independent_network_witness") is False
        and result.get("required_adapter_keys")
        == list(REQUIRED_OFFICIAL_ADAPTER_KEYS)
        and type(counts) is dict
        and set(counts) == set(REQUIRED_OFFICIAL_ADAPTER_KEYS)
        and all(
            type(row) is dict
            and set(row) == _PER_ADAPTER_COUNT_FIELDS
            and all(_native_non_negative(value) for value in row.values())
            and row["succeeded_run_count"]
            + row["disallowed_status_run_count"]
            == row["terminal_run_count"]
            and row["source_error_run_count"] <= row["terminal_run_count"]
            and row["config_mismatch_run_count"] <= row["terminal_run_count"]
            and row["market_activity_run_count"] <= row["terminal_run_count"]
            for row in counts.values()
        )
        and type(v1_summary) is dict
        and set(v1_summary) == _V1_SUMMARY_FIELDS
        and v1_summary.get("version") == SOURCE_MONITORING_SOAK_VERDICT_VERSION
        and v1_summary.get("overall_status")
        in {
            "NOT_EVALUATED",
            "EVIDENCE_VERIFIED",
            "FAILED",
            "INVALID_LEDGER",
            "INCOMPLETE_UNSEALED",
        }
        and v1_summary.get("continuity_verdict")
        in {"NOT_EVALUATED", "PASS", "FAIL", "INCOMPLETE_UNSEALED"}
        and v1_summary.get("production_binding_verdict")
        in {"NOT_EVALUATED", "PASS", "FAIL"}
        and v1_summary.get("database_verdict")
        in {"NOT_EVALUATED", "PASS", "FAIL"}
        and v1_summary.get("source_acceptance_verdict") == "NOT_EVALUATED"
        and v1_summary.get("overall_acceptance") == "NOT_CLAIMED"
        and type(v1_summary.get("verdict_sha256")) is str
        and (
            v1_summary["verdict_sha256"] == ""
            or _SHA256_RE.fullmatch(v1_summary["verdict_sha256"]) is not None
        )
        and type(result.get("issue_count")) is int
        and result["issue_count"] >= len(result.get("issues", []))
        and type(result.get("issues")) is list
        and len(result["issues"]) <= MAX_SOAK_OPERATIONAL_ACCEPTANCE_ISSUES
        and all(
            type(issue) is dict
            and set(issue) == _ISSUE_FIELDS
            and issue["scope"] in {"bundle", "artifact", "v1", "operational"}
            and type(issue["code"]) is str
            and _ISSUE_CODE_RE.fullmatch(issue["code"]) is not None
            and _native_non_negative(issue["sequence_no"])
            and type(issue["adapter_key"]) is str
            and (
                issue["adapter_key"] == ""
                or _ADAPTER_KEY_RE.fullmatch(issue["adapter_key"]) is not None
            )
            and type(issue["run_id"]) is str
            and _RUN_ID_RE.fullmatch(issue["run_id"]) is not None
            for issue in result["issues"]
        )
        and type(result.get("issues_truncated")) is bool
        and type(result.get("safety")) is dict
        and set(result["safety"]) == _SAFETY_FIELDS
        and result["safety"] == _SAFETY
    )
    passed = (
        result.get("overall_status") == "OPERATIONAL_SOURCE_PATH_ACCEPTED"
        and result.get("source_acceptance_verdict") == "PASS"
    )
    counters_pass = type(counts) is dict and all(
        counts[adapter_key]["terminal_run_count"] >= 1
        and counts[adapter_key]["succeeded_run_count"] >= 1
        and all(
            counts[adapter_key][field] == 0
            for field in _PER_ADAPTER_COUNT_FIELDS
            - {"terminal_run_count", "succeeded_run_count"}
        )
        for adapter_key in REQUIRED_OFFICIAL_ADAPTER_KEYS
    )
    v1_pass = type(v1_summary) is dict and all(
        (
            v1_summary.get("overall_status") == "EVIDENCE_VERIFIED",
            v1_summary.get("continuity_verdict") == "PASS",
            v1_summary.get("production_binding_verdict") == "PASS",
            v1_summary.get("database_verdict") == "PASS",
            v1_summary.get("source_acceptance_verdict") == "NOT_EVALUATED",
            v1_summary.get("overall_acceptance") == "NOT_CLAIMED",
        )
    )
    if (
        not valid
        or passed != (issues.total == 0 and counters_pass and v1_pass)
        or result.get("overall_acceptance") != "NOT_CLAIMED"
    ):
        raise SourceMonitoringSoakAcceptanceError(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_OUTPUT_INVALID",
            "source-monitoring operational acceptance escaped its closed v2 contract",
        )
    unsigned = {key: value for key, value in result.items() if key != "verdict_sha256"}
    result["verdict_sha256"] = canonical_sha256(unsigned)
    return result


def _failure(scope: str, code: str) -> dict[str, Any]:
    result = _blank_result()
    issues = _IssueCollector()
    issues.add(scope, code)
    return _seal_result(result, issues)


def _is_reparse(metadata: os.stat_result) -> bool:
    flag = int(getattr(statlib, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    return bool(flag and attributes & flag)


def _windows_drive_type(drive: str) -> int:
    """Classify a DOS drive root without touching bundle path components."""

    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_drive_type = kernel32.GetDriveTypeW
        get_drive_type.argtypes = (ctypes.c_wchar_p,)
        get_drive_type.restype = ctypes.c_uint
        return int(get_drive_type(f"{drive}\\"))
    except (AttributeError, OSError, TypeError, ValueError):
        return 0


def _reject_remote_path_before_filesystem_access(raw_path: str, *, label: str) -> None:
    """Reject UNC/device/remote-drive paths before any path dereference."""

    normalized = raw_path.replace("/", "\\")
    folded = normalized.casefold()
    if (
        normalized.startswith("\\\\")
        or folded.startswith("\\??\\")
        or folded.startswith("\\device\\")
    ):
        raise SourceMonitoringSoakAcceptanceError(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_PATH_INVALID",
            f"{label} must use a local filesystem path",
        )
    if os.name != "nt":
        return

    drive, _tail = ntpath.splitdrive(normalized)
    if not drive:
        current_directory = os.getcwd().replace("/", "\\")
        current_folded = current_directory.casefold()
        if (
            current_directory.startswith("\\\\")
            or current_folded.startswith("\\??\\")
            or current_folded.startswith("\\device\\")
        ):
            raise SourceMonitoringSoakAcceptanceError(
                "SOURCE_MONITORING_SOAK_OPERATIONAL_PATH_INVALID",
                f"{label} must use a local filesystem path",
            )
        drive, _tail = ntpath.splitdrive(current_directory)
    if re.fullmatch(r"[A-Za-z]:", drive) is None:
        raise SourceMonitoringSoakAcceptanceError(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_PATH_INVALID",
            f"{label} must use a local DOS drive",
        )
    if _windows_drive_type(drive) not in _WINDOWS_LOCAL_DRIVE_TYPES:
        raise SourceMonitoringSoakAcceptanceError(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_PATH_INVALID",
            f"{label} may not use a remote or indeterminate drive",
        )


def _native_path(value: Any, *, label: str) -> Path:
    if type(value) not in {str, _NATIVE_PATH_TYPE}:
        raise SourceMonitoringSoakAcceptanceError(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_PATH_INVALID",
            f"{label} must be a native string or native Path",
        )
    if type(value) is str and (not value or value != value.strip()):
        raise SourceMonitoringSoakAcceptanceError(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_PATH_INVALID",
            f"{label} is empty or non-canonical",
        )
    try:
        raw_path = value if type(value) is str else str(value)
        _reject_remote_path_before_filesystem_access(raw_path, label=label)
        requested = Path(value).expanduser()
        _reject_remote_path_before_filesystem_access(str(requested), label=label)
        if first_reparse_component(requested) is not None:
            raise SourceMonitoringSoakAcceptanceError(
                "SOURCE_MONITORING_SOAK_OPERATIONAL_PATH_INVALID",
                f"{label} may not contain a symlink or reparse point",
            )
        return requested.resolve(strict=True)
    except SourceMonitoringSoakAcceptanceError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise SourceMonitoringSoakAcceptanceError(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_PATH_INVALID",
            f"{label} is unavailable",
        ) from exc


def _scan_bundle(path: Path) -> tuple[tuple[int, int], frozenset[str]]:
    try:
        before = path.lstat()
        if not statlib.S_ISDIR(before.st_mode) or _is_reparse(before):
            raise OSError("bundle is not an independent directory")
        names_seen: set[str] = set()
        too_many_entries = False
        with os.scandir(path) as entries:
            for entry in entries:
                if len(names_seen) >= len(_FIXED_BUNDLE_NAMES):
                    too_many_entries = True
                    break
                names_seen.add(entry.name)
        names = frozenset(names_seen)
        after = path.lstat()
    except OSError as exc:
        raise SourceMonitoringSoakAcceptanceError(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_BUNDLE_INVALID",
            "soak bundle cannot be inspected safely",
        ) from exc
    before_identity = (int(before.st_dev), int(before.st_ino))
    after_identity = (int(after.st_dev), int(after.st_ino))
    if before_identity != after_identity or _is_reparse(after):
        raise SourceMonitoringSoakAcceptanceError(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_BUNDLE_CHANGED",
            "soak bundle identity changed while it was inspected",
        )
    if too_many_entries or names != _FIXED_BUNDLE_NAMES:
        raise SourceMonitoringSoakAcceptanceError(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_BUNDLE_CONTENTS_INVALID",
            "soak bundle must contain exactly the four fixed evidence artifacts",
        )
    return before_identity, names


def _bundle_paths(value: Any) -> tuple[Path, Path, Path, Path, tuple[int, int]]:
    bundle = _native_path(value, label="bundle_path")
    identity, _names = _scan_bundle(bundle)
    return (
        bundle / SOAK_PLAN_FILENAME,
        bundle / SOAK_BASELINE_INVENTORY_FILENAME,
        bundle / SOAK_LEDGER_FILENAME,
        bundle / SOAK_FINAL_INVENTORY_FILENAME,
        identity,
    )


def _regular_file_signature(path: Path) -> tuple[int, int, int, int, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SourceMonitoringSoakAcceptanceError(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_ARTIFACT_CHANGED",
            "a soak artifact became unavailable",
        ) from exc
    if (
        not statlib.S_ISREG(metadata.st_mode)
        or int(metadata.st_nlink) != 1
        or _is_reparse(metadata)
        or first_reparse_component(path) is not None
    ):
        raise SourceMonitoringSoakAcceptanceError(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_ARTIFACT_INVALID",
            "each soak artifact must be an independent non-reparse regular file",
        )
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _capture_signatures(paths: tuple[Path, ...]) -> tuple[tuple[int, ...], ...]:
    return tuple(_regular_file_signature(path) for path in paths)


def _expected_bindings(plan: dict[str, Any]) -> dict[str, str]:
    return {
        field: plan[field]
        for field in (
            "settings_sha256",
            "registry_sha256",
            "code_identity_sha256",
            "db_startup_identity_sha256",
            "db_schema_sha256",
            "preview_sha256",
            "enabled_adapter_keys_sha256",
        )
    }


def _native_int_object(value: Any, fields: frozenset[str]) -> bool:
    return (
        type(value) is dict
        and set(value) == fields
        and all(_native_non_negative(item) for item in value.values())
    )


def _validate_v1_verdict(value: Any) -> dict[str, Any] | None:
    if type(value) is not dict or set(value) != _V1_RESULT_FIELDS:
        return None
    identity = value.get("identity")
    counts = value.get("counts")
    timing = value.get("timing")
    bindings = value.get("bindings")
    issues = value.get("issues")
    safety = value.get("safety")
    if (
        value.get("version") != SOURCE_MONITORING_SOAK_VERDICT_VERSION
        or value.get("overall_status")
        not in {"EVIDENCE_VERIFIED", "FAILED", "INVALID_LEDGER", "INCOMPLETE_UNSEALED"}
        or value.get("continuity_verdict")
        not in {"PASS", "FAIL", "INCOMPLETE_UNSEALED"}
        or value.get("production_binding_verdict")
        not in {"PASS", "FAIL", "NOT_EVALUATED"}
        or value.get("database_verdict") not in {"PASS", "FAIL", "NOT_EVALUATED"}
        or type(identity) is not dict
        or set(identity) != _V1_IDENTITY_FIELDS
        or type(identity.get("campaign_id")) is not str
        or _CAMPAIGN_ID_RE.fullmatch(identity["campaign_id"]) is None
        or type(identity.get("session_id")) is not str
        or _SESSION_ID_RE.fullmatch(identity["session_id"]) is None
        or type(identity.get("runtime_id")) is not str
        or _RUNTIME_ID_RE.fullmatch(identity["runtime_id"]) is None
        or not _native_int_object(counts, _V1_COUNT_FIELDS)
        or not _native_int_object(timing, _V1_TIMING_FIELDS)
        or type(bindings) is not dict
        or set(bindings) != _V1_BINDING_FIELDS
        or type(bindings.get("ledger_terminal")) is not bool
        or any(
            type(bindings[field]) is not str
            or (
                bindings[field] != ""
                and _SHA256_RE.fullmatch(bindings[field]) is None
            )
            for field in _V1_BINDING_FIELDS - {"ledger_terminal"}
        )
        or type(issues) is not list
        or len(issues) > 64
        or any(
            type(issue) is not dict
            or set(issue) != _V1_ISSUE_FIELDS
            or issue.get("scope")
            not in {"ledger", "continuity", "binding", "database"}
            or type(issue.get("code")) is not str
            or _ISSUE_CODE_RE.fullmatch(issue["code"]) is None
            or not _native_non_negative(issue.get("sequence_no"))
            or type(issue.get("run_id")) is not str
            or _RUN_ID_RE.fullmatch(issue["run_id"]) is None
            for issue in issues
        )
        or type(value.get("issue_count")) is not int
        or value["issue_count"] < len(issues)
        or type(value.get("issues_truncated")) is not bool
        or value["issues_truncated"] is not (value["issue_count"] > len(issues))
        or type(safety) is not dict
        or safety != _V1_SAFETY
        or value.get("source_acceptance_verdict") != "NOT_EVALUATED"
        or value.get("overall_acceptance") != "NOT_CLAIMED"
        or type(value.get("verdict_sha256")) is not str
        or _SHA256_RE.fullmatch(value["verdict_sha256"]) is None
    ):
        return None
    try:
        unsigned = {
            key: item for key, item in value.items() if key != "verdict_sha256"
        }
        if canonical_sha256(unsigned) != value["verdict_sha256"]:
            return None
    except (RecursionError, TypeError, ValueError):
        return None
    return value


def _copy_v1_summary(value: dict[str, Any]) -> dict[str, str]:
    return {
        field: value[field]
        for field in (
            "version",
            "overall_status",
            "continuity_verdict",
            "production_binding_verdict",
            "database_verdict",
            "source_acceptance_verdict",
            "overall_acceptance",
            "verdict_sha256",
        )
    }


def _v1_passed(value: dict[str, Any]) -> bool:
    return (
        value["overall_status"] == "EVIDENCE_VERIFIED"
        and value["continuity_verdict"] == "PASS"
        and value["production_binding_verdict"] == "PASS"
        and value["database_verdict"] == "PASS"
        and value["source_acceptance_verdict"] == "NOT_EVALUATED"
        and value["overall_acceptance"] == "NOT_CLAIMED"
    )


def _artifact_error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return (
        code
        if type(code) is str and _ISSUE_CODE_RE.fullmatch(code) is not None
        else "SOURCE_MONITORING_SOAK_OPERATIONAL_ARTIFACT_INVALID"
    )


def _verify_official_source_operational_acceptance_paths(
    *,
    plan_path: Any,
    baseline_inventory_path: Any,
    ledger_path: Any,
    final_inventory_path: Any,
    _bundle_identity: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Private explicit-path seam used by focused tests and bundle dispatch."""

    result = _blank_result()
    issues = _IssueCollector()
    try:
        paths = tuple(
            _native_path(value, label=label)
            for value, label in (
                (plan_path, "plan_path"),
                (baseline_inventory_path, "baseline_inventory_path"),
                (ledger_path, "ledger_path"),
                (final_inventory_path, "final_inventory_path"),
            )
        )
        if len(set(paths)) != 4:
            raise SourceMonitoringSoakAcceptanceError(
                "SOURCE_MONITORING_SOAK_OPERATIONAL_ARTIFACT_ALIAS",
                "the four explicit artifact paths must be distinct",
            )
        plan_file, baseline_file, ledger_file, final_file = paths
        pre_load_signatures = _capture_signatures(paths)
        plan = load_source_monitoring_soak_plan(plan_file)
        baseline = load_soak_db_inventory(baseline_file)
        final = load_soak_db_inventory(final_file)
        initial_signatures = _capture_signatures(paths)
        if initial_signatures != pre_load_signatures:
            raise SourceMonitoringSoakAcceptanceError(
                "SOURCE_MONITORING_SOAK_OPERATIONAL_ARTIFACT_CHANGED",
                "a soak artifact changed while the four-piece set was loaded",
            )
    except (
        SourceMonitoringSoakAcceptanceError,
        SourceMonitoringSoakPlanError,
        SoakDbInventoryError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        issues.add("artifact", _artifact_error_code(exc))
        return _seal_result(result, issues)

    plan_keys = tuple(row["adapter_key"] for row in plan["enabled_adapters"])
    planned_key_set = set(plan_keys)
    required_key_set = set(REQUIRED_OFFICIAL_ADAPTER_KEYS)
    for adapter_key in sorted(required_key_set - planned_key_set):
        issues.add(
            "operational",
            "SOURCE_MONITORING_SOAK_REQUIRED_ADAPTER_MISSING",
            adapter_key=adapter_key,
        )
    for adapter_key in sorted(planned_key_set - required_key_set):
        issues.add(
            "operational",
            "SOURCE_MONITORING_SOAK_UNEXPECTED_ADAPTER_CONFIGURED",
            adapter_key=adapter_key,
        )
    planned_config_versions = {
        row["adapter_key"]: row["config_version"]
        for row in plan["enabled_adapters"]
    }
    total_terminal_runs = 0

    def observe_operational(record: dict[str, Any]) -> None:
        nonlocal total_terminal_runs
        if record["event_type"] != SOAK_EVENT_RUN_TERMINAL:
            return
        total_terminal_runs += 1
        sequence_no = record["sequence_no"]
        payload = record["payload"]
        adapter_key = payload["adapter_key"]
        run_id = payload["run_id"]
        if adapter_key not in required_key_set:
            issues.add(
                "operational",
                "SOURCE_MONITORING_SOAK_UNEXPECTED_ADAPTER_OBSERVED",
                sequence_no=sequence_no,
                adapter_key=adapter_key,
                run_id=run_id,
            )
            return
        counters = result["per_adapter_counts"][adapter_key]
        if payload["status"] == "SUCCEEDED":
            counters["succeeded_run_count"] += 1
        else:
            counters["disallowed_status_run_count"] += 1
            issues.add(
                "operational",
                "SOURCE_MONITORING_SOAK_DISALLOWED_RUN_STATUS",
                sequence_no=sequence_no,
                adapter_key=adapter_key,
                run_id=run_id,
            )
        counters["terminal_run_count"] += 1
        rejected_count = payload["counts"]["rejected_count"]
        counters["rejected_item_count"] += rejected_count
        if rejected_count:
            issues.add(
                "operational",
                "SOURCE_MONITORING_SOAK_REJECTED_SOURCE_ITEMS",
                sequence_no=sequence_no,
                adapter_key=adapter_key,
                run_id=run_id,
            )
        if payload["error_code"]:
            counters["source_error_run_count"] += 1
            issues.add(
                "operational",
                "SOURCE_MONITORING_SOAK_SOURCE_ERROR_OBSERVED",
                sequence_no=sequence_no,
                adapter_key=adapter_key,
                run_id=run_id,
            )
        if (
            payload["counts"]["accepted_count"] > 0
            and payload["import_receipt_sha256"] == ""
        ):
            counters["missing_import_receipt_run_count"] += 1
            issues.add(
                "operational",
                "SOURCE_MONITORING_SOAK_ACCEPTED_ITEMS_RECEIPT_MISSING",
                sequence_no=sequence_no,
                adapter_key=adapter_key,
                run_id=run_id,
            )
        if payload["config_version"] != planned_config_versions.get(adapter_key):
            counters["config_mismatch_run_count"] += 1
            issues.add(
                "operational",
                "SOURCE_MONITORING_SOAK_CONFIG_VERSION_MISMATCH",
                sequence_no=sequence_no,
                adapter_key=adapter_key,
                run_id=run_id,
            )
        if payload["market_calls_performed"] != 0:
            counters["market_activity_run_count"] += 1
            issues.add(
                "operational",
                "SOURCE_MONITORING_SOAK_MARKET_ACTIVITY_OBSERVED",
                sequence_no=sequence_no,
                adapter_key=adapter_key,
                run_id=run_id,
            )

    # V1 owns the only ledger stream.  The operational layer receives each
    # already-validated record synchronously from that same open stream, so no
    # pathname is reopened between the two evidence layers.
    try:
        raw_v1 = _verify_soak_evidence_with_observer(
            ledger_file,
            baseline_inventory=baseline,
            final_inventory=final,
            expected_bindings=_expected_bindings(plan),
            expected_enabled_adapter_keys=REQUIRED_OFFICIAL_ADAPTER_KEYS,
            validated_record_observer=observe_operational,
        )
    except (
        SourceMonitoringSoakVerifierError,
        SourceMonitoringSoakEvidenceError,
        SoakDbInventoryError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        issues.add("v1", _artifact_error_code(exc))
        return _seal_result(result, issues)

    v1 = _validate_v1_verdict(raw_v1)
    if v1 is None:
        issues.add("v1", "SOURCE_MONITORING_SOAK_OPERATIONAL_V1_VERDICT_INVALID")
        return _seal_result(result, issues)
    result["v1_evidence_verdict"] = _copy_v1_summary(v1)

    try:
        if _capture_signatures(paths) != initial_signatures:
            raise SourceMonitoringSoakAcceptanceError(
                "SOURCE_MONITORING_SOAK_OPERATIONAL_ARTIFACT_CHANGED",
                "a soak artifact changed across v1 verification",
            )
        if _bundle_identity is not None:
            bundle_identity, _names = _scan_bundle(plan_file.parent)
            if bundle_identity != _bundle_identity:
                raise SourceMonitoringSoakAcceptanceError(
                    "SOURCE_MONITORING_SOAK_OPERATIONAL_BUNDLE_CHANGED",
                    "soak bundle identity changed across v1 verification",
                )
    except SourceMonitoringSoakAcceptanceError as exc:
        issues.add("artifact", exc.code)

    identity = v1["identity"]
    if (
        identity["campaign_id"] != plan["campaign_id"]
        or identity["session_id"] != plan["session_id"]
    ):
        issues.add(
            "operational",
            "SOURCE_MONITORING_SOAK_PLAN_LEDGER_IDENTITY_MISMATCH",
        )
    if not _v1_passed(v1):
        issues.add("v1", "SOURCE_MONITORING_SOAK_V1_EVIDENCE_NOT_VERIFIED")
    if total_terminal_runs != v1["counts"]["run_terminal_count"]:
        issues.add(
            "operational",
            "SOURCE_MONITORING_SOAK_OPERATIONAL_LEDGER_BINDING_MISMATCH",
        )

    for adapter_key in REQUIRED_OFFICIAL_ADAPTER_KEYS:
        counters = result["per_adapter_counts"][adapter_key]
        if counters["terminal_run_count"] == 0:
            issues.add(
                "operational",
                "SOURCE_MONITORING_SOAK_REQUIRED_ADAPTER_NOT_OBSERVED",
                adapter_key=adapter_key,
            )
        if counters["succeeded_run_count"] == 0:
            issues.add(
                "operational",
                "SOURCE_MONITORING_SOAK_REQUIRED_SUCCESS_MISSING",
                adapter_key=adapter_key,
            )

    if issues.total == 0:
        result["overall_status"] = "OPERATIONAL_SOURCE_PATH_ACCEPTED"
        result["source_acceptance_verdict"] = "PASS"
    return _seal_result(result, issues)


def verify_official_source_operational_acceptance(
    bundle_path: str | Path,
) -> dict[str, Any]:
    """Verify the four fixed artifacts in one exact official soak bundle."""

    try:
        plan, baseline, ledger, final, bundle_identity = _bundle_paths(bundle_path)
    except SourceMonitoringSoakAcceptanceError as exc:
        return _failure("bundle", exc.code)
    return _verify_official_source_operational_acceptance_paths(
        plan_path=plan,
        baseline_inventory_path=baseline,
        ledger_path=ledger,
        final_inventory_path=final,
        _bundle_identity=bundle_identity,
    )


__all__ = [
    "MAX_SOAK_OPERATIONAL_ACCEPTANCE_ISSUES",
    "REQUIRED_OFFICIAL_ADAPTER_KEYS",
    "SOURCE_MONITORING_SOAK_OPERATIONAL_ACCEPTANCE_VERSION",
    "SourceMonitoringSoakAcceptanceError",
    "verify_official_source_operational_acceptance",
]
