from __future__ import annotations

import socket
import urllib.error
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .output import ProviderOutputCapabilities


PROVIDER_ERROR_CODES = frozenset({
    "timeout",
    "network",
    "http_status",
    "empty_response",
    "invalid_response",
    "provider_error",
})


def normalize_provider_error_code(value: Any) -> str:
    code = str(value or "").strip().lower()
    return code if code in PROVIDER_ERROR_CODES else "provider_error"


def classify_provider_exception(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        return "http_status"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "timeout"
        return "network"
    if isinstance(exc, (ConnectionError, socket.gaierror, OSError)):
        return "network"
    return "provider_error"


def safe_provider_error_message(
    display_name: str,
    error_code: str,
    *,
    status_code: int = 0,
) -> str:
    code = normalize_provider_error_code(error_code)
    if code == "timeout":
        return f"{display_name} 请求超时。"
    if code == "network":
        return f"{display_name} 连接失败，请检查网络或服务状态。"
    if code == "http_status":
        return (
            f"{display_name} 请求失败（HTTP {int(status_code)}）。"
            if status_code
            else f"{display_name} 请求失败。"
        )
    if code == "empty_response":
        return f"{display_name} 没有返回可显示文本。"
    if code == "invalid_response":
        return f"{display_name} 返回格式无法解析。"
    return f"{display_name} 模型调用失败。"


@dataclass(slots=True)
class ProviderResponse:
    ok: bool
    content: str = ""
    provider: str = ""
    model: str = ""
    error: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""


@dataclass(slots=True)
class ProviderProbeResult:
    provider: str
    model: str
    configured: bool
    reachable: bool
    model_access: bool
    latency_ms: int
    error_code: str = ""
    message: str = ""

    @property
    def ready(self) -> bool:
        return self.configured and self.reachable and self.model_access

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "configured": self.configured,
            "reachable": self.reachable,
            "model_access": self.model_access,
            "latency_ms": max(0, int(self.latency_ms)),
            "error_code": self.error_code,
            "message": self.message,
            "ready": self.ready,
        }


class ChatProvider(Protocol):
    provider_id: str

    def status(self) -> dict[str, Any]: ...

    def probe(self, *, model: str = "") -> ProviderProbeResult: ...

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse: ...


class OutputCapableChatProvider(ChatProvider, Protocol):
    """Optional structured-output extension for explicitly capable adapters."""

    def output_capabilities(self) -> "ProviderOutputCapabilities": ...

    def generate_json(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
    ) -> ProviderResponse: ...
