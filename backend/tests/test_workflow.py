import pytest
from genslide_agentscope.domain import Content, ExecuteRequest, Memory, ServiceError, content_hash
from genslide_agentscope.skills import SkillRegistry
from genslide_agentscope.workflow import execute, length_target

class Model:
    def __init__(self, *responses): self.responses, self.calls = list(responses), []
    async def complete(self, system, payload): self.calls.append(payload); return self.responses.pop(0)
def req(**changes):
    base = dict(engine="agentscope", tenant_id="t", user_id="u", session_id="s", runtime_epoch="e", action_id="a", authorization="x", expected_session_version=0, expected_lifecycle_version=1, mode="assistant", requested_output="text", message="Guide")
    base.update(changes); return ExecuteRequest(**base)
def body(title="Guide", sections=("Intro",)): return {"title": title, "sections": [{"title": s, "body": "Text"} for s in sections]}
def decision(effect="deliverable", **kw): return {"effect": effect, "target_kind": "writing", "skill_id": "writing", **kw}
@pytest.mark.asyncio
async def test_execute_uses_decide_then_compose_and_keeps_materials_out_of_decide():
    model = Model(decision(), {"effect":"deliverable", "deliverable":body()})
    result = await execute(req(), Memory(), "PRIVATE", model, SkillRegistry())
    assert [c["phase"] for c in model.calls] == ["decide", "compose"]
    assert "materials" not in model.calls[0] and model.calls[1]["materials"] == "PRIVATE"
    assert result.effect == "deliverable"
@pytest.mark.asyncio
async def test_local_edit_scope_replaces_only_named_section_and_hashes_snapshot():
    current = Content.model_validate(body(sections=("Intro", "Details"))); skills = SkillRegistry()
    memory = Memory(content=current, content_hash=content_hash(current), target_kind="writing", skill_id="writing", skill_version=skills.skills["writing"]["version"], skill_hash=skills.skills["writing"]["hash"])
    model = Model(decision(edit_scope=["Details"], needs_full_content=True), {"effect":"deliverable", "deliverable":body(sections=("Details",))})
    result = await execute(req(message="tighten Details"), memory, "", model, skills)
    assert [s.title for s in result.content.sections] == ["Intro", "Details"] and result.content.sections[0].body == "Text"
    assert result.memory.content_hash == content_hash(result.content)
@pytest.mark.parametrize("text, expected", [("约3000字",3000),("5000-8000字",6500),("3页",None)])
def test_length_requirement_parses(text, expected): assert length_target({"length":text}) == expected
@pytest.mark.asyncio
async def test_invalid_edit_scope_is_rejected():
    with pytest.raises(ServiceError, match="EDIT_SCOPE_INVALID"):
        await execute(req(), Memory(content=body(), target_kind="writing"), "", Model(decision(edit_scope=["Missing"], needs_full_content=True)), SkillRegistry())

@pytest.mark.asyncio
async def test_explicit_output_intent_mismatch_is_rejected():
    model = Model(decision(target_kind="writing"))
    with pytest.raises(ServiceError, match="OUTPUT_INTENT_MISMATCH"):
        await execute(req(requested_output="document"), Memory(), "", model, SkillRegistry())
