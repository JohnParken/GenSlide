"""Public contract and whitelisted runtime state. No framework-specific objects."""
from __future__ import annotations
import hashlib
import json
from typing import Any, Literal, Mapping
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

Kind = Literal["writing", "document", "presentation"]
Effect = Literal["reply", "outline", "deliverable"]
class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class ServiceError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)

class ExecuteRequest(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    api_contract_version: Literal["1"] = "1"
    engine: Literal["agentscope"]
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    runtime_epoch: str = Field(min_length=1, max_length=128)
    action_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    authorization: SecretStr
    expected_session_version: int = Field(ge=0)
    expected_lifecycle_version: int = Field(ge=1)
    mode: Literal["assistant"] = "assistant"
    message: str = Field(default="", max_length=16000)
    current_file_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=10)
    requested_output: Literal["auto", "text", "document", "presentation"] = "auto"
    requested_skill_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def bounded_inputs(self):
        if any(not x or len(x) > 256 for x in self.current_file_ids):
            raise ValueError("invalid file ID")
        if len(set(self.current_file_ids)) != len(self.current_file_ids):
            raise ValueError("duplicate file ID")
        if self.requested_skill_id is not None and not self.requested_skill_id.strip():
            raise ValueError("invalid skill ID")
        return self

    @property
    def target_kind(self) -> Kind:
        """Compatibility view for internal rendering; the public request uses output intent."""
        return {"text": "writing", "document": "document", "presentation": "presentation"}.get(
            self.requested_output, "document"
        )

    def session_key(self) -> str:
        raw = [self.tenant_id, self.user_id, self.session_id, self.runtime_epoch]
        return digest(raw)

    def identity(self) -> dict:
        return self.model_dump(include={"tenant_id", "user_id", "session_id", "runtime_epoch", "action_id", "engine", "expected_session_version", "expected_lifecycle_version", "api_contract_version", "mode"})

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
    next_actions: list[str] = Field(default_factory=lambda: ["assistant_turn"], max_length=3)

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
    # the BFF snapshot; the bounded current body is available only when the trusted snapshot
    # explicitly carries it for a follow-up turn.
    content_hash: str | None = Field(default=None, max_length=128)
    content: Content | None = None
    target_kind: Kind | None = None
    skill_id: str | None = Field(default=None, max_length=128)
    skill_version: str | None = Field(default=None, max_length=64)
    skill_hash: str | None = Field(default=None, max_length=128)

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
Memory.model_rebuild()


class ExecutionSnapshot(StrictModel):
    """Trusted BFF snapshot: only state needed to continue a turn.

    Conversation guidance and confirmation markers are intentionally excluded.  The BFF
    owns lifecycle/session arbitration; this object is a bounded data handoff, not authority.
    """

    schema_version: Literal[1] = 1
    target_kind: Kind | None = None
    requirements: dict[str, str] = Field(default_factory=dict)
    outline: dict[str, Any] | None = None
    content: Content | None = None
    content_hash: str | None = Field(default=None, max_length=128)
    skill_id: str | None = Field(default=None, max_length=128)
    skill_version: str | None = Field(default=None, max_length=64)
    skill_hash: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_snapshot(self):
        allowed = {"topic", "audience", "language", "length", "style", "purpose", "constraints"}
        if set(self.requirements) - allowed or any(len(v) > 2000 for v in self.requirements.values()):
            raise ValueError("invalid snapshot requirements")
        if self.outline is not None:
            if "confirmed_hash" in self.outline or "guidance" in self.outline:
                raise ValueError("snapshot contains transient outline state")
            try:
                Outline.model_validate(self.outline)
            except Exception as exc:
                raise ValueError("invalid snapshot outline") from exc
        if self.content is not None and self.content_hash != content_hash(self.content):
            raise ValueError("snapshot content hash mismatch")
        return self


def snapshot_from_memory(memory: Memory) -> dict[str, Any]:
    """Build the minimal trusted snapshot sent back with a committed result."""
    outline = memory.outline.model_dump(exclude={"confirmed_hash"}) if memory.outline else None
    value = ExecutionSnapshot(
        target_kind=memory.target_kind,
        requirements=memory.requirements,
        outline=outline,
        content=memory.content,
        content_hash=memory.content_hash,
        skill_id=memory.skill_id,
        skill_version=memory.skill_version,
        skill_hash=memory.skill_hash,
    )
    return value.model_dump(exclude_none=True)


def memory_from_snapshot(snapshot: Mapping[str, Any]) -> Memory:
    """Restore only the whitelisted fields from a claim-provided snapshot."""
    checked = ExecutionSnapshot.model_validate(snapshot)
    outline = None
    if checked.outline is not None:
        outline = Outline.model_validate(checked.outline)
    return Memory(
        target_kind=checked.target_kind,
        requirements=checked.requirements,
        outline=outline,
        content=checked.content,
        content_hash=checked.content_hash,
        skill_id=checked.skill_id,
        skill_version=checked.skill_version,
        skill_hash=checked.skill_hash,
    )

class WorkResult(StrictModel):
    memory: Memory
    result: dict
    content: Content | None = None
    # Unified assistant turns use an explicit effect.  The legacy `result` payload
    # remains available to older callers while these fields make rendering and BFF
    # persistence independent from operation names.
    effect: Effect = "reply"
    reply: str = Field(default="", max_length=12000)
    outline: Outline | None = None
    deliverable: Content | None = None
    output: dict[str, Any] = Field(default_factory=dict)


class AssistantTurn(StrictModel):
    """Validated model output for one conversational assistant turn."""

    effect: Effect
    reply: str = Field(default="", max_length=12000)
    outline: Outline | None = None
    deliverable: Content | None = None
    skill_id: str | None = Field(default=None, max_length=128)
    output: dict[str, Any] = Field(default_factory=dict)


# A descriptive alias used by adapters and tests that call the turn result a decision.
TurnResult = AssistantTurn

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
