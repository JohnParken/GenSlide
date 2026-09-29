import pytest
from pathlib import Path
from genslide_agentscope.domain import Content, ExecuteRequest, Memory, content_hash
from genslide_agentscope.skills import SkillRegistry
from genslide_agentscope.workflow import execute
def req(**kw):
    d=dict(engine="agentscope",tenant_id="t",user_id="u",session_id="s",runtime_epoch="e",action_id="a",authorization="x",expected_session_version=0,expected_lifecycle_version=1,mode="assistant",requested_output="text",message="Guide"); d.update(kw); return ExecuteRequest(**d)
def content(): return {"title":"Guide","sections":[{"title":"Intro","body":"Text"},{"title":"Steps","body":"More"}]}
class Model:
    def __init__(self,*r): self.responses=list(r); self.calls=[]
    async def complete(self,system,payload): self.calls.append(payload); return self.responses.pop(0)
def writing_skills(tmp_path):
    skill = Path(tmp_path) / "writing" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("---\nname: writing\ndescription: Test writing skill\nmetadata:\n  target_kind: writing\n---\nTest instructions", encoding="utf-8")
    return SkillRegistry(Path(tmp_path))
@pytest.mark.asyncio
async def test_direct_drafting_returns_deliverable_without_outline_confirmation(tmp_path):
    model=Model({"effect":"deliverable","target_kind":"writing","skill_id":"writing"},{"effect":"deliverable","deliverable":content()})
    result=await execute(req(message="Write it now"),Memory(),"",model,writing_skills(tmp_path)); assert result.content.title=="Guide" and result.memory.content_hash==content_hash(result.content)
@pytest.mark.asyncio
async def test_local_edit_payload_contains_exact_scope_and_preserves_other_sections(tmp_path):
    original=Content.model_validate(content()); skills=writing_skills(tmp_path); memory=Memory(content=original,content_hash=content_hash(original),target_kind="writing",skill_id="writing",skill_version=skills.skills["writing"]["version"],skill_hash=skills.skills["writing"]["hash"])
    model=Model({"effect":"deliverable","target_kind":"writing","skill_id":"writing","needs_full_content":True,"edit_scope":["Steps"]},{"effect":"deliverable","deliverable":{"title":"Guide","sections":[{"title":"Steps","body":"Updated"}]}})
    result=await execute(req(message="Update Steps"),memory,"",model,skills); assert model.calls[1]["decision"]["edit_scope"]==["Steps"] and result.content.sections[0].body=="Text" and result.content.sections[1].body=="Updated"

@pytest.mark.asyncio
async def test_reply_does_not_create_file_content(tmp_path):
    model = Model({"effect": "reply", "target_kind": "writing", "skill_id": "writing"},
                  {"effect": "reply", "reply": "Need more context."})
    result = await execute(req(), Memory(), "", model, writing_skills(tmp_path))
    assert result.effect == "reply"
    assert result.content is None
    assert result.deliverable is None


@pytest.mark.asyncio
async def test_multi_turn_continuity_with_pending_options(tmp_path):
    from genslide_agentscope.domain import snapshot_from_memory, memory_from_snapshot

    skills = writing_skills(tmp_path)
    # Turn 1: Memory holds pending options from previous turn
    mem1 = Memory(
        last_reply="请选择风格：1. 商务正式 2. 轻松幽默",
        pending_options={"1": "商务正式", "2": "轻松幽默"},
    )
    # User says "选第二个"
    model = Model(
        {"effect": "reply", "target_kind": "writing", "skill_id": "writing",
         "requirement_updates": {"style": "轻松幽默"}},
        {"effect": "reply", "reply": "已采用轻松幽默风格。"},
    )
    result = await execute(req(message="选第二个"), mem1, "", model, skills)
    assert result.memory.requirements.get("style") == "轻松幽默"
    assert "轻松幽默" not in result.memory.pending_options.values()

    # Snapshot round-trip preserves state
    snap = snapshot_from_memory(result.memory)
    restored = memory_from_snapshot(snap)
    assert restored.requirements.get("style") == "轻松幽默"


@pytest.mark.asyncio
async def test_large_content_local_edit_does_not_blow_context_budget(tmp_path):
    from genslide_agentscope.domain import Section
    from genslide_agentscope.model import encode_payload

    # Create 3 sections with 8000 Chinese characters each (total 24000 characters ~ 72KB)
    large_content = Content(
        title="大型技术规划报告",
        sections=[
            Section(title=f"第{i}章 详尽分析", body="中" * 8000)
            for i in range(1, 4)
        ]
    )
    skills = writing_skills(tmp_path)
    mem = Memory(
        content=large_content,
        content_hash=content_hash(large_content),
        target_kind="writing",
        skill_id="writing",
        skill_version=skills.skills["writing"]["version"],
        skill_hash=skills.skills["writing"]["hash"],
    )

    # Local revision on only Chapter 2
    model = Model(
        {
            "effect": "deliverable",
            "target_kind": "writing",
            "skill_id": "writing",
            "needs_full_content": False,
            "edit_scope": ["第2章 详尽分析"],
        },
        {
            "effect": "deliverable",
            "deliverable": {
                "title": "大型技术规划报告",
                "sections": [{"title": "第2章 详尽分析", "body": "更新后的第2章内容"}],
            },
        },
    )

    res = await execute(req(message="更新第2章内容"), mem, "", model, skills)
    compose_payload = model.calls[1]

    # Verify that the compose payload only transmitted Chapter 2
    scoped = compose_payload["current_content"]
    assert len(scoped["sections"]) == 1
    assert scoped["sections"][0]["title"] == "第2章 详尽分析"

    # Verify payload encodes strictly within the 60000 byte budget!
    encoded = encode_payload(compose_payload)
    assert len(encoded.encode("utf-8")) < 60000

    # Verify other chapters are preserved intact
    assert res.content.sections[0].body == "中" * 8000
    assert res.content.sections[1].body == "更新后的第2章内容"
    assert res.content.sections[2].body == "中" * 8000

