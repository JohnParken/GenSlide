import os
import json

import pytest

from genslide_langgraph.domain import ExecuteRequest, Memory, ServiceError
from genslide_langgraph.skills import SkillRegistry
from genslide_langgraph.workflow import execute


def skill(skill_id="custom", target_kind="writing", **kwargs):
    value = {
        "skill_id": skill_id,
        "version": "1",
        "target_kind": target_kind,
        "instructions": {"clarify": "c", "outline": "o", "generate": "g"},
    }
    value.update(kwargs)
    return value


def put(root, skill_id, value):
    if "instructions" not in value:
        path = root / skill_id / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("not markdown", encoding="utf-8")
        return
    name = value["skill_id"]
    metadata = "" if value.get("target_kind") is None else f"  target_kind: {value['target_kind']}\n"
    content = f"---\nname: {name}\ndescription: test\nmetadata:\n  version: '{value.get('version', '1')}'\n{metadata}---\n" + value.get("instructions", {}).get("clarify", "")
    path = root / skill_id / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")


def test_discovers_arbitrary_filenames_and_selects_requested_skill(tmp_path):
    put(tmp_path, "custom-writing", skill("custom-writing"))
    put(tmp_path, "custom-document", skill("custom-document", "document"))
    registry = SkillRegistry(tmp_path)
    assert registry.get("writing", "custom-writing")["skill_id"] == "custom-writing"
    assert registry.get("document", "custom-document")["skill_id"] == "custom-document"


def test_reload_reflects_deleted_skill(tmp_path):
    put(tmp_path, "one", skill("one"))
    put(tmp_path, "two", skill("two"))
    registry = SkillRegistry(tmp_path)
    (tmp_path / "one" / "SKILL.md").unlink()
    assert registry.get("writing", "one")["skill_id"] == "one"
    reloaded = SkillRegistry(tmp_path)
    assert reloaded.get("writing", "two")["skill_id"] == "two"
    with pytest.raises(ServiceError, match="SKILL_NOT_FOUND"):
        reloaded.get("writing", "one")


def test_empty_directory_fails(tmp_path):
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path)


def test_legacy_json_is_ignored_and_json_only_directory_fails(tmp_path):
    (tmp_path / "legacy.json").write_text(json.dumps(skill("legacy")), encoding="utf-8")
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path)
    put(tmp_path, "markdown", skill("markdown"))
    registry = SkillRegistry(tmp_path)
    with pytest.raises(ServiceError, match="SKILL_NOT_FOUND"):
        registry.get("writing", "legacy")


def test_environment_directory_is_used(tmp_path, monkeypatch):
    put(tmp_path, "env-skill", skill("env-skill"))
    monkeypatch.setenv("GENSLIDE_SKILLS_DIR", os.fspath(tmp_path))
    assert SkillRegistry().get("writing", "env-skill")["skill_id"] == "env-skill"


@pytest.mark.parametrize("value", [
    skill("bad/id"),
    skill("bad", instructions={"clarify": "", "outline": "o", "generate": "g"}),
    skill("bad", target_kind="invalid"),
    {"skill_id": "bad"},
])
def test_invalid_skill_definition_fails(tmp_path, value):
    put(tmp_path, "bad", value)
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path)


def test_duplicate_ids_invalid_markdown_and_symlink_fail(tmp_path):
    put(tmp_path, "a", skill("same"))
    put(tmp_path, "b", skill("same", target_kind="document"))
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path)
    (tmp_path / "a" / "SKILL.md").unlink()
    (tmp_path / "b" / "SKILL.md").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path)
    (tmp_path / "b" / "SKILL.md").unlink()
    put(tmp_path, "real", skill("real"))
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path)


def test_selection_errors_and_defensive_copy(tmp_path):
    put(tmp_path, "custom", skill("custom"))
    registry = SkillRegistry(tmp_path)
    result = registry.get("writing", "custom")
    result["instructions"]["clarify"] = "changed"
    assert registry.get("writing", "custom")["instructions"]["clarify"] == "c"
    with pytest.raises(ServiceError, match="SKILL_NOT_FOUND"):
        registry.get("writing", "missing")
    with pytest.raises(ServiceError, match="SKILL_TARGET_MISMATCH"):
        registry.get("document", "custom")
    with pytest.raises(ServiceError, match="SKILL_TARGET_MISMATCH"):
        registry.get("writing", "../custom")


@pytest.mark.asyncio
async def test_workflow_inherits_bound_skill_and_rejects_other_skill(tmp_path):
    put(tmp_path, "custom", skill("custom"))
    put(tmp_path, "other", skill("other"))
    registry = SkillRegistry(tmp_path)
    values = dict(engine="langgraph", tenant_id="t", user_id="u", session_id="s",
                  runtime_epoch="e", action_id="a", authorization="x",
                  expected_session_version=0, target_kind="writing")
    class Model:
        async def complete(self, system, payload):
            if payload["operation"] == "create_outline":
                return {"title": "Guide", "nodes": [{"node_id": "n", "title": "Intro"}]}
            return {"title": "Guide", "sections": [{"title": "Intro", "body": "Text"}]}
    created = await execute(ExecuteRequest(**values, operation="create_outline", skill_id="custom"),
                            Memory(requirements={"topic": "Guide"}), "", Model(), registry)
    args = dict(draft_id=created.memory.outline.draft_id, expected_outline_version=1)
    confirmed = await execute(ExecuteRequest(**values, operation="confirm_outline", **args),
                              created.memory, "", Model(), registry)
    done = await execute(ExecuteRequest(**values, operation="generate", **args),
                         confirmed.memory, "", Model(), registry)
    assert done.memory.outline.skill_id == "custom"
    with pytest.raises(ServiceError, match="SKILL_VERSION_UNAVAILABLE"):
        await execute(ExecuteRequest(**values, operation="generate", skill_id="other", **args),
                      confirmed.memory, "", Model(), registry)
