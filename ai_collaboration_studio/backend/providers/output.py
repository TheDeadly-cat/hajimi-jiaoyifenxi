from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .base import ProviderResponse


OUTPUT_CAPABILITIES_VERSION = "provider_output_capabilities_v1"
OUTPUT_MODE_JSON_SCHEMA = "json_schema"
OUTPUT_MODE_JSON_OBJECT = "json_object"
OUTPUT_MODE_PROMPT_JSON = "prompt_json"
OUTPUT_MODE_PRIORITY = (
    OUTPUT_MODE_JSON_SCHEMA,
    OUTPUT_MODE_JSON_OBJECT,
    OUTPUT_MODE_PROMPT_JSON,
)
_OUTPUT_MODES = frozenset(OUTPUT_MODE_PRIORITY)
_SCHEMA_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")


class ProviderOutputCapabilityError(RuntimeError):
    """A safe, pre-call failure in output capability negotiation or dispatch."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical_modes(values: Any) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        return ()
    requested = {
        str(value or "").strip().lower()
        for value in values
        if str(value or "").strip().lower() in _OUTPUT_MODES
    }
    return tuple(mode for mode in OUTPUT_MODE_PRIORITY if mode in requested)


@dataclass(frozen=True, slots=True)
class ProviderOutputCapabilities:
    modes: tuple[str, ...]
    declared: bool = True
    version: str = OUTPUT_CAPABILITIES_VERSION

    def __post_init__(self) -> None:
        canonical = _canonical_modes(self.modes)
        if not canonical:
            raise ValueError("Provider output capabilities require a supported mode")
        if self.version != OUTPUT_CAPABILITIES_VERSION:
            raise ValueError("Unsupported provider output capability version")
        object.__setattr__(self, "modes", canonical)
        object.__setattr__(self, "declared", bool(self.declared))

    @property
    def preferred_mode(self) -> str:
        return self.modes[0]

    def as_safe_dict(self) -> dict[str, Any]:
        """Return canonical public metadata with no adapter internals or secrets."""

        return {
            "version": OUTPUT_CAPABILITIES_VERSION,
            "modes": list(self.modes),
            "preferred_mode": self.preferred_mode,
            "declared": self.declared,
        }


@dataclass(frozen=True, slots=True)
class ProviderOutputSelection:
    mode: str
    supported_modes: tuple[str, ...]
    declared: bool
    capabilities_version: str = OUTPUT_CAPABILITIES_VERSION

    def as_safe_dict(self) -> dict[str, Any]:
        return {
            "capabilities_version": self.capabilities_version,
            "mode": self.mode,
            "supported_modes": list(self.supported_modes),
            "declared": self.declared,
        }


@dataclass(frozen=True, slots=True)
class ProviderTurnOutput:
    response: ProviderResponse
    selection: ProviderOutputSelection

    @property
    def mode(self) -> str:
        return self.selection.mode


def _compatibility_capabilities() -> ProviderOutputCapabilities:
    return ProviderOutputCapabilities(
        modes=(OUTPUT_MODE_PROMPT_JSON,),
        declared=False,
    )


def provider_output_capabilities(provider: Any) -> ProviderOutputCapabilities:
    """Resolve a provider's declared modes without trusting arbitrary metadata.

    Legacy and test providers that do not declare capabilities retain the old
    prompt-driven JSON behavior. A malformed declaration is treated the same as
    an absent declaration; it never unlocks a stronger transport mode.
    """

    capability_getter = getattr(provider, "output_capabilities", None)
    if not callable(capability_getter):
        return _compatibility_capabilities()
    try:
        raw = capability_getter()
    except Exception:
        return _compatibility_capabilities()

    if isinstance(raw, ProviderOutputCapabilities):
        modes = raw.modes
    elif isinstance(raw, Mapping):
        modes = _canonical_modes(raw.get("modes"))
    else:
        modes = _canonical_modes(raw)
    if not modes:
        return _compatibility_capabilities()
    return ProviderOutputCapabilities(modes=modes, declared=True)


def provider_output_capability_dict(provider: Any) -> dict[str, Any]:
    return provider_output_capabilities(provider).as_safe_dict()


def select_provider_output_mode(
    provider: Any,
    *,
    preferred_modes: Sequence[str] = OUTPUT_MODE_PRIORITY,
) -> ProviderOutputSelection:
    """Select exactly one mode using the global deterministic priority."""

    allowed_modes = _canonical_modes(preferred_modes)
    if not allowed_modes:
        raise ProviderOutputCapabilityError(
            "provider_output_mode_invalid",
            "No supported Provider output mode was requested.",
        )
    capabilities = provider_output_capabilities(provider)
    mode = next(
        (item for item in allowed_modes if item in capabilities.modes),
        "",
    )
    if not mode:
        raise ProviderOutputCapabilityError(
            "provider_output_mode_unavailable",
            "The Provider does not support an allowed output mode.",
        )
    return ProviderOutputSelection(
        mode=mode,
        supported_modes=capabilities.modes,
        declared=capabilities.declared,
    )


def generate_turn_output(
    provider: Any,
    *,
    instructions: str,
    input_text: str,
    model: str = "",
    preferred_modes: Sequence[str] = OUTPUT_MODE_PRIORITY,
    json_schema: Mapping[str, Any] | None = None,
    schema_name: str = "",
) -> ProviderTurnOutput:
    """Dispatch one structured turn request with no retry or mode fallback.

    Negotiation and handler validation happen before the only Provider call.
    Exceptions from that call are intentionally propagated to the owning
    service, which already classifies and records terminal failures.
    """

    selection = select_provider_output_mode(
        provider,
        preferred_modes=preferred_modes,
    )
    request: dict[str, Any] = {
        "instructions": str(instructions or ""),
        "input_text": str(input_text or ""),
        "model": str(model or ""),
    }
    if selection.mode == OUTPUT_MODE_JSON_SCHEMA:
        if not isinstance(json_schema, Mapping) or not json_schema:
            raise ProviderOutputCapabilityError(
                "provider_output_schema_required",
                "JSON Schema output requires a non-empty schema.",
            )
        clean_schema_name = str(schema_name or "").strip()
        if not _SCHEMA_NAME_PATTERN.fullmatch(clean_schema_name):
            raise ProviderOutputCapabilityError(
                "provider_output_schema_name_invalid",
                "JSON Schema output requires a valid schema name.",
            )
        handler = getattr(provider, "generate_json_schema", None)
        request["json_schema"] = copy.deepcopy(dict(json_schema))
        request["schema_name"] = clean_schema_name
    elif selection.mode == OUTPUT_MODE_JSON_OBJECT:
        handler = getattr(provider, "generate_json", None)
    else:
        handler = getattr(provider, "generate", None)
    if not callable(handler):
        raise ProviderOutputCapabilityError(
            "provider_output_handler_missing",
            "The Provider output handler is not implemented.",
        )

    response = handler(**request)
    if not isinstance(response, ProviderResponse):
        raise ProviderOutputCapabilityError(
            "provider_output_response_invalid",
            "The Provider returned an invalid response object.",
        )
    return ProviderTurnOutput(response=response, selection=selection)
