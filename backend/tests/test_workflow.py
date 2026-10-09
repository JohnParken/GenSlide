import pytest
from pathlib import Path
from genslide_agentscope.authoring import length_target
from genslide_agentscope.domain import Content, ExecuteRequest, Memory, ServiceError, content_hash, snapshot_from_memory, memory_from_snapshot
from genslide_agentscope.engine import Engine
from genslide_agentscope.skills import SkillRegistry


class Model:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, system, payload):
        self.calls.append((system, payload))
        return self.responses.pop(0)


def req(**changes):
    base = dict(
        engine="agentscope",
        tenant_id="t",
        user_id="u",
        session_id="s",
        runtime_epoch="e",
        action_id="a",
        authorization="x",
        expected_session_version=0,
        expected_lifecycle_version=1,
        mode="assistant",
        requested_output="text",
        message="Guide",
    )
    base.update(changes)
    return ExecuteRequest(**base)


def body(title="Guide", sections=("Intro",)):
    return {"title": title, "sections": [{"title": s, "body": "Text"} for s in sections]}


def writing_skills(tmp_path):
    skill = Path(tmp_path) / "writing" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        "---\nname: writing\ndescription: Test writing skill\nmetadata:\n  target_kind: writing\n---\nTest instructions",
        encoding="utf-8",
    )
    return SkillRegistry(Path(tmp_path))


@pytest.mark.asyncio
async def test_engine_executes_unified_react_lifecycle_with_materials(tmp_path):
    model = Model(
        {
            "thought": "Draft deliverable using materials.",
            "action": "final_reply",
            "action_input": {
                "effect": "deliverable",
                "target_kind": "writing",
                "skill_id": "writing",
                "deliverable": body(),
            },
        }
    )
    engine = Engine(model)
    engine.skills = writing_skills(tmp_path)
    result = await engine.run("k", req(), Memory(), "PRIVATE")
    assert len(model.calls) == 1
    assert model.calls[0][1]["materials"] == "PRIVATE"
    assert result.effect == "deliverable"
    assert result.memory.ledger is not None


@pytest.mark.asyncio
async def test_engine_multi_step_react_with_plan_tool_and_ledger_snapshot_persistence(tmp_path):
    """Verify multi-step ReAct loop (plan_tasks -> call_tool -> update_task -> final_reply) and GoalLedger snapshot round-trip."""
    async def tool_handler(name, params):
        if name == "search_facts":
            return {"revenue": "120M"}
        return "ok"

    class MultiStepModel:
        def __init__(self):
            self.step = 0
            self.calls = []

        async def complete(self, system, payload):
            self.calls.append((system, payload))
            self.step += 1
            if self.step == 1:
                return {
                    "thought": "Plan subtasks first.",
                    "action": "plan_tasks",
                    "action_input": {"tasks": [{"title": "Fetch revenue data"}]},
                }
            if self.step == 2:
                return {
                    "thought": "Call tool to fetch data.",
                    "action": "call_tool",
                    "action_input": {"tool_name": "search_facts", "parameters": {"q": "Q4"}},
                }
            if self.step == 3:
                # Extract task_id from history observation or prompt
                hist = payload["history"]
                obs = hist[0]["observation"]
                task_id = obs.split("['")[1].split("']")[0]
                return {
                    "thought": "Mark task completed.",
                    "action": "update_task",
                    "action_input": {"task_id": task_id, "status": "completed", "summary": "Revenue 120M"},
                }
            return {
                "thought": "All tasks completed, deliver final document.",
                "action": "final_reply",
                "action_input": {
                    "effect": "deliverable",
                    "target_kind": "writing",
                    "skill_id": "writing",
                    "reply": "已完成调研并生成报告",
                    "deliverable": body(title="Q4 Report", sections=("Revenue",)),
                },
            }

    model = MultiStepModel()
    engine = Engine(model, tool_handler=tool_handler)
    engine.skills = writing_skills(tmp_path)
    result = await engine.run("k", req(message="调研Q4数据并写报告"), Memory(), "")

    assert result.effect == "deliverable"
    assert result.result["react_iterations"] == 4
    assert result.memory.ledger is not None
    assert result.memory.ledger.is_all_completed()

    # Verify GoalLedger survives BFF snapshot round-trip
    snap = snapshot_from_memory(result.memory)
    restored = memory_from_snapshot(snap)
    assert restored.ledger is not None
    assert restored.ledger.is_all_completed()
    assert restored.ledger.tasks[0].title == "Fetch revenue data"


@pytest.mark.asyncio
async def test_local_edit_scope_replaces_only_named_section_and_hashes_snapshot(tmp_path):
    current = Content.model_validate(body(sections=("Intro", "Details")))
    skills = writing_skills(tmp_path)
    memory = Memory(
        content=current,
        content_hash=content_hash(current),
        target_kind="writing",
        skill_id="writing",
        skill_version=skills.skills["writing"]["version"],
        skill_hash=skills.skills["writing"]["hash"],
    )
    model = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "deliverable",
                "target_kind": "writing",
                "skill_id": "writing",
                "edit_scope": ["Details"],
                "deliverable": body(sections=("Details",)),
            },
        }
    )
    engine = Engine(model)
    engine.skills = skills
    result = await engine.run("k", req(message="tighten Details"), memory, "")
    assert [s.title for s in result.content.sections] == ["Intro", "Details"] and result.content.sections[0].body == "Text"
    assert result.memory.content_hash == content_hash(result.content)


@pytest.mark.parametrize("text, expected", [("约3000字", 3000), ("5000-8000字", 6500), ("3页", None)])
def test_length_requirement_parses(text, expected):
    assert length_target({"length": text}) == expected


@pytest.mark.asyncio
async def test_invalid_edit_scope_is_rejected(tmp_path):
    model = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "deliverable",
                "target_kind": "writing",
                "skill_id": "writing",
                "edit_scope": ["Missing"],
                "deliverable": body(sections=("Missing",)),
            },
        }
    )
    engine = Engine(model)
    engine.skills = writing_skills(tmp_path)
    with pytest.raises(ServiceError, match="EDIT_SCOPE_INVALID"):
        await engine.run("k", req(), Memory(content=body(), target_kind="writing"), "")


@pytest.mark.asyncio
async def test_explicit_output_intent_mismatch_is_rejected():
    model = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "deliverable",
                "target_kind": "writing",
                "skill_id": "document",
                "deliverable": body(),
            },
        }
    )
    engine = Engine(model)
    with pytest.raises(ServiceError, match="OUTPUT_INTENT_MISMATCH"):
        await engine.run("k", req(requested_output="document"), Memory(), "")
