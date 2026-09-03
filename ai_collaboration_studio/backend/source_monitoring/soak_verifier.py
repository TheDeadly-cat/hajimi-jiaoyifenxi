"""Bounded continuity and database verification for soak evidence v1.

The verifier consumes an already-written JSONL ledger plus caller-supplied,
sealed database inventories.  It never discovers or opens a database, starts a
runtime, polls a source, or performs Provider, market, model, or trading work.

Passing continuity and database binding deliberately does not establish real
source acceptance.  V1 always reports that question as not evaluated.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from .contracts import canonical_sha256
from .soak_db_inventory import (
    MAX_SOAK_SESSION_RUN_IDS,
    SoakDbInventoryError,
    validate_soak_db_inventory_delta,
)
from .soak_evidence import (
    SOAK_EVENT_RUN_TERMINAL,
    SOAK_EVENT_RUNTIME_SAMPLE,
    SOAK_EVENT_SESSION_ENDED,
    SOAK_EVENT_SESSION_STARTED,
    SourceMonitoringSoakEvidenceError,
    validate_soak_evidence,
)
from .soak_plan import (
    SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS,
    SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS,
    SOURCE_MONITORING_SOAK_SAMPLE_INTERVAL_NS,
)


SOURCE_MONITORING_SOAK_VERDICT_VERSION = "source_monitoring_soak_verdict_v1"
REQUIRED_SOAK_DURATION_NS = SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS
REQUIRED_SOAK_SAMPLE_INTERVAL_NS = SOURCE_MONITORING_SOAK_SAMPLE_INTERVAL_NS
REQUIRED_SOAK_MAXIMUM_SAMPLE_GAP_NS = (
    SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS
)
MAX_SOAK_VERDICT_ISSUES = 64

_ISSUE_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,99}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ADAPTER_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_RESULT_FIELDS = frozenset(
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
_IDENTITY_FIELDS = frozenset({"campaign_id", "session_id", "runtime_id"})
_COUNT_FIELDS = frozenset(
    {
        "ledger_record_count",
        "runtime_sample_count",
        "run_terminal_count",
        "unique_terminal_run_count",
        "expected_adapter_count",
        "covered_adapter_count",
    }
)
_TIMING_FIELDS = frozenset(
    {
        "required_duration_ns",
        "declared_sample_interval_ns",
        "declared_maximum_sample_gap_ns",
        "observed_elapsed_ns",
        "maximum_observed_sample_gap_ns",
    }
)
_BINDING_FIELDS = frozenset(
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
_ISSUE_FIELDS = frozenset({"scope", "code", "sequence_no", "run_id"})
_PRODUCTION_BINDING_FIELDS = frozenset(
    {
        "settings_sha256",
        "registry_sha256",
        "code_identity_sha256",
        "db_startup_identity_sha256",
        "db_schema_sha256",
        "preview_sha256",
        "enabled_adapter_keys_sha256",
    }
)
_PRODUCTION_BINDING_ISSUE_CODES = {
    "settings_sha256": "SOAK_SETTINGS_BINDING_MISMATCH",
    "registry_sha256": "SOAK_REGISTRY_BINDING_MISMATCH",
    "code_identity_sha256": "SOAK_CODE_IDENTITY_BINDING_MISMATCH",
    "db_startup_identity_sha256": "SOAK_DB_STARTUP_IDENTITY_BINDING_MISMATCH",
    "db_schema_sha256": "SOAK_DB_SCHEMA_BINDING_MISMATCH",
    "preview_sha256": "SOAK_PREVIEW_BINDING_MISMATCH",
    "enabled_adapter_keys_sha256": "SOAK_ENABLED_ADAPTER_KEYS_BINDING_MISMATCH",
}
_SAFETY_FIELDS = frozenset(
    {
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


class SourceMonitoringSoakVerifierError(ValueError):
    """Raised only when the verifier's own closed output contract is broken."""


