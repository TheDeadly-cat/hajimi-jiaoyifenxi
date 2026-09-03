"""Strict append-only JSONL evidence for Source Monitoring soak sessions.

This module is deliberately storage-only.  It does not open Studio SQLite,
construct an adapter, start a runtime, or perform network, Provider, market, or
trading work.  A later runner may feed it bounded runtime samples and terminal
run projections.

The hash chain detects accidental or post-run deletion, reordering, duplicate
insertion, truncation, replacement, and content drift.  It is not a signature
or an independently anchored anti-tamper attestation.
"""

from __future__ import annotations

import copy
import json
import os
import re
import stat as statlib
import threading
from pathlib import Path
from typing import Any, BinaryIO, Callable

from ..path_identity import first_reparse_component
from .contracts import MAX_NATIVE_INTEGER, canonical_json, canonical_sha256


SOURCE_MONITORING_SOAK_RECORD_VERSION = "source_monitoring_soak_record_v1"
SOURCE_MONITORING_SOAK_STREAM_VERSION = "source_monitoring_soak_stream_v1"

MAX_SOAK_RECORD_BYTES = 64 * 1024
MAX_SOAK_LEDGER_BYTES = 512 * 1024 * 1024
MAX_SOAK_RECORDS = 200_000
ZERO_SHA256 = "0" * 64

SOAK_EVENT_SESSION_STARTED = "SESSION_STARTED"
SOAK_EVENT_RUNTIME_SAMPLE = "RUNTIME_SAMPLE"
SOAK_EVENT_RUN_TERMINAL = "RUN_TERMINAL"
SOAK_EVENT_SESSION_ENDED = "SESSION_ENDED"
SOAK_EVENT_TYPES = frozenset(
    {
        SOAK_EVENT_SESSION_STARTED,
        SOAK_EVENT_RUNTIME_SAMPLE,
        SOAK_EVENT_RUN_TERMINAL,
        SOAK_EVENT_SESSION_ENDED,
    }
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "version",
        "campaign_id",
        "session_id",
        "runtime_id",
        "sequence_no",
        "event_type",
        "wall_time_ms",
        "monotonic_elapsed_ns",
        "previous_record_sha256",
        "payload",
        "record_sha256",
    }
)
_START_FIELDS = frozenset(
    {
        "mode",
        "preview_sha256",
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
        "recovered_running_count",
        "enabled_adapter_count",
        "enabled_adapter_keys_sha256",
    }
)
_SAMPLE_FIELDS = frozenset(
    {
        "runtime_status",
        "thread_alive",
        "liveness_verified",
        "heartbeat_age_ms",
        "active_adapter",
        "last_loop_at",
    }
)
_RUN_FIELDS = frozenset(
    {
        "adapter_key",
        "config_version",
        "run_id",
        "status",
        "state_recorded",
        "run_record_sha256",
        "import_receipt_sha256",
        "counts",
        "error_code",
        "market_calls_performed",
        "source_evidence_status",
    }
)
_RUN_COUNT_FIELDS = frozenset(
    {
        "observed_count",
        "accepted_count",
        "duplicate_count",
        "rejected_count",
    }
)
_END_FIELDS = frozenset(
    {
        "reason",
        "elapsed_ns",
        "runtime_stopped_cleanly",
        "session_run_count",
        "final_run_inventory_sha256",
        "safety",
    }
)
_END_SAFETY_FIELDS = frozenset(
    {
        "provider_calls_performed",
        "model_calls_performed",
        "formal_rounds_created",
        "execution_capability",
        "live_trading_allowed",
    }
)

_CAMPAIGN_ID_RE = re.compile(r"source_soak_campaign_[0-9a-f]{32}\Z")
_SESSION_ID_RE = re.compile(r"source_soak_session_[0-9a-f]{32}\Z")
_RUNTIME_ID_RE = re.compile(r"source_monitor_runtime_[0-9a-f]{32}\Z")
_RUN_ID_RE = re.compile(r"source_run_[0-9a-f]{32}\Z")
_ADAPTER_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

_MODES = frozenset({"official", "futu"})
_RUNTIME_STATUSES = frozenset(
    {
        "disabled",
        "stopped",
        "starting",
        "running",
        "degraded",
        "stalled",
        "failed",
        "stopping",
    }
)
_RUN_STATUSES = frozenset(
    {
        "SUCCEEDED",
        "DEGRADED",
        "FAILED",
        "DRY_RUN",
        "DRY_RUN_FAILED",
        "ABANDONED",
    }
)
_END_REASONS = frozenset(
    {
        "duration_reached",
        "operator_interrupted",
        "runtime_failed",
        "runtime_stalled",
        "evidence_write_failed",
        "start_failed",
    }
)


