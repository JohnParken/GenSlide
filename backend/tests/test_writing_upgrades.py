import pytest
from pathlib import Path
from genslide_agentscope.authoring import _is_option_selected
from genslide_agentscope.domain import Content, ExecuteRequest, Memory, Section, content_hash, snapshot_from_memory, memory_from_snapshot
from genslide_agentscope.engine import Engine
from genslide_agentscope.model import encode_payload
from genslide_agentscope.skills import SkillRegistry


def req(**kw):
    d = dict(
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
    d.update(kw)
    return ExecuteRequest(**d)


def content():
    return {"title": "Guide", "sections": [{"title": "Intro", "body": "Text"}, {"title": "Steps", "body": "More"}]}


class Model:
    def __init__(self, *r):
        self.responses = list(r)
        self.calls = []

    async def complete(self, system, payload):
        self.calls.append(payload)
        return self.responses.pop(0)


def writing_skills(tmp_path):
    skill = Path(tmp_path) / "writing" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        "---\nname: writing\ndescription: Test writing skill\nmetadata:\n  target_kind: writing\n---\nTest instructions",
        encoding="utf-8",
    )
    return SkillRegistry(Path(tmp_path))


async def run_engine(request, memory, materials, model, skills):
    engine = Engine(model)
    engine.skills = skills
    return await engine.run("k", request, memory, materials)


@pytest.mark.asyncio
async def test_direct_drafting_returns_deliverable_without_outline_confirmation(tmp_path):
    model = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "deliverable",
                "target_kind": "writing",
                "skill_id": "writing",
                "deliverable": content(),
            },
        }
    )
    result = await run_engine(req(message="Write it now"), Memory(), "", model, writing_skills(tmp_path))
    assert result.content.title == "Guide" and result.memory.content_hash == content_hash(result.content)


@pytest.mark.asyncio
async def test_local_edit_payload_contains_exact_scope_and_preserves_other_sections(tmp_path):
    original = Content.model_validate(content())
    skills = writing_skills(tmp_path)
    memory = Memory(
        content=original,
        content_hash=content_hash(original),
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
                "edit_scope": ["Steps"],
                "deliverable": {"title": "Guide", "sections": [{"title": "Steps", "body": "Updated"}]},
            },
        }
    )
    result = await run_engine(req(message="Update Steps"), memory, "", model, skills)
    assert result.content.sections[0].body == "Text" and result.content.sections[1].body == "Updated"


@pytest.mark.asyncio
async def test_reply_does_not_create_file_content(tmp_path):
    model = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "reply": "Need more context.",
            },
        }
    )
    result = await run_engine(req(), Memory(), "", model, writing_skills(tmp_path))
    assert result.effect == "reply"
    assert result.content is None
    assert result.deliverable is None


@pytest.mark.asyncio
async def test_multi_turn_continuity_with_pending_options(tmp_path):
    skills = writing_skills(tmp_path)
    mem1 = Memory(
        last_reply="请选择风格：1. 商务正式 2. 轻松幽默",
        pending_options={"1": "商务正式", "2": "轻松幽默"},
    )
    model = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "requirement_updates": {"style": "轻松幽默"},
                "reply": "已采用轻松幽默风格。",
            },
        }
    )
    result = await run_engine(req(message="选第二个"), mem1, "", model, skills)
    assert result.memory.requirements.get("style") == "轻松幽默"
    assert "轻松幽默" not in result.memory.pending_options.values()

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
    model = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "requirement_updates": {"style": "轻松幽默"},
                "reply": "好的，我们聊聊周末去哪玩。",
            },
        }
    )
    result = await run_engine(req(message="换个话题，我想聊聊周末去哪玩"), mem, "", model, skills)
    assert "style" not in result.memory.requirements


def test_option_selection_negative_and_word_boundary_guards():
    options = {"1": "商务正式", "2": "轻松幽默"}

    assert not _is_option_selected("不要选第二个", "2", "轻松幽默", options)
    assert not _is_option_selected("别用轻松幽默", "2", "轻松幽默", options)
    assert not _is_option_selected("排除第2项", "2", "轻松幽默", options)

    assert not _is_option_selected("帮我分析API", "1", "商务正式", options)
    assert not _is_option_selected("探讨RESTful架构", "1", "商务正式", options)

    assert not _is_option_selected("第一个还是第二个好", "1", "商务正式", options)

    assert _is_option_selected("选第二个", "2", "轻松幽默", options)
    assert _is_option_selected("用轻松幽默", "2", "轻松幽默", options)
    assert _is_option_selected("选A", "1", "商务正式", {"A": "商务正式", "B": "轻松幽默"})

    letter_options = {"A": "商务正式", "B": "轻松幽默"}
    assert not _is_option_selected("不要选第二个", "B", "轻松幽默", letter_options)
    assert not _is_option_selected("排除第2项", "B", "轻松幽默", letter_options)
    assert _is_option_selected("选第二个", "B", "轻松幽默", letter_options)

    assert not _is_option_selected("不要选B", "B", "轻松幽默", letter_options)
    assert not _is_option_selected("别选B项", "B", "轻松幽默", letter_options)
    assert not _is_option_selected("不要B", "B", "轻松幽默", letter_options)
    assert _is_option_selected("选B", "B", "轻松幽默", letter_options)


