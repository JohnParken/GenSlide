"""Priority Prompt Contributors Pipeline for General Cloud Agent."""

from .base import PromptContext, PromptContributor
from .security import ProtectedSecurityContributor, PROTECTED_SECURITY_PROMPT
from .contract import ExecutionContractContributor
from .goal import GoalLedgerContributor
from .pipeline import (
    SkillPromptContributor,
    WorkspaceContextContributor,
    PromptPipeline,
    create_default_pipeline,
    create_runtime_pipeline,
)

__all__ = [
    "PromptContext",
    "PromptContributor",
    "ProtectedSecurityContributor",
    "PROTECTED_SECURITY_PROMPT",
    "ExecutionContractContributor",
    "GoalLedgerContributor",
    "SkillPromptContributor",
    "WorkspaceContextContributor",
    "PromptPipeline",
    "create_default_pipeline",
    "create_runtime_pipeline",
]
