"""
Local (gpt4all) Provider plugin for GenSlide.
"""

import logging
import os
from typing import Any

from llm.base import BaseProvider
from llm.registry import register_provider

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_MODEL = "Meta-Llama-3-8B-Instruct.Q4_0.gguf"


@register_provider("local", aliases=["gpt4all"])
class LocalProvider(BaseProvider):
    name = "local"
    aliases = ["gpt4all"]
    description = "Local inference via gpt4all wrapper (Llama 3)"

    def build_llm(self, temperature: float = 0.3, **kwargs: Any) -> Any:
        from llm.gpt4all_wrapper import GPT4AllChatWrapper

        model_name = os.getenv("LOCAL_MODEL_NAME", DEFAULT_LOCAL_MODEL)
        model_path = os.getenv("LOCAL_MODEL_PATH", "").strip() or None

        logger.info(
            "LLM provider: local gpt4all  model=%s  temperature=%.1f",
            model_name,
            temperature,
        )
        return GPT4AllChatWrapper(
            model_name=model_name,
            model_path=model_path,
            temperature=temperature,
            **kwargs,
        )
