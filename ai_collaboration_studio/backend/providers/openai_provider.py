from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from ..config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL
from ..execution_boundary import build_text_provider_request
from .base import (
    ProviderProbeResult,
    ProviderResponse,
    classify_provider_exception,
    safe_provider_error_message,
)
from .output import OUTPUT_MODE_JSON_SCHEMA, ProviderOutputCapabilities
from .http_transport import read_provider_response
from .probe import model_missing_probe, perform_http_probe, unconfigured_probe


_JSON_SCHEMA_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_ERROR_BYTES = 64 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _urlopen_no_redirect(request: urllib.request.Request, *, timeout: float) -> Any:
    """Open one validated API URL without forwarding credentials on redirects."""

    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _bounded_read(response: Any, maximum: int) -> bytes:
    return read_provider_response(response, maximum)


def _read_json_response(response: Any) -> Any:
    return json.loads(_bounded_read(response, _MAX_RESPONSE_BYTES).decode("utf-8"))


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


def _response_refused(payload: Mapping[str, Any]) -> bool:
    """Detect a Responses API refusal without returning its untrusted text."""

    direct = payload.get("refusal")
    if isinstance(direct, str) and direct.strip():
        return True
    output = payload.get("output")
    if not isinstance(output, list):
        return False
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content_items = item.get("content")
        if not isinstance(content_items, list):
            continue
        for content in content_items:
            if not isinstance(content, Mapping):
                continue
            if str(content.get("type") or "").strip().lower() == "refusal":
                return True
            refusal = content.get("refusal")
            if isinstance(refusal, str) and refusal.strip():
                return True
    return False


def _response_usage(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw_usage = payload.get("usage")
    usage = dict(raw_usage) if isinstance(raw_usage, Mapping) else {}
    request_id = payload.get("id")
    if isinstance(request_id, str) and request_id.strip():
        usage.setdefault("request_id", request_id.strip()[:200])
    return usage


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
        if any(
            token in f"{code} {message}"
            for token in ("insufficient_quota", "quota", "balance")
        ):
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

    def __init__(
        self,
        *,
        api_key: str = OPENAI_API_KEY,
        base_url: str = OPENAI_BASE_URL,
        default_model: str = OPENAI_MODEL,
    ) -> None:
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

    @staticmethod
    def output_capabilities() -> ProviderOutputCapabilities:
        """Declare only the Responses API mode implemented by this adapter."""

        return ProviderOutputCapabilities(modes=(OUTPUT_MODE_JSON_SCHEMA,))

    def probe(self, *, model: str = "") -> ProviderProbeResult:
        selected_model = str(model or "").strip() or self._default_model
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
            open_request=_urlopen_no_redirect,
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
            with _urlopen_no_redirect(request, timeout=60) as response:
                payload = _read_json_response(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = _bounded_read(exc, _MAX_ERROR_BYTES).decode(
                    "utf-8", errors="ignore"
                )[:500]
            except (OSError, TypeError, ValueError):
                detail = ""
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=_http_error_text(detail, exc.code),
                error_code="http_status",
            )
        except (ValueError, UnicodeDecodeError):
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

    def generate_json_schema(
        self,
        *,
        instructions: str,
        input_text: str,
        json_schema: Mapping[str, Any],
        schema_name: str,
        model: str = "",
    ) -> ProviderResponse:
        """Generate one strict JSON Schema response with no tools or fallback."""

        selected_model = str(model or "").strip() or self._default_model
        clean_schema_name = str(schema_name or "").strip()
        if not self._api_key:
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error="OPENAI_API_KEY 未配置",
                error_code="provider_error",
            )
        if (
            not selected_model
            or not isinstance(json_schema, Mapping)
            or not json_schema
            or not _JSON_SCHEMA_NAME_PATTERN.fullmatch(clean_schema_name)
        ):
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=safe_provider_error_message("OpenAI", "invalid_response"),
                error_code="invalid_response",
            )
        try:
            schema_payload = json.loads(json.dumps(dict(json_schema), allow_nan=False))
        except (TypeError, ValueError):
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=safe_provider_error_message("OpenAI", "invalid_response"),
                error_code="invalid_response",
            )

        body = {
            "model": selected_model,
            "instructions": str(instructions or ""),
            "input": str(input_text or ""),
            "max_output_tokens": 900,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": clean_schema_name,
                    "schema": schema_payload,
                    "strict": True,
                },
            },
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
        try:
            with _urlopen_no_redirect(request, timeout=60) as response:
                payload = _read_json_response(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = _bounded_read(exc, _MAX_ERROR_BYTES).decode(
                    "utf-8", errors="ignore"
                )[:500]
            except (OSError, TypeError, ValueError):
                detail = ""
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=selected_model,
                error=_http_error_text(detail, exc.code),
                error_code="http_status",
            )
        except (ValueError, UnicodeDecodeError):
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
        usage = _response_usage(payload)
        status = str(payload.get("status") or "").strip().lower()
        if status and status != "completed":
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=str(payload.get("model") or selected_model),
                error=safe_provider_error_message("OpenAI", "invalid_response"),
                error_code="invalid_response",
                usage=usage,
            )
        if _response_refused(payload):
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=str(payload.get("model") or selected_model),
                error=safe_provider_error_message("OpenAI", "provider_error"),
                error_code="provider_error",
                usage=usage,
            )

        content = _response_text(payload)
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return ProviderResponse(
                ok=False,
                provider=self.provider_id,
                model=str(payload.get("model") or selected_model),
                error=safe_provider_error_message("OpenAI", "invalid_response"),
                error_code="invalid_response",
                usage=usage,
            )
        return ProviderResponse(
            ok=True,
            content=json.dumps(parsed, ensure_ascii=False, separators=(",", ":")),
            provider=self.provider_id,
            model=str(payload.get("model") or selected_model),
            usage=usage,
        )