class _IssueCollector:
    def __init__(self) -> None:
        self.total = 0
        self.rows: list[dict[str, Any]] = []
        self.by_scope = {
            "ledger": 0,
            "continuity": 0,
            "binding": 0,
            "database": 0,
        }

    def add(
        self,
        scope: str,
        code: str,
        *,
        sequence_no: int = 0,
        run_id: str = "",
    ) -> None:
        clean_scope = (
            scope
            if scope in {"ledger", "continuity", "binding", "database"}
            else "ledger"
        )
        clean_code = (
            code
            if type(code) is str and _ISSUE_CODE_RE.fullmatch(code)
            else "SOURCE_MONITORING_SOAK_VERIFICATION_FAILED"
        )
        clean_sequence = (
            sequence_no
            if type(sequence_no) is int and not isinstance(sequence_no, bool) and sequence_no >= 0
            else 0
        )
        clean_run_id = run_id if type(run_id) is str and len(run_id) <= 160 else ""
        self.total += 1
        self.by_scope[clean_scope] += 1
        if len(self.rows) < MAX_SOAK_VERDICT_ISSUES:
            self.rows.append(
                {
                    "scope": clean_scope,
                    "code": clean_code,
                    "sequence_no": clean_sequence,
                    "run_id": clean_run_id,
                }
            )

    def add_hidden(self, count: int) -> None:
        if type(count) is int and count > 0:
            self.total += count

    def count(self, *scopes: str) -> int:
        return sum(self.by_scope.get(scope, 0) for scope in scopes)


def _blank_result() -> dict[str, Any]:
    return {
        "version": SOURCE_MONITORING_SOAK_VERDICT_VERSION,
        "overall_status": "FAILED",
        "continuity_verdict": "FAIL",
        "production_binding_verdict": "NOT_EVALUATED",
        "database_verdict": "NOT_EVALUATED",
        "source_acceptance_verdict": "NOT_EVALUATED",
        "overall_acceptance": "NOT_CLAIMED",
        "identity": {
            "campaign_id": "",
            "session_id": "",
            "runtime_id": "",
        },
        "counts": {
            "ledger_record_count": 0,
            "runtime_sample_count": 0,
            "run_terminal_count": 0,
            "unique_terminal_run_count": 0,
            "expected_adapter_count": 0,
            "covered_adapter_count": 0,
        },
        "timing": {
            "required_duration_ns": REQUIRED_SOAK_DURATION_NS,
            "declared_sample_interval_ns": 0,
            "declared_maximum_sample_gap_ns": 0,
            "observed_elapsed_ns": 0,
            "maximum_observed_sample_gap_ns": 0,
        },
        "bindings": {
            "ledger_terminal": False,
            "last_record_sha256": "",
            "baseline_inventory_sha256": "",
            "final_inventory_sha256": "",
            "database_delta_verdict_sha256": "",
            "expected_production_bindings_sha256": "",
            "observed_production_bindings_sha256": "",
        },
        "issue_count": 0,
        "issues": [],
        "issues_truncated": False,
        "verdict_sha256": "",
        "safety": dict(_SAFETY),
    }


def _safe_hash(value: Any) -> str:
    return value if type(value) is str and _SHA256_RE.fullmatch(value) else ""


def _validate_expected_production_bindings(
    value: Any,
    *,
    issues: _IssueCollector,
) -> dict[str, str] | None:
    if value is None:
        issues.add("binding", "SOAK_EXPECTED_BINDINGS_MISSING")
        return None
    if type(value) is not dict or set(value) != _PRODUCTION_BINDING_FIELDS:
        issues.add("binding", "SOAK_EXPECTED_BINDINGS_INVALID")
        return None
    normalized: dict[str, str] = {}
    for field in sorted(_PRODUCTION_BINDING_FIELDS):
        candidate = value.get(field)
        if type(candidate) is not str or _SHA256_RE.fullmatch(candidate) is None:
            issues.add("binding", "SOAK_EXPECTED_BINDINGS_INVALID")
            return None
        normalized[field] = candidate
    return normalized


def _validate_expected_adapter_keys(
    value: Any,
    *,
    issues: _IssueCollector,
) -> tuple[str, ...] | None:
    if (
        type(value) not in {list, tuple}
        or not 1 <= len(value) <= 50
        or any(
            type(item) is not str or _ADAPTER_KEY_RE.fullmatch(item) is None
            for item in value
        )
    ):
        issues.add("binding", "SOAK_EXPECTED_ADAPTER_KEYS_INVALID")
        return None
    normalized = tuple(value)
    if tuple(sorted(normalized)) != normalized or len(set(normalized)) != len(normalized):
        issues.add("binding", "SOAK_EXPECTED_ADAPTER_KEYS_INVALID")
        return None
    return normalized