class SourceMonitoringSoakEvidenceError(ValueError):
    """A bounded failure raised by the soak evidence contract."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _error(code: str, message: str) -> SourceMonitoringSoakEvidenceError:
    return SourceMonitoringSoakEvidenceError(message, code=code)


def _native_non_negative(value: Any, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_NATIVE_INTEGER:
        raise _error(
            "SOURCE_MONITORING_SOAK_INTEGER_INVALID",
            f"{field} must be a non-negative native signed 64-bit integer",
        )
    return value


def _positive(value: Any, field: str) -> int:
    result = _native_non_negative(value, field)
    if result == 0:
        raise _error(
            "SOURCE_MONITORING_SOAK_INTEGER_INVALID",
            f"{field} must be positive",
        )
    return result


def _sha256(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise _error(
            "SOURCE_MONITORING_SOAK_HASH_INVALID",
            f"{field} must be a lowercase SHA-256 digest",
        )
    return value


def _bounded_token(
    value: Any,
    field: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str:
        raise _error(
            "SOURCE_MONITORING_SOAK_TEXT_INVALID",
            f"{field} must be a native string",
        )
    if (not value and not allow_empty) or len(value) > maximum:
        raise _error(
            "SOURCE_MONITORING_SOAK_TEXT_INVALID",
            f"{field} is outside its bounded text contract",
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _error(
            "SOURCE_MONITORING_SOAK_TEXT_INVALID",
            f"{field} contains a control character",
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _error(
            "SOURCE_MONITORING_SOAK_TEXT_INVALID",
            f"{field} is not valid UTF-8",
        ) from exc
    return value


def _exact_fields(value: Any, expected: frozenset[str], field: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            f"{field} must contain exactly the closed v1 field set",
        )
    return value


def _validate_start_payload(value: Any) -> dict[str, Any]:
    payload = _exact_fields(value, _START_FIELDS, "SESSION_STARTED.payload")
    if type(payload["mode"]) is not str or payload["mode"] not in _MODES:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "SESSION_STARTED.mode must be official or futu",
        )
    required = _positive(payload["required_duration_ns"], "required_duration_ns")
    sample = _positive(payload["sample_interval_ns"], "sample_interval_ns")
    maximum_gap = _positive(
        payload["maximum_sample_gap_ns"],
        "maximum_sample_gap_ns",
    )
    if maximum_gap < sample or sample > required:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "soak sampling bounds are internally inconsistent",
        )
    for field in (
        "preview_sha256",
        "settings_sha256",
        "registry_sha256",
        "code_identity_sha256",
        "db_startup_identity_sha256",
        "db_schema_sha256",
        "baseline_run_inventory_sha256",
        "enabled_adapter_keys_sha256",
    ):
        _sha256(payload[field], field)
    _native_non_negative(payload["baseline_run_count"], "baseline_run_count")
    _native_non_negative(
        payload["recovered_running_count"],
        "recovered_running_count",
    )
    if _positive(payload["enabled_adapter_count"], "enabled_adapter_count") > 50:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "enabled_adapter_count exceeds the closed registry bound",
        )
    return copy.deepcopy(payload)


def _validate_sample_payload(value: Any) -> dict[str, Any]:
    payload = _exact_fields(value, _SAMPLE_FIELDS, "RUNTIME_SAMPLE.payload")
    status = payload["runtime_status"]
    if type(status) is not str or status not in _RUNTIME_STATUSES:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "runtime_status is outside the closed runtime status set",
        )
    if type(payload["thread_alive"]) is not bool or type(
        payload["liveness_verified"]
    ) is not bool:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "runtime liveness fields must be native booleans",
        )
    if payload["liveness_verified"] and (
        not payload["thread_alive"] or status not in {"running", "degraded"}
    ):
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "verified liveness is inconsistent with the runtime sample",
        )
    _native_non_negative(payload["heartbeat_age_ms"], "heartbeat_age_ms")
    _native_non_negative(payload["last_loop_at"], "last_loop_at")
    active_adapter = _bounded_token(
        payload["active_adapter"],
        "active_adapter",
        maximum=64,
        allow_empty=True,
    )
    if active_adapter and _ADAPTER_KEY_RE.fullmatch(active_adapter) is None:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "active_adapter is not canonical",
        )
    return copy.deepcopy(payload)


def _validate_run_payload(value: Any) -> dict[str, Any]:
    payload = _exact_fields(value, _RUN_FIELDS, "RUN_TERMINAL.payload")
    adapter_key = _bounded_token(
        payload["adapter_key"],
        "adapter_key",
        maximum=64,
    )
    if _ADAPTER_KEY_RE.fullmatch(adapter_key) is None:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "adapter_key is not canonical",
        )
    _bounded_token(payload["config_version"], "config_version", maximum=160)
    state_recorded = payload["state_recorded"]
    if type(state_recorded) is not bool:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "state_recorded must be a native boolean",
        )
    run_id = payload["run_id"]
    run_hash = payload["run_record_sha256"]
    if state_recorded:
        if type(run_id) is not str or _RUN_ID_RE.fullmatch(run_id) is None:
            raise _error(
                "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
                "a recorded terminal run requires a canonical run_id",
            )
        _sha256(run_hash, "run_record_sha256")
    elif run_id != "" or run_hash != "":
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "an unrecorded boundary result cannot claim a run row",
        )
    status = payload["status"]
    if type(status) is not str or status not in _RUN_STATUSES:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "run status is outside the closed terminal status set",
        )
    _sha256(
        payload["import_receipt_sha256"],
        "import_receipt_sha256",
        allow_empty=True,
    )
    counts = _exact_fields(payload["counts"], _RUN_COUNT_FIELDS, "counts")
    for field in _RUN_COUNT_FIELDS:
        _native_non_negative(counts[field], field)
    if (
        counts["accepted_count"]
        + counts["duplicate_count"]
        + counts["rejected_count"]
        > counts["observed_count"]
    ):
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "terminal counts cannot exceed observed_count",
        )
    error_code = _bounded_token(
        payload["error_code"],
        "error_code",
        maximum=80,
        allow_empty=True,
    )
    if error_code and _ERROR_CODE_RE.fullmatch(error_code) is None:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "error_code is not canonical",
        )
    market_calls = payload["market_calls_performed"]
    if market_calls is not None and (
        type(market_calls) is not int or not 0 <= market_calls <= 50
    ):
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "market_calls_performed must be null or an integer from 0 to 50",
        )
    if payload["source_evidence_status"] != "not_evaluated":
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "v1 cannot claim source acceptance evidence",
        )
    return copy.deepcopy(payload)


def _validate_end_payload(value: Any, *, monotonic_elapsed_ns: int) -> dict[str, Any]:
    payload = _exact_fields(value, _END_FIELDS, "SESSION_ENDED.payload")
    reason = payload["reason"]
    if type(reason) is not str or reason not in _END_REASONS:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "session end reason is outside the closed v1 set",
        )
    elapsed = _native_non_negative(payload["elapsed_ns"], "elapsed_ns")
    if elapsed != monotonic_elapsed_ns:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "SESSION_ENDED elapsed_ns must match its monotonic envelope",
        )
    if type(payload["runtime_stopped_cleanly"]) is not bool:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "runtime_stopped_cleanly must be a native boolean",
        )
    _native_non_negative(payload["session_run_count"], "session_run_count")
    _sha256(payload["final_run_inventory_sha256"], "final_run_inventory_sha256")
    safety = _exact_fields(payload["safety"], _END_SAFETY_FIELDS, "safety")
    if (
        type(safety["provider_calls_performed"]) is not int
        or safety["provider_calls_performed"] != 0
        or type(safety["model_calls_performed"]) is not int
        or safety["model_calls_performed"] != 0
        or type(safety["formal_rounds_created"]) is not int
        or safety["formal_rounds_created"] != 0
        or type(safety["execution_capability"]) is not str
        or safety["execution_capability"] != "none"
        or type(safety["live_trading_allowed"]) is not bool
        or safety["live_trading_allowed"] is not False
    ):
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "SESSION_ENDED safety boundary is invalid",
        )
    return copy.deepcopy(payload)


def _validate_payload(
    event_type: str,
    payload: Any,
    *,
    monotonic_elapsed_ns: int,
) -> dict[str, Any]:
    if event_type == SOAK_EVENT_SESSION_STARTED:
        return _validate_start_payload(payload)
    if event_type == SOAK_EVENT_RUNTIME_SAMPLE:
        return _validate_sample_payload(payload)
    if event_type == SOAK_EVENT_RUN_TERMINAL:
        return _validate_run_payload(payload)
    if event_type == SOAK_EVENT_SESSION_ENDED:
        return _validate_end_payload(
            payload,
            monotonic_elapsed_ns=monotonic_elapsed_ns,
        )
    raise _error(
        "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
        "event_type is outside the closed v1 event set",
    )


def _record_digest(record: dict[str, Any]) -> str:
    unsigned = dict(record)
    unsigned.pop("record_sha256", None)
    return canonical_sha256(unsigned)


def _reparse_flag(metadata: os.stat_result) -> bool:
    flag = int(getattr(statlib, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    return bool(flag and attributes & flag)


def _require_independent_regular(metadata: os.stat_result, *, label: str) -> None:
    if (
        not statlib.S_ISREG(metadata.st_mode)
        or int(metadata.st_nlink) != 1
        or _reparse_flag(metadata)
    ):
        raise _error(
            "SOURCE_MONITORING_SOAK_PATH_INVALID",
            f"{label} must be an independent non-reparse regular file",
        )


def _stable_file_signature(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
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


def _unaliased_new_path(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise _error(
            "SOURCE_MONITORING_SOAK_PATH_INVALID",
            "soak ledger path must be a string or Path",
        )
    requested = Path(value).expanduser()
    if requested.suffix != ".jsonl":
        raise _error(
            "SOURCE_MONITORING_SOAK_PATH_INVALID",
            "soak ledger path must end in .jsonl",
        )
    if first_reparse_component(requested) is not None:
        raise _error(
            "SOURCE_MONITORING_SOAK_PATH_INVALID",
            "soak ledger path may not contain a symlink or reparse point",
        )
    parent = requested.parent.resolve(strict=False)
    if not parent.is_dir():
        raise _error(
            "SOURCE_MONITORING_SOAK_PATH_INVALID",
            "soak ledger parent directory must already exist",
        )
    return (parent / requested.name).resolve(strict=False)


def _unaliased_existing_path(value: str | Path) -> Path:
    requested = _unaliased_new_path(value)
    if first_reparse_component(requested) is not None:
        raise _error(
            "SOURCE_MONITORING_SOAK_PATH_INVALID",
            "soak ledger path may not contain a symlink or reparse point",
        )
    try:
        metadata = requested.lstat()
    except OSError as exc:
        raise _error(
            "SOURCE_MONITORING_SOAK_READ_FAILED",
            "soak ledger is unavailable",
        ) from exc
    _require_independent_regular(metadata, label="soak ledger")
    if metadata.st_size > MAX_SOAK_LEDGER_BYTES:
        raise _error(
            "SOURCE_MONITORING_SOAK_LIMIT_EXCEEDED",
            "soak ledger exceeds the bounded file size",
        )
    return requested


def _lock_stream_exclusive(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    stream.seek(0, os.SEEK_END)


def _unlock_stream(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _validate_record(
    value: Any,
    *,
    expected_sequence: int,
    expected_previous_sha256: str,
    expected_ids: tuple[str, str, str] | None,
    previous_monotonic_ns: int | None,
    terminal_seen: bool,
) -> dict[str, Any]:
    record = _exact_fields(value, _TOP_LEVEL_FIELDS, "record")
    if record["version"] != SOURCE_MONITORING_SOAK_RECORD_VERSION:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "unsupported soak evidence record version",
        )
    identifiers = (
        record["campaign_id"],
        record["session_id"],
        record["runtime_id"],
    )
    if (
        type(identifiers[0]) is not str
        or _CAMPAIGN_ID_RE.fullmatch(identifiers[0]) is None
        or type(identifiers[1]) is not str
        or _SESSION_ID_RE.fullmatch(identifiers[1]) is None
        or type(identifiers[2]) is not str
        or _RUNTIME_ID_RE.fullmatch(identifiers[2]) is None
    ):
        raise _error(
            "SOURCE_MONITORING_SOAK_IDENTITY_INVALID",
            "soak evidence identifiers are invalid",
        )
    if expected_ids is not None and identifiers != expected_ids:
        raise _error(
            "SOURCE_MONITORING_SOAK_IDENTITY_INVALID",
            "soak evidence identifiers changed within one ledger",
        )
    sequence = _positive(record["sequence_no"], "sequence_no")
    if sequence != expected_sequence:
        raise _error(
            "SOURCE_MONITORING_SOAK_CHAIN_INVALID",
            "soak evidence sequence is duplicated or non-contiguous",
        )
    event_type = record["event_type"]
    if type(event_type) is not str or event_type not in SOAK_EVENT_TYPES:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            "event_type is outside the closed v1 event set",
        )
    if sequence == 1 and event_type != SOAK_EVENT_SESSION_STARTED:
        raise _error(
            "SOURCE_MONITORING_SOAK_TRANSITION_INVALID",
            "the first soak evidence record must be SESSION_STARTED",
        )
    if sequence > 1 and event_type == SOAK_EVENT_SESSION_STARTED:
        raise _error(
            "SOURCE_MONITORING_SOAK_TRANSITION_INVALID",
            "SESSION_STARTED may appear only once",
        )
    if terminal_seen:
        raise _error(
            "SOURCE_MONITORING_SOAK_TRANSITION_INVALID",
            "records cannot follow SESSION_ENDED",
        )
    _native_non_negative(record["wall_time_ms"], "wall_time_ms")
    monotonic_ns = _native_non_negative(
        record["monotonic_elapsed_ns"],
        "monotonic_elapsed_ns",
    )
    if sequence == 1 and monotonic_ns != 0:
        raise _error(
            "SOURCE_MONITORING_SOAK_MONOTONIC_INVALID",
            "SESSION_STARTED must bind monotonic_elapsed_ns=0",
        )
    if previous_monotonic_ns is not None and monotonic_ns < previous_monotonic_ns:
        raise _error(
            "SOURCE_MONITORING_SOAK_MONOTONIC_INVALID",
            "monotonic_elapsed_ns moved backwards",
        )
    previous_hash = _sha256(
        record["previous_record_sha256"],
        "previous_record_sha256",
    )
    if previous_hash != expected_previous_sha256:
        raise _error(
            "SOURCE_MONITORING_SOAK_CHAIN_INVALID",
            "soak evidence previous-record hash is invalid",
        )
    _validate_payload(
        event_type,
        record["payload"],
        monotonic_elapsed_ns=monotonic_ns,
    )
    claimed_hash = _sha256(record["record_sha256"], "record_sha256")
    if claimed_hash != _record_digest(record):
        raise _error(
            "SOURCE_MONITORING_SOAK_CHAIN_INVALID",
            "soak evidence record hash is invalid",
        )
    return copy.deepcopy(record)


def _decode_unique_json(raw: bytes, *, line_number: int) -> dict[str, Any]:
    if not raw.endswith(b"\n"):
        raise _error(
            "SOURCE_MONITORING_SOAK_TRUNCATED",
            f"soak evidence line {line_number} is not newline-terminated",
        )
    content = raw[:-1]
    if not content or b"\r" in content:
        raise _error(
            "SOURCE_MONITORING_SOAK_FORMAT_INVALID",
            f"soak evidence line {line_number} is empty or non-canonical",
        )

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        decoded = json.loads(
            content.decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _error(
            "SOURCE_MONITORING_SOAK_FORMAT_INVALID",
            f"soak evidence line {line_number} is not strict JSON",
        ) from exc
    if type(decoded) is not dict:
        raise _error(
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
            f"soak evidence line {line_number} must be an object",
        )
    try:
        canonical = canonical_json(decoded).encode("utf-8") + b"\n"
    except (RecursionError, TypeError, ValueError) as exc:
        raise _error(
            "SOURCE_MONITORING_SOAK_FORMAT_INVALID",
            f"soak evidence line {line_number} is outside canonical JSON",
        ) from exc
    if canonical != raw:
        raise _error(
            "SOURCE_MONITORING_SOAK_FORMAT_INVALID",
            f"soak evidence line {line_number} is not canonical JSONL",
        )
    return decoded


def validate_soak_evidence(
    path: str | Path,
    *,
    on_record: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Stream and validate one immutable point-in-time ledger.

    Records are not retained unless the caller supplies ``on_record``.  A
    structurally valid but interrupted ledger returns ``terminal=False``;
    higher-level acceptance code must never treat that as a completed soak.
    """

    if on_record is not None and not callable(on_record):
        raise _error(
            "SOURCE_MONITORING_SOAK_CONSUMER_INVALID",
            "on_record must be callable",
        )
    ledger_path = _unaliased_existing_path(path)
    before = ledger_path.lstat()
    _require_independent_regular(before, label="soak ledger")
    count = 0
    expected_previous = ZERO_SHA256
    identifiers: tuple[str, str, str] | None = None
    previous_monotonic: int | None = None
    terminal = False
    last_event = ""
    last_hash = ZERO_SHA256
    try:
        with ledger_path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            _require_independent_regular(opened, label="opened soak ledger")
            if (
                not os.path.samestat(before, opened)
                or _stable_file_signature(before) != _stable_file_signature(opened)
            ):
                raise _error(
                    "SOURCE_MONITORING_SOAK_FILE_CHANGED",
                    "soak ledger identity changed while opening",
                )
            while True:
                raw = stream.readline(MAX_SOAK_RECORD_BYTES + 1)
                if raw == b"":
                    break
                if len(raw) > MAX_SOAK_RECORD_BYTES:
                    raise _error(
                        "SOURCE_MONITORING_SOAK_LIMIT_EXCEEDED",
                        "one soak evidence record exceeds 64 KiB",
                    )
                count += 1
                if count > MAX_SOAK_RECORDS:
                    raise _error(
                        "SOURCE_MONITORING_SOAK_LIMIT_EXCEEDED",
                        "soak ledger exceeds its record-count bound",
                    )
                decoded = _decode_unique_json(raw, line_number=count)
                record = _validate_record(
                    decoded,
                    expected_sequence=count,
                    expected_previous_sha256=expected_previous,
                    expected_ids=identifiers,
                    previous_monotonic_ns=previous_monotonic,
                    terminal_seen=terminal,
                )
                if identifiers is None:
                    identifiers = (
                        record["campaign_id"],
                        record["session_id"],
                        record["runtime_id"],
                    )
                previous_monotonic = record["monotonic_elapsed_ns"]
                last_event = record["event_type"]
                terminal = last_event == SOAK_EVENT_SESSION_ENDED
                expected_previous = record["record_sha256"]
                last_hash = expected_previous
                if on_record is not None:
                    on_record(copy.deepcopy(record))
            after_fd = os.fstat(stream.fileno())
        after_path = ledger_path.lstat()
    except SourceMonitoringSoakEvidenceError:
        raise
    except OSError as exc:
        raise _error(
            "SOURCE_MONITORING_SOAK_READ_FAILED",
            "soak ledger could not be read",
        ) from exc
    _require_independent_regular(after_fd, label="read soak ledger")
    _require_independent_regular(after_path, label="soak ledger")
    if (
        not os.path.samestat(before, after_fd)
        or not os.path.samestat(before, after_path)
        or _stable_file_signature(before) != _stable_file_signature(after_fd)
        or _stable_file_signature(before) != _stable_file_signature(after_path)
    ):
        raise _error(
            "SOURCE_MONITORING_SOAK_FILE_CHANGED",
            "soak ledger changed while it was being validated",
        )
    if count == 0 or identifiers is None:
        raise _error(
            "SOURCE_MONITORING_SOAK_FORMAT_INVALID",
            "soak ledger must contain at least SESSION_STARTED",
        )
    return {
        "version": SOURCE_MONITORING_SOAK_STREAM_VERSION,
        "campaign_id": identifiers[0],
        "session_id": identifiers[1],
        "runtime_id": identifiers[2],
        "record_count": count,
        "last_event_type": last_event,
        "last_record_sha256": last_hash,
        "last_monotonic_elapsed_ns": previous_monotonic,
        "terminal": terminal,
        "source_acceptance_verdict": "NOT_EVALUATED",
        "overall_acceptance": "NOT_CLAIMED",
    }


