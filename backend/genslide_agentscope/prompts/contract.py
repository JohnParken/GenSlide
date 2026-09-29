"""Priority 80: Output Schema and Execution Contract.

Ensures the agent complies with the required structured communication format.
"""
from __future__ import annotations

import json
from typing import Any, Type
from pydantic import BaseModel

from .base import PromptContext, PromptContributor


class ExecutionContractContributor(PromptContributor):
    """Enforces strict JSON or structured schema adherence."""

    name = "execution_contract"
    priority = 80

    def __init__(
        self,
        schema_model: Type[BaseModel] | None = None,
        custom_instructions: str | None = None,
    ) -> None:
        self.schema_model = schema_model
        self.custom_instructions = custom_instructions

    def contribute(self, ctx: PromptContext) -> str | None:
        parts: list[str] = [
            "## OUTPUT FORMAT CONTRACT",
            "1. Return ONLY valid, parseable JSON conforming strictly to the expected schema.",
            "2. Do not enclose the output in conversational chatter before or after the JSON.",
            "3. Ensure all keys and string values are enclosed in double quotes.",
        ]

        if self.custom_instructions:
            parts.append(self.custom_instructions)

        if self.schema_model is not None:
            schema_json = json.dumps(
                self.schema_model.model_json_schema(),
                indent=2,
                ensure_ascii=False,
            )
            parts.append(f"### Expected JSON Schema:\n```json\n{schema_json}\n```")

        return "\n".join(parts)