@pytest.mark.asyncio
async def test_end_to_end_negation_and_comparison_questions_rejected(tmp_path):
    skills = writing_skills(tmp_path)
    mem = Memory(
        last_reply="请选择风格：A. 商务正式 B. 轻松幽默",
        pending_options={"A": "商务正式", "B": "轻松幽默"},
    )

    model1 = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "requirement_updates": {"style": "轻松幽默"},
                "reply": "好的，不使用轻松幽默。",
            },
        }
    )
    res1 = await run_engine(req(message="别用轻松幽默"), mem, "", model1, skills)
    assert "style" not in res1.memory.requirements

    model2 = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "requirement_updates": {"style": "轻松幽默"},
                "reply": "这两个风格各有千秋...",
            },
        }
    )
    res2 = await run_engine(req(message="商务正式还是轻松幽默哪个好"), mem, "", model2, skills)
    assert "style" not in res2.memory.requirements

    model3 = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "requirement_updates": {"style": "轻松幽默"},
                "reply": "好的，排除第二个选项。",
            },
        }
    )
    res3 = await run_engine(req(message="不要选第二个"), mem, "", model3, skills)
    assert "style" not in res3.memory.requirements

    model4 = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "requirement_updates": {"style": "轻松幽默"},
                "reply": "好的，排除B选项。",
            },
        }
    )
    res4 = await run_engine(req(message="不要选B"), mem, "", model4, skills)
    assert "style" not in res4.memory.requirements


@pytest.mark.asyncio
async def test_explicit_directive_and_topic_comparison_accepted(tmp_path):
    skills = writing_skills(tmp_path)
    mem = Memory(
        last_reply="请选择风格：A. 商务正式 B. 轻松幽默",
        pending_options={"A": "商务正式", "B": "轻松幽默"},
    )

    model1 = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "requirement_updates": {"topic": "人工智能与传统算法的对比"},
                "reply": "好的，已记录主题。",
            },
        }
    )
    res1 = await run_engine(req(message="写一篇人工智能与传统算法的对比"), mem, "", model1, skills)
    assert res1.memory.requirements.get("topic") == "人工智能与传统算法的对比"

    model2 = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "requirement_updates": {"style": "轻松幽默"},
                "reply": "好的，采用轻松幽默风格。",
            },
        }
    )
    res2 = await run_engine(req(message="还是用轻松幽默吧"), mem, "", model2, skills)
    assert res2.memory.requirements.get("style") == "轻松幽默"


@pytest.mark.asyncio
async def test_analysis_comparison_question_is_not_adopted_as_requirement(tmp_path):
    skills = writing_skills(tmp_path)
    mem = Memory(
        last_reply="请选择风格：1. 商务正式 2. 轻松幽默",
        pending_options={"1": "商务正式", "2": "轻松幽默"},
    )
    model = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "requirement_updates": {"style": "轻松幽默"},
                "reply": "商务正式和轻松幽默的对比如下...",
            },
        }
    )
    res = await run_engine(req(message="请分析商务正式和轻松幽默的对比，哪个更适合？"), mem, "", model, skills)
    assert "style" not in res.memory.requirements


@pytest.mark.asyncio
async def test_length_inquiry_does_not_overwrite_existing_length(tmp_path):
    skills = writing_skills(tmp_path)
    mem = Memory(requirements={"length": "800字"})
    model = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "reply": "500字和1000字各有优劣...",
            },
        }
    )
    res = await run_engine(req(message="文章写500字还是1000字更合适？"), mem, "", model, skills)
    assert res.memory.requirements.get("length") == "800字"


@pytest.mark.asyncio
async def test_compound_sentence_inquiry_on_one_field_does_not_block_other_clear_fields(tmp_path):
    skills = writing_skills(tmp_path)
    mem = Memory(
        last_reply="请选择风格：1. 商务正式 2. 轻松幽默",
        pending_options={"1": "商务正式", "2": "轻松幽默"},
    )
    model = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "requirement_updates": {"audience": "高中生", "style": "轻松幽默"},
                "reply": "好的，针对高中生群体分析风格...",
            },
        }
    )
    res = await run_engine(req(message="写一篇面向高中生的科普文章，商务正式还是轻松幽默哪个好？"), mem, "", model, skills)
    assert res.memory.requirements.get("audience") == "高中生"
    assert "style" not in res.memory.requirements


@pytest.mark.asyncio
async def test_polite_task_request_and_directive_are_accepted(tmp_path):
    skills = writing_skills(tmp_path)
    mem = Memory(
        last_reply="请选择风格：1. 商务正式 2. 轻松幽默",
        pending_options={"1": "商务正式", "2": "轻松幽默"},
    )

    model1 = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "requirement_updates": {"topic": "人工智能与传统算法的对比"},
                "reply": "没问题，可以为您写这篇对比文章。",
            },
        }
    )
    res1 = await run_engine(req(message="请写一篇人工智能与传统算法的对比，可以吗？"), mem, "", model1, skills)
    assert res1.memory.requirements.get("topic") == "人工智能与传统算法的对比"

    model2 = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "requirement_updates": {"style": "轻松幽默"},
                "reply": "好的，采用轻松幽默风格。",
            },
        }
    )
    res2 = await run_engine(req(message="还是用轻松幽默好了"), mem, "", model2, skills)
    assert res2.memory.requirements.get("style") == "轻松幽默"


