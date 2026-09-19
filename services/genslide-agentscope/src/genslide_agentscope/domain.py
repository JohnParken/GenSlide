"""Public contract and whitelisted runtime state. No framework-specific objects."""
from __future__ import annotations
import hashlib
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

Operation = Literal["clarify", "create_outline", "revise_outline", "explain_outline", "confirm_outline", "generate", "revise_content"]
Kind = Literal["writing", "document", "presentation"]
#: Operations that produce or rewrite a full deliverable body (long deadline, generation slot).
GENERATION_OPERATIONS = frozenset({"generate", "revise_content"})

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class ServiceError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)

class ExecuteRequest(StrictModel):
    api_contract_version: Literal["1"] = "1"
    engine: Literal["agentscope"]
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    runtime_epoch: str = Field(min_length=1, max_length=128)
    action_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    authorization: SecretStr
    expected_session_version: int = Field(ge=0)
    operation: Operation
    target_kind: Kind
    message: str = Field(default="", max_length=16000)
    current_file_ids: list[str] = Field(default_factory=list, max_length=10)
    draft_id: str | None = Field(default=None, max_length=128)
    expected_outline_version: int | None = Field(default=None, ge=1)
    answers: dict[str, str] = Field(default_factory=dict, max_length=3)
    accepted_proposal_ids: list[str] = Field(default_factory=list, max_length=3)
    requirement_updates: dict[str, str] = Field(default_factory=dict)
    requires_materials: bool | None = None
    skill_id: str | None = None
    # revise_content carries the current draft back so the pod stays stateless; the retained
    # content hash proves the client is revising the revision the pod last produced.
    content: Content | None = None
    expected_content_hash: str | None = Field(default=None, max_length=128)
    section_titles: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def bounded_inputs(self):
        if any(not x or len(x) > 256 for x in self.current_file_ids):
            raise ValueError("invalid file ID")
        if len(set(self.current_file_ids)) != len(self.current_file_ids):
            raise ValueError("duplicate file ID")
        allowed = {"topic", "audience", "language", "length", "style", "purpose", "constraints"}
        if set(self.requirement_updates) - allowed:
            raise ValueError("unknown requirement")
        if any(len(v) > 2000 for v in [*self.answers.values(), *self.requirement_updates.values()]):
            raise ValueError("input too long")
        if any(not title or len(title) > 200 for title in self.section_titles):
            raise ValueError("invalid section title")
        if len(set(self.section_titles)) != len(self.section_titles):
            raise ValueError("duplicate section title")
        return self

    def session_key(self) -> str:
        raw = [self.tenant_id, self.user_id, self.session_id, self.runtime_epoch]
        return digest(raw)

    def identity(self) -> dict:
        return self.model_dump(include={"tenant_id", "user_id", "session_id", "runtime_epoch", "action_id", "engine", "expected_session_version", "api_contract_version"})

class Question(StrictModel):
    question_id: str = Field(max_length=128)
    field: Literal["topic", "audience", "language", "length", "style", "purpose", "constraints"]
    text: str = Field(min_length=1, max_length=500)
    options: list[str] = Field(default_factory=list, max_length=3)
    required: bool = False

class Proposal(StrictModel):
    proposal_id: str = Field(max_length=128)
    field: Literal["topic", "audience", "language", "length", "style", "purpose", "constraints"]
    value: str = Field(min_length=1, max_length=500)
    reason: str = Field(default="", max_length=500)

class Guidance(StrictModel):
    stage: Literal["clarify", "outline", "confirmed", "generated"] = "clarify"
    summary: str = Field(default="", max_length=2000)
    questions: list[Question] = Field(default_factory=list, max_length=3)
    proposals: list[Proposal] = Field(default_factory=list, max_length=3)
    next_actions: list[Operation] = Field(default_factory=lambda: ["clarify"])

class OutlineNode(StrictModel):
    node_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)

class Outline(StrictModel):
    draft_id: str
    outline_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    target_kind: Kind
    nodes: list[OutlineNode] = Field(min_length=1, max_length=30)
    requires_materials: bool = False
    skill_id: str
    skill_version: str
    skill_hash: str
    confirmed_hash: str | None = None

class Memory(StrictModel):
    schema_version: Literal[1] = 1
    requires_materials: bool = False
    requirements: dict[str, str] = Field(default_factory=dict)
    guidance: Guidance = Field(default_factory=Guidance)
    outline: Outline | None = None
    # Hash of the last generated body. Only the hash is retained; the body itself stays with
    # the BFF so a finished draft cannot blow the local session memory budget.
    content_hash: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def whitelist_requirements(self):
        allowed = {"topic", "audience", "language", "length", "style", "purpose", "constraints"}
        if set(self.requirements) - allowed or any(len(v) > 2000 for v in self.requirements.values()):
            raise ValueError("invalid retained requirements")
        return self

class Section(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12000)
    notes: str = Field(default="", max_length=4000)

class Content(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    sections: list[Section] = Field(min_length=1, max_length=30)


# Content is declared after ExecuteRequest, so resolve that forward reference once here.
ExecuteRequest.model_rebuild()

class WorkResult(StrictModel):
    memory: Memory
    result: dict
    content: Content | None = None

def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

def confirmation_hash(memory: Memory) -> str:
    if memory.outline is None:
        raise ServiceError("OUTLINE_REQUIRED")
    return digest({"requirements": memory.requirements,
                   "outline": memory.outline.model_dump(exclude={"confirmed_hash"})})


def content_hash(content: Content) -> str:
    """Stable digest of a generated body; bound into memory after generate/revise_content."""
    return digest(content.model_dump())
