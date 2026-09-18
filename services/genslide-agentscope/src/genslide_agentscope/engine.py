"""AgentScope SDK authoring; only validated requirements/outline survive a turn."""
from pydantic import ValidationError
from .domain import Memory, ServiceError
from .model import Model
from .skills import SkillRegistry
from .workflow import execute

class Engine:
    name = "agentscope"

    def __init__(self, model=None):
        self.model = model if model is not None else Model()
        self.skills = SkillRegistry()
        self.committed = {}
        self.turn_counts = {}

    def list_skills(self):
        return self.skills.list_skills()

    def reload_skills(self):
        return self.skills.reload()

    async def read(self, key):
        memory = self.committed.get(key)
        return memory.model_copy(deep=True) if memory is not None else None

    async def run(self, key, request, memory, materials):
        if self.turn_counts.get(key, 0) >= 32:
            raise ServiceError("CONTEXT_CAPACITY", 413)
        try:
            return await execute(request, memory, materials, self.model, self.skills)
        except ValidationError as exc:
            raise ServiceError("MODEL_OUTPUT_INVALID", 502) from exc

    async def publish(self, key, work):
        self.committed[key] = Memory.model_validate(work.memory.model_dump())
        self.turn_counts[key] = self.turn_counts.get(key, 0) + 1

    async def delete(self, key):
        self.committed.pop(key, None)
        self.turn_counts.pop(key, None)

    async def aclose(self):
        self.committed.clear()
        self.turn_counts.clear()
        if hasattr(self.model, "aclose"):
            await self.model.aclose()
