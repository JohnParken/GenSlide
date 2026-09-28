import pytest
from pydantic import ValidationError
from genslide_agentscope.domain import ExecuteRequest, Memory, ServiceError
from genslide_agentscope.skills import SkillRegistry
from genslide_agentscope.workflow import execute
def req(**kw):
    d=dict(engine="agentscope",tenant_id="t",user_id="u",session_id="s",runtime_epoch="e",action_id="a",authorization="secret",expected_session_version=0,expected_lifecycle_version=1,mode="assistant",requested_output="text",message="Guide"); d.update(kw); return ExecuteRequest(**d)
@pytest.mark.asyncio
async def test_reply_turn_has_no_confirm_gating(tmp_path):
    class Model:
        async def complete(self, system, payload): return {"effect":"reply","target_kind":"writing","skill_id":"writing"} if payload["phase"]=="decide" else {"effect":"reply","reply":"Which audience?"}
    skill = tmp_path / "writing" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text("---\nname: writing\ndescription: Test writing skill\nmetadata:\n  target_kind: writing\n---\nTest instructions", encoding="utf-8")
    result=await execute(req(),Memory(),"",Model(),SkillRegistry(tmp_path)); assert result.reply=="Which audience?" and result.memory.outline is None
def test_request_requires_new_lifecycle_and_mode_fields():
    with pytest.raises(ValidationError): req(expected_lifecycle_version=None)
    with pytest.raises(ValidationError): req(mode="legacy")
def test_memory_and_skill_selection_remain_whitelisted():
    with pytest.raises(ValidationError): Memory(requirements={"old_document":"secret"})
    with pytest.raises(ServiceError,match="SKILL_TARGET_MISMATCH"): SkillRegistry().get("writing","../scripts/run.py")
