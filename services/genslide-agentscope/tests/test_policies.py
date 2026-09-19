import pytest
from pydantic import ValidationError
from genslide_agentscope.domain import ExecuteRequest, Memory, ServiceError
from genslide_agentscope.skills import SkillRegistry
from genslide_agentscope.workflow import execute

class Model:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []
    async def complete(self, system, payload):
        self.calls.append(payload)
        return self.outputs.pop(0)

def req(operation="clarify", **kwargs):
    data = dict(engine="agentscope", tenant_id="t", user_id="u", session_id="s", runtime_epoch="e",
                action_id="a", authorization="secret", expected_session_version=0,
                operation=operation, target_kind="writing")
    return ExecuteRequest(**(data | kwargs))

def outline():
    return {"title":"Guide", "nodes":[{"node_id":"n", "title":"Overview"}]}

@pytest.mark.asyncio
async def test_accepted_suggestions_and_stale_question_ids():
    model = Model({"requirements":{}, "answer":"Which tone?", "guidance":{
        "questions":[{"question_id":"q", "field":"audience", "text":"For whom?"}],
        "proposals":[{"proposal_id":"p", "field":"style", "value":"Friendly"}]}})
    work = await execute(req(), Memory(), "", model, SkillRegistry())
    assert not work.memory.requirements
    question = work.memory.guidance.questions[0]
    proposal = work.memory.guidance.proposals[0]
    model = Model(outline())
    next_work = await execute(req("create_outline", message="A guide",
        answers={question.question_id:"Beginners"}, accepted_proposal_ids=[proposal.proposal_id]),
        work.memory, "", model, SkillRegistry())
    assert next_work.memory.requirements == {"audience":"Beginners", "style":"Friendly"}
    with pytest.raises(ServiceError, match="GUIDANCE_VERSION_CONFLICT"):
        await execute(req(answers={question.question_id:"Experts"}), next_work.memory, "", model, SkillRegistry())

@pytest.mark.asyncio
async def test_material_marker_survives_clarification_not_file_contents():
    model = Model({"requirements":{"topic":"Guide"}, "guidance":{}}, outline())
    first = await execute(req(message="Guide", current_file_ids=["old-file"]), Memory(), "SECRET", model, SkillRegistry())
    second = await execute(req("create_outline"), first.memory, "", model, SkillRegistry())
    assert second.memory.outline.requires_materials
    assert "old-file" not in second.memory.model_dump_json()
    assert "SECRET" not in str(model.calls)
    o = second.memory.outline
    args = dict(draft_id=o.draft_id, expected_outline_version=1)
    confirmed = await execute(req("confirm_outline", **args), second.memory, "", model, SkillRegistry())
    with pytest.raises(ServiceError, match="MISSING_CURRENT_FILES"):
        await execute(req("generate", **args), confirmed.memory, "", model, SkillRegistry())

@pytest.mark.asyncio
async def test_requirements_change_revokes_confirmation():
    skills = SkillRegistry()
    work = await execute(req("create_outline", message="Guide"), Memory(), "", Model(outline()), skills)
    o = work.memory.outline
    args = dict(draft_id=o.draft_id, expected_outline_version=1)
    work = await execute(req("confirm_outline", **args), work.memory, "", Model(), skills)
    work = await execute(req(requirement_updates={"language":"Chinese"}), work.memory, "",
                         Model({"requirements":{}, "guidance":{}}), skills)
    assert work.memory.outline.confirmed_hash is None

@pytest.mark.asyncio
async def test_model_cannot_change_generation_structure():
    skills = SkillRegistry()
    work = await execute(req("create_outline", message="Guide"), Memory(), "", Model(outline()), skills)
    args = dict(draft_id=work.memory.outline.draft_id, expected_outline_version=1)
    work = await execute(req("confirm_outline", **args), work.memory, "", Model(), skills)
    wrong = {"title":"Guide","sections":[{"title":"Changed","body":"body"}]}
    # A structure miss is retried once per batch, so the model is asked twice before failing.
    model = Model(wrong, wrong)
    with pytest.raises(ServiceError, match="GENERATED_STRUCTURE_MISMATCH"):
        await execute(req("generate", **args), work.memory, "", model, skills)
    assert len(model.calls) == 2

def test_strict_request_and_memory_whitelist():
    with pytest.raises(ValidationError):
        req(artifact_id="old-artifact")
    with pytest.raises(ValidationError):
        Memory(requirements={"old_document":"secret"})
    with pytest.raises(ServiceError, match="SKILL_TARGET_MISMATCH"):
        SkillRegistry().get("writing", "../scripts/run.py")

@pytest.mark.asyncio
async def test_inferred_requirements_remain_unaccepted_proposals():
    work = await execute(req(message="帮我写一篇文章"), Memory(), "",
                         Model({"requirements":{"audience":"企业高管"}, "guidance":{}}), SkillRegistry())
    assert "audience" not in work.memory.requirements
    assert work.memory.guidance.proposals[0].value == "企业高管"
