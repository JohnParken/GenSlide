"""Regression tests for the P1 writing upgrades.

Covers staged skill instructions, batched generation, per-section content revision and
material condensation.
"""
import pytest

from genslide_agentscope.domain import (Content, ExecuteRequest, Memory, ServiceError,
                                        content_hash)
from genslide_agentscope.skills import SkillRegistry
from genslide_agentscope.workflow import GENERATE_BATCH_SECTIONS, execute


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, system, payload):
        self.calls.append((system, payload))
        if not self.responses:
            raise AssertionError(f"unexpected model call: {payload.get('operation')}")
        response = self.responses.pop(0)
        return response(payload) if callable(response) else response


def request(operation, **changes):
    values = dict(engine="agentscope", tenant_id="t", user_id="u", session_id="s", runtime_epoch="e",
                  action_id="a", authorization="x", expected_session_version=0, operation=operation,
                  target_kind="writing", message="A useful guide" if operation == "clarify" else "")
    values.update(changes)
    return ExecuteRequest(**values)


def outline_body(nodes=1):
    titles = [f"Section {index}" for index in range(1, nodes + 1)]
    return {"title": "Guide",
            "nodes": [{"node_id": f"n{index}", "title": title} for index, title in enumerate(titles, 1)]}


def content_body(titles, marker="body"):
    return {"title": "Guide", "sections": [{"title": title, "body": f"{marker} of {title}"} for title in titles]}


def batch_body(titles, marker="body"):
    """Batched generate calls return only the requested sections, without the document title."""
    return {"sections": [{"title": title, "body": f"{marker} of {title}"} for title in titles]}


async def confirmed_outline(skills, nodes=1):
    created = await execute(request("create_outline"), Memory(requirements={"topic": "Guide"}), "",
                            ScriptedModel(outline_body(nodes)), skills)
    draft = created.memory.outline
    confirmed = await execute(request("confirm_outline", draft_id=draft.draft_id,
                                      expected_outline_version=draft.outline_version),
                              created.memory, "", ScriptedModel(), skills)
    return confirmed.memory, draft


def test_skill_manual_is_limited_to_the_generate_stage():
    instructions = SkillRegistry().skills["official-document-skill"]["instructions"]
    assert "Document-Type Decision" not in instructions["clarify"]
    assert "Document-Type Decision" in instructions["generate"]
    # Clarify must be a small intake, not the whole manual.
    assert len(instructions["clarify"]) < len(instructions["generate"]) // 10


@pytest.mark.asyncio
async def test_clarify_prompt_for_the_official_skill_stays_small():
    model = ScriptedModel({"requirements": {"topic": "通知"}, "guidance": {}})
    await execute(request("clarify", message="写个通知", target_kind="document",
                          skill_id="official-document-skill"), Memory(), "", model, SkillRegistry())
    system = model.calls[0][0]
    assert "Document-Type Decision" not in system
    assert len(system.encode()) < 4_000


@pytest.mark.asyncio
async def test_long_documents_generate_in_batches_covering_every_section():
    skills = SkillRegistry()
    memory, draft = await confirmed_outline(skills, nodes=GENERATE_BATCH_SECTIONS + 2)
    titles = [node.title for node in memory.outline.nodes]
    seen = []

    def respond(payload):
        scoped = payload["section_titles"]
        seen.append(list(scoped))
        return batch_body(scoped)

    model = ScriptedModel(respond, respond)
    done = await execute(request("generate", draft_id=draft.draft_id, expected_outline_version=1),
                         memory, "", model, skills)
    assert seen == [titles[:GENERATE_BATCH_SECTIONS], titles[GENERATE_BATCH_SECTIONS:]]
    assert [section.title for section in done.content.sections] == titles


@pytest.mark.asyncio
async def test_a_failed_batch_is_retried_without_rewriting_earlier_batches():
    skills = SkillRegistry()
    memory, draft = await confirmed_outline(skills, nodes=GENERATE_BATCH_SECTIONS + 2)
    titles = [node.title for node in memory.outline.nodes]
    first, second = titles[:GENERATE_BATCH_SECTIONS], titles[GENERATE_BATCH_SECTIONS:]
    seen = []

    def respond(payload):
        scoped = list(payload["section_titles"])
        seen.append(scoped)
        if scoped == second and seen.count(second) == 1:
            return {"sections": [{"title": "wrong title", "body": "x"}]}
        return batch_body(scoped)

    model = ScriptedModel(respond, respond, respond)
    done = await execute(request("generate", draft_id=draft.draft_id, expected_outline_version=1),
                         memory, "", model, skills)
    assert seen == [first, second, second]
    assert [section.title for section in done.content.sections] == titles


@pytest.mark.asyncio
async def test_revise_content_rewrites_only_the_requested_sections():
    skills = SkillRegistry()
    memory, draft = await confirmed_outline(skills, nodes=2)
    titles = [node.title for node in memory.outline.nodes]
    generated = await execute(request("generate", draft_id=draft.draft_id, expected_outline_version=1),
                              memory, "", ScriptedModel(content_body(titles)), skills)
    original = generated.memory.content_hash
    submitted = content_body(titles)

    revised = await execute(request("revise_content", draft_id=draft.draft_id, expected_outline_version=1,
                                    message="tighten section 2", content=submitted,
                                    expected_content_hash=original, section_titles=[titles[1]]),
                            generated.memory, "", ScriptedModel({"sections": [
                                {"title": titles[1], "body": "tightened"}]}), skills)
    assert revised.content.sections[0].body == f"body of {titles[0]}"
    assert revised.content.sections[1].body == "tightened"
    assert revised.memory.content_hash != original
    assert revised.memory.guidance.stage == "generated"
    assert "revise_content" in revised.memory.guidance.next_actions


