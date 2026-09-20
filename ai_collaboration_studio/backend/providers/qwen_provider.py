from __future__ import annotations

from urllib.parse import urlparse
import re

from ..config import QWEN_API_KEY, QWEN_BASE_URL, QWEN_MODEL
from .base import ProviderProbeResult, ProviderResponse, output_token_limit
from .compatible_chat_provider import CompatibleChatProvider
from .output import OUTPUT_MODE_JSON_OBJECT, ProviderOutputCapabilities


def is_qwen_backend_endpoint(base_url: str) -> bool:
    """Only the documented Beijing pay-as-you-go service, never coding plans."""
    parsed = urlparse(base_url)
    try:
        port = parsed.port
    except ValueError:
        return False
    hostname = parsed.hostname or ""
    return bool(
        parsed.scheme == "https" and port in (None, 443)
        and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
        and parsed.path.rstrip("/") == "/compatible-mode/v1"
        and (hostname == "dashscope.aliyuncs.com"
             or re.fullmatch(r"[a-z0-9][a-z0-9-]*\.cn-beijing\.maas\.aliyuncs\.com", hostname)
             and hostname != "token-plan.cn-beijing.maas.aliyuncs.com")
    )


class QwenProvider(CompatibleChatProvider):
    provider_id = "qwen"
    # For supported Qwen models, max_tokens excludes reasoning; this includes it.
    completion_token_limit = True

    def __init__(self, *, api_key: str = QWEN_API_KEY, base_url: str = QWEN_BASE_URL,
                 default_model: str = QWEN_MODEL) -> None:
        self._endpoint_allowed = is_qwen_backend_endpoint(base_url)
        super().__init__(provider_id=self.provider_id, display_name="千问 Qwen（百炼按量 API）",
                         api_key=api_key, api_key_name="QWEN_API_KEY / DASHSCOPE_API_KEY",
                         base_url=base_url, default_model=default_model)

    def status(self):
        status = super().status()
        if not self._endpoint_allowed:
            status.update(configured=False, configuration_error="QWEN_BACKEND_ENDPOINT_NOT_ALLOWED")
        return status

    def probe(self, *, model: str = "") -> ProviderProbeResult:
        if not self._endpoint_allowed:
            return ProviderProbeResult(provider=self.provider_id, model=model or self._default_model,
                                       configured=False, reachable=False, model_access=False, latency_ms=0,
                                       error_code="QWEN_BACKEND_ENDPOINT_NOT_ALLOWED",
                                       message="请使用北京地域百炼按量 API；套餐端点不适用于项目后端。")
        return super().probe(model=model)

    def _generate(self, **kwargs) -> ProviderResponse:
        if not self._endpoint_allowed:
            return ProviderResponse(ok=False, provider=self.provider_id,
                                    model=kwargs.get("model") or self._default_model,
                                    error="请使用北京地域百炼按量 API；套餐端点不适用于项目后端。",
                                    error_code="QWEN_BACKEND_ENDPOINT_NOT_ALLOWED")
        return super()._generate(**kwargs)

    @staticmethod
    def output_capabilities() -> ProviderOutputCapabilities:
        return ProviderOutputCapabilities(modes=(OUTPUT_MODE_JSON_OBJECT,))

    def generate_json(self, *, instructions: str, input_text: str, model: str = "",
                      max_output_tokens: int | None = None) -> ProviderResponse:
        return self._generate(instructions=instructions, input_text=input_text, model=model,
                              max_tokens=output_token_limit(max_output_tokens, default=4096),
                              timeout_seconds=180, response_format={"type": "json_object"})
