from __future__ import annotations

from ..config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
from .base import ProviderResponse
from .compatible_chat_provider import CompatibleChatProvider
from .output import OUTPUT_MODE_JSON_OBJECT, ProviderOutputCapabilities


class DeepSeekProvider(CompatibleChatProvider):
    provider_id = "deepseek"

    def __init__(
        self,
        *,
        api_key: str = DEEPSEEK_API_KEY,
        base_url: str = DEEPSEEK_BASE_URL,
        default_model: str = DEEPSEEK_MODEL,
    ) -> None:
        super().__init__(
            provider_id=self.provider_id,
            display_name="DeepSeek",
            api_key=api_key,
            api_key_name="DEEPSEEK_API_KEY",
            base_url=base_url,
            default_model=default_model,
        )

    @staticmethod
    def output_capabilities() -> ProviderOutputCapabilities:
        return ProviderOutputCapabilities(modes=(OUTPUT_MODE_JSON_OBJECT,))

    def generate_json(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
    ) -> ProviderResponse:
        """Generate a bounded JSON artifact without changing normal chat behavior."""
        return self._generate(
            instructions=instructions,
            input_text=input_text,
            model=model,
            max_tokens=3200,
            timeout_seconds=180,
            response_format={"type": "json_object"},
        )
