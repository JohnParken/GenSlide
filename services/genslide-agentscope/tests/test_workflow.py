import pytest

from genslide_agentscope.domain import ExecuteRequest, Memory, ServiceError
from genslide_agentscope.engine import Engine
from genslide_agentscope.skills import SkillRegistry
from genslide_agentscope.workflow import execute


class ScriptedModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, system, payload):
        self.calls.append((system, payload))
        return self.responses.pop(0)


def request(operation, **changes):
    values = dict(engine="agentscope", tenant_id="tenant-a", user_id="user-a",
                  session_id="session-a", runtime_epoch="epoch-a", action_id="action-1",
                  authorization="test", expected_session_version=0, operation=operation,
                  target_kind="writing", message="A useful guide" if operation == "clarify" else "")
    values.update(changes)
    return ExecuteRequest(**values)


def discussion():
    return {"requirements": {"topic": "A useful guide"}, "guidance": {
        "stage": "clarify", "questions": [], "proposals": [], "next_actions": ["clarify"]},
        "answer": "I have enough context."}


def outline(title="A useful guide"):
    return {"title": title, "nodes": [{"node_id": "intro", "title": "Introduction"},
                                         {"node_id": "steps", "title": "Practical steps"}]}


def generated(title="A useful guide"):
    return {"title": title, "sections": [
        {"title": "Introduction", "body": "A clear introduction."},
        {"title": "Practical steps", "body": "Useful steps."}]}


@pytest.mark.asyncio
async def test_guided_writing_flow_and_materials_stay_out_of_memory_prompts():
    model = ScriptedModel(discussion(), outline(), outline("A useful guide, revised"),
                          generated("A useful guide, revised"))
    skills = SkillRegistry()
    first = await execute(request("clarify"), Memory(), "PRIVATE ATTACHMENT", model, skills)
    assert "PRIVATE ATTACHMENT" not in str(model.calls[0][1])
    created = await execute(request("create_outline"), first.memory, "", model, skills)
    revised = await execute(request("revise_outline", draft_id=created.memory.outline.draft_id,
                                   expected_outline_version=1), created.memory, "", model, skills)
    assert revised.memory.outline.outline_version == 2
    assert revised.memory.outline.confirmed_hash is None
    confirm_model = ScriptedModel()
    confirmed = await execute(request("confirm_outline", draft_id=revised.memory.outline.draft_id,
                                      expected_outline_version=2), revised.memory, "", confirm_model, skills)
    assert confirm_model.calls == []
    done = await execute(request("generate", draft_id=revised.memory.outline.draft_id,
                                 expected_outline_version=2), confirmed.memory, "CURRENT FILE",
                         model, skills)
    assert done.content.title == "A useful guide, revised"
    assert model.calls[-1][1]["materials"] == "CURRENT FILE"


@pytest.mark.asyncio
async def test_workflow_rejects_unconfirmed_stale_wrong_target_and_forbidden_updates():
    model = ScriptedModel(outline())
    skills = SkillRegistry()
    created = await execute(request("create_outline"), Memory(requirements={"topic": "Guide"}), "", model, skills)
    kwargs = dict(draft_id=created.memory.outline.draft_id, expected_outline_version=1)
    with pytest.raises(ServiceError, match="OUTLINE_NOT_CONFIRMED"):
        await execute(request("generate", **kwargs), created.memory, "", ScriptedModel(), skills)
    with pytest.raises(ServiceError, match="OUTLINE_VERSION_CONFLICT"):
        await execute(request("confirm_outline", draft_id=kwargs["draft_id"], expected_outline_version=2),
                      created.memory, "", ScriptedModel(), skills)
    with pytest.raises(ServiceError, match="TARGET_KIND_MISMATCH"):
        await execute(request("confirm_outline", target_kind="document", **kwargs), created.memory,
                      "", ScriptedModel(), skills)
    with pytest.raises(ServiceError, match="EXPLICIT_REVISION_REQUIRED"):
        await execute(request("confirm_outline", requirement_updates={"style": "formal"}, **kwargs),
                      created.memory, "", ScriptedModel(), skills)


@pytest.mark.asyncio
async def test_engine_hides_pending_memory_until_publish_and_isolates_tenant_keys():
    engine = Engine(ScriptedModel(discussion()))
    req = request("clarify")
    key = req.session_key()
    other_tenant = request("clarify", tenant_id="tenant-b").session_key()
    work = await engine.run(key, req, Memory(), "")
    assert await engine.read(key) is None
    await engine.publish(key, work)
    assert (await engine.read(key)).requirements["topic"] == "A useful guide"
    assert await engine.read(other_tenant) is None
    await engine.delete(key)
    assert await engine.read(key) is None
    await engine.aclose()
