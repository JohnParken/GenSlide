"""Standard 8-phase runtime lifecycle and hook action definitions.

Inspired by QwenPaw's industrial-grade phase orchestration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Phase(str, Enum):
    """The 8 canonical request execution lifecycle phases."""
    PRE_DISPATCH = "pre_dispatch"
    POST_DISPATCH = "post_dispatch"
    PRE_AGENT_BUILD = "pre_agent_build"
    POST_AGENT_BUILD = "post_agent_build"
    PRE_EXECUTE = "pre_execute"
    POST_RESPONSE = "post_response"
    ON_ERROR = "on_error"
    FINALLY = "finally"


class HookAction(str, Enum):
    """Action returned by a hook to dictate execution flow."""
    # Continue to next hook / phase
    CONTINUE = "continue"
    # Immediately interrupt remaining hooks and stages, returning early
    SHORT_CIRCUIT = "short_circuit"
    # Sticky flag: skip main model reasoning, move directly to post-processing
    SKIP_AGENT = "skip_agent"


@dataclass
class HookResult:
    """Result returned by a single hook or a phase runner."""
    action: HookAction = HookAction.CONTINUE
    response: Any = None
    error: Exception | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
