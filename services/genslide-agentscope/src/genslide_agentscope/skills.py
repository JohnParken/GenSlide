"""Startup discovery of instruction-only skills; never executes plugin code."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
from importlib.resources import files
import yaml
from .domain import ServiceError

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class _MetadataLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("skill YAML keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _markdown_skill(raw):
    try:
        lines = raw.decode("utf-8-sig").splitlines()
        if not lines or lines[0] != "---":
            raise ValueError("SKILL.md requires YAML frontmatter")
        end = lines.index("---", 1)
        header = "\n".join(lines[1:end])
        if any(isinstance(t, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
               for t in yaml.scan(header)):
            raise ValueError("skill YAML aliases and anchors are not supported")
        data = yaml.load(header, Loader=_MetadataLoader)
    except (yaml.YAMLError, UnicodeError, RecursionError) as exc:
        raise ValueError("invalid skill YAML") from exc
    if not isinstance(data, dict) or not {"name", "description"} <= data.keys():
        raise ValueError("skill name and description are required")
    if set(data) - {"name", "description", "metadata", "license", "compatibility", "allowed-tools"}:
        raise ValueError("unsupported skill frontmatter fields")
    if not isinstance(data["description"], str) or not data["description"].strip() or len(data["description"]) > 1024:
        raise ValueError("invalid skill description")
    for key in ("license", "compatibility", "allowed-tools"):
        if key in data and not isinstance(data[key], str):
            raise ValueError("skill informational fields must be strings")
    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict) or any(not isinstance(v, str) for v in metadata.values()):
        raise ValueError("skill metadata values must be strings")
    body = "\n".join(lines[end + 1:]).strip()
    return {
        "skill_id": data["name"], "version": metadata.get("version", "1"),
        "target_kind": metadata.get("target_kind"),
        "instructions": {stage: body for stage in ("clarify", "outline", "generate")},
    }


class SkillRegistry:
    def __init__(self, root: Path | None = None):
        configured = os.environ.get("GENSLIDE_SKILLS_DIR")
        root = Path(root) if root is not None else (
            Path(configured) if configured is not None else files(__package__).joinpath("skills")
        )
        if not root.is_dir():
            raise ValueError("skill directory does not exist")
        entries = []
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if hasattr(child, "is_symlink") and child.is_symlink():
                raise ValueError("skill directory entries must not be symlinks")
            if child.is_dir():
                entry = child.joinpath("SKILL.md")
                if (hasattr(entry, "is_symlink") and entry.is_symlink()) or entry.is_file():
                    entries.append(entry)
        if not entries or len(entries) > 128:
            raise ValueError("skill directory must contain 1 to 128 skills")
        self.skills = {}
        for entry in entries:
            if (hasattr(entry, "is_symlink") and entry.is_symlink()) or not entry.is_file():
                raise ValueError("skill entries must be regular files, not symlinks")
            with entry.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("skill file exceeds 64 KiB")
            try:
                data = _markdown_skill(raw)
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"invalid skill definition: {entry.name}") from exc
            if not isinstance(data, dict) or set(data) != {"skill_id", "version", "target_kind", "instructions"}:
                raise ValueError("invalid skill fields")
            if any(not isinstance(data[k], str) or not _ID.fullmatch(data[k]) for k in ("skill_id", "version")):
                raise ValueError("invalid skill identity or version")
            generic = data["target_kind"] is None
            if not generic and (not isinstance(data["target_kind"], str) or data["target_kind"] not in {"writing", "document", "presentation"}):
                raise ValueError("invalid skill target")
            instructions = data["instructions"]
            if not isinstance(instructions, dict) or set(instructions) != {"clarify", "outline", "generate"}:
                raise ValueError("invalid skill stages")
            if any(not isinstance(v, str) or not v.strip() or len(v) > 4000 for v in instructions.values()):
                raise ValueError("invalid skill instructions")
            if data["skill_id"] in self.skills:
                raise ValueError("duplicate skill ID")
            data["hash"] = hashlib.sha256(raw).hexdigest()
            self.skills[data["skill_id"]] = data

    def get(self, kind, requested=None):
        selected = requested if requested is not None else kind
        if not isinstance(selected, str) or not _ID.fullmatch(selected):
            raise ServiceError("SKILL_TARGET_MISMATCH", 422)
        skill = self.skills.get(selected)
        if skill is None:
            raise ServiceError("SKILL_NOT_FOUND", 422)
        if kind not in {"writing", "document", "presentation"} or skill["target_kind"] not in (None, kind):
            raise ServiceError("SKILL_TARGET_MISMATCH", 422)
        result = deepcopy(skill)
        result["target_kind"] = kind
        return result
