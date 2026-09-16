"""
OpenAI Provider plugin for GenSlide.
"""

import logging
import os
from typing import Any

from llm.base import BaseProvider
from llm.registry import register_provider

logger = logging.getLogger(__name__)


@register_provider("openai")
class OpenAIProvider(BaseProvider):
    name = "openai"
    description = "OpenAI ChatOpenAI (gpt-4o)"

    def validate_environment(self) -> None:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise EnvironmentError(
                "LLM_PROVIDER=openai but OPENAI_API_KEY is not set. "
                "Add it to your .env file."
            )

    def build_llm(self, temperature: float = 0.3, **kwargs: Any) -> Any:
        self.validate_environment()
        from langchain_openai import ChatOpenAI

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        model_name = os.getenv("OPENAI_MODEL_NAME", "gpt-4o")

        logger.info("LLM provider: OpenAI %s (temperature=%.1f)", model_name, temperature)
        return ChatOpenAI(
            model=model_name,
            temperature=temperature,
            max_tokens=1024,
            response_format={"type": "json_object"},
            api_key=api_key,
            **kwargs,
        )
