"""Priority 60: Goal Ledger & Long-Horizon Progress Contributor.

Dynamically renders the active long-horizon mission goal and milestone todo ledger
to keep the agent focused across multiple turns.
"""
from __future__ import annotations

from typing import Any
from .base import PromptContext, PromptContributor


class GoalLedgerContributor(PromptContributor):
    """Injects active goal and milestone todo status into system prompt."""

    name = "goal_ledger"
    priority = 60

    def contribute(self, ctx: PromptContext) -> str | None:
        if not ctx.goal and not ctx.milestones:
            return None

        lines: list[str] = ["## ACTIVE LONG-HORIZON MISSION"]

        if ctx.goal:
            lines.append(f"**Primary Objective:** {ctx.goal}")

        if ctx.milestones:
            lines.append("\n### Task Progress & Milestones (Todo Ledger):")
            for idx, item in enumerate(ctx.milestones, 1):
                status = item.get("status", "pending")
                title = item.get("title") or item.get("content", f"Step {idx}")

                if status == "completed":
                    marker = "[x] (Completed)"
                elif status == "in_progress":
                    marker = "[/] (IN PROGRESS - CURRENT FOCUS)"
                elif status == "blocked":
                    marker = "[!] (BLOCKED)"
                else:
                    marker = "[ ] (Pending)"

                detail = item.get("description", "")
                extra_str = f" - {detail}" if detail else ""
                lines.append(f"{idx}. {marker} **{title}**{extra_str}")

            lines.append(
                "\nGuideline: Maintain strict focus on the current in-progress task. "
                "Do not jump ahead until the active milestone is completed."
            )

        return "\n".join(lines)
