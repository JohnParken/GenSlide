"""Startup discovery of instruction-only skills; never executes plugin code."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import unicodedata
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


def _stage_instructions(body: str) -> dict[str, str]:
    """Legacy demo aliases only; no stage parsing or stage-based execution."""
    return {name: body for name in ("clarify", "outline", "generate")}


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
    kind_to_output = {"writing": "text", "document": "document", "presentation": "presentation"}
    legacy_target = metadata.get("target_kind")
    supported = [item.strip() for item in metadata.get(
        "supported_outputs", kind_to_output.get(legacy_target, "text,document,presentation")).split(",")]
    if not supported or len(set(supported)) != len(supported) or set(supported) - {"text", "document", "presentation"}:
        raise ValueError("invalid supported outputs")
    default_output = metadata.get("default_output", supported[0])
    if default_output not in supported:
        raise ValueError("default output is not supported")
    try:
        priority = int(metadata.get("priority", "0"))
    except ValueError as exc:
        raise ValueError("invalid skill priority") from exc
    if not -1000 <= priority <= 1000:
        raise ValueError("invalid skill priority")
    full_text = raw.decode("utf-8-sig").strip()
    # Keep the complete administrator-authored manual; legacy demo keys are aliases.
    output = {
        key: value for key, value in metadata.items()
        if key.startswith("output") or key in {"format", "mime_type", "extension", "renderer"}
    }
    return {
        "skill_id": data["name"],
        "version": metadata.get("version", "1"),
        "target_kind": metadata.get("target_kind"),
        "description": data["description"],
        "instructions": _stage_instructions(body),
        "content": body,
        "full_text": full_text,
        "metadata": metadata,
        "output": output,
        "supported_outputs": supported,
        "default_output": default_output,
        "priority": priority,
    }


class SkillRegistry:
    def __init__(self, root: Path | None = None):
        self._custom_root = Path(root) if root is not None else None
        self.skills = {}
        self.reload()

    def reload(self):
        """Dynamic reload of skills from disk without server restart."""
        configured = os.environ.get("GENSLIDE_SKILLS_DIR")
        roots: list[Path] = []
        if self._custom_root is not None:
            roots = [self._custom_root]
        elif configured is not None:
            roots = [Path(configured)]
        else:
            roots = [Path(files(__package__).joinpath("skills"))]
            # Also discover shared root-level skills directory if present
            workspace_skills = Path.cwd() / "skills"
            if workspace_skills.is_dir() and workspace_skills.resolve() != roots[0].resolve():
                roots.append(workspace_skills)

        entries = []
        for root in roots:
            if not root.is_dir():
                raise ValueError("skill directory does not exist")
            for child in sorted(root.iterdir(), key=lambda p: p.name):
                if hasattr(child, "is_symlink") and child.is_symlink():
                    raise ValueError("skill directory entries must not be symlinks")
                if child.is_dir():
                    entry = child.joinpath("SKILL.md")
                    if (hasattr(entry, "is_symlink") and entry.is_symlink()) or entry.is_file():
                        entries.append(entry)

        if not entries or len(entries) > 128:
            raise ValueError("skill directory must contain 1 to 128 skills")

        new_skills = {}
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
            if not isinstance(data, dict) or not {"skill_id", "version", "target_kind", "instructions", "description"} <= set(data):
                raise ValueError("invalid skill fields")
            if any(not isinstance(data[k], str) or not _ID.fullmatch(data[k]) for k in ("skill_id", "version")):
                raise ValueError("invalid skill identity or version")
            generic = data["target_kind"] is None
            if not generic and (not isinstance(data["target_kind"], str) or data["target_kind"] not in {"writing", "document", "presentation"}):
                raise ValueError("invalid skill target")
            instructions = data["instructions"]
            if not isinstance(instructions, dict) or set(instructions) != {"clarify", "outline", "generate"}:
                raise ValueError("invalid skill stages")
            if any(not isinstance(v, str) or not v.strip() or len(v) > 65536 for v in instructions.values()):
                raise ValueError("invalid skill instructions")
            for key in ("content", "full_text"):
                if not isinstance(data[key], str) or not data[key].strip() or len(data[key]) > 65536:
                    raise ValueError("invalid skill content")
            if not isinstance(data.get("metadata"), dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in data["metadata"].items()
            ):
                raise ValueError("invalid skill metadata")
            if not isinstance(data.get("output"), dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in data["output"].items()
            ):
                raise ValueError("invalid skill output metadata")
            if data["skill_id"] in new_skills:
                raise ValueError("duplicate skill ID")
            data["hash"] = hashlib.sha256(raw).hexdigest()
            new_skills[data["skill_id"]] = data

        self.skills = new_skills
        return self.list_skills()

    def list_skills(self) -> list[dict]:
        """Return list of discovered skills and their metadata."""
        return [
            {
                "skill_id": skill["skill_id"],
                "name": skill["skill_id"],
                "description": skill.get("description", ""),
                "target_kind": skill.get("target_kind"),
                "version": skill.get("version", "1"),
                "hash": skill.get("hash", ""),
                "metadata": deepcopy(skill.get("metadata", {})),
                "output": deepcopy(skill.get("output", {})),
                "supported_outputs": list(skill["supported_outputs"]),
                "default_output": skill["default_output"],
                "priority": skill["priority"],
            }
            for skill in sorted(self.skills.values(), key=lambda s: s["skill_id"])
        ]

    @staticmethod
    def _tokens(value: str) -> set[str]:
        normalized = unicodedata.normalize("NFKC", value or "").lower()
        tokens = set(re.findall(r"[a-z0-9][a-z0-9_.-]{1,63}", normalized))
        for phrase in re.findall(r"[\u4e00-\u9fff]+", normalized):
            tokens.update(phrase[i:i + 2] for i in range(len(phrase) - 1))
        return tokens

    def _auto_select(self, kind: str, query: str = "") -> dict:
        candidates = [skill for skill in self.skills.values()
                      if ("text" if kind == "writing" else kind) in skill["supported_outputs"]]
        if not candidates:
            raise ServiceError("SKILL_NOT_FOUND", 422)
        default = next((skill for skill in candidates if skill["skill_id"] == kind), None)
        if not query.strip():
            return default or sorted(candidates, key=lambda item: item["skill_id"])[0]
        terms = self._tokens(query)
        def score(skill):
            identity = self._tokens(" ".join((skill["skill_id"], skill["description"], skill["content"])))
            exact = sum(1 for term in terms if term in identity)
            name = sum(1 for term in terms if term in self._tokens(skill["skill_id"]))
            target = 1 if skill["target_kind"] == kind else 0
            builtin = 1 if skill["skill_id"] == kind else 0
            return (name * 4 + exact, skill["priority"], target, builtin, skill["skill_id"])
        return max(candidates, key=score)

    def get(self, kind, requested=None, query: str = ""):
        selected = requested
        if not isinstance(selected, str) or not _ID.fullmatch(selected):
            if selected is not None:
                raise ServiceError("SKILL_TARGET_MISMATCH", 422)
            skill = self._auto_select(kind, query)
        else:
            skill = self.skills.get(selected)
            if skill is None:
                raise ServiceError("SKILL_NOT_FOUND", 422)
        if kind not in {"writing", "document", "presentation"} or ("text" if kind == "writing" else kind) not in skill["supported_outputs"]:
            raise ServiceError("SKILL_TARGET_MISMATCH", 422)
        result = deepcopy(skill)
        result["target_kind"] = kind
        return result

    def get_bound(self, kind: str, skill_id: str, version: str, skill_hash: str):
        """Resolve a previously selected skill without silently upgrading it."""
        skill = self.get(kind, skill_id)
        if skill["version"] != version or skill["hash"] != skill_hash:
            raise ServiceError("SKILL_VERSION_UNAVAILABLE", 409)
        return skill
