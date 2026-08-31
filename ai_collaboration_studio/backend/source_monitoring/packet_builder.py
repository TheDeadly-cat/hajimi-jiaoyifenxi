"""Build closed-channel packets for the existing Source Inbox contract."""

from __future__ import annotations

import copy
import unicodedata
from datetime import datetime, timezone
from typing import Any

from ..source_inbox_contracts import (
    MAX_SOURCE_IMPORT_BYTES,
    MAX_SOURCE_ITEMS,
    PROJECT_SOURCE_ITEM_VERSION,
    SOURCE_IMPORT_PACKET_VERSION,
    SourceInboxContractError,
    accept_source_import,
)
from .contracts import (
    FUTU_ANOMALY_SOURCE_CHANNEL,
    OFFICIAL_SOURCE_CHANNEL,
    SOURCE_MONITORING_SOURCE_CHANNELS,
    AdapterPollResult,
    SourceMonitoringContractError,
    canonical_json,
    normalize_adapter_key,
)


MAX_PACKET_TIMESTAMP_MS = 253_402_300_799_999


class SourcePacketBuildError(SourceMonitoringContractError):
    """Raised when fixed packet construction cannot satisfy the inbox contract."""


def _bounded_text(value: Any, *, field: str, maximum: int) -> str:
    if type(value) is not str:
        raise SourcePacketBuildError(
            "SOURCE_MONITORING_PACKET_TEXT_INVALID",
            f"{field} must be a native string",
        )
    clean = unicodedata.normalize("NFC", value).strip()
    if (
        not clean
        or len(clean) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in clean)
    ):
        raise SourcePacketBuildError(
            "SOURCE_MONITORING_PACKET_TEXT_INVALID",
            f"{field} is empty, oversized, or contains a control character",
        )
    try:
        clean.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SourcePacketBuildError(
            "SOURCE_MONITORING_PACKET_TEXT_INVALID",
            f"{field} is not valid UTF-8 text",
        ) from exc
    return clean


def _native_timestamp_ms(value: Any, *, field: str) -> int:
    if (
        type(value) is not int
        or value < 0
        or value > MAX_PACKET_TIMESTAMP_MS
    ):
        raise SourcePacketBuildError(
            "SOURCE_MONITORING_PACKET_TIME_INVALID",
            f"{field} must be a non-negative native millisecond timestamp",
        )
    return value


def _rfc3339_from_ms(value: int) -> str:
    seconds, milliseconds = divmod(value, 1_000)
    moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
    base = moment.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{milliseconds:03d}Z" if milliseconds else f"{base}Z"