@pytest.mark.asyncio
async def test_large_skill_catalog_pruned_under_model_context_budget(tmp_path):
    class StrictBudgetModel:
        def __init__(self):
            self.pruned_catalog_len = 0

        async def complete(self, prompt, payload):
            encode_payload(payload)
            self.pruned_catalog_len = len(payload.get("skills", []))
            return {
                "action": "final_reply",
                "action_input": {
                    "effect": "reply",
                    "target_kind": "writing",
                    "skill_id": "skill_0",
                    "reply": "已为您成功选择技能。",
                },
            }

    reg = SkillRegistry.__new__(SkillRegistry)
    reg.skills = {}
    for i in range(24):
        reg.skills[f"skill_{i}"] = {
            "skill_id": f"skill_{i}",
            "name": f"Skill {i}",
            "description": "这是详细的专业写作领域技能描述说明。" * 60,
            "target_kind": "writing",
            "supported_outputs": ["text"],
            "default_output": "text",
            "priority": 100 - i,
            "content": "技能提示词正文",
            "version": "1.0",
            "hash": f"hash_{i}",
        }

    model = StrictBudgetModel()
    res = await run_engine(req(message="你好"), Memory(), "", model, reg)
    assert res.memory.skill_id == "skill_0"
    assert model.pruned_catalog_len > 0


@pytest.mark.asyncio
async def test_payload_includes_last_reply_for_conversational_continuity(tmp_path):
    skills = writing_skills(tmp_path)
    mem = Memory(last_reply="上次回复：1. 性能优化 2. 内存治理 3. 架构重构")
    model = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "reply": "这是针对内存治理的展开说明。",
            },
        }
    )
    result = await run_engine(req(message="展开你上次回复的第二点"), mem, "", model, skills)

    turn_payload = model.calls[0]
    assert turn_payload.get("last_reply") == "上次回复：1. 性能优化 2. 内存治理 3. 架构重构"
    assert result.result.get("reply") == "这是针对内存治理的展开说明。"


@pytest.mark.asyncio
async def test_large_content_local_edit_does_not_blow_context_budget(tmp_path):
    large_content = Content(
        title="大型技术规划报告",
        sections=[Section(title=f"第{i}章 详尽分析", body="中" * 8000) for i in range(1, 4)],
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

    model = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "deliverable",
                "target_kind": "writing",
                "skill_id": "writing",
                "edit_scope": ["第2章 详尽分析"],
                "deliverable": {
                    "title": "大型技术规划报告",
                    "sections": [{"title": "第2章 详尽分析", "body": "更新后的第2章内容"}],
                },
            },
        }
    )

    res = await run_engine(req(message="更新第2章内容"), mem, "", model, skills)
    turn_payload = model.calls[0]

    scoped = turn_payload["current_content"]
    assert len(scoped["sections"]) == 1
    assert scoped["sections"][0]["title"] == "第2章 详尽分析"

    encoded = encode_payload(turn_payload)
    assert len(encoded.encode("utf-8")) < 60000

    assert res.content.sections[0].body == "中" * 8000
    assert res.content.sections[1].body == "更新后的第2章内容"
    assert res.content.sections[2].body == "中" * 8000


@pytest.mark.asyncio
async def test_dynamic_budget_allocation_with_8000_char_section_and_huge_materials(tmp_path):
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

    huge_materials = "材" * 20000

    model = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "deliverable",
                "target_kind": "writing",
                "skill_id": "writing",
                "edit_scope": ["架构详述"],
                "deliverable": {
                    "title": "大型技术规划报告",
                    "sections": [{"title": "架构详述", "body": "按截取材料精炼后的架构详述"}],
                },
            },
        }
    )

    res = await run_engine(req(message="参考材料优化架构详述"), mem, huge_materials, model, skills)
    turn_payload = model.calls[0]

    encoded = encode_payload(turn_payload)
    assert len(encoded.encode("utf-8")) <= 58000

    assumptions = res.result.get("user_visible_assumptions", [])
    assert any("参考材料已按安全预算做有界截取" in a for a in assumptions)


@pytest.mark.asyncio
async def test_escaping_material_inflation_converges_under_budget(tmp_path):
    skills = writing_skills(tmp_path)
    mem = Memory()

    escaped_materials = '段落："引用内容"，路径：C:\\Program Files\\App。\n' * 1000

    model = Model(
        {
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "target_kind": "writing",
                "skill_id": "writing",
                "reply": "已结合材料分析。",
            },
        }
    )

    await run_engine(req(message="请分析该材料"), mem, escaped_materials, model, skills)
    turn_payload = model.calls[0]

    encoded = encode_payload(turn_payload)
    assert len(encoded.encode("utf-8")) <= 58000
