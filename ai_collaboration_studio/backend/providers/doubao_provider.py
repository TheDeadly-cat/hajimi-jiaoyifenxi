from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..config import ARK_API_KEY, ARK_BASE_URL, ARK_MODEL
from ..execution_boundary import build_text_provider_request
from .base import (
    ProviderProbeResult,
    ProviderResponse,
    classify_provider_exception,
    safe_provider_error_message,
)
from .compatible_chat_provider import provider_http_error
from .openai_provider import _response_text
from .output import OUTPUT_MODE_JSON_OBJECT, ProviderOutputCapabilities
from .probe import model_missing_probe, perform_http_probe, unconfigured_probe


def _probe_response_text(payload: dict[str, Any]) -> str:
    response_status = str(payload.get("status") or "").strip().lower()
    if response_status in {"incomplete", "failed", "cancelled"}:
        raise ValueError("terminal probe response is not complete")
    return _response_text(payload)


class DoubaoProvider:
    provider_id = "doubao"

    def __init__(
        self,
        *,
        api_key: str = ARK_API_KEY,
        base_url: str = ARK_BASE_URL,
        default_model: str = ARK_MODEL,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model

    def status(self) -> dict[str, Any]:
        return {
            "id": self.provider_id,
            "name": "豆包 / 火山方舟",
            "configured": bool(self._api_key),
            "model": self._default_model,
            "api": "Responses API",
        }

    @staticmethod
    def output_capabilities() -> ProviderOutputCapabilities:
        return ProviderOutputCapabilities(modes=(OUTPUT_MODE_JSON_OBJECT,))

    def probe(self, *, model: str = "") -> ProviderProbeResult:
        selected_model = model.strip() or self._default_model
        if not self._api_key:
            return unconfigured_probe(
                provider_id=self.provider_id,
                model=selected_model,
                display_name="豆包 / 火山方舟",
            )
        if not selected_model:
            return model_missing_probe(
                provider_id=self.provider_id,
                display_name="豆包 / 火山方舟",
            )
        body = {
            "model": selected_model,
            "input": "Reply with OK.",
            "max_output_tokens": 4,
            "thinking": {"type": "disabled"},
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
            display_name="豆包 / 火山方舟",
            response_text_extractor=_probe_response_text,
        )

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        return self._generate(
            instructions=instructions,
            input_text=input_text,
            model=model,
            max_output_tokens=4096,
            timeout_seconds=60,
        )

    def generate_json(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
    ) -> ProviderResponse:
        """Generate a longer JSON artifact without changing normal chat limits."""
        return self._generate(
            instructions=instructions,
            input_text=input_text,
            model=model,
            max_output_tokens=6400,
            timeout_seconds=240,
            text_format={"type": "json_object"},
        )

    def _generate(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
        max_output_tokens: int,
        timeout_seconds: int,
        text_format: dict[str, Any] | None = None,
    ) -> ProviderResponse:
        selected_model = model.strip() or self._default_model
        if not self._api_key:
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error="ARK_API_KEY 未配置",
                error_code="provider_error",
            )
        body = {
            "model": selected_model,
            "instructions": instructions,
            "input": input_text,
            "max_output_tokens": max(1, int(max_output_tokens)),
            "thinking": {"type": "disabled"},
            "store": False,
        }
        if text_format:
            body["text"] = {"format": text_format}
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
        try:
            with urllib.request.urlopen(
                request,
                timeout=max(1, int(timeout_seconds)),
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:800]
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=provider_http_error(detail, exc.code, "豆包 / 火山方舟"),
                error_code="http_status",
            )
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=safe_provider_error_message("豆包 / 火山方舟", "invalid_response"),
                error_code="invalid_response",
            )
        except Exception as exc:
            error_code = classify_provider_exception(exc)
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=safe_provider_error_message("豆包 / 火山方舟", error_code),
                error_code=error_code,
            )
        if not isinstance(payload, dict):
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=safe_provider_error_message("豆包 / 火山方舟", "invalid_response"),
                error_code="invalid_response",
            )
        response_status = str(payload.get("status") or "").strip().lower()
        if response_status in {"incomplete", "failed", "cancelled"}:
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=str(payload.get("model") or selected_model),
                error=safe_provider_error_message("豆包 / 火山方舟", "invalid_response"),
                error_code="invalid_response",
                usage=payload.get("usage") or {},
            )
        content = _response_text(payload)
        return ProviderResponse(
            ok=bool(content),
            content=content,
            provider=self.provider_id,
            model=str(payload.get("model") or selected_model),
            error="" if content else "豆包 / 火山方舟没有返回可显示文本",
            error_code="" if content else "empty_response",
            usage=payload.get("usage") or {},
        )
