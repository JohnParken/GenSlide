"""Read-only, packaged workflow instructions; no code/tool discovery."""
import hashlib
import json
from importlib.resources import files
from .domain import ServiceError

class SkillRegistry:
    def __init__(self):
        self.skills = {}
        root = files(__package__).joinpath("skills")
        for kind in ("writing", "document", "presentation"):
            raw = root.joinpath(kind + ".json").read_text("utf-8")
            data = json.loads(raw)
            if set(data) != {"skill_id", "version", "target_kind", "instructions"}:
                raise ValueError("invalid builtin skill")
            if data["skill_id"] != kind or data["target_kind"] != kind or data["version"] != "1":
                raise ValueError("invalid builtin identity")
            if set(data["instructions"]) != {"clarify", "outline", "generate"}:
                raise ValueError("invalid builtin stages")
            if any(not isinstance(v, str) or len(v) > 4000 for v in data["instructions"].values()):
                raise ValueError("invalid builtin instructions")
            data["hash"] = hashlib.sha256(raw.encode()).hexdigest()
            self.skills[kind] = data

    def get(self, kind, requested=None):
        if requested is not None and requested != kind:
            raise ServiceError("SKILL_TARGET_MISMATCH", 422)
        return self.skills[kind]

