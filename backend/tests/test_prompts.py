"""Unit tests for the priority prompt contributors pipeline."""

import pytest
from pydantic import BaseModel, Field

from genslide_agentscope.prompts import (
    PromptContext,
    PromptContributor,
    ProtectedSecurityContributor,
    ExecutionContractContributor,
    GoalLedgerContributor,
    SkillPromptContributor,
    WorkspaceContextContributor,
    PromptPipeline,
    create_default_pipeline,
    create_runtime_pipeline,
)


class SampleOutputSchema(BaseModel):
    action: str = Field(description="Action name")
    parameters: dict[str, str] = Field(default_factory=dict)


def test_prompt_context_defaults():
    ctx = PromptContext()
    assert ctx.session_id == ""
    assert ctx.goal == ""
    assert ctx.milestones == ()
    assert ctx.environment_info == {}


def test_protected_security_contributor():
    c = ProtectedSecurityContributor()
    assert c.name == "protected_security"
    assert c.priority == 100
    text = c.contribute(PromptContext())
    assert "NON-NEGOTIABLE" in text
    assert "SECURITY & BOUNDARY DEFENSE" in text


def test_execution_contract_with_schema():
    c = ExecutionContractContributor(schema_model=SampleOutputSchema)
    assert c.name == "execution_contract"
    assert c.priority == 80
    text = c.contribute(PromptContext())
    assert "OUTPUT FORMAT CONTRACT" in text
    assert "SampleOutputSchema" in text
    assert "Expected JSON Schema" in text


def test_goal_ledger_contributor_empty():
    c = GoalLedgerContributor()
    assert c.name == "goal_ledger"
    assert c.priority == 60
    # Returns None when no goal or milestones exist
    assert c.contribute(PromptContext()) is None


def test_goal_ledger_contributor_with_milestones():
    ctx = PromptContext(
        goal="Build an autonomous workflow system",
        milestones=(
            {"title": "Collect data", "status": "completed"},
            {"title": "Implement core logic", "status": "in_progress", "description": "Writing tests"},
            {"title": "Deploy to cloud", "status": "pending"},
        )
    )
    c = GoalLedgerContributor()
    text = c.contribute(ctx)
    assert "ACTIVE LONG-HORIZON MISSION" in text
    assert "Build an autonomous workflow system" in text
    assert "[x] (Completed)" in text
    assert "[/] (IN PROGRESS - CURRENT FOCUS)" in text
    assert "[ ] (Pending)" in text


def test_pipeline_sorting_and_rendering():
    pipeline = PromptPipeline()

    class LowPriorityContributor:
        name = "low"
        priority = 10
        def contribute(self, ctx):
            return "LOW PRIORITY CONTENT"

    class HighPriorityContributor:
        name = "high"
        priority = 90
        def contribute(self, ctx):
            return "HIGH PRIORITY CONTENT"

    pipeline.register(LowPriorityContributor())
    pipeline.register(HighPriorityContributor())

    sorted_list = pipeline.get_sorted_contributors()
    assert sorted_list[0].name == "high"
    assert sorted_list[1].name == "low"

    prompt = pipeline.build_system_prompt(PromptContext())
    assert "HIGH PRIORITY CONTENT" in prompt
    assert "LOW PRIORITY CONTENT" in prompt
    # High priority should appear before low priority
    assert prompt.index("HIGH PRIORITY CONTENT") < prompt.index("LOW PRIORITY CONTENT")


def test_pipeline_remove_contributor():
    pipeline = PromptPipeline()
    c = ProtectedSecurityContributor()
    pipeline.register(c)
    assert len(pipeline.get_sorted_contributors()) == 1
    removed = pipeline.remove("protected_security")
    assert removed is True
    assert len(pipeline.get_sorted_contributors()) == 0


def test_create_default_pipeline():
    pipeline = create_default_pipeline()
    contributors = pipeline.get_sorted_contributors()
    names = [c.name for c in contributors]
    assert names == ["protected_security", "execution_contract", "goal_ledger"]

    ctx = PromptContext(
        goal="Autonomous cloud agent testing",
        milestones=({"title": "Run test suite", "status": "in_progress"},),
    )
    prompt = pipeline.build_system_prompt(ctx)
    assert "SECURITY & BOUNDARY DEFENSE" in prompt
    assert "OUTPUT FORMAT CONTRACT" in prompt
    assert "ACTIVE LONG-HORIZON MISSION" in prompt


def test_create_runtime_pipeline_five_tiers():
    pipeline = create_runtime_pipeline()
    contributors = pipeline.get_sorted_contributors()
    names = [c.name for c in contributors]
    assert names == [
        "protected_security",
        "execution_contract",
        "goal_ledger",
        "skill_prompt",
        "workspace_context",
    ]
    ctx = PromptContext(
        goal="Draft architecture specification",
        milestones=({"title": "Outline sections", "status": "in_progress"},),
        active_skill_name="document",
        skill_instructions="Use formal technical structure.",
        materials_brief="Key metric: 99.99% SLA.",
        environment_info={"target_kind": "document"},
    )
    prompt = pipeline.build_system_prompt(ctx)
    assert "ACTIVE SKILL INSTRUCTIONS" in prompt
    assert "Use formal technical structure." in prompt
    assert "WORKSPACE & SESSION CONTEXT" in prompt
    assert "Key metric: 99.99% SLA." in prompt
    assert prompt.index("SECURITY & BOUNDARY DEFENSE") < prompt.index("ACTIVE SKILL INSTRUCTIONS")
    assert prompt.index("ACTIVE SKILL INSTRUCTIONS") < prompt.index("WORKSPACE & SESSION CONTEXT")