def load_soak_evidence(
    path: str | Path,
    *,
    maximum_records: int = MAX_SOAK_RECORDS,
) -> tuple[dict[str, Any], ...]:
    """Load a bounded ledger after using the same streaming validator."""

    maximum = _positive(maximum_records, "maximum_records")
    if maximum > MAX_SOAK_RECORDS:
        raise _error(
            "SOURCE_MONITORING_SOAK_LIMIT_EXCEEDED",
            "maximum_records exceeds the v1 ledger bound",
        )
    records: list[dict[str, Any]] = []

    def collect(record: dict[str, Any]) -> None:
        if len(records) >= maximum:
            raise _error(
                "SOURCE_MONITORING_SOAK_LIMIT_EXCEEDED",
                "soak ledger exceeds the requested load bound",
            )
        records.append(record)

    validate_soak_evidence(path, on_record=collect)
    return tuple(records)


class SoakEvidenceWriter:
    """Own one new JSONL ledger for exactly one process-local session.

    The constructor never opens an existing path, so neither crash recovery nor
    a second writer can resume a prior ledger.  Any I/O or identity failure
    permanently poisons and closes this writer instance.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        campaign_id: str,
        session_id: str,
        runtime_id: str,
        _fsync: Callable[[int], Any] | None = None,
        _write: Callable[[BinaryIO, memoryview], Any] | None = None,
    ) -> None:
        if _fsync is not None and not callable(_fsync):
            raise _error(
                "SOURCE_MONITORING_SOAK_IO_HOOK_INVALID",
                "_fsync must be callable",
            )
        if _write is not None and not callable(_write):
            raise _error(
                "SOURCE_MONITORING_SOAK_IO_HOOK_INVALID",
                "_write must be callable",
            )
        identifiers = (campaign_id, session_id, runtime_id)
        if (
            type(campaign_id) is not str
            or _CAMPAIGN_ID_RE.fullmatch(campaign_id) is None
            or type(session_id) is not str
            or _SESSION_ID_RE.fullmatch(session_id) is None
            or type(runtime_id) is not str
            or _RUNTIME_ID_RE.fullmatch(runtime_id) is None
        ):
            raise _error(
                "SOURCE_MONITORING_SOAK_IDENTITY_INVALID",
                "soak writer identifiers are invalid",
            )
        ledger_path = _unaliased_new_path(path)
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_APPEND
        flags |= int(getattr(os, "O_BINARY", 0) or 0)
        flags |= int(getattr(os, "O_NOINHERIT", 0) or 0)
        try:
            descriptor = os.open(ledger_path, flags, 0o600)
        except FileExistsError as exc:
            raise _error(
                "SOURCE_MONITORING_SOAK_LEDGER_EXISTS",
                "soak ledger already exists and cannot be resumed",
            ) from exc
        except OSError as exc:
            raise _error(
                "SOURCE_MONITORING_SOAK_WRITE_FAILED",
                "soak ledger could not be created",
            ) from exc
        try:
            stream = os.fdopen(descriptor, "a+b", buffering=0)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            _lock_stream_exclusive(stream)
            opened = os.fstat(stream.fileno())
            path_metadata = ledger_path.lstat()
            _require_independent_regular(opened, label="opened soak ledger")
            _require_independent_regular(path_metadata, label="soak ledger")
            if (
                first_reparse_component(ledger_path) is not None
                or not os.path.samestat(opened, path_metadata)
                or opened.st_size != 0
                or _stable_file_signature(opened)
                != _stable_file_signature(path_metadata)
            ):
                raise _error(
                    "SOURCE_MONITORING_SOAK_FILE_CHANGED",
                    "new soak ledger identity or initial length is invalid",
                )
            _fsync_parent_directory(ledger_path)
        except BaseException as exc:
            stream.close()
            if isinstance(exc, SourceMonitoringSoakEvidenceError) or not isinstance(
                exc,
                Exception,
            ):
                raise
            raise _error(
                "SOURCE_MONITORING_SOAK_WRITE_FAILED",
                "soak ledger could not acquire exclusive ownership",
            ) from exc

        self.path = ledger_path
        self.campaign_id, self.session_id, self.runtime_id = identifiers
        self._stream = stream
        self._identity = opened
        self._expected_signature = _stable_file_signature(opened)
        self._expected_length = 0
        self._sequence = 0
        self._previous_sha256 = ZERO_SHA256
        self._previous_monotonic_ns: int | None = None
        self._terminal = False
        self._closed = False
        self._failed = False
        self._exclusive_lock_held = True
        self._lock = threading.RLock()
        self._fsync = _fsync or os.fsync
        self._write = _write or (lambda target, data: target.write(data))

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def terminal(self) -> bool:
        return self._terminal

    @property
    def record_count(self) -> int:
        return self._sequence

    @property
    def last_record_sha256(self) -> str:
        return self._previous_sha256

    def _assert_open_identity(self) -> None:
        if self._closed:
            raise _error(
                "SOURCE_MONITORING_SOAK_WRITER_CLOSED",
                "soak evidence writer is closed",
            )
        try:
            descriptor_metadata = os.fstat(self._stream.fileno())
            path_metadata = self.path.lstat()
        except OSError as exc:
            raise _error(
                "SOURCE_MONITORING_SOAK_FILE_CHANGED",
                "soak ledger identity could not be revalidated",
            ) from exc
        _require_independent_regular(
            descriptor_metadata,
            label="opened soak ledger",
        )
        _require_independent_regular(path_metadata, label="soak ledger")
        if (
            not os.path.samestat(self._identity, descriptor_metadata)
            or not os.path.samestat(self._identity, path_metadata)
            or descriptor_metadata.st_size != self._expected_length
            or path_metadata.st_size != self._expected_length
            or _stable_file_signature(descriptor_metadata)
            != self._expected_signature
            or _stable_file_signature(path_metadata) != self._expected_signature
        ):
            raise _error(
                "SOURCE_MONITORING_SOAK_FILE_CHANGED",
                "soak ledger identity or length changed outside its writer",
            )

    def _poison(self) -> None:
        self._failed = True
        self._closed = True
        try:
            if self._exclusive_lock_held and not self._stream.closed:
                _unlock_stream(self._stream)
                self._exclusive_lock_held = False
        except BaseException:
            pass
        try:
            self._stream.close()
        except BaseException:
            pass

    def append(
        self,
        event_type: str,
        *,
        wall_time_ms: Any,
        monotonic_elapsed_ns: Any,
        payload: Any,
    ) -> dict[str, Any]:
        """Append, flush, and fsync one canonical record."""

        with self._lock:
            if self._closed:
                raise _error(
                    "SOURCE_MONITORING_SOAK_WRITER_CLOSED",
                    "soak evidence writer is closed",
                )
            if self._terminal:
                raise _error(
                    "SOURCE_MONITORING_SOAK_WRITER_TERMINAL",
                    "soak evidence writer is already terminal",
                )
            try:
                self._assert_open_identity()
                sequence = self._sequence + 1
                monotonic_ns = _native_non_negative(
                    monotonic_elapsed_ns,
                    "monotonic_elapsed_ns",
                )
                record: dict[str, Any] = {
                    "version": SOURCE_MONITORING_SOAK_RECORD_VERSION,
                    "campaign_id": self.campaign_id,
                    "session_id": self.session_id,
                    "runtime_id": self.runtime_id,
                    "sequence_no": sequence,
                    "event_type": event_type,
                    "wall_time_ms": wall_time_ms,
                    "monotonic_elapsed_ns": monotonic_ns,
                    "previous_record_sha256": self._previous_sha256,
                    "payload": payload,
                    "record_sha256": ZERO_SHA256,
                }
                record["record_sha256"] = _record_digest(record)
                verified = _validate_record(
                    record,
                    expected_sequence=sequence,
                    expected_previous_sha256=self._previous_sha256,
                    expected_ids=(
                        self.campaign_id,
                        self.session_id,
                        self.runtime_id,
                    ),
                    previous_monotonic_ns=self._previous_monotonic_ns,
                    terminal_seen=False,
                )
                encoded = canonical_json(verified).encode("utf-8") + b"\n"
                if len(encoded) > MAX_SOAK_RECORD_BYTES:
                    raise _error(
                        "SOURCE_MONITORING_SOAK_LIMIT_EXCEEDED",
                        "one soak evidence record exceeds 64 KiB",
                    )
                if self._expected_length + len(encoded) > MAX_SOAK_LEDGER_BYTES:
                    raise _error(
                        "SOURCE_MONITORING_SOAK_LIMIT_EXCEEDED",
                        "soak ledger exceeds the bounded file size",
                    )
                if sequence > MAX_SOAK_RECORDS:
                    raise _error(
                        "SOURCE_MONITORING_SOAK_LIMIT_EXCEEDED",
                        "soak ledger exceeds its record-count bound",
                    )

                remaining = memoryview(encoded)
                while remaining:
                    written = self._write(self._stream, remaining)
                    if type(written) is not int or written <= 0 or written > len(remaining):
                        raise OSError("soak ledger write did not make bounded progress")
                    remaining = remaining[written:]
                self._stream.flush()
                self._fsync(self._stream.fileno())
                expected_length = self._expected_length + len(encoded)
                descriptor_metadata = os.fstat(self._stream.fileno())
                path_metadata = self.path.lstat()
                if (
                    not os.path.samestat(self._identity, descriptor_metadata)
                    or not os.path.samestat(self._identity, path_metadata)
                    or descriptor_metadata.st_size != expected_length
                    or path_metadata.st_size != expected_length
                    or _stable_file_signature(descriptor_metadata)
                    != _stable_file_signature(path_metadata)
                ):
                    raise _error(
                        "SOURCE_MONITORING_SOAK_FILE_CHANGED",
                        "soak ledger identity or length changed during append",
                    )
            except SourceMonitoringSoakEvidenceError:
                self._poison()
                raise
            except BaseException as exc:
                self._poison()
                if not isinstance(exc, Exception):
                    raise
                raise _error(
                    "SOURCE_MONITORING_SOAK_WRITE_FAILED",
                    "soak evidence append, flush, or fsync failed",
                ) from exc

            self._expected_length = expected_length
            self._expected_signature = _stable_file_signature(descriptor_metadata)
            self._sequence = sequence
            self._previous_sha256 = verified["record_sha256"]
            self._previous_monotonic_ns = monotonic_ns
            if event_type == SOAK_EVENT_SESSION_ENDED:
                self._terminal = True
            return copy.deepcopy(verified)

    def close(self) -> None:
        """Close permanently; an existing ledger can never be resumed."""

        with self._lock:
            if self._closed:
                return
            try:
                self._assert_open_identity()
                self._stream.flush()
                self._fsync(self._stream.fileno())
            except BaseException as exc:
                self._poison()
                if isinstance(exc, SourceMonitoringSoakEvidenceError):
                    raise
                raise _error(
                    "SOURCE_MONITORING_SOAK_WRITE_FAILED",
                    "soak evidence close flush or fsync failed",
                ) from exc
            try:
                _unlock_stream(self._stream)
                self._exclusive_lock_held = False
            except BaseException as exc:
                self._poison()
                if not isinstance(exc, Exception):
                    raise
                raise _error(
                    "SOURCE_MONITORING_SOAK_WRITE_FAILED",
                    "soak evidence writer lock release failed",
                ) from exc
            self._stream.close()
            self._closed = True

    def __enter__(self) -> "SoakEvidenceWriter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


__all__ = [
    "MAX_SOAK_LEDGER_BYTES",
    "MAX_SOAK_RECORD_BYTES",
    "MAX_SOAK_RECORDS",
    "SOAK_EVENT_RUN_TERMINAL",
    "SOAK_EVENT_RUNTIME_SAMPLE",
    "SOAK_EVENT_SESSION_ENDED",
    "SOAK_EVENT_SESSION_STARTED",
    "SOAK_EVENT_TYPES",
    "SOURCE_MONITORING_SOAK_RECORD_VERSION",
    "SOURCE_MONITORING_SOAK_STREAM_VERSION",
    "SourceMonitoringSoakEvidenceError",
    "SoakEvidenceWriter",
    "ZERO_SHA256",
    "load_soak_evidence",
    "validate_soak_evidence",
]
