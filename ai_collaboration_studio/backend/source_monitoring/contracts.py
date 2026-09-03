"""Closed, deterministic contracts for official source polling.

The objects in this module are pure value contracts.  They perform no I/O and
accept only exact Python representations of standard JSON values.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any


MAX_ADAPTER_KEY_CHARS = 64
MAX_CHECKPOINT_BYTES = 64 * 1024
MAX_OBSERVED_ITEMS_PER_POLL = 50
MAX_SOURCE_ERRORS_PER_POLL = 50
MAX_ERROR_CODE_CHARS = 80
MAX_ERROR_MESSAGE_CHARS = 1_000
MAX_ERROR_SCOPE_CHARS = 160
MAX_ETAG_CHARS = 1_024
MAX_LAST_MODIFIED_CHARS = 256
MAX_JSON_DEPTH = 32
MAX_NATIVE_INTEGER = (1 << 63) - 1
MAX_MARKET_CALLS_PER_POLL = 50

OFFICIAL_SOURCE_CLASS = "official_source"
READONLY_MARKET_SOURCE_CLASS = "readonly_market"
OFFICIAL_SOURCE_CHANNEL = "official_source_monitor"
FUTU_ANOMALY_SOURCE_CHANNEL = "futu_anomaly_monitor"
SOURCE_MONITORING_SOURCE_CLASSES = frozenset({
    OFFICIAL_SOURCE_CLASS,
    READONLY_MARKET_SOURCE_CLASS,
})
SOURCE_MONITORING_SOURCE_CHANNELS = frozenset({
    OFFICIAL_SOURCE_CHANNEL,
    FUTU_ANOMALY_SOURCE_CHANNEL,
})

_ADAPTER_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")


class SourceMonitoringContractError(ValueError):
    """A bounded, machine-readable source-monitoring contract failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _contract_error(code: str, message: str) -> SourceMonitoringContractError:
    return SourceMonitoringContractError(code, message)


def _bounded_text(
    value: Any,
    *,
    field: str,
    maximum: int,
    allow_empty: bool = False,
    allow_multiline: bool = False,
) -> str:
    if type(value) is not str:
        raise _contract_error(
            "SOURCE_MONITORING_TEXT_INVALID",
            f"{field} must be a native string",
        )
    clean = unicodedata.normalize("NFC", value).replace("\r\n", "\n").strip()
    allowed_controls = {"\n", "\t"} if allow_multiline else set()
    if any(
        (ord(character) < 32 and character not in allowed_controls)
        or ord(character) == 127
        for character in clean
    ):
        raise _contract_error(
            "SOURCE_MONITORING_TEXT_INVALID",
            f"{field} contains a forbidden control character",
        )
    try:
        clean.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _contract_error(
            "SOURCE_MONITORING_TEXT_INVALID",
            f"{field} is not valid UTF-8 text",
        ) from exc
    if not clean and not allow_empty:
        raise _contract_error(
            "SOURCE_MONITORING_TEXT_INVALID",
            f"{field} must not be empty",
        )
    if len(clean) > maximum:
        raise _contract_error(
            "SOURCE_MONITORING_TEXT_TOO_LONG",
            f"{field} exceeds {maximum} characters",
        )
    return clean


def normalize_adapter_key(value: Any) -> str:
    """Return one lowercase adapter key or fail without coercion."""

    clean = _bounded_text(
        value,
        field="adapter_key",
        maximum=MAX_ADAPTER_KEY_CHARS,
    )
    if not _ADAPTER_KEY_RE.fullmatch(clean):
        raise _contract_error(
            "SOURCE_MONITORING_ADAPTER_KEY_INVALID",
            "adapter_key must match [a-z][a-z0-9_]{0,63}",
        )
    return clean