def _seal_result(result: dict[str, Any], issues: _IssueCollector) -> dict[str, Any]:
    result["issue_count"] = issues.total
    result["issues"] = list(issues.rows)
    result["issues_truncated"] = issues.total > len(issues.rows)
    if (
        set(result) != _RESULT_FIELDS
        or set(result["identity"]) != _IDENTITY_FIELDS
        or set(result["counts"]) != _COUNT_FIELDS
        or set(result["timing"]) != _TIMING_FIELDS
        or set(result["bindings"]) != _BINDING_FIELDS
        or any(type(issue) is not dict or set(issue) != _ISSUE_FIELDS for issue in result["issues"])
        or set(result["safety"]) != _SAFETY_FIELDS
        or result["safety"] != _SAFETY
        or result["source_acceptance_verdict"] != "NOT_EVALUATED"
        or result["overall_acceptance"] != "NOT_CLAIMED"
    ):
        raise SourceMonitoringSoakVerifierError(
            "source monitoring soak verdict escaped its closed v1 contract"
        )
    unsigned = {key: value for key, value in result.items() if key != "verdict_sha256"}
    result["verdict_sha256"] = canonical_sha256(unsigned)
    return result


def _verify_soak_evidence(
    ledger_path: str | Path,
    *,
    baseline_inventory: Any,
    final_inventory: Any,
    expected_bindings: Any = None,
    expected_enabled_adapter_keys: Any = None,
    validated_record_observer: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Verify one ledger without reading any database or external source."""

    if validated_record_observer is not None and not callable(
        validated_record_observer
    ):
        raise SourceMonitoringSoakVerifierError(
            "validated_record_observer must be callable"
        )
    result = _blank_result()
    issues = _IssueCollector()
    start_record: dict[str, Any] | None = None
    end_record: dict[str, Any] | None = None
    runtime_sample_count = 0
    last_sample_time_ns = 0
    maximum_observed_sample_gap_ns = 0
    run_terminal_count = 0
    unrecorded_terminal_count = 0
    terminal_by_id: dict[str, dict[str, Any]] = {}
    terminal_adapter_keys: set[str] = set()
    duplicate_run_ids: set[str] = set()
    record_count = 0

    def observe(record: dict[str, Any]) -> None:
        nonlocal start_record, end_record, record_count
        nonlocal runtime_sample_count, last_sample_time_ns
        nonlocal maximum_observed_sample_gap_ns
        nonlocal run_terminal_count, unrecorded_terminal_count
        record_count += 1
        if validated_record_observer is not None:
            validated_record_observer(record)
        event_type = record["event_type"]
        if event_type == SOAK_EVENT_SESSION_STARTED:
            start_record = record
            return
        if event_type == SOAK_EVENT_RUNTIME_SAMPLE:
            sample_time_ns = record["monotonic_elapsed_ns"]
            sample_gap_ns = sample_time_ns - last_sample_time_ns
            runtime_sample_count += 1
            last_sample_time_ns = sample_time_ns
            maximum_observed_sample_gap_ns = max(
                maximum_observed_sample_gap_ns,
                sample_gap_ns,
            )
            payload = record["payload"]
            if not (
                payload["thread_alive"] is True
                and payload["liveness_verified"] is True
                and payload["runtime_status"] in {"running", "degraded"}
            ):
                issues.add(
                    "continuity",
                    "SOAK_RUNTIME_SAMPLE_NOT_LIVE",
                    sequence_no=record["sequence_no"],
                )
            if (
                start_record is not None
                and sample_gap_ns
                > start_record["payload"]["maximum_sample_gap_ns"]
            ):
                issues.add(
                    "continuity",
                    "SOAK_SAMPLE_GAP_EXCEEDED",
                    sequence_no=record["sequence_no"],
                )
            return
        if event_type == SOAK_EVENT_RUN_TERMINAL:
            run_terminal_count += 1
            payload = record["payload"]
            run_id = payload["run_id"]
            if payload["state_recorded"] is not True:
                unrecorded_terminal_count += 1
                issues.add(
                    "continuity",
                    "SOAK_TERMINAL_RUN_STATE_NOT_RECORDED",
                    sequence_no=record["sequence_no"],
                    run_id=run_id,
                )
                return
            if run_id in terminal_by_id:
                duplicate_run_ids.add(run_id)
                issues.add(
                    "continuity",
                    "SOAK_TERMINAL_RUN_ID_DUPLICATE",
                    sequence_no=record["sequence_no"],
                    run_id=run_id,
                )
                return
            if len(terminal_by_id) >= MAX_SOAK_SESSION_RUN_IDS:
                issues.add(
                    "continuity",
                    "SOAK_TERMINAL_RUN_LIMIT_EXCEEDED",
                    sequence_no=record["sequence_no"],
                    run_id=run_id,
                )
                return
            terminal_by_id[run_id] = record
            terminal_adapter_keys.add(payload["adapter_key"])
            return
        if event_type == SOAK_EVENT_SESSION_ENDED:
            end_record = record

    try:
        ledger_summary = validate_soak_evidence(ledger_path, on_record=observe)
    except SourceMonitoringSoakEvidenceError as exc:
        issues.add("ledger", exc.code)
        result["overall_status"] = "INVALID_LEDGER"
        result["counts"]["ledger_record_count"] = record_count
        result["counts"]["runtime_sample_count"] = runtime_sample_count
        result["counts"]["run_terminal_count"] = run_terminal_count
        result["counts"]["unique_terminal_run_count"] = len(terminal_by_id)
        return _seal_result(result, issues)

    result["identity"] = {
        "campaign_id": ledger_summary["campaign_id"],
        "session_id": ledger_summary["session_id"],
        "runtime_id": ledger_summary["runtime_id"],
    }
    result["counts"] = {
        "ledger_record_count": ledger_summary["record_count"],
        "runtime_sample_count": runtime_sample_count,
        "run_terminal_count": run_terminal_count,
        "unique_terminal_run_count": len(terminal_by_id),
        "expected_adapter_count": 0,
        "covered_adapter_count": 0,
    }
    result["bindings"]["ledger_terminal"] = ledger_summary["terminal"]
    result["bindings"]["last_record_sha256"] = ledger_summary[
        "last_record_sha256"
    ]

    if start_record is None:  # The ledger validator makes this unreachable.
        issues.add("ledger", "SOAK_SESSION_START_MISSING")
        result["overall_status"] = "INVALID_LEDGER"
        return _seal_result(result, issues)

    start_payload = start_record["payload"]
    observed_bindings = {
        field: start_payload[field]
        for field in sorted(_PRODUCTION_BINDING_FIELDS)
    }
    result["bindings"]["observed_production_bindings_sha256"] = canonical_sha256(
        observed_bindings
    )
    normalized_expected_bindings = _validate_expected_production_bindings(
        expected_bindings,
        issues=issues,
    )
    if normalized_expected_bindings is not None:
        result["bindings"]["expected_production_bindings_sha256"] = canonical_sha256(
            normalized_expected_bindings
        )
        for field in sorted(_PRODUCTION_BINDING_FIELDS):
            if normalized_expected_bindings[field] != observed_bindings[field]:
                issues.add(
                    "binding",
                    _PRODUCTION_BINDING_ISSUE_CODES[field],
                )
    normalized_expected_adapter_keys = _validate_expected_adapter_keys(
        expected_enabled_adapter_keys,
        issues=issues,
    )
    if normalized_expected_adapter_keys is not None:
        expected_adapter_set = set(normalized_expected_adapter_keys)
        result["counts"]["expected_adapter_count"] = len(expected_adapter_set)
        result["counts"]["covered_adapter_count"] = len(
            terminal_adapter_keys & expected_adapter_set
        )
        if start_payload["enabled_adapter_count"] != len(expected_adapter_set):
            issues.add("binding", "SOAK_ENABLED_ADAPTER_COUNT_MISMATCH")
        if start_payload["enabled_adapter_keys_sha256"] != canonical_sha256(
            list(normalized_expected_adapter_keys)
        ):
            issues.add("binding", "SOAK_ENABLED_ADAPTER_KEYS_BINDING_MISMATCH")
        for _adapter_key in sorted(expected_adapter_set - terminal_adapter_keys):
            issues.add("binding", "SOAK_ENABLED_ADAPTER_NOT_COVERED")
        for _adapter_key in sorted(terminal_adapter_keys - expected_adapter_set):
            issues.add("binding", "SOAK_UNEXPECTED_ADAPTER_OBSERVED")
    if start_payload["mode"] != "official":
        issues.add("binding", "SOAK_MODE_POLICY_INVALID")
    result["production_binding_verdict"] = (
        "PASS"
        if normalized_expected_bindings is not None
        and normalized_expected_adapter_keys is not None
        and issues.count("binding") == 0
        else "FAIL"
    )
    declared_required = start_payload["required_duration_ns"]
    declared_sample_interval = start_payload["sample_interval_ns"]
    declared_maximum_gap = start_payload["maximum_sample_gap_ns"]
    result["timing"]["declared_sample_interval_ns"] = declared_sample_interval
    result["timing"]["declared_maximum_sample_gap_ns"] = declared_maximum_gap
    if declared_required != REQUIRED_SOAK_DURATION_NS:
        issues.add("continuity", "SOAK_REQUIRED_DURATION_POLICY_INVALID")
    if declared_sample_interval != REQUIRED_SOAK_SAMPLE_INTERVAL_NS:
        issues.add("continuity", "SOAK_SAMPLE_INTERVAL_POLICY_INVALID")
    if declared_maximum_gap != REQUIRED_SOAK_MAXIMUM_SAMPLE_GAP_NS:
        issues.add("continuity", "SOAK_MAXIMUM_SAMPLE_GAP_POLICY_INVALID")
    if start_payload["recovered_running_count"] != 0:
        issues.add("continuity", "SOAK_RECOVERED_RUNNING_ROWS_PRESENT")

    if not ledger_summary["terminal"] or end_record is None:
        issues.add("ledger", "SOAK_SESSION_END_MISSING")
        result["overall_status"] = "INCOMPLETE_UNSEALED"
        result["continuity_verdict"] = "INCOMPLETE_UNSEALED"
        result["database_verdict"] = "NOT_EVALUATED"
        return _seal_result(result, issues)

    end_payload = end_record["payload"]
    elapsed_ns = end_payload["elapsed_ns"]
    result["timing"]["observed_elapsed_ns"] = elapsed_ns
    if end_payload["reason"] != "duration_reached":
        issues.add("continuity", "SOAK_END_REASON_INVALID")
    if end_payload["runtime_stopped_cleanly"] is not True:
        issues.add("continuity", "SOAK_RUNTIME_STOP_NOT_CLEAN")
    if elapsed_ns < REQUIRED_SOAK_DURATION_NS:
        issues.add("continuity", "SOAK_DURATION_INCOMPLETE")
    if runtime_sample_count == 0:
        issues.add("continuity", "SOAK_RUNTIME_SAMPLE_MISSING")
    else:
        final_gap_ns = elapsed_ns - last_sample_time_ns
        maximum_observed_sample_gap_ns = max(
            maximum_observed_sample_gap_ns,
            final_gap_ns,
        )
        if final_gap_ns > declared_maximum_gap:
            issues.add(
                "continuity",
                "SOAK_SAMPLE_GAP_EXCEEDED",
                sequence_no=end_record["sequence_no"],
            )
    result["timing"]["maximum_observed_sample_gap_ns"] = (
        maximum_observed_sample_gap_ns
    )

    result["continuity_verdict"] = (
        "PASS" if issues.count("ledger", "continuity") == 0 else "FAIL"
    )

    database_issue_start = issues.total
    delta_verdict: dict[str, Any] | None = None
    local_declarations_valid = (
        not duplicate_run_ids
        and unrecorded_terminal_count == 0
        and len(terminal_by_id) == run_terminal_count
    )

    try:
        if type(baseline_inventory) is not dict or type(final_inventory) is not dict:
            raise SoakDbInventoryError(
                "SOAK_DB_INVENTORY_INVALID",
                "caller-supplied inventories must be native objects",
            )
        result["bindings"]["baseline_inventory_sha256"] = _safe_hash(
            baseline_inventory.get("inventory_sha256")
        )
        result["bindings"]["final_inventory_sha256"] = _safe_hash(
            final_inventory.get("inventory_sha256")
        )
        if not local_declarations_valid:
            issues.add("database", "SOAK_DB_SESSION_DECLARATIONS_INVALID")
        else:
            terminal_ids = list(terminal_by_id)
            declarations = [
                {
                    "run_id": run_id,
                    "status": terminal_by_id[run_id]["payload"]["status"],
                    "state_recorded": True,
                    "run_record_sha256": terminal_by_id[run_id]["payload"][
                        "run_record_sha256"
                    ],
                    "import_receipt_sha256": terminal_by_id[run_id]["payload"][
                        "import_receipt_sha256"
                    ],
                }
                for run_id in terminal_ids
            ]
            delta_verdict = validate_soak_db_inventory_delta(
                baseline_inventory,
                final_inventory,
                session_terminal_run_ids=terminal_ids,
                session_run_declarations=declarations,
            )
            result["bindings"]["database_delta_verdict_sha256"] = delta_verdict[
                "verdict_sha256"
            ]
            if delta_verdict["verdict"] != "PASS":
                for issue in delta_verdict["issues"]:
                    issues.add(
                        "database",
                        issue["code"],
                        run_id=issue["run_id"],
                    )
                issues.add_hidden(
                    delta_verdict["issue_count"] - len(delta_verdict["issues"])
                )

        if start_payload["baseline_run_count"] != baseline_inventory.get("run_count"):
            issues.add("database", "SOAK_BASELINE_RUN_COUNT_MISMATCH")
        if (
            start_payload["baseline_run_inventory_sha256"]
            != baseline_inventory.get("inventory_sha256")
        ):
            issues.add("database", "SOAK_BASELINE_INVENTORY_HASH_MISMATCH")
        if (
            end_payload["final_run_inventory_sha256"]
            != final_inventory.get("inventory_sha256")
        ):
            issues.add("database", "SOAK_FINAL_INVENTORY_HASH_MISMATCH")
        if end_payload["session_run_count"] != run_terminal_count:
            issues.add("database", "SOAK_SESSION_RUN_COUNT_MISMATCH")

        final_runs_value = final_inventory.get("runs")
        final_by_id = (
            {
                entry["run_id"]: entry
                for entry in final_runs_value
                if type(entry) is dict and type(entry.get("run_id")) is str
            }
            if type(final_runs_value) is list
            else {}
        )
        for run_id, record in terminal_by_id.items():
            final_entry = final_by_id.get(run_id)
            if final_entry is None:
                continue
            payload = record["payload"]
            if payload["run_record_sha256"] != final_entry.get("row_sha256"):
                issues.add(
                    "database",
                    "SOAK_RUN_ROW_HASH_MISMATCH",
                    sequence_no=record["sequence_no"],
                    run_id=run_id,
                )
            if payload["import_receipt_sha256"] != final_entry.get("receipt_sha256"):
                issues.add(
                    "database",
                    "SOAK_IMPORT_RECEIPT_HASH_MISMATCH",
                    sequence_no=record["sequence_no"],
                    run_id=run_id,
                )
    except SoakDbInventoryError as exc:
        issues.add("database", exc.code)

    result["database_verdict"] = (
        "PASS"
        if issues.total == database_issue_start
        and delta_verdict is not None
        and delta_verdict["verdict"] == "PASS"
        else "FAIL"
    )
    result["overall_status"] = (
        "EVIDENCE_VERIFIED"
        if result["continuity_verdict"] == "PASS"
        and result["production_binding_verdict"] == "PASS"
        and result["database_verdict"] == "PASS"
        else "FAILED"
    )
    return _seal_result(result, issues)


def verify_soak_evidence(
    ledger_path: str | Path,
    *,
    baseline_inventory: Any,
    final_inventory: Any,
    expected_bindings: Any = None,
    expected_enabled_adapter_keys: Any = None,
) -> dict[str, Any]:
    """Verify one ledger without exposing an observation callback publicly."""

    return _verify_soak_evidence(
        ledger_path,
        baseline_inventory=baseline_inventory,
        final_inventory=final_inventory,
        expected_bindings=expected_bindings,
        expected_enabled_adapter_keys=expected_enabled_adapter_keys,
    )


def _verify_soak_evidence_with_observer(
    ledger_path: str | Path,
    *,
    baseline_inventory: Any,
    final_inventory: Any,
    expected_bindings: Any = None,
    expected_enabled_adapter_keys: Any = None,
    validated_record_observer: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Private one-pass bridge for a trusted layered verifier."""

    return _verify_soak_evidence(
        ledger_path,
        baseline_inventory=baseline_inventory,
        final_inventory=final_inventory,
        expected_bindings=expected_bindings,
        expected_enabled_adapter_keys=expected_enabled_adapter_keys,
        validated_record_observer=validated_record_observer,
    )


__all__ = [
    "MAX_SOAK_VERDICT_ISSUES",
    "REQUIRED_SOAK_DURATION_NS",
    "REQUIRED_SOAK_MAXIMUM_SAMPLE_GAP_NS",
    "REQUIRED_SOAK_SAMPLE_INTERVAL_NS",
    "SOURCE_MONITORING_SOAK_VERDICT_VERSION",
    "SourceMonitoringSoakVerifierError",
    "verify_soak_evidence",
]