@pytest.mark.asyncio
async def test_revise_content_rejects_stale_or_unscoped_drafts():
    skills = SkillRegistry()
    memory, draft = await confirmed_outline(skills, nodes=2)
    titles = [node.title for node in memory.outline.nodes]
    generated = await execute(request("generate", draft_id=draft.draft_id, expected_outline_version=1),
                              memory, "", ScriptedModel(content_body(titles)), skills)
    submitted = content_body(titles)
    values = dict(draft_id=draft.draft_id, expected_outline_version=1, message="tighten",
                  content=submitted, expected_content_hash=generated.memory.content_hash)

    with pytest.raises(ServiceError, match="CONTENT_REQUIRED"):
        # An outline is confirmed but nothing has been generated yet.
        await execute(request("revise_content", **{**values, "content": None}),
                      memory, "", ScriptedModel(), skills)
    with pytest.raises(ServiceError, match="CONTENT_VERSION_CONFLICT"):
        await execute(request("revise_content", **{**values, "expected_content_hash": "stale"}),
                      generated.memory, "", ScriptedModel(), skills)
    with pytest.raises(ServiceError, match="UNKNOWN_SECTION"):
        await execute(request("revise_content", **{**values, "section_titles": ["No such section"]}),
                      generated.memory, "", ScriptedModel(), skills)
    with pytest.raises(ServiceError, match="REVISION_INSTRUCTION_REQUIRED"):
        await execute(request("revise_content", **{**values, "message": "   "}),
                      generated.memory, "", ScriptedModel(), skills)


@pytest.mark.asyncio
async def test_oversize_materials_are_condensed_instead_of_failing():
    skills = SkillRegistry()
    memory, draft = await confirmed_outline(skills, nodes=1)
    titles = [node.title for node in memory.outline.nodes]
    condensed = []

    def respond(payload):
        if payload["operation"] == "material_brief":
            return {"facts": ["事实一", "事实二"]}
        condensed.append(payload["materials"])
        return content_body(titles)

    # 40k Chinese characters is ~120 KB, far past the 60 KB model payload budget, and is
    # condensed in 15k-character chunks: three brief calls plus the single generate call.
    model = ScriptedModel(respond, respond, respond, respond)
    done = await execute(request("generate", draft_id=draft.draft_id, expected_outline_version=1),
                         memory, "测" * 40_000, model, skills)
    assert model.calls[0][1]["operation"] == "material_brief"
    assert sum(1 for _, payload in model.calls if payload["operation"] == "material_brief") == 3
    assert len(condensed) == 1
    assert condensed[0].startswith("- 事实一\n- 事实二")
    assert len(condensed[0]) < 1_000  # a condensed brief, not the 40k-character original
    assert done.content.sections[0].title == titles[0]


def test_content_hash_is_stable_and_order_sensitive():
    body = Content.model_validate(content_body(["A", "B"]))
    assert content_hash(body) == content_hash(Content.model_validate(content_body(["A", "B"])))
    assert content_hash(body) != content_hash(Content.model_validate(content_body(["B", "A"])))


@pytest.mark.asyncio
async def test_outline_section_count_is_corrected_when_the_model_overshoots():
    skills = SkillRegistry()
    memory = Memory(requirements={"topic": "Guide", "length": "约3000字"})

    def outline_with(count):
        return {"title": "Guide",
                "nodes": [{"node_id": f"n{i}", "title": f"S{i}"} for i in range(1, count + 1)]}

    model = ScriptedModel(outline_with(9), outline_with(3))
    work = await execute(request("create_outline"), memory, "", model, skills)
    # A 3000-character target implies 2-5 sections, so the 9-section answer is rejected once.
    assert len(model.calls) == 2
    assert "does not match the confirmed length" in model.calls[1][0]
    assert len(work.memory.outline.nodes) == 3


@pytest.mark.asyncio
async def test_batches_carry_their_own_share_of_the_length_budget():
    skills = SkillRegistry()
    memory = Memory(requirements={"topic": "Guide", "length": "4000字"})
    created = await execute(request("create_outline"), memory, "", ScriptedModel(outline_body(6)), skills)
    draft = created.memory.outline
    confirmed = await execute(request("confirm_outline", draft_id=draft.draft_id,
                                      expected_outline_version=1), created.memory, "", ScriptedModel(), skills)
    model = ScriptedModel(lambda payload: batch_body(payload["section_titles"]),
                          lambda payload: batch_body(payload["section_titles"]))
    await execute(request("generate", draft_id=draft.draft_id, expected_outline_version=1),
                  confirmed.memory, "", model, skills)
    assert len(model.calls) == 2
    # Each batch is told its own share of the total length, not just the whole-document budget.
    assert all("This batch of" in system for system, _ in model.calls)


def test_explicit_length_is_read_from_the_users_own_wording():
    from genslide_agentscope.workflow import explicit_length
    assert explicit_length("写一份报告，约3000字，中文") == "3000字"
    assert explicit_length("需要 5000-8000字 的篇幅") == "5000-8000字"
    assert explicit_length("大约1.5万字") == "1.5万字"
    # Non-length numbers must not be mistaken for a length budget.
    assert explicit_length("2026年微服务与云原生架构演进报告") is None
    assert explicit_length("做成3页的PPT") is None
    assert explicit_length("") is None


@pytest.mark.asyncio
async def test_clarify_adopts_a_length_the_model_left_out():
    model = ScriptedModel({"requirements": {"topic": "报告"}, "guidance": {}})
    work = await execute(request("clarify", message="写一份报告，约3000字"), Memory(), "", model,
                         SkillRegistry())
    assert work.memory.requirements["length"] == "3000字"