def _json_path_child(path: str, key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _normalize_json_value(
    value: Any,
    *,
    path: str,
    depth: int,
    active_containers: set[int],
) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise _contract_error(
            "SOURCE_MONITORING_JSON_TOO_DEEP",
            f"{path} exceeds the maximum JSON depth",
        )
    if value is None or type(value) in {bool, int, str}:
        if type(value) is str:
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise _contract_error(
                    "SOURCE_MONITORING_JSON_TEXT_INVALID",
                    f"{path} is not valid UTF-8 text",
                ) from exc
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _contract_error(
                "SOURCE_MONITORING_JSON_NONFINITE_NUMBER",
                f"{path} must be a finite JSON number",
            )
        return value
    if type(value) not in {dict, list}:
        raise _contract_error(
            "SOURCE_MONITORING_JSON_TYPE_INVALID",
            f"{path} contains a non-native JSON value",
        )

    identity = id(value)
    if identity in active_containers:
        raise _contract_error(
            "SOURCE_MONITORING_JSON_CYCLE",
            f"{path} contains a JSON cycle",
        )
    active_containers.add(identity)
    try:
        if type(value) is list:
            return [
                _normalize_json_value(
                    item,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    active_containers=active_containers,
                )
                for index, item in enumerate(value)
            ]

        for key in value:
            if type(key) is not str:
                raise _contract_error(
                    "SOURCE_MONITORING_JSON_KEY_INVALID",
                    f"{path} contains a non-native string key",
                )
            try:
                key.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise _contract_error(
                    "SOURCE_MONITORING_JSON_KEY_INVALID",
                    f"{path} contains an invalid UTF-8 key",
                ) from exc
        return {
            key: _normalize_json_value(
                value[key],
                path=_json_path_child(path, key),
                depth=depth + 1,
                active_containers=active_containers,
            )
            for key in sorted(value)
        }
    finally:
        active_containers.remove(identity)


def _normalized_json(value: Any) -> Any:
    return _normalize_json_value(
        value,
        path="$",
        depth=0,
        active_containers=set(),
    )


def canonical_json(value: Any) -> str:
    """Serialize a closed native JSON value deterministically."""

    normalized = _normalized_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    """Hash the UTF-8 bytes of :func:`canonical_json`."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_checkpoint(value: Any) -> dict[str, Any]:
    """Return a defensive, closed checkpoint within the 64 KiB envelope."""

    if type(value) is not dict:
        raise _contract_error(
            "SOURCE_MONITORING_CHECKPOINT_INVALID",
            "checkpoint must be a native JSON object",
        )
    normalized = _normalized_json(value)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_CHECKPOINT_BYTES:
        raise _contract_error(
            "SOURCE_MONITORING_CHECKPOINT_TOO_LARGE",
            f"checkpoint exceeds {MAX_CHECKPOINT_BYTES} UTF-8 bytes",
        )
    return copy.deepcopy(normalized)


def _native_non_negative_integer(value: Any, *, field: str) -> int:
    if (
        type(value) is not int
        or value < 0
        or value > MAX_NATIVE_INTEGER
    ):
        raise _contract_error(
            "SOURCE_MONITORING_INTEGER_INVALID",
            f"{field} must be a non-negative native 64-bit integer",
        )
    return value


def _normalize_error_code(value: Any) -> str:
    clean = _bounded_text(
        value,
        field="source_error.code",
        maximum=MAX_ERROR_CODE_CHARS,
    ).upper()
    if not _ERROR_CODE_RE.fullmatch(clean):
        raise _contract_error(
            "SOURCE_MONITORING_ERROR_CODE_INVALID",
            "source_error.code must match [A-Z][A-Z0-9_]{0,79}",
        )
    return clean


@dataclass(frozen=True, slots=True)
class SourcePollError:
    """One bounded failure reported by an adapter poll."""

    code: str
    message: str
    scope: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _normalize_error_code(self.code))
        object.__setattr__(
            self,
            "message",
            _bounded_text(
                self.message,
                field="source_error.message",
                maximum=MAX_ERROR_MESSAGE_CHARS,
                allow_multiline=True,
            ),
        )
        object.__setattr__(
            self,
            "scope",
            _bounded_text(
                self.scope,
                field="source_error.scope",
                maximum=MAX_ERROR_SCOPE_CHARS,
                allow_empty=True,
            ),
        )

    @classmethod
    def build(
        cls,
        code: Any,
        message: Any,
        scope: Any = "",
    ) -> "SourcePollError":
        return cls(code=code, message=message, scope=scope)

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "message": self.message,
            "scope": self.scope,
        }


def _normalize_observed_items(value: Any) -> tuple[dict[str, Any], ...]:
    if type(value) not in {list, tuple}:
        raise _contract_error(
            "SOURCE_MONITORING_ITEMS_INVALID",
            "observed_items must be a native list or tuple",
        )
    if len(value) > MAX_OBSERVED_ITEMS_PER_POLL:
        raise _contract_error(
            "SOURCE_MONITORING_ITEMS_TOO_MANY",
            f"observed_items exceeds {MAX_OBSERVED_ITEMS_PER_POLL} items",
        )
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if type(item) is not dict:
            raise _contract_error(
                "SOURCE_MONITORING_ITEM_INVALID",
                f"observed_items[{index}] must be a native JSON object",
            )
        clean = _normalized_json(item)
        normalized.append(copy.deepcopy(clean))
    return tuple(normalized)


def _normalize_source_errors(value: Any) -> tuple[SourcePollError, ...]:
    if type(value) not in {list, tuple}:
        raise _contract_error(
            "SOURCE_MONITORING_ERRORS_INVALID",
            "source_errors must be a native list or tuple",
        )
    if len(value) > MAX_SOURCE_ERRORS_PER_POLL:
        raise _contract_error(
            "SOURCE_MONITORING_ERRORS_TOO_MANY",
            f"source_errors exceeds {MAX_SOURCE_ERRORS_PER_POLL} errors",
        )
    normalized: list[SourcePollError] = []
    for index, item in enumerate(value):
        if type(item) is not SourcePollError:
            raise _contract_error(
                "SOURCE_MONITORING_ERROR_INVALID",
                f"source_errors[{index}] must be SourcePollError",
            )
        normalized.append(
            SourcePollError.build(item.code, item.message, item.scope)
        )
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class AdapterPollResult:
    """A closed, defensive snapshot of one adapter poll result."""

    adapter_key: str
    started_checkpoint: dict[str, Any]
    next_checkpoint: dict[str, Any]
    observed_items: tuple[dict[str, Any], ...]
    source_errors: tuple[SourcePollError, ...]
    retry_after_ms: int
    captured_at_ms: int
    etag: str
    last_modified: str
    duplicate_count: int
    rejected_count: int
    market_calls_performed: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "adapter_key",
            normalize_adapter_key(self.adapter_key),
        )
        object.__setattr__(
            self,
            "started_checkpoint",
            normalize_checkpoint(self.started_checkpoint),
        )
        object.__setattr__(
            self,
            "next_checkpoint",
            normalize_checkpoint(self.next_checkpoint),
        )
        object.__setattr__(
            self,
            "observed_items",
            _normalize_observed_items(self.observed_items),
        )
        object.__setattr__(
            self,
            "source_errors",
            _normalize_source_errors(self.source_errors),
        )
        for field_name in (
            "retry_after_ms",
            "captured_at_ms",
            "duplicate_count",
            "rejected_count",
            "market_calls_performed",
        ):
            object.__setattr__(
                self,
                field_name,
                _native_non_negative_integer(
                    getattr(self, field_name),
                    field=field_name,
                ),
            )
        object.__setattr__(
            self,
            "etag",
            _bounded_text(
                self.etag,
                field="etag",
                maximum=MAX_ETAG_CHARS,
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "last_modified",
            _bounded_text(
                self.last_modified,
                field="last_modified",
                maximum=MAX_LAST_MODIFIED_CHARS,
                allow_empty=True,
            ),
        )

    @classmethod
    def build(
        cls,
        adapter_key: Any,
        started_checkpoint: Any,
        next_checkpoint: Any,
        observed_items: Any,
        source_errors: Any = (),
        retry_after_ms: Any = 0,
        *,
        captured_at_ms: Any,
        etag: Any = "",
        last_modified: Any = "",
        duplicate_count: Any = 0,
        rejected_count: Any = 0,
        market_calls_performed: Any = 0,
    ) -> "AdapterPollResult":
        return cls(
            adapter_key=adapter_key,
            started_checkpoint=started_checkpoint,
            next_checkpoint=next_checkpoint,
            observed_items=observed_items,
            source_errors=source_errors,
            retry_after_ms=retry_after_ms,
            captured_at_ms=captured_at_ms,
            etag=etag,
            last_modified=last_modified,
            duplicate_count=duplicate_count,
            rejected_count=rejected_count,
            market_calls_performed=market_calls_performed,
        )

    @property
    def observed_count(self) -> int:
        return len(self.observed_items) + self.duplicate_count + self.rejected_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_key": self.adapter_key,
            "started_checkpoint": copy.deepcopy(self.started_checkpoint),
            "next_checkpoint": copy.deepcopy(self.next_checkpoint),
            "observed_items": copy.deepcopy(list(self.observed_items)),
            "source_errors": [error.to_dict() for error in self.source_errors],
            "observed_count": self.observed_count,
            "retry_after_ms": self.retry_after_ms,
            "captured_at_ms": self.captured_at_ms,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "duplicate_count": self.duplicate_count,
            "rejected_count": self.rejected_count,
            "market_calls_performed": self.market_calls_performed,
        }


__all__ = [
    "MAX_ADAPTER_KEY_CHARS",
    "MAX_CHECKPOINT_BYTES",
    "MAX_ERROR_CODE_CHARS",
    "MAX_ERROR_MESSAGE_CHARS",
    "MAX_ERROR_SCOPE_CHARS",
    "MAX_ETAG_CHARS",
    "MAX_JSON_DEPTH",
    "MAX_LAST_MODIFIED_CHARS",
    "MAX_MARKET_CALLS_PER_POLL",
    "MAX_NATIVE_INTEGER",
    "MAX_OBSERVED_ITEMS_PER_POLL",
    "MAX_SOURCE_ERRORS_PER_POLL",
    "OFFICIAL_SOURCE_CHANNEL",
    "OFFICIAL_SOURCE_CLASS",
    "FUTU_ANOMALY_SOURCE_CHANNEL",
    "READONLY_MARKET_SOURCE_CLASS",
    "SOURCE_MONITORING_SOURCE_CHANNELS",
    "SOURCE_MONITORING_SOURCE_CLASSES",
    "AdapterPollResult",
    "SourceMonitoringContractError",
    "SourcePollError",
    "canonical_json",
    "canonical_sha256",
    "normalize_adapter_key",
    "normalize_checkpoint",
]
