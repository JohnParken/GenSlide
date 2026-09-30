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
async def test_unselected_pending_option_is_not_adopted_when_user_switches_topic(tmp_path):
    skills = writing_skills(tmp_path)
    mem = Memory(
        last_reply="请选择风格：1. 商务正式 2. 轻松幽默",
        pending_options={"1": "商务正式", "2": "轻松幽默"},
    )
    # User switches topic instead of selecting an option
    # Model mistakenly or hallucinatorily suggests adopting "轻松幽默"
    model = Model(
        {
            "effect": "reply",
            "target_kind": "writing",
            "skill_id": "writing",
            "requirement_updates": {"style": "轻松幽默"},
        },
        {"effect": "reply", "reply": "好的，我们聊聊周末去哪玩。"},
    )
    result = await execute(req(message="换个话题，我想聊聊周末去哪玩"), mem, "", model, skills)

    # Verify: style must NOT be adopted as a confirmed requirement!
    assert "style" not in result.memory.requirements


def test_option_selection_negative_and_word_boundary_guards():
    from genslide_agentscope.workflow import _is_option_selected

    options = {"1": "商务正式", "2": "轻松幽默"}

    # 1. Negative intent: "不要选第二个"
    assert not _is_option_selected("不要选第二个", "2", "轻松幽默", options)
    assert not _is_option_selected("别用轻松幽默", "2", "轻松幽默", options)
    assert not _is_option_selected("排除第2项", "2", "轻松幽默", options)

    # 2. English word boundary: "帮我分析API" contains letter 'A'
    assert not _is_option_selected("帮我分析API", "1", "商务正式", options)
    assert not _is_option_selected("探讨RESTful架构", "1", "商务正式", options)

    # 3. Disambiguation: "第一个还是第二个好"
    assert not _is_option_selected("第一个还是第二个好", "1", "商务正式", options)

    # 4. Legitimate affirmative selection
    assert _is_option_selected("选第二个", "2", "轻松幽默", options)
    assert _is_option_selected("用轻松幽默", "2", "轻松幽默", options)
    assert _is_option_selected("选A", "1", "商务正式", {"A": "商务正式", "B": "轻松幽默"})

    # 5. Position-based negative check on letter-keyed options:
    # "不要选第二个" on {"A": ..., "B": ...} must be rejected for B
    letter_options = {"A": "商务正式", "B": "轻松幽默"}
    assert not _is_option_selected("不要选第二个", "B", "轻松幽默", letter_options)
    assert not _is_option_selected("排除第2项", "B", "轻松幽默", letter_options)
    assert _is_option_selected("选第二个", "B", "轻松幽默", letter_options)


@pytest.mark.asyncio
async def test_end_to_end_negation_and_comparison_questions_rejected(tmp_path):
    """Verify that directly mentioning candidate values in negative or comparison contexts never updates requirements."""
    skills = writing_skills(tmp_path)
    mem = Memory(
        last_reply="请选择风格：A. 商务正式 B. 轻松幽默",
        pending_options={"A": "商务正式", "B": "轻松幽默"},
    )

    # 1. Negative intent directly mentioning candidate: "别用轻松幽默"
    model1 = Model(
        {"effect": "reply", "target_kind": "writing", "skill_id": "writing",
         "requirement_updates": {"style": "轻松幽默"}},
        {"effect": "reply", "reply": "好的，不使用轻松幽默。"},
    )
    res1 = await execute(req(message="别用轻松幽默"), mem, "", model1, skills)
    assert "style" not in res1.memory.requirements

    # 2. Comparison question: "商务正式还是轻松幽默哪个好"
    model2 = Model(
        {"effect": "reply", "target_kind": "writing", "skill_id": "writing",
         "requirement_updates": {"style": "轻松幽默"}},
        {"effect": "reply", "reply": "这两个风格各有千秋..."},
    )
    res2 = await execute(req(message="商务正式还是轻松幽默哪个好"), mem, "", model2, skills)
    assert "style" not in res2.memory.requirements

    # 3. Position-based negation on letter options: "不要选第二个"
    model3 = Model(
        {"effect": "reply", "target_kind": "writing", "skill_id": "writing",
         "requirement_updates": {"style": "轻松幽默"}},
        {"effect": "reply", "reply": "好的，排除第二个选项。"},
    )
    res3 = await execute(req(message="不要选第二个"), mem, "", model3, skills)
    assert "style" not in res3.memory.requirements



@pytest.mark.asyncio
async def test_compose_payload_includes_last_reply_for_conversational_continuity(tmp_path):
    skills = writing_skills(tmp_path)
    mem = Memory(last_reply="上次回复：1. 性能优化 2. 内存治理 3. 架构重构")
    model = Model(
        {"effect": "reply", "target_kind": "writing", "skill_id": "writing"},
        {"effect": "reply", "reply": "这是针对内存治理的展开说明。"},
    )
    result = await execute(req(message="展开你上次回复的第二点"), mem, "", model, skills)

    # Verify compose payload in call 1 received last_reply
    compose_payload = model.calls[1]
    assert compose_payload.get("last_reply") == "上次回复：1. 性能优化 2. 内存治理 3. 架构重构"
    assert result.result.get("reply") == "这是针对内存治理的展开说明。"





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


@pytest.mark.asyncio
async def test_dynamic_budget_allocation_with_8000_char_section_and_huge_materials(tmp_path):
    from genslide_agentscope.domain import Section
    from genslide_agentscope.model import encode_payload

    # Target section: 8000 Chinese chars (~24KB)
    large_content = Content(
        title="大型技术规划报告",
        sections=[Section(title="架构详述", body="中" * 8000)],
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

    # Long materials: 20000 Chinese chars (~60KB alone)
    huge_materials = "材" * 20000

    model = Model(
        {
            "effect": "deliverable",
            "target_kind": "writing",
            "skill_id": "writing",
            "needs_full_content": False,
            "edit_scope": ["架构详述"],
            "user_visible_assumptions": [],
        },
        {
            "effect": "deliverable",
            "deliverable": {
                "title": "大型技术规划报告",
                "sections": [{"title": "架构详述", "body": "按截取材料精炼后的架构详述"}],
            },
        },
    )

    res = await execute(req(message="参考材料优化架构详述"), mem, huge_materials, model, skills)
    compose_payload = model.calls[1]

    # Verify total bytes strictly <= 60000 bytes, avoiding 413
    encoded = encode_payload(compose_payload)
    assert len(encoded.encode("utf-8")) <= 58000

    # Verify mandatory user disclosure in assumptions
    assumptions = res.result.get("user_visible_assumptions", [])
    assert any("参考材料已按安全预算做有界截取" in a for a in assumptions)


@pytest.mark.asyncio
async def test_escaping_material_inflation_converges_under_budget(tmp_path):
    from genslide_agentscope.model import encode_payload

    skills = writing_skills(tmp_path)
    mem = Memory()

    # 30,000 characters of heavily escaped text (quotes, backslashes, newlines)
    escaped_materials = '段落："引用内容"，路径：C:\\Program Files\\App。\n' * 1000

    model = Model(
        {"effect": "reply", "target_kind": "writing", "skill_id": "writing"},
        {"effect": "reply", "reply": "已结合材料分析。"},
    )

    res = await execute(req(message="请分析该材料"), mem, escaped_materials, model, skills)
    compose_payload = model.calls[1]

    # Verify that the JSON-encoded payload strictly obeys the 60000 limit and does not trigger 413!
    encoded = encode_payload(compose_payload)
    assert len(encoded.encode("utf-8")) <= 58000



