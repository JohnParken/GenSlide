"""
LLM provider factory for GenSlide.

Reads LLM_PROVIDER from the environment and returns the appropriate
LangChain-compatible LLM instance. All agents import from here — they
never instantiate their own LLM directly.

Architecture:
    This factory now delegates to a pluggable ProviderRegistry.
    New providers can be registered via the `@register_provider` decorator
    in `llm.registry` without modifying existing agent code.

Supported providers (configurable in .env via LLM_PROVIDER):
    - openai   → ChatOpenAI (gpt-4o), requires OPENAI_API_KEY
    - local    → gpt4all wrapper (Llama 3 by default, runs on CPU)
    - chatbbc  → corporate two-stage RPC/HTTP private protocol (alias: tl, custom)
"""

import logging
import os
from functools import lru_cache
from typing import List

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from llm.registry import load_builtin_providers, registry

logger = logging.getLogger(__name__)

# Ensure built-in providers are loaded and registered
load_builtin_providers()

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_PROVIDER = "openai"


def list_available_providers() -> List[str]:
    """Return a list of all registered provider names."""
    return registry.list_providers()


def get_provider_name() -> str:
    """Return the active canonical provider name from the environment."""
    raw = os.getenv("LLM_PROVIDER", DEFAULT_PROVIDER).strip().lower()
    if not registry.is_registered(raw):
        logger.warning(
            "Unknown LLM_PROVIDER='%s'. Available providers: %s. Falling back to '%s'.",
            raw,
            registry.list_providers(),
            DEFAULT_PROVIDER,
        )
        return DEFAULT_PROVIDER
    # Normalize to canonical registered name
    return registry.get(raw).name


@lru_cache(maxsize=8)
def get_llm(temperature: float = 0.3):
    """
    Return a cached LangChain-compatible LLM for the configured provider.

    All providers expose the same `.invoke(messages)` interface so agents
    can call them identically.

    Args:
        temperature: sampling temperature (cached per value — agents
                     that need different temperatures call get_llm()
                     with their specific value).

    Returns:
        A LangChain BaseChatModel-compatible object exposing .invoke().
    """
    provider_name = get_provider_name()
    provider = registry.get(provider_name)
    return provider.build_llm(temperature=temperature)