"""Priority Prompt Pipeline.

Assembles multiple prompt contributors by priority order into a coherent,
unified system prompt for the agent.
"""
from __future__ import annotations

import logging
from typing import Sequence

from .base import PromptContext, PromptContributor
from .security import ProtectedSecurityContributor
from .contract import ExecutionContractContributor
from .goal import GoalLedgerContributor

logger = logging.getLogger(__name__)


class SkillPromptContributor(PromptContributor):
    """Priority 40: Injects active skill instructions and domain rules."""

    name = "skill_prompt"
    priority = 40

    def contribute(self, ctx: PromptContext) -> str | None:
        if not ctx.active_skill_name and not ctx.skill_instructions:
            return None
        lines: list[str] = ["## ACTIVE SKILL INSTRUCTIONS"]
        if ctx.active_skill_name:
            lines.append(f"**Skill ID:** {ctx.active_skill_name}")
        if ctx.skill_instructions:
            lines.append(ctx.skill_instructions.strip())
        return "\n\n".join(lines)


class WorkspaceContextContributor(PromptContributor):
    """Priority 20: Injects dynamic session facts, confirmed requirements, and materials brief."""

    name = "workspace_context"
    priority = 20

    def contribute(self, ctx: PromptContext) -> str | None:
        if not ctx.materials_brief and not ctx.environment_info:
            return None
        lines: list[str] = ["## WORKSPACE & SESSION CONTEXT"]
        if ctx.environment_info:
            for key, val in ctx.environment_info.items():
                if val is not None and val != "" and val != {} and val != []:
                    lines.append(f"- **{key}**: {val}")
        if ctx.materials_brief:
            lines.append(f"### Reference Materials Brief:\n{ctx.materials_brief}")
        return "\n".join(lines) if len(lines) > 1 else None


class PromptPipeline:
    """Manages prompt contributors and builds unified system prompts."""

    def __init__(self, contributors: Sequence[PromptContributor] | None = None) -> None:
        self._contributors: list[PromptContributor] = list(contributors) if contributors else []

    def register(self, contributor: PromptContributor) -> PromptPipeline:
        """Register a new contributor to the pipeline."""
        self._contributors.append(contributor)
        return self

    def remove(self, name: str) -> bool:
        """Remove a contributor by name. Returns True if removed."""
        initial_len = len(self._contributors)
        self._contributors = [c for c in self._contributors if c.name != name]
        return len(self._contributors) < initial_len

    def get_sorted_contributors(self) -> list[PromptContributor]:
        """Return contributors sorted by priority descending (highest priority first)."""
        return sorted(self._contributors, key=lambda c: c.priority, reverse=True)

    def build_system_prompt(self, ctx: PromptContext) -> str:
        """Assemble all active contributors into a structured system prompt."""
        sorted_contributors = self.get_sorted_contributors()
        blocks: list[str] = []

        for contributor in sorted_contributors:
            try:
                slice_text = contributor.contribute(ctx)
                if slice_text and slice_text.strip():
                    blocks.append(slice_text.strip())
            except Exception as exc:
                logger.error(
                    "Error executing prompt contributor %s (priority %s): %s",
                    getattr(contributor, "name", "unknown"),
                    getattr(contributor, "priority", "unknown"),
                    exc,
                )

        return "\n\n---\n\n".join(blocks)


def create_default_pipeline() -> PromptPipeline:
    """Create standard pipeline suitable for cloud long-horizon agent tasks."""
    pipeline = PromptPipeline()
    pipeline.register(ProtectedSecurityContributor())
    pipeline.register(ExecutionContractContributor())
    pipeline.register(GoalLedgerContributor())
    return pipeline


def create_runtime_pipeline() -> PromptPipeline:
    """Create full 5-tier pipeline (P100/P80/P60/P40/P20) for unified runtime execution."""
    pipeline = create_default_pipeline()
    pipeline.register(SkillPromptContributor())
    pipeline.register(WorkspaceContextContributor())
    return pipeline
