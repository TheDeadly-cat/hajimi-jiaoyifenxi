from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL
from ..execution_boundary import build_text_provider_request
from .base import (
    ProviderProbeResult,
    ProviderResponse,
    classify_provider_exception,
    safe_provider_error_message,
)
from .probe import model_missing_probe, perform_http_probe, unconfigured_probe


def _response_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts)


def _http_error_text(raw: str, status_code: int) -> str:
    try:
        payload = json.loads(raw)
        error = payload.get("error") if isinstance(payload, dict) else {}
        code = str((error or {}).get("code") or "").lower()
        message = str((error or {}).get("message") or "").lower()
        if code == "insufficient_quota":
            return "OpenAI 配额不足，请检查该项目的余额或账单设置。"
        if code == "invalid_api_key":
            return "OpenAI 密钥无效或已失效。"
        if code == "rate_limit_exceeded":
            return "OpenAI 请求频率受限，请稍后重试。"
        if any(token in f"{code} {message}" for token in ("insufficient_quota", "quota", "balance")):
            return "OpenAI 配额不足，请检查该项目的余额或账单设置。"
    except Exception:
        pass
    return safe_provider_error_message(
        "OpenAI",
        "http_status",
        status_code=status_code,
    )


class OpenAIProvider:
    provider_id = "openai"

    def __init__(self, *, api_key: str = OPENAI_API_KEY, base_url: str = OPENAI_BASE_URL, default_model: str = OPENAI_MODEL) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model

    def status(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "name": "OpenAI",
            "configured": bool(self._api_key),
            "model": self._default_model,
            "api": "Responses API",
        }

    def probe(self, *, model: str = "") -> ProviderProbeResult:
        selected_model = model.strip() or self._default_model
        if not self._api_key:
            return unconfigured_probe(
                provider_id=self.provider_id,
                model=selected_model,
                display_name="OpenAI",
            )
        if not selected_model:
            return model_missing_probe(
                provider_id=self.provider_id,
                display_name="OpenAI",
            )
        body = {
            "model": selected_model,
            "input": "Reply with OK.",
            "max_output_tokens": 4,
            "store": False,
        }
        request = build_text_provider_request(
            self._base_url,
            "responses",
            body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": "AICollaborationStudio/0.2",
            },
        )
        return perform_http_probe(
            request,
            provider_id=self.provider_id,
            model=selected_model,
            display_name="OpenAI",
        )

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        selected_model = model or self._default_model
        if not self._api_key:
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error="OPENAI_API_KEY 未配置",
                error_code="provider_error",
            )
        body = {
            "model": selected_model,
            "instructions": instructions,
            "input": input_text,
            "max_output_tokens": 900,
            "store": False,
        }
        request = build_text_provider_request(
            self._base_url,
            "responses",
            body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": "AICollaborationStudio/0.1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:500]
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=_http_error_text(detail, exc.code),
                error_code="http_status",
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=safe_provider_error_message("OpenAI", "invalid_response"),
                error_code="invalid_response",
            )
        except Exception as exc:
            error_code = classify_provider_exception(exc)
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=safe_provider_error_message("OpenAI", error_code),
                error_code=error_code,
            )
        if not isinstance(payload, dict):
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=safe_provider_error_message("OpenAI", "invalid_response"),
                error_code="invalid_response",
            )
        content = _response_text(payload)
        return ProviderResponse(
            ok=bool(content),
            content=content,
            provider=self.provider_id,
            model=str(payload.get("model") or selected_model),
            error="" if content else "模型没有返回可显示文本",
            error_code="" if content else "empty_response",
            usage=payload.get("usage") or {},
        )
