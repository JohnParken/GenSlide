"""LangGraph execution with native in-memory checkpoints and commit pointers."""
from dataclasses import dataclass
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.runtime import Runtime
from pydantic import ValidationError
from .domain import ExecuteRequest, Memory, WorkResult, ServiceError
from .model import Model
from .skills import SkillRegistry
from .workflow import execute

class GraphState(TypedDict):
    memory: dict

@dataclass
class Turn:
    request: ExecuteRequest
    materials: str
    work: WorkResult | None = None

class Engine:
    name = "langgraph"

    def __init__(self, model=None):
        self.model = model if model is not None else Model()
        self.skills = SkillRegistry()
        self.saver = InMemorySaver()
        self.committed = {}
        self.pending = {}
        self.turn_counts = {}
        graph = StateGraph(GraphState, context_schema=Turn)
        graph.add_node("author", self._author)
        graph.add_edge(START, "author")
        graph.add_edge("author", END)
        self.graph = graph.compile(checkpointer=self.saver)

    def list_skills(self):
        return self.skills.list_skills()

    def reload_skills(self):
        return self.skills.reload()

    async def _author(self, state: GraphState, runtime: Runtime[Turn]):
        turn = runtime.context
        turn.work = await execute(turn.request, Memory.model_validate(state["memory"]),
                                  turn.materials, self.model, self.skills)
        return {"memory": turn.work.memory.model_dump()}

    async def read(self, key):
        config = self.committed.get(key)
        if config is None:
            return None
        state = await self.graph.aget_state(config)
        return Memory.model_validate(state.values["memory"])

    async def run(self, key, request, memory, materials):
        if self.turn_counts.get(key, 0) >= 32:
            raise ServiceError("CONTEXT_CAPACITY", 413)
        turn = Turn(request=request, materials=materials)
        try:
            await self.graph.ainvoke({"memory": memory.model_dump()},
                                    {"configurable": {"thread_id": key}},
                                    context=turn)
        except ValidationError as exc:
            raise ServiceError("MODEL_OUTPUT_INVALID", 502) from exc
        state = await self.graph.aget_state({"configurable": {"thread_id": key}})
        self.pending[key] = state.config
        if turn.work is None:
            raise ServiceError("ENGINE_RESULT_MISSING", 500)
        return turn.work

    async def publish(self, key, work):
        config = self.pending.pop(key)
        self.committed[key] = config
        self.turn_counts[key] = self.turn_counts.get(key, 0) + 1

    async def delete(self, key):
        await self.saver.adelete_thread(key)
        self.committed.pop(key, None)
        self.pending.pop(key, None)
        self.turn_counts.pop(key, None)

    async def aclose(self):
        for key in set(self.committed) | set(self.pending):
            await self.saver.adelete_thread(key)
        self.committed.clear()
        self.pending.clear()
        if hasattr(self.model, "aclose"):
            await self.model.aclose()

