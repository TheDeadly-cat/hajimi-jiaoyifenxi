from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..execution_boundary import build_text_provider_request
from .base import (
    ProviderProbeResult,
    ProviderResponse,
    classify_provider_exception,
    safe_provider_error_message,
)
from .probe import model_missing_probe, perform_http_probe, unconfigured_probe


def chat_response_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return "\n\n".join(parts)
    return ""


def chat_finish_reason(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    return str(choices[0].get("finish_reason") or "").strip().lower()


def chat_probe_response_text(payload: dict[str, Any]) -> str:
    """Accept bounded reasoning text as proof of model access during probes."""
    content = chat_response_text(payload)
    if content:
        return content
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""
    reasoning_content = message.get("reasoning_content")
    return reasoning_content.strip() if isinstance(reasoning_content, str) else ""


def provider_http_error(raw: str, status_code: int, display_name: str) -> str:
    try:
        payload = json.loads(raw)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("type") or "").lower()
            message = str(error.get("message") or "").strip().lower()
        else:
            code = str(payload.get("code") or "").lower() if isinstance(payload, dict) else ""
            message = str(payload.get("message") or "").strip().lower() if isinstance(payload, dict) else ""
        if any(token in code for token in ("invalid_api_key", "authentication", "unauthorized")) or status_code in {401, 403}:
            return f"{display_name} 密钥无效、未授权或模型尚未开通。"
        if any(token in f"{code} {message}" for token in ("insufficient_quota", "quota", "balance")):
            return f"{display_name} 配额或余额不足。"
        if "rate" in code or status_code == 429:
            return f"{display_name} 请求频率受限，请稍后重试。"
    except Exception:
        pass
    return safe_provider_error_message(
        display_name,
        "http_status",
        status_code=status_code,
    )


class CompatibleChatProvider:
    def __init__(
        self,
        *,
        provider_id: str,
        display_name: str,
        api_key: str,
        api_key_name: str,
        base_url: str,
        default_model: str,
    ) -> None:
        self.provider_id = provider_id
        self._display_name = display_name
        self._api_key = api_key
        self._api_key_name = api_key_name
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model

    def status(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "name": self._display_name,
            "configured": bool(self._api_key),
            "model": self._default_model,
            "api": "Chat Completions",
        }

    def probe(self, *, model: str = "") -> ProviderProbeResult:
        selected_model = model.strip() or self._default_model
        if not self._api_key:
            return unconfigured_probe(
                provider_id=self.provider_id,
                model=selected_model,
                display_name=self._display_name,
            )
        if not selected_model:
            return model_missing_probe(
                provider_id=self.provider_id,
                display_name=self._display_name,
            )
        body = {
            "model": selected_model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 4,
            "stream": False,
        }
        request = build_text_provider_request(
            self._base_url,
            "chat_completions",
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
            display_name=self._display_name,
            response_text_extractor=chat_probe_response_text,
        )

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        return self._generate(
            instructions=instructions,
            input_text=input_text,
            model=model,
            max_tokens=4096,
            timeout_seconds=60,
        )

    def _generate(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
        max_tokens: int,
        timeout_seconds: int,
        response_format: dict[str, str] | None = None,
    ) -> ProviderResponse:
        selected_model = model.strip() or self._default_model
        if not self._api_key:
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=f"{self._api_key_name} 未配置",
                error_code="provider_error",
            )
        body = {
            "model": selected_model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": input_text},
            ],
            "max_tokens": max(1, int(max_tokens)),
            "stream": False,
        }
        if response_format:
            body["response_format"] = response_format
        request = build_text_provider_request(
            self._base_url,
            "chat_completions",
            body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": "AICollaborationStudio/0.2",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=max(1, int(timeout_seconds))) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:800]
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=provider_http_error(detail, exc.code, self._display_name),
                error_code="http_status",
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=safe_provider_error_message(self._display_name, "invalid_response"),
                error_code="invalid_response",
            )
        except Exception as exc:
            error_code = classify_provider_exception(exc)
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=safe_provider_error_message(self._display_name, error_code),
                error_code=error_code,
            )
        if not isinstance(payload, dict):
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=safe_provider_error_message(self._display_name, "invalid_response"),
                error_code="invalid_response",
            )
        if chat_finish_reason(payload) == "length":
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=str(payload.get("model") or selected_model),
                error=safe_provider_error_message(self._display_name, "invalid_response"),
                error_code="invalid_response",
                usage=payload.get("usage") or {},
            )
        content = chat_response_text(payload)
        return ProviderResponse(
            ok=bool(content),
            content=content,
            provider=self.provider_id,
            model=str(payload.get("model") or selected_model),
            error="" if content else safe_provider_error_message(self._display_name, "empty_response"),
            error_code="" if content else "empty_response",
            usage=payload.get("usage") or {},
        )
