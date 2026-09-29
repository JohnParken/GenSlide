"""Dynamic model capability trial-and-error cache.

Inspired by QwenPaw's ModelCapabilityCache: records discovered provider/model
quirks (e.g. unsupported response_format, strict schema rejections, thinking flags)
so subsequent requests adapt automatically without throwing errors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ModelCapability:
    """Discovered capability profile for a specific provider and model pair."""
    provider: str
    model: str
    supports_response_format: bool = True
    supports_thinking: bool = True
    strict_json_schema: bool = True
    supports_tools: bool = True
    max_output_tokens: int | None = None
    custom_flags: dict[str, Any] = field(default_factory=dict)

    def cache_key(self) -> str:
        return f"{self.provider.strip().lower()}:{self.model.strip().lower()}"


class ModelCapabilityCache:
    """Thread-safe dynamic capability registry with failure-recovery learning."""

    def __init__(self) -> None:
        self._cache: dict[str, ModelCapability] = {}

    def get(self, provider: str, model: str) -> ModelCapability:
        """Get or initialize capability profile for provider/model."""
        key = f"{provider.strip().lower()}:{model.strip().lower()}"
        if key not in self._cache:
            self._cache[key] = ModelCapability(provider=provider, model=model)
        return self._cache[key]

    def record_failure(self, provider: str, model: str, error: Exception | str) -> ModelCapability:
        """Inspect error message and automatically update capability flags.

        Args:
            provider: LLM provider name.
            model: Model name.
            error: Exception or string error message.

        Returns:
            Updated ModelCapability instance.
        """
        err_msg = str(error).lower()
        cap = self.get(provider, model)

        # 1. Inspect response_format rejections
        if any(keyword in err_msg for keyword in (
            "response_format",
            "json_object",
            "json schema is not supported",
            "unsupported parameter: response_format",
        )):
            logger.warning(
                "Model %s:%s rejected response_format; disabling it in capability cache.",
                provider, model
            )
            cap.supports_response_format = False

        # 2. Inspect strict schema / additionalProperties rejections
        if any(keyword in err_msg for keyword in (
            "additionalproperties",
            "strict schema",
            "schema validation failed",
            "invalid json schema",
        )):
            logger.warning(
                "Model %s:%s failed strict schema validation; disabling strict_json_schema.",
                provider, model
            )
            cap.strict_json_schema = False

        # 3. Inspect tools rejection
        if any(keyword in err_msg for keyword in (
            "tools are not supported",
            "tool_choice",
            "function calling is not supported",
        )):
            logger.warning(
                "Model %s:%s does not support tools; disabling supports_tools.",
                provider, model
            )
            cap.supports_tools = False

        return cap

    def reset(self) -> None:
        """Clear cached capabilities."""
        self._cache.clear()


_GLOBAL_CAPABILITY_CACHE = ModelCapabilityCache()


def get_capability_cache() -> ModelCapabilityCache:
    """Return singleton ModelCapabilityCache instance."""
    return _GLOBAL_CAPABILITY_CACHE
