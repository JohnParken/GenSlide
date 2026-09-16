"""
Base interfaces and protocols for GenSlide LLM providers and adapters.
"""

from abc import ABC, abstractmethod
from typing import Any, List, Optional, Protocol, runtime_checkable

try:
    from langchain_core.messages import AIMessage, BaseMessage
except ImportError:  # pragma: no cover
    class BaseMessage:  # type: ignore[no-redef]
        def __init__(self, content: Any = "", **kwargs: Any) -> None:
            self.content = content

    class AIMessage(BaseMessage):  # type: ignore[no-redef]
        pass


@runtime_checkable
class BaseLLMAdapter(Protocol):
    """
    Protocol defining the uniform interface expected by all GenSlide agents.
    Mirrors the LangChain ChatModel `.invoke()` signature.
    """

    def invoke(self, messages: List[BaseMessage], **kwargs: Any) -> AIMessage:
        """Process messages and return an AIMessage with the model's response."""
        ...


class BaseProvider(ABC):
    """
    Abstract base class for all pluggable LLM providers.
    Each provider knows how to validate its configuration and construct an LLM instance.
    """

    name: str = ""
    aliases: List[str] = []
    description: str = ""

    @abstractmethod
    def build_llm(self, temperature: float = 0.3, **kwargs: Any) -> Any:
        """
        Instantiate and return a LangChain-compatible LLM adapter or model.
        Must satisfy BaseLLMAdapter (exposing .invoke(messages) -> AIMessage).
        """
        pass

    def validate_environment(self) -> None:
        """
        Optional pre-check for required environment variables or dependencies.
        Subclasses should raise EnvironmentError or ImportError if requirements are unmet.
        """
        pass
