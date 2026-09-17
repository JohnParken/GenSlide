import hashlib
import json

import pytest

from genslide_agentscope.domain import ExecuteRequest, Memory, ServiceError
from genslide_agentscope.skills import SkillRegistry
from genslide_agentscope.workflow import execute

BODY = "Use this exact instruction body for every stage."


def write_md(root, skill_id="markdown-skill", *, metadata="", body=BODY, name="markdown-skill", description="A useful skill"):
    content = f"---\nname: {name}\ndescription: {description}\n{metadata}---\n{body}"
    path = root / skill_id / "SKILL.md"
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")
    return path, content.encode()


def write_json(root, skill_id="json-skill"):
    (root / f"{skill_id}.json").write_text(json.dumps({
        "skill_id": skill_id, "version": "1", "target_kind": "writing",
        "instructions": {"clarify": "c", "outline": "o", "generate": "g"},
    }), encoding="utf-8")


def test_markdown_metadata_body_hash_and_default_target(tmp_path):
    _, raw = write_md(tmp_path, metadata="metadata:\n  version: '2'\n")
    registry = SkillRegistry(tmp_path)
    for kind in ("writing", "document", "presentation"):
        skill = registry.get(kind, "markdown-skill")
        assert skill["target_kind"] == kind
        assert skill["version"] == "2"
        assert skill["skill_id"] == "markdown-skill"
        assert skill["hash"] == hashlib.sha256(raw).hexdigest()
        assert skill["instructions"] == {"clarify": BODY, "outline": BODY, "generate": BODY}


@pytest.mark.parametrize("text", [
    "name: x\ndescription: y\n---\nbody",
    "---\nname: x\ndescription: y\nmetadata:\n  version: &v '1'\n  target_kind: *v\n---\nbody",
    "---\nname: x\ndescription: y\nmetadata: [\n---\nbody",
    "---\nname: x\nname: y\ndescription: z\n---\nbody",
    "---\nname: x\ndescription: y\nmetadata: {version: !!python/object/apply:os.system [id]}\n---\nbody",
])
def test_bad_frontmatter_fails(tmp_path, text):
    path = tmp_path / "x" / "SKILL.md"
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path)


@pytest.mark.parametrize("metadata", ["metadata:\n  version: ''\n", "metadata:\n  version: 2\n", "metadata:\n  target_kind: invalid\n", "metadata: [x]\n"])
def test_invalid_metadata_fails(tmp_path, metadata):
    write_md(tmp_path, metadata=metadata)
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path)


@pytest.mark.parametrize("kwargs", [{"name": ""}, {"description": " "}, {"body": " "}, {"body": "x" * 4001}])
def test_blank_or_oversize_markdown_fields_fail(tmp_path, kwargs):
    write_md(tmp_path, **kwargs)
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path)


def test_duplicate_ids_symlink_directories_and_unrelated_files_fail(tmp_path):
    write_md(tmp_path, "same", name="same")
    write_md(tmp_path, "same-copy", name="same")
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path)
    (tmp_path / "same-copy" / "SKILL.md").unlink()
    (tmp_path / "same-copy").rmdir()
    (tmp_path / "aux").mkdir()
    (tmp_path / "aux" / "script.py").write_text("ignored", encoding="utf-8")
    (tmp_path / "same" / "references").mkdir()
    (tmp_path / "same" / "references" / "extra.md").write_text("ignored", encoding="utf-8")
    assert set(SkillRegistry(tmp_path).skills) == {"same"}
    (tmp_path / "link").symlink_to(tmp_path / "same", target_is_directory=True)
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path)


def test_legacy_json_is_ignored_and_json_only_directory_fails(tmp_path):
    write_json(tmp_path, "legacy")
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path)
    write_md(tmp_path)
    registry = SkillRegistry(tmp_path)
    assert set(registry.skills) == {"markdown-skill"}


def test_markdown_target_constraint_and_file_symlink(tmp_path):
    path, _ = write_md(tmp_path, metadata="metadata:\n  target_kind: document\n")
    registry = SkillRegistry(tmp_path)
    assert registry.get("document", "markdown-skill")["target_kind"] == "document"
    with pytest.raises(ServiceError, match="SKILL_TARGET_MISMATCH"):
        registry.get("writing", "markdown-skill")
    source = path.with_name("source.md")
    path.rename(source)
    path.symlink_to(source)
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path)


def test_removed_or_changed_markdown_requires_reload(tmp_path):
    path, _ = write_md(tmp_path)
    registry = SkillRegistry(tmp_path)
    path.unlink()
    assert registry.get("writing", "markdown-skill")
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path)
    path.write_text("---\nname: markdown-skill\ndescription: changed\n---\nchanged", encoding="utf-8")
    assert SkillRegistry(tmp_path).get("writing", "markdown-skill")["hash"] != registry.get("writing", "markdown-skill")["hash"]


@pytest.mark.asyncio
async def test_workflow_receives_body_and_binds_markdown_skill(tmp_path):
    write_md(tmp_path)
    registry = SkillRegistry(tmp_path)
    values = dict(engine="agentscope", tenant_id="t", user_id="u", session_id="s", runtime_epoch="e", action_id="a", authorization="x", expected_session_version=0, target_kind="writing")

    class Model:
        def __init__(self): self.calls = []
        async def complete(self, system, payload):
            self.calls.append(system)
            if payload["operation"] == "create_outline":
                return {"title": "Guide", "nodes": [{"node_id": "n", "title": "Intro"}]}
            return {"title": "Guide", "sections": [{"title": "Intro", "body": "Text"}]}

    model = Model()
    created = await execute(ExecuteRequest(**values, operation="create_outline", skill_id="markdown-skill"), Memory(requirements={"topic": "Guide"}), "", model, registry)
    assert BODY in model.calls[0]
    args = dict(draft_id=created.memory.outline.draft_id, expected_outline_version=1)
    confirmed = await execute(ExecuteRequest(**values, operation="confirm_outline", **args), created.memory, "", model, registry)
    done = await execute(ExecuteRequest(**values, operation="generate", **args), confirmed.memory, "", model, registry)
    assert done.memory.outline.skill_id == "markdown-skill"
    assert BODY in model.calls[-1]
