from __future__ import annotations

from ..config import GLM_API_KEY, GLM_BASE_URL, GLM_MODEL
from .compatible_chat_provider import CompatibleChatProvider
from .output import OUTPUT_MODE_PROMPT_JSON, ProviderOutputCapabilities


class GLMProvider(CompatibleChatProvider):
    provider_id = "glm"

    def __init__(
        self,
        *,
        api_key: str = GLM_API_KEY,
        base_url: str = GLM_BASE_URL,
        default_model: str = GLM_MODEL,
    ) -> None:
        super().__init__(
            provider_id=self.provider_id,
            display_name="智谱 GLM",
            api_key=api_key,
            api_key_name="GLM_API_KEY / ZHIPUAI_API_KEY",
            base_url=base_url,
            default_model=default_model,
        )

    @staticmethod
    def output_capabilities() -> ProviderOutputCapabilities:
        return ProviderOutputCapabilities(modes=(OUTPUT_MODE_PROMPT_JSON,))
