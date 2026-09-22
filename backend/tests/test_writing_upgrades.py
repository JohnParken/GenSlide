import pytest
from genslide_agentscope.domain import Content, ExecuteRequest, Memory, content_hash
from genslide_agentscope.skills import SkillRegistry
from genslide_agentscope.workflow import execute
def req(**kw):
    d=dict(engine="agentscope",tenant_id="t",user_id="u",session_id="s",runtime_epoch="e",action_id="a",authorization="x",expected_session_version=0,expected_lifecycle_version=1,mode="assistant",requested_output="text",message="Guide"); d.update(kw); return ExecuteRequest(**d)
def content(): return {"title":"Guide","sections":[{"title":"Intro","body":"Text"},{"title":"Steps","body":"More"}]}
class Model:
    def __init__(self,*r): self.responses=list(r); self.calls=[]
    async def complete(self,system,payload): self.calls.append(payload); return self.responses.pop(0)
@pytest.mark.asyncio
async def test_direct_drafting_returns_deliverable_without_outline_confirmation():
    model=Model({"effect":"deliverable","target_kind":"writing","skill_id":"writing"},{"effect":"deliverable","deliverable":content()})
    result=await execute(req(message="Write it now"),Memory(),"",model,SkillRegistry()); assert result.content.title=="Guide" and result.memory.content_hash==content_hash(result.content)
@pytest.mark.asyncio
async def test_local_edit_payload_contains_exact_scope_and_preserves_other_sections():
    original=Content.model_validate(content()); skills=SkillRegistry(); memory=Memory(content=original,content_hash=content_hash(original),target_kind="writing",skill_id="writing",skill_version=skills.skills["writing"]["version"],skill_hash=skills.skills["writing"]["hash"])
    model=Model({"effect":"deliverable","target_kind":"writing","skill_id":"writing","needs_full_content":True,"edit_scope":["Steps"]},{"effect":"deliverable","deliverable":{"title":"Guide","sections":[{"title":"Steps","body":"Updated"}]}})
    result=await execute(req(message="Update Steps"),memory,"",model,skills); assert model.calls[1]["decision"]["edit_scope"]==["Steps"] and result.content.sections[0].body=="Text" and result.content.sections[1].body=="Updated"

@pytest.mark.asyncio
async def test_reply_does_not_create_file_content():
    model = Model({"effect": "reply", "target_kind": "writing", "skill_id": "writing"},
                  {"effect": "reply", "reply": "Need more context."})
    result = await execute(req(), Memory(), "", model, SkillRegistry())
    assert result.effect == "reply"
    assert result.content is None
    assert result.deliverable is None