def _max_items(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= MAX_SOURCE_ITEMS:
        raise SourcePacketBuildError(
            "SOURCE_MONITORING_PACKET_ITEM_LIMIT_INVALID",
            "max_items must be a native integer between 1 and 50",
        )
    return value


def normalize_source_channel(value: Any) -> str:
    if type(value) is not str or value not in SOURCE_MONITORING_SOURCE_CHANNELS:
        raise SourcePacketBuildError(
            "SOURCE_MONITORING_PACKET_CHANNEL_INVALID",
            "source_channel is not in the closed monitoring channel set",
        )
    return value


def _observed_items(value: Any, *, maximum: int) -> list[dict[str, Any]]:
    if type(value) not in {list, tuple}:
        raise SourcePacketBuildError(
            "SOURCE_MONITORING_PACKET_ITEMS_INVALID",
            "observed_items must be a native list or tuple",
        )
    if len(value) > maximum:
        raise SourcePacketBuildError(
            "SOURCE_MONITORING_PACKET_ITEMS_TOO_MANY",
            f"observed_items exceeds the configured limit of {maximum}",
        )
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if type(item) is not dict:
            raise SourcePacketBuildError(
                "SOURCE_MONITORING_PACKET_ITEM_INVALID",
                f"observed_items[{index}] must be a native JSON object",
            )
        if item.get("version") != PROJECT_SOURCE_ITEM_VERSION:
            raise SourcePacketBuildError(
                "SOURCE_MONITORING_PACKET_ITEM_INVALID",
                f"observed_items[{index}] has an unsupported version",
            )
        result.append(copy.deepcopy(item))
    return result


def build_source_import_packet(
    *,
    adapter_key: Any,
    external_run_id: Any,
    captured_at_ms: Any,
    observed_items: Any,
    source_channel: Any = OFFICIAL_SOURCE_CHANNEL,
    cutoff_at_ms: Any | None = None,
    max_items: Any = MAX_SOURCE_ITEMS,
) -> dict[str, Any]:
    """Build and fully validate one raw import packet on a closed channel."""

    clean_adapter_key = normalize_adapter_key(adapter_key)
    clean_source_channel = normalize_source_channel(source_channel)
    clean_run_id = _bounded_text(
        external_run_id,
        field="external_run_id",
        maximum=200,
    )
    checked_ms = _native_timestamp_ms(captured_at_ms, field="captured_at_ms")
    cutoff_ms = (
        checked_ms
        if cutoff_at_ms is None
        else _native_timestamp_ms(cutoff_at_ms, field="cutoff_at_ms")
    )
    if cutoff_ms > checked_ms:
        raise SourcePacketBuildError(
            "SOURCE_MONITORING_PACKET_TIME_ORDER_INVALID",
            "cutoff_at_ms must not be later than captured_at_ms",
        )
    item_limit = _max_items(max_items)
    items = _observed_items(observed_items, maximum=item_limit)
    packet: dict[str, Any] = {
        "version": SOURCE_IMPORT_PACKET_VERSION,
        "source_channel": clean_source_channel,
        "source_key": clean_adapter_key,
        "external_run_id": clean_run_id,
        "checked_at": _rfc3339_from_ms(checked_ms),
        "cutoff_at": _rfc3339_from_ms(cutoff_ms),
        "meaningful_change": bool(items),
        "items": items,
        "generation": {
            "channel": clean_source_channel,
            "model": "",
            "cost": {
                "status": "unavailable",
                "amount": None,
                "currency": "",
                "usage_source": "not_applicable",
            },
            "correlated_output": False,
        },
    }
    canonical_source_import_payload(packet, received_at_ms=checked_ms)
    return copy.deepcopy(packet)


def canonical_source_import_payload(
    packet: Any,
    *,
    received_at_ms: Any,
) -> str:
    """Serialize and validate through the exact parser used by Source Inbox."""

    if type(packet) is not dict:
        raise SourcePacketBuildError(
            "SOURCE_MONITORING_PACKET_INVALID",
            "packet must be a native JSON object",
        )
    receipt_time = _native_timestamp_ms(
        received_at_ms,
        field="received_at_ms",
    )
    try:
        serialized = canonical_json(packet)
    except SourceMonitoringContractError as exc:
        raise SourcePacketBuildError(
            "SOURCE_MONITORING_PACKET_INVALID",
            str(exc),
        ) from exc
    if len(serialized.encode("utf-8")) > MAX_SOURCE_IMPORT_BYTES:
        raise SourcePacketBuildError(
            "SOURCE_MONITORING_PACKET_TOO_LARGE",
            f"packet exceeds {MAX_SOURCE_IMPORT_BYTES} UTF-8 bytes",
        )
    try:
        accept_source_import(serialized, received_at_ms=receipt_time)
    except SourceInboxContractError as exc:
        raise SourcePacketBuildError(
            "SOURCE_MONITORING_PACKET_INVALID",
            str(exc),
        ) from exc
    return serialized


def build_source_import_payload(
    *,
    adapter_key: Any,
    external_run_id: Any,
    captured_at_ms: Any,
    observed_items: Any,
    source_channel: Any = OFFICIAL_SOURCE_CHANNEL,
    cutoff_at_ms: Any | None = None,
    max_items: Any = MAX_SOURCE_ITEMS,
    received_at_ms: Any | None = None,
) -> str:
    packet = build_source_import_packet(
        adapter_key=adapter_key,
        external_run_id=external_run_id,
        captured_at_ms=captured_at_ms,
        observed_items=observed_items,
        source_channel=source_channel,
        cutoff_at_ms=cutoff_at_ms,
        max_items=max_items,
    )
    return canonical_source_import_payload(
        packet,
        received_at_ms=(
            captured_at_ms if received_at_ms is None else received_at_ms
        ),
    )


def build_packet_from_poll_result(
    result: Any,
    *,
    external_run_id: Any,
    cutoff_at_ms: Any | None = None,
    max_items: Any = MAX_SOURCE_ITEMS,
    source_channel: Any = OFFICIAL_SOURCE_CHANNEL,
) -> dict[str, Any]:
    if type(result) is not AdapterPollResult:
        raise SourcePacketBuildError(
            "SOURCE_MONITORING_POLL_RESULT_INVALID",
            "result must be AdapterPollResult",
        )
    return build_source_import_packet(
        adapter_key=result.adapter_key,
        external_run_id=external_run_id,
        captured_at_ms=result.captured_at_ms,
        observed_items=result.observed_items,
        source_channel=source_channel,
        cutoff_at_ms=cutoff_at_ms,
        max_items=max_items,
    )


def build_payload_from_poll_result(
    result: Any,
    *,
    external_run_id: Any,
    cutoff_at_ms: Any | None = None,
    max_items: Any = MAX_SOURCE_ITEMS,
    received_at_ms: Any | None = None,
    source_channel: Any = OFFICIAL_SOURCE_CHANNEL,
) -> str:
    packet = build_packet_from_poll_result(
        result,
        external_run_id=external_run_id,
        cutoff_at_ms=cutoff_at_ms,
        max_items=max_items,
        source_channel=source_channel,
    )
    return canonical_source_import_payload(
        packet,
        received_at_ms=(
            result.captured_at_ms if received_at_ms is None else received_at_ms
        ),
    )


__all__ = [
    "MAX_PACKET_TIMESTAMP_MS",
    "FUTU_ANOMALY_SOURCE_CHANNEL",
    "OFFICIAL_SOURCE_CHANNEL",
    "SourcePacketBuildError",
    "build_payload_from_poll_result",
    "build_packet_from_poll_result",
    "build_source_import_payload",
    "build_source_import_packet",
    "canonical_source_import_payload",
    "normalize_source_channel",
]
