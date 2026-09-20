"""Explicit Ark-hosted GLM route; never a replacement for direct Zhipu keys."""
from __future__ import annotations

from ..config import ARK_API_KEY
from .base import ProviderProbeResult, ProviderResponse
from .doubao_provider import DoubaoProvider
from .output import OUTPUT_MODE_PROMPT_JSON, ProviderOutputCapabilities

ARK_GLM_MODELS = frozenset({"glm-5-2-260617", "glm-5-3-flash-260828"})
ARK_RESPONSES_URL = "https://ark.cn-beijing.volces.com/api/v3"


class ArkGLMProvider(DoubaoProvider):
    provider_id = "glm"
    service_platform = "volcengine_ark"
    display_name = "智谱 GLM / 火山方舟"
    # Leave the model's documented default thinking mode in force. The Responses
    # max_output_tokens limit covers reasoning and visible output together.
    thinking_disabled = False
    normal_timeout_seconds = 240

    def __init__(self, *, api_key: str = ARK_API_KEY) -> None:
        super().__init__(api_key=api_key, base_url=ARK_RESPONSES_URL, default_model="")

    def status(self):
        return {**super().status(), "service_platform": self.service_platform}

    @staticmethod
    def output_capabilities() -> ProviderOutputCapabilities:
        return ProviderOutputCapabilities(modes=(OUTPUT_MODE_PROMPT_JSON,))

    def probe(self, *, model: str = "") -> ProviderProbeResult:
        return ProviderProbeResult(provider=self.provider_id, model=model,
            configured=bool(self._api_key), reachable=False, model_access=False,
            latency_ms=0, error_code="probe_not_supported",
            message="方舟 GLM 受控入口不额外探测；请使用已批准的单次证据回答。")

    def generate(self, *, instructions: str, input_text: str, model: str = "", max_output_tokens: int | None = None) -> ProviderResponse:
        if model not in ARK_GLM_MODELS:
            return ProviderResponse(ok=False, provider=self.provider_id, model=model,
                error="请选择已核对的方舟 GLM 固定模型 ID", error_code="model_not_allowed")
        return super().generate(instructions=instructions, input_text=input_text,
                                model=model, max_output_tokens=max_output_tokens)

    def generate_json(self, **kwargs) -> ProviderResponse:
        return ProviderResponse(ok=False, provider=self.provider_id,
            model=str(kwargs.get("model") or ""), error="方舟 GLM 使用提示约束 JSON，不声明原生 JSON Object 能力",
            error_code="output_mode_not_supported")
