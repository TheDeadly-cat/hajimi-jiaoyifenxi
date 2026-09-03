"""Side-effect-free adapter protocol and safety metadata validation."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import re
from typing import Any, Protocol, runtime_checkable

from ..contracts import (
    FUTU_ANOMALY_SOURCE_CHANNEL,
    MAX_ETAG_CHARS,
    MAX_LAST_MODIFIED_CHARS,
    MAX_MARKET_CALLS_PER_POLL,
    MAX_OBSERVED_ITEMS_PER_POLL,
    OFFICIAL_SOURCE_CHANNEL,
    OFFICIAL_SOURCE_CLASS,
    READONLY_MARKET_SOURCE_CLASS,
    SOURCE_MONITORING_SOURCE_CHANNELS,
    SOURCE_MONITORING_SOURCE_CLASSES,
    AdapterPollResult,
    SourceMonitoringContractError,
    normalize_adapter_key,
)


SOURCE_ADAPTER_CONTRACT_VERSION = "source_adapter_v1"
MIN_POLL_INTERVAL_MS = 60_000
MAX_POLL_INTERVAL_MS = 7 * 24 * 60 * 60 * 1_000

_CONFIG_VERSION_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")


class SourceAdapterContractError(SourceMonitoringContractError):
    """Raised when an adapter does not satisfy the closed local protocol."""


def _exact_poll_header(value: Any, *, field: str, maximum: int) -> str:
    if type(value) is not str:
        raise SourceAdapterContractError(
            "SOURCE_ADAPTER_POLL_CONTEXT_INVALID",
            f"{field} must be a native string",
        )
    if value != value.strip() or len(value) > maximum or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise SourceAdapterContractError(
            "SOURCE_ADAPTER_POLL_CONTEXT_INVALID",
            f"{field} must be canonical bounded single-line text",
        )
    return value


def validate_poll_context(
    *,
    etag: Any,
    last_modified: Any,
    max_items: Any,
) -> tuple[str, str, int]:
    """Validate one supervisor poll context without coercing identities."""

    clean_etag = _exact_poll_header(
        etag,
        field="etag",
        maximum=MAX_ETAG_CHARS,
    )
    clean_last_modified = _exact_poll_header(
        last_modified,
        field="last_modified",
        maximum=MAX_LAST_MODIFIED_CHARS,
    )
    if (
        type(max_items) is not int
        or not 1 <= max_items <= MAX_OBSERVED_ITEMS_PER_POLL
    ):
        raise SourceAdapterContractError(
            "SOURCE_ADAPTER_POLL_CONTEXT_INVALID",
            "max_items must be a native integer between 1 and 50",
        )
    return clean_etag, clean_last_modified, max_items


@dataclass(frozen=True, slots=True)
class SourceAdapterMetadata:
    contract_version: str
    adapter_key: str
    config_version: str
    poll_interval_ms: int
    max_candidates_per_poll: int
    official_source: bool
    source_class: str
    source_channel: str
    max_market_calls_per_poll: int
    execution_capability: str
    live_trading_allowed: bool

    def __post_init__(self) -> None:
        if (
            type(self.contract_version) is not str
            or self.contract_version != SOURCE_ADAPTER_CONTRACT_VERSION
        ):
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_CONTRACT_VERSION_INVALID",
                "adapter contract_version is unsupported",
            )
        clean_key = normalize_adapter_key(self.adapter_key)
        if clean_key != self.adapter_key:
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_KEY_NONCANONICAL",
                "adapter_key must already be canonical",
            )
        if (
            type(self.config_version) is not str
            or not _CONFIG_VERSION_RE.fullmatch(self.config_version)
        ):
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_CONFIG_VERSION_INVALID",
                "config_version must be a canonical lowercase version token",
            )
        if (
            type(self.poll_interval_ms) is not int
            or not MIN_POLL_INTERVAL_MS
            <= self.poll_interval_ms
            <= MAX_POLL_INTERVAL_MS
        ):
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_POLL_INTERVAL_INVALID",
                "poll_interval_ms must be a native integer between one minute and seven days",
            )
        if (
            type(self.max_candidates_per_poll) is not int
            or not 1
            <= self.max_candidates_per_poll
            <= MAX_OBSERVED_ITEMS_PER_POLL
        ):
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_CANDIDATE_BOUND_INVALID",
                "max_candidates_per_poll must be a native integer between 1 and 50",
            )
        if type(self.official_source) is not bool:
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_OFFICIAL_FLAG_INVALID",
                "official_source must be a native boolean",
            )
        if (
            type(self.source_class) is not str
            or self.source_class not in SOURCE_MONITORING_SOURCE_CLASSES
        ):
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_SOURCE_CLASS_INVALID",
                "source_class is not in the closed monitoring source-class set",
            )
        if (
            type(self.source_channel) is not str
            or self.source_channel not in SOURCE_MONITORING_SOURCE_CHANNELS
        ):
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_SOURCE_CHANNEL_INVALID",
                "source_channel is not in the closed monitoring channel set",
            )
        if (
            type(self.max_market_calls_per_poll) is not int
            or not 0
            <= self.max_market_calls_per_poll
            <= MAX_MARKET_CALLS_PER_POLL
        ):
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_MARKET_CALL_BOUND_INVALID",
                (
                    "max_market_calls_per_poll must be a native integer "
                    f"between 0 and {MAX_MARKET_CALLS_PER_POLL}"
                ),
            )
        expected_source = (
            (OFFICIAL_SOURCE_CLASS, OFFICIAL_SOURCE_CHANNEL, 0)
            if self.official_source
            else (
                READONLY_MARKET_SOURCE_CLASS,
                FUTU_ANOMALY_SOURCE_CHANNEL,
                1,
            )
        )
        if (
            self.source_class != expected_source[0]
            or self.source_channel != expected_source[1]
            or (
                self.max_market_calls_per_poll != 0
                if self.official_source
                else self.max_market_calls_per_poll < expected_source[2]
            )
        ):
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_SOURCE_BINDING_INVALID",
                "official and read-only market source metadata must use their exact channel binding",
            )
        if (
            type(self.execution_capability) is not str
            or self.execution_capability != "none"
        ):
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_EXECUTION_BOUNDARY_INVALID",
                "adapter execution_capability must be none",
            )
        if type(self.live_trading_allowed) is not bool:
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_LIVE_BOUNDARY_INVALID",
                "live_trading_allowed must be a native boolean",
            )
        if self.live_trading_allowed is not False:
            raise SourceAdapterContractError(
                "SOURCE_ADAPTER_LIVE_BOUNDARY_INVALID",
                "live_trading_allowed must be false",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "adapter_key": self.adapter_key,
            "config_version": self.config_version,
            "poll_interval_ms": self.poll_interval_ms,
            "max_candidates_per_poll": self.max_candidates_per_poll,
            "official_source": self.official_source,
            "source_class": self.source_class,
            "source_channel": self.source_channel,
            "max_market_calls_per_poll": self.max_market_calls_per_poll,
            "execution_capability": self.execution_capability,
            "live_trading_allowed": self.live_trading_allowed,
        }


@runtime_checkable
class SourceAdapter(Protocol):
    """Closed polling port implemented by fixed, code-registered adapters."""

    contract_version: str
    adapter_key: str
    config_version: str
    poll_interval_ms: int
    max_candidates_per_poll: int
    official_source: bool
    execution_capability: str
    live_trading_allowed: bool

    def poll(
        self,
        checkpoint: dict[str, Any],
        *,
        observed_at_ms: int,
        etag: str = "",
        last_modified: str = "",
        max_items: int = 50,
    ) -> AdapterPollResult: ...


def validate_source_adapter(adapter: Any) -> SourceAdapterMetadata:
    """Validate and snapshot one adapter's exact safety metadata."""

    if adapter is None or isinstance(adapter, type):
        raise SourceAdapterContractError(
            "SOURCE_ADAPTER_INVALID",
            "adapter must be an instance",
        )
    poll = getattr(adapter, "poll", None)
    if not callable(poll):
        raise SourceAdapterContractError(
            "SOURCE_ADAPTER_POLL_MISSING",
            "adapter must implement poll(checkpoint, observed_at_ms=...)",
        )
    try:
        signature = inspect.signature(poll)
    except (TypeError, ValueError) as exc:
        raise SourceAdapterContractError(
            "SOURCE_ADAPTER_POLL_SIGNATURE_INVALID",
            "adapter poll signature cannot be inspected",
        ) from exc
    parameters = tuple(signature.parameters.values())
    expected_names = (
        "checkpoint",
        "observed_at_ms",
        "etag",
        "last_modified",
        "max_items",
    )
    if (
        tuple(parameter.name for parameter in parameters) != expected_names
        or parameters[0].kind
        not in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        }
        or parameters[0].default is not inspect.Parameter.empty
        or any(
            parameter.kind is not inspect.Parameter.KEYWORD_ONLY
            for parameter in parameters[1:]
        )
        or parameters[1].default is not inspect.Parameter.empty
        or type(parameters[2].default) is not str
        or parameters[2].default != ""
        or type(parameters[3].default) is not str
        or parameters[3].default != ""
        or type(parameters[4].default) is not int
        or parameters[4].default != 50
    ):
        raise SourceAdapterContractError(
            "SOURCE_ADAPTER_POLL_SIGNATURE_INVALID",
            (
                "adapter poll must be poll(checkpoint, *, observed_at_ms, "
                "etag='', last_modified='', max_items=50)"
            ),
        )
    official_source = getattr(adapter, "official_source", None)
    return SourceAdapterMetadata(
        contract_version=getattr(adapter, "contract_version", None),
        adapter_key=getattr(adapter, "adapter_key", None),
        config_version=getattr(adapter, "config_version", None),
        poll_interval_ms=getattr(adapter, "poll_interval_ms", None),
        max_candidates_per_poll=getattr(
            adapter,
            "max_candidates_per_poll",
            None,
        ),
        official_source=official_source,
        source_class=getattr(
            adapter,
            "source_class",
            OFFICIAL_SOURCE_CLASS if official_source is True else None,
        ),
        source_channel=getattr(
            adapter,
            "source_channel",
            OFFICIAL_SOURCE_CHANNEL if official_source is True else None,
        ),
        max_market_calls_per_poll=getattr(
            adapter,
            "max_market_calls_per_poll",
            0 if official_source is True else None,
        ),
        execution_capability=getattr(adapter, "execution_capability", None),
        live_trading_allowed=getattr(adapter, "live_trading_allowed", None),
    )


__all__ = [
    "SOURCE_ADAPTER_CONTRACT_VERSION",
    "MAX_POLL_INTERVAL_MS",
    "MIN_POLL_INTERVAL_MS",
    "SourceAdapter",
    "SourceAdapterContractError",
    "SourceAdapterMetadata",
    "validate_poll_context",
    "validate_source_adapter",
]
