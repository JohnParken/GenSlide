"""
LLM module for GenSlide.
"""

from llm.base import BaseLLMAdapter, BaseProvider
from llm.llm_provider import get_llm, get_provider_name, list_available_providers
from llm.registry import ProviderRegistry, register_provider, registry

__all__ = [
    "get_llm",
    "get_provider_name",
    "list_available_providers",
    "register_provider",
    "registry",
    "ProviderRegistry",
    "BaseProvider",
    "BaseLLMAdapter",
]
