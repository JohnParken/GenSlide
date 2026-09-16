"""
Provider registry for GenSlide.

Enables modular, pluggable LLM provider registration and lookup.
New providers can register themselves using the `@register_provider` decorator
without touching core agent or dispatch code.
"""

import logging
from typing import Callable, Dict, List, Optional, Type, Union

from llm.base import BaseProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Registry maintaining available LLM providers."""

    def __init__(self) -> None:
        self._providers: Dict[str, BaseProvider] = {}
        self._aliases: Dict[str, str] = {}

    def register(
        self,
        provider_or_cls: Union[BaseProvider, Type[BaseProvider]],
        name: Optional[str] = None,
        aliases: Optional[List[str]] = None,
    ) -> BaseProvider:
        """
        Register a provider instance or class.

        Args:
            provider_or_cls: An instance or subclass of BaseProvider.
            name: Primary identifier. If omitted, uses provider.name.
            aliases: Optional alternative lookup names.
        """
        if isinstance(provider_or_cls, type) and issubclass(provider_or_cls, BaseProvider):
            instance = provider_or_cls()
        elif isinstance(provider_or_cls, BaseProvider):
            instance = provider_or_cls
        else:
            raise TypeError(
                f"Expected BaseProvider subclass or instance, got {type(provider_or_cls)}"
            )

        primary_name = (name or instance.name).strip().lower()
        if not primary_name:
            raise ValueError(f"Provider {instance} must specify a non-empty name.")

        self._providers[primary_name] = instance
        logger.debug("Registered LLM provider: '%s' (%s)", primary_name, instance.description)

        all_aliases = list(instance.aliases)
        if aliases:
            all_aliases.extend(aliases)

        for alias in all_aliases:
            alias_clean = alias.strip().lower()
            if alias_clean and alias_clean != primary_name:
                self._aliases[alias_clean] = primary_name

        return instance

    def get(self, name: str) -> BaseProvider:
        """
        Retrieve a registered provider by name or alias (case-insensitive).
        Raises KeyError if not found.
        """
        key = name.strip().lower()
        canonical_name = self._aliases.get(key, key)
        if canonical_name in self._providers:
            return self._providers[canonical_name]

        available = self.list_providers()
        raise KeyError(
            f"LLM provider '{name}' is not registered. Available providers: {available}"
        )

    def is_registered(self, name: str) -> bool:
        """Check if a provider name or alias is registered."""
        key = name.strip().lower()
        canonical_name = self._aliases.get(key, key)
        return canonical_name in self._providers

    def list_providers(self) -> List[str]:
        """Return a sorted list of all canonical provider names."""
        return sorted(self._providers.keys())

    def clear(self) -> None:
        """Clear all registered providers (primarily for testing)."""
        self._providers.clear()
        self._aliases.clear()


# Global default registry instance
registry = ProviderRegistry()


def register_provider(name: Optional[str] = None, aliases: Optional[List[str]] = None):
    """
    Decorator to register a BaseProvider class into the global registry.

    Usage:
        @register_provider("my_provider", aliases=["mp"])
        class MyProvider(BaseProvider):
            ...
    """

    def decorator(cls: Type[BaseProvider]) -> Type[BaseProvider]:
        registry.register(cls, name=name, aliases=aliases)
        return cls

    return decorator


def load_builtin_providers() -> None:
    """Ensure all built-in providers are imported and registered."""
    import llm.providers.openai_provider  # noqa: F401
    import llm.providers.local_provider   # noqa: F401
    import llm.providers.chatbbc_provider # noqa: F401
