"""Base interfaces for the Priority Prompt Contributors Pipeline.

Decouples hardcoded prompts into prioritized, composable contributors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class PromptContext:
    """Execution context passed to each prompt contributor."""
    session_id: str = ""
    tenant_id: str = ""
    user_id: str = ""
    goal: str = ""
    milestones: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    active_skill_name: str | None = None
    skill_instructions: str | None = None
    user_message: str = ""
    materials_brief: str | None = None
    environment_info: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PromptContributor(Protocol):
    """Protocol for a modular prompt slice contributor."""

    name: str
    priority: int  # Standard range: 0 (lowest) to 100 (highest)

    def contribute(self, ctx: PromptContext) -> str | None:
        """Return a string slice to append to the system prompt, or None to skip."""
        ...
