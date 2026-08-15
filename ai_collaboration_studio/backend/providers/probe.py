from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .base import ProviderProbeResult


PROBE_TIMEOUT_SECONDS = 15
PROBE_RESPONSE_MAX_BYTES = 64_000


ProbeTextExtractor = Callable[[dict[str, Any]], str]


def unconfigured_probe(
    *,
    provider_id: str,
    model: str,
    display_name: str,
) -> ProviderProbeResult:
    return ProviderProbeResult(
        provider=provider_id,
        model=model,
        configured=False,
        reachable=False,
        model_access=False,
        latency_ms=0,
        error_code="not_configured",
        message=f"{display_name} 尚未配置。",
    )


def model_missing_probe(
    *,
    provider_id: str,
    display_name: str,
) -> ProviderProbeResult:
    return ProviderProbeResult(
        provider=provider_id,
        model="",
        configured=True,
        reachable=False,
        model_access=False,
        latency_ms=0,
        error_code="model_not_configured",
        message=f"{display_name} 尚未配置模型。",
    )


def skipped_probe(
    *,
    provider_id: str,
    model: str,
    configured: bool,
    display_name: str,
) -> ProviderProbeResult:
    return ProviderProbeResult(
        provider=provider_id,
        model=model,
        configured=configured,
        reachable=False,
        model_access=False,
        latency_ms=0,
        error_code="PROVIDER_SKIPPED",
        message=f"{display_name} 已按本次会前检查设置跳过，未发送网络请求。",
    )


def _error_marker(raw: str) -> str:
    """Extract only enough server data to classify an error; never return it."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    candidates: list[Any] = []
    if isinstance(error, dict):
        candidates.extend((error.get("code"), error.get("type"), error.get("message")))
    candidates.extend((payload.get("code"), payload.get("type"), payload.get("message")))
    return " ".join(str(item or "").lower() for item in candidates)[:1200]


def _http_failure(
    *,
    provider_id: str,
    model: str,
    display_name: str,
    status_code: int,
    raw: str,
    latency_ms: int,
) -> ProviderProbeResult:
    marker = _error_marker(raw)
    if status_code in {401, 403} or any(
        token in marker
        for token in ("invalid_api_key", "authentication", "unauthorized", "permission_denied")
    ):
        error_code = "authentication_or_model_access_denied"
        message = f"{display_name} 未通过认证或无权访问该模型。"
    elif status_code == 404 or any(
        token in marker for token in ("model_not_found", "unknown_model", "deployment_not_found")
    ):
        error_code = "model_not_found"
        message = f"{display_name} 未找到指定模型。"
    elif status_code == 429 or "rate_limit" in marker:
        error_code = "rate_limited"
        message = f"{display_name} 当前请求频率受限。"
    elif status_code == 408:
        error_code = "timeout"
        message = f"{display_name} 探测超时。"
    elif status_code in {402} or any(
        token in marker for token in ("insufficient_quota", "quota", "balance", "billing")
    ):
        error_code = "quota_exhausted"
        message = f"{display_name} 当前配额或余额不可用。"
    elif status_code >= 500:
        error_code = "provider_unavailable"
        message = f"{display_name} 服务暂时不可用。"
    else:
        error_code = "request_rejected"
        message = f"{display_name} 拒绝了模型探测请求。"
    return ProviderProbeResult(
        provider=provider_id,
        model=model,
        configured=True,
        reachable=True,
        model_access=False,
        latency_ms=latency_ms,
        error_code=error_code,
        message=message,
    )


def _successful_response_failure(
    *,
    provider_id: str,
    model: str,
    display_name: str,
    error_code: str,
    latency_ms: int,
) -> ProviderProbeResult:
    code = "empty_response" if error_code == "empty_response" else "invalid_response"
    return ProviderProbeResult(
        provider=provider_id,
        model=model,
        configured=True,
        reachable=True,
        model_access=False,
        latency_ms=latency_ms,
        error_code=code,
        message=(
            f"{display_name} 探测响应没有可用文本。"
            if code == "empty_response"
            else f"{display_name} 探测响应格式无效。"
        ),
    )


def perform_http_probe(
    request: urllib.request.Request,
    *,
    provider_id: str,
    model: str,
    display_name: str,
    timeout_seconds: int = PROBE_TIMEOUT_SECONDS,
    response_text_extractor: ProbeTextExtractor | None = None,
) -> ProviderProbeResult:
    started = time.perf_counter()
    response_body = b""
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response_text_extractor is not None:
                response_body = response.read(PROBE_RESPONSE_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(4096).decode("utf-8", errors="ignore")
        except Exception:
            raw = ""
        return _http_failure(
            provider_id=provider_id,
            model=model,
            display_name=display_name,
            status_code=int(exc.code),
            raw=raw,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
    except (TimeoutError, socket.timeout):
        return ProviderProbeResult(
            provider=provider_id,
            model=model,
            configured=True,
            reachable=False,
            model_access=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_code="timeout",
            message=f"{display_name} 探测超时。",
        )
    except urllib.error.URLError:
        return ProviderProbeResult(
            provider=provider_id,
            model=model,
            configured=True,
            reachable=False,
            model_access=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_code="connection_failed",
            message=f"{display_name} 当前无法连接。",
        )
    except Exception:
        return ProviderProbeResult(
            provider=provider_id,
            model=model,
            configured=True,
            reachable=False,
            model_access=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_code="probe_failed",
            message=f"{display_name} 探测失败。",
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    if response_text_extractor is not None:
        if not response_body:
            return _successful_response_failure(
                provider_id=provider_id,
                model=model,
                display_name=display_name,
                error_code="empty_response",
                latency_ms=latency_ms,
            )
        if len(response_body) > PROBE_RESPONSE_MAX_BYTES:
            return _successful_response_failure(
                provider_id=provider_id,
                model=model,
                display_name=display_name,
                error_code="invalid_response",
                latency_ms=latency_ms,
            )
        try:
            payload = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return _successful_response_failure(
                provider_id=provider_id,
                model=model,
                display_name=display_name,
                error_code="invalid_response",
                latency_ms=latency_ms,
            )
        if not isinstance(payload, dict):
            return _successful_response_failure(
                provider_id=provider_id,
                model=model,
                display_name=display_name,
                error_code="invalid_response",
                latency_ms=latency_ms,
            )
        try:
            response_text = response_text_extractor(payload)
        except Exception:
            return _successful_response_failure(
                provider_id=provider_id,
                model=model,
                display_name=display_name,
                error_code="invalid_response",
                latency_ms=latency_ms,
            )
        if not isinstance(response_text, str) or not response_text.strip():
            return _successful_response_failure(
                provider_id=provider_id,
                model=model,
                display_name=display_name,
                error_code="empty_response",
                latency_ms=latency_ms,
            )
    return ProviderProbeResult(
        provider=provider_id,
        model=model,
        configured=True,
        reachable=True,
        model_access=True,
        latency_ms=latency_ms,
        message=f"{display_name} 连接与模型访问正常。",
    )
