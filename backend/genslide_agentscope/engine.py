"""Unified AgentScope execution engine powered by the 8-phase RuntimeEngine and ReActAgent."""
from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any
import uuid
from pydantic import Field, ValidationError

from .authoring import (
    FIELDS,
    _OUTPUT_KIND,
    _is_option_selected,
    _is_requirement_affirmed,
    explicit_length,
    prune_catalog_for_decision,
)
from .domain import (
    Content,
    ExecuteRequest,
    Memory,
    Outline,
    OutlineNode,
    ServiceError,
    StrictModel,
    WorkResult,
    content_hash,
)
from .model import Model
from .planning import CompositeGate, GoalLedger
from .prompts import PromptPipeline, create_runtime_pipeline
from .runtime import (
    HookAction,
    HookBase,
    HookContext,
    HookResult,
    Phase,
    ReActAgent,
    ReActResult,
    RuntimeEngine,
)
from .skills import SkillRegistry


BUDGET_CAP = 58000


class OutlineAnswer(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    nodes: list[OutlineNode] = Field(min_length=1, max_length=30)


def _validate_generated(content: Content, outline: Outline, target_kind: str) -> None:
    if content.title != outline.title:
        raise ServiceError("GENERATED_STRUCTURE_MISMATCH", 502)
    if [section.title for section in content.sections] != [node.title for node in outline.nodes]:
        raise ServiceError("GENERATED_STRUCTURE_MISMATCH", 502)
    if target_kind == "presentation" and any(len(section.body) > 1000 for section in content.sections):
        raise ServiceError("SLIDE_CONTENT_TOO_LONG", 502)


def _outline_from_raw(raw: Any) -> OutlineAnswer:
    if not isinstance(raw, dict):
        raise ServiceError("MODEL_OUTPUT_INVALID", 502)
    try:
        draft = OutlineAnswer.model_validate(raw)
    except Exception as exc:
        raise ServiceError("MODEL_OUTPUT_INVALID", 502) from exc
    if len({node.node_id for node in draft.nodes}) != len(draft.nodes):
        raise ServiceError("DUPLICATE_OUTLINE_NODE", 502)
    if len({node.title for node in draft.nodes}) != len(draft.nodes):
        raise ServiceError("DUPLICATE_OUTLINE_NODE", 502)
    return draft


def _outline_for_content(content: Content, previous: Outline | None, skill: dict, kind: str) -> Outline:
    existing = {node.title: node.node_id for node in (previous.nodes if previous else [])}
    used = set(existing.values())
    nodes = []
    for index, section in enumerate(content.sections, 1):
        node_id = existing.get(section.title) or f"sec-{index}"
        while node_id in used and existing.get(section.title) != node_id:
            node_id += "-new"
        used.add(node_id)
        nodes.append(OutlineNode(node_id=node_id, title=section.title))
    return Outline(
        draft_id=previous.draft_id if previous else uuid.uuid4().hex,
        outline_version=(previous.outline_version + 1) if previous else 1,
        title=content.title,
        target_kind=kind,
        nodes=nodes,
        requires_materials=False,
        skill_id=skill["skill_id"],
        skill_version=skill["version"],
        skill_hash=skill["hash"],
    )


def _skill_result(skill: dict, kind: str) -> dict:
    return {
        "skill_id": skill["skill_id"],
        "version": skill["version"],
        "hash": skill["hash"],
        "target_kind": kind,
        "metadata": skill.get("metadata", {}),
        "output": skill.get("output", {}),
    }


def _scope_content_for_turn(memory: Memory, message: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Scope manuscript content so local edits on large manuscripts fit within the 58KB budget."""
    if memory.content is None:
        return None, []

    full_dump = memory.content.model_dump()
    full_bytes = len(json.dumps(full_dump, ensure_ascii=False).encode("utf-8"))
    if full_bytes <= 30000:
        return full_dump, []

    # When manuscript is large, identify targeted sections from user message
    matched_titles: list[str] = []
    msg = message or ""
    for idx, sec in enumerate(memory.content.sections, 1):
        if sec.title in msg or f"第{idx}章" in msg or f"第{idx}节" in msg or f"第{idx}部分" in msg:
            matched_titles.append(sec.title)
        else:
            # Also check significant prefix/tokens of section title
            parts = [p for p in re.split(r"[\s:：、，,-]+", sec.title) if len(p) >= 2]
            if any(p in msg for p in parts):
                matched_titles.append(sec.title)

    if matched_titles:
        target_set = set(matched_titles)
        scoped_sections = [s.model_dump() for s in memory.content.sections if s.title in target_set]
        scoped_content = {
            "title": memory.content.title,
            "sections": scoped_sections,
            "other_section_titles": [s.title for s in memory.content.sections if s.title not in target_set],
        }
        return scoped_content, matched_titles

    return full_dump, []


class _PreDispatchHook(HookBase):
    """Phase 1 (PRE_DISPATCH): Snapshot skill registry and validate skill/output boundaries."""
    name = "engine_pre_dispatch"
    phase = Phase.PRE_DISPATCH
    priority = 90

    def __init__(self, skills_provider):
        self._skills_provider = skills_provider

    async def run(self, ctx: HookContext) -> HookResult:
        request: ExecuteRequest = ctx.request
        memory: Memory = ctx.extras["memory"]

        # Snapshot the registry before the first await so reload cannot mutate an in-flight turn
        live_skills: SkillRegistry = self._skills_provider()
        registry = SkillRegistry.__new__(SkillRegistry)
        registry.skills = deepcopy(live_skills.skills)

        if request.requested_skill_id and request.requested_skill_id not in registry.skills:
            raise ServiceError("SKILL_NOT_FOUND", 422)

        catalog = registry.list_skills()
        pruned_catalog = prune_catalog_for_decision(
            catalog, request.requested_skill_id, memory.skill_id, request.requested_output
        )

        # Resolve initial target_kind and bound skill
        if request.requested_output != "auto":
            initial_kind = _OUTPUT_KIND[request.requested_output]
        elif memory.target_kind in {"writing", "document", "presentation"}:
            initial_kind = memory.target_kind
        else:
            candidate_id = request.requested_skill_id or (pruned_catalog[0]["skill_id"] if pruned_catalog else None)
            cand_skill = registry.skills.get(candidate_id) if candidate_id else None
            if cand_skill and cand_skill.get("target_kind") in {"writing", "document", "presentation"}:
                initial_kind = cand_skill["target_kind"]
            elif cand_skill and cand_skill.get("default_output") in _OUTPUT_KIND:
                initial_kind = _OUTPUT_KIND[cand_skill["default_output"]]
            else:
                initial_kind = "document"

        req_out_token = "text" if initial_kind == "writing" else initial_kind
        selected_id = request.requested_skill_id
        if selected_id is None and memory.skill_id:
            mem_skill = registry.skills.get(memory.skill_id)
            if mem_skill and req_out_token in mem_skill.get("supported_outputs", []):
                selected_id = memory.skill_id

        if selected_id and selected_id == memory.skill_id and not request.requested_skill_id and memory.skill_version and memory.skill_hash:
            skill = registry.get_bound(initial_kind, selected_id, memory.skill_version, memory.skill_hash)
        else:
            skill = registry.get(initial_kind, selected_id, query=request.message)

        ctx.extras["registry"] = registry
        ctx.extras["pruned_catalog"] = pruned_catalog
        ctx.extras["initial_kind"] = initial_kind
        ctx.extras["initial_skill"] = skill
        return HookResult(action=HookAction.CONTINUE)


class _PostDispatchHook(HookBase):
    """Phase 2 (POST_DISPATCH): Restore or initialize GoalLedger and affirm explicit user requirements."""
    name = "engine_post_dispatch"
    phase = Phase.POST_DISPATCH
    priority = 90

    async def run(self, ctx: HookContext) -> HookResult:
        request: ExecuteRequest = ctx.request
        memory: Memory = ctx.extras["memory"]

        stated = explicit_length(request.message)
        if stated is not None and _is_requirement_affirmed(request.message, stated, memory.pending_options):
            memory.requirements["length"] = stated

        if memory.ledger is not None:
            ledger = memory.ledger.model_copy(deep=True)
            if request.message and not ledger.goal:
                ledger.goal = request.message[:2000]
        else:
            goal_text = request.message or memory.requirements.get("topic", "Fulfil user request")
            ledger = GoalLedger(goal=goal_text[:2000])

        ctx.extras["ledger"] = ledger
        return HookResult(action=HookAction.CONTINUE)


class _PreAgentBuildHook(HookBase):
    """Phase 3 (PRE_AGENT_BUILD): Enforce 58KB context budget and prepare prompt/turn payload."""
    name = "engine_pre_agent_build"
    phase = Phase.PRE_AGENT_BUILD
    priority = 90

    async def run(self, ctx: HookContext) -> HookResult:
        request: ExecuteRequest = ctx.request
        memory: Memory = ctx.extras["memory"]
        materials: str = ctx.extras.get("raw_materials", "")
        skill: dict = ctx.extras["initial_skill"]
        pruned_catalog: list[dict] = ctx.extras["pruned_catalog"]

        scoped_content, auto_scoped_titles = _scope_content_for_turn(memory, request.message)
        ctx.extras["auto_scoped_titles"] = auto_scoped_titles

        summary = {
            "requirements": memory.requirements,
            "target_kind": memory.target_kind,
            "skill_id": memory.skill_id,
            "section_titles": [s.title for s in memory.content.sections] if memory.content else [],
            "outline": memory.outline.model_dump(exclude={"confirmed_hash"}) if memory.outline else None,
            "last_reply": memory.last_reply,
            "pending_options": memory.pending_options,
        }

        base_turn_payload = {
            "message": request.message,
            "last_reply": memory.last_reply,
            "requirements": memory.requirements,
            "outline": summary["outline"],
            "current_content": scoped_content,
            "context": summary,
            "requested_output": request.requested_output,
            "requested_skill_id": request.requested_skill_id,
            "skills": pruned_catalog,
            "has_materials": bool(materials),
            "materials": "",
        }
        # Reserve space for ReActAgent wrapper fields (iteration, goal, history, instruction)
        react_overhead_probe = {
            "iteration": 0,
            "goal": ctx.extras["ledger"].goal,
            "history": [],
            "instruction": (
                "Choose next action. Return JSON conforming to ReActStep: "
                "{'thought': str, 'action': 'call_tool'|'update_task'|'plan_tasks'|'final_reply', "
                "'action_input': dict}"
            ),
            **base_turn_payload,
        }
        base_bytes = len(json.dumps(react_overhead_probe, ensure_ascii=False).encode("utf-8"))
        if base_bytes > BUDGET_CAP:
            raise ServiceError("MODEL_CONTEXT_TOO_LARGE", 413)

        safe_materials = materials
        assumptions: list[str] = []
        if materials:
            available_mat_bytes = max(0, BUDGET_CAP - base_bytes - 200)
            notice = "\n\n[材料已按单次安全预算做有界截取]"
            notice_bytes = len(notice.encode("utf-8"))
            mat_encoded = materials.encode("utf-8")

            truncated = False
            if len(mat_encoded) > available_mat_bytes:
                slice_bytes = max(0, available_mat_bytes - notice_bytes)
                safe_materials = mat_encoded[:slice_bytes].decode("utf-8", errors="ignore") + notice
                truncated = True

            test_probe = dict(react_overhead_probe)
            test_probe["materials"] = safe_materials
            test_encoded = json.dumps(test_probe, ensure_ascii=False).encode("utf-8")

            while len(test_encoded) > BUDGET_CAP and len(safe_materials) > len(notice):
                overflow = len(test_encoded) - BUDGET_CAP
                cur_bytes = len(safe_materials.encode("utf-8"))
                target_bytes = max(notice_bytes, cur_bytes - overflow - 500)
                inner_slice = max(0, target_bytes - notice_bytes)
                safe_materials = safe_materials.encode("utf-8")[:inner_slice].decode("utf-8", errors="ignore") + notice
                test_probe["materials"] = safe_materials
                test_encoded = json.dumps(test_probe, ensure_ascii=False).encode("utf-8")
                truncated = True

            if truncated:
                assumptions.append("由于单轮输入容量限制，参考材料已按安全预算做有界截取")

        turn_payload = dict(base_turn_payload)
        turn_payload["materials"] = safe_materials

        ctx.extras["turn_payload"] = turn_payload
        ctx.extras["budget_assumptions"] = assumptions
        ctx.extras["active_skill_name"] = skill["skill_id"]
        ctx.extras["skill_instructions"] = skill["content"]
        ctx.extras["environment_info"] = {
            "target_kind": ctx.extras["initial_kind"],
            "confirmed_requirements": memory.requirements,
            "has_existing_manuscript": memory.content is not None,
            "pending_options": memory.pending_options,
        }
        return HookResult(action=HookAction.CONTINUE)


class _PostAgentBuildHook(HookBase):
    """Phase 4 (POST_AGENT_BUILD): Assemble PromptPipeline and ReActAgent instance."""
    name = "engine_post_agent_build"
    phase = Phase.POST_AGENT_BUILD
    priority = 90

    def __init__(self, model_provider, pipeline_provider, tool_handler_provider):
        self._model_provider = model_provider
        self._pipeline_provider = pipeline_provider
        self._tool_handler_provider = tool_handler_provider

    async def run(self, ctx: HookContext) -> HookResult:
        agent = ReActAgent(
            model=self._model_provider(),
            prompt_pipeline=self._pipeline_provider(),
            gates=CompositeGate(),
            tool_handler=self._tool_handler_provider(),
        )
        ctx.extras["react_agent"] = agent
        return HookResult(action=HookAction.CONTINUE)


class _PreExecuteHook(HookBase):
    """Phase 5 (PRE_EXECUTE): Emit progress before entering ReAct loop."""
    name = "engine_pre_execute"
    phase = Phase.PRE_EXECUTE
    priority = 90

    async def run(self, ctx: HookContext) -> HookResult:
        progress = ctx.extras.get("progress")
        if progress is not None:
            await progress("running_react_agent", {"session_id": ctx.session_id})
        return HookResult(action=HookAction.CONTINUE)


class _PostResponseHook(HookBase):
    """Phase 6 (POST_RESPONSE): Validate ReActResult, reconcile Memory & GoalLedger, and build WorkResult."""
    name = "engine_post_response"
    phase = Phase.POST_RESPONSE
    priority = 90

    async def run(self, ctx: HookContext) -> HookResult:
        request: ExecuteRequest = ctx.request
        memory: Memory = ctx.extras["memory"]
        registry: SkillRegistry = ctx.extras["registry"]
        materials: str = ctx.extras.get("raw_materials", "")
        react_result: ReActResult = ctx.response

        if not isinstance(react_result, ReActResult):
            raise ServiceError("ENGINE_CONTRACT_ERROR", 502)

        if react_result.status != "completed":
            raise ServiceError("MODEL_OUTPUT_INVALID", 502)

        raw = dict(react_result.final_output)
        if not raw and react_result.final_reply:
            raw = {"effect": "reply", "reply": react_result.final_reply}

        # Resolve effect
        effect = raw.get("effect") or raw.get("kind")
        if effect is None:
            legacy = raw.get("skill")
            if legacy in {"generate", "revise"}:
                effect = "deliverable"
            elif legacy in {"reply", "outline", "deliverable"}:
                effect = legacy
            elif raw.get("deliverable") is not None or raw.get("content") is not None:
                effect = "deliverable"
            elif raw.get("outline") is not None:
                effect = "outline"
            else:
                effect = "reply"

        if effect not in {"reply", "outline", "deliverable"}:
            raise ServiceError("MODEL_OUTPUT_INVALID", 502)

        # Resolve target_kind and skill
        kind = raw.get("target_kind") or ctx.extras["initial_kind"]
        if kind not in {"writing", "document", "presentation"}:
            raise ServiceError("MODEL_OUTPUT_INVALID", 502)

        if request.requested_output != "auto" and kind != _OUTPUT_KIND[request.requested_output]:
            raise ServiceError("OUTPUT_INTENT_MISMATCH", 422)

        model_skill_id = raw.get("skill_id")
        if request.requested_skill_id and model_skill_id not in (None, request.requested_skill_id):
            raise ServiceError("ENGINE_CONTRACT_ERROR", 502)

        selected_skill_id = request.requested_skill_id or model_skill_id or memory.skill_id
        skill = registry.get(kind, selected_skill_id, query=request.message)
        if (
            selected_skill_id
            and selected_skill_id == memory.skill_id
            and not request.requested_skill_id
            and memory.skill_version
            and memory.skill_hash
        ):
            skill = registry.get_bound(kind, selected_skill_id, memory.skill_version, memory.skill_hash)

        if raw.get("needs_materials") and not materials:
            raise ServiceError("MISSING_CURRENT_FILES", 422)
        if raw.get("needs_full_content") and memory.content is None:
            raise ServiceError("CURRENT_CONTENT_REQUIRED", 422)

        # Apply affirmed requirement updates
        req_updates = raw.get("requirement_updates")
        valid_updates: dict[str, str] = {}
        if isinstance(req_updates, dict):
            valid_updates = {
                k: v
                for k, v in req_updates.items()
                if isinstance(k, str)
                and isinstance(v, str)
                and k in FIELDS
                and v.strip()
                and len(v) <= 2000
                and _is_requirement_affirmed(request.message, v, memory.pending_options)
            }
            memory.requirements.update(valid_updates)

        # Validate edit_scope if provided
        edit_scope = raw.get("edit_scope") or []
        if not isinstance(edit_scope, list) or any(not isinstance(x, str) for x in edit_scope):
            raise ServiceError("MODEL_OUTPUT_INVALID", 502)
        titles = [s.title for s in memory.content.sections] if memory.content else []
        if len(set(edit_scope)) != len(edit_scope) or set(edit_scope) - set(titles):
            raise ServiceError("EDIT_SCOPE_INVALID", 422)
        if edit_scope and (effect != "deliverable" or memory.target_kind != kind):
            raise ServiceError("EDIT_SCOPE_INVALID", 422)

        reply = raw.get("reply", raw.get("reply_text", raw.get("answer", react_result.final_reply)))
        if reply is None:
            reply = ""
        if not isinstance(reply, str) or len(reply) > 12000:
            raise ServiceError("MODEL_OUTPUT_INVALID", 502)
        reply = reply.strip()

        raw_output_meta = raw.get("output", raw.get("output_metadata", {}))
        if not isinstance(raw_output_meta, dict):
            raise ServiceError("MODEL_OUTPUT_INVALID", 502)

        deliverable_raw = raw.get("deliverable", raw.get("content"))
        if isinstance(deliverable_raw, dict) and isinstance(deliverable_raw.get("content"), dict):
            deliverable_raw = deliverable_raw["content"]

        if effect == "reply" and not reply:
            raise ServiceError("MODEL_OUTPUT_INVALID", 502)
        if effect != "deliverable" and deliverable_raw is not None:
            raise ServiceError("ENGINE_CONTRACT_ERROR", 502)

        content: Content | None = None
        if effect == "outline":
            draft = _outline_from_raw(raw.get("outline"))
            previous = memory.outline
            memory.outline = Outline(
                draft_id=previous.draft_id if previous else uuid.uuid4().hex,
                outline_version=previous.outline_version + 1 if previous else 1,
                title=draft.title,
                target_kind=kind,
                nodes=draft.nodes,
                skill_id=skill["skill_id"],
                skill_version=skill["version"],
                skill_hash=skill["hash"],
            )
        elif effect == "deliverable":
            try:
                content = Content.model_validate(deliverable_raw)
            except Exception as exc:
                raise ServiceError("MODEL_OUTPUT_INVALID", 502) from exc
            if len(set(s.title for s in content.sections)) != len(content.sections):
                raise ServiceError("GENERATED_STRUCTURE_MISMATCH", 502)

            effective_scope = edit_scope
            if not effective_scope and memory.content is not None:
                auto_scoped = ctx.extras.get("auto_scoped_titles") or []
                if auto_scoped and [s.title for s in content.sections] == auto_scoped:
                    effective_scope = auto_scoped

            if effective_scope:
                if content.title != memory.content.title or [s.title for s in content.sections] != effective_scope:
                    raise ServiceError("GENERATED_STRUCTURE_MISMATCH", 502)
                replacements = {s.title: s for s in content.sections}
                content = Content(
                    title=memory.content.title,
                    sections=[
                        replacements.get(s.title, s).model_copy(deep=True)
                        for s in memory.content.sections
                    ],
                )
            memory.outline = _outline_for_content(content, memory.outline, skill, kind)
            _validate_generated(content, memory.outline, kind)
            memory.content = content
            memory.content_hash = content_hash(content)

        if effect != "reply" or memory.content is None:
            memory.target_kind = kind
            memory.skill_id = skill["skill_id"]
            memory.skill_version = skill["version"]
            memory.skill_hash = skill["hash"]

        # Update conversational continuity & GoalLedger in memory
        memory.last_reply = reply[:2000] if reply else None
        raw_pending = raw.get("pending_options")
        if isinstance(raw_pending, dict) and raw_pending:
            memory.pending_options = {
                str(k): str(v)
                for k, v in raw_pending.items()
                if len(str(k)) <= 100 and len(str(v)) <= 500
            }
        elif valid_updates:
            adopted = set(valid_updates.values())
            memory.pending_options = {
                k: v for k, v in memory.pending_options.items() if v not in adopted
            }

        memory.ledger = react_result.ledger

        assumptions: list[str] = list(ctx.extras.get("budget_assumptions", []))
        raw_assumptions = raw.get("user_visible_assumptions")
        if isinstance(raw_assumptions, list):
            for item in raw_assumptions[:5]:
                if isinstance(item, str) and item not in assumptions:
                    assumptions.append(item)

        output = {
            "target_kind": kind,
            "format": {"writing": "markdown", "document": "docx", "presentation": "pptx"}[kind],
        }
        result_payload = {
            "effect": effect,
            "reply": reply,
            "outline": (
                memory.outline.model_dump(exclude={"confirmed_hash"})
                if effect == "outline" and memory.outline
                else None
            ),
            "requirements": memory.requirements,
            "skill": _skill_result(skill, kind),
            "user_visible_assumptions": assumptions,
            "output": output,
            "react_iterations": react_result.iterations,
        }
        ctx.response = WorkResult(
            memory=memory,
            result=result_payload,
            content=content,
            effect=effect,
            reply=reply,
            outline=memory.outline,
            deliverable=content,
            output=output,
        )
        return HookResult(action=HookAction.CONTINUE, response=ctx.response)


class Engine:
    """Default AgentScope execution engine integrated with the 8-phase RuntimeEngine and ReActAgent."""
    name = "agentscope"

    def __init__(self, model=None, settings=None, tool_handler=None):
        if model is not None:
            self.model = model
        else:
            self.model = Model(settings=settings)
        self.skills = SkillRegistry()
        self.tool_handler = tool_handler
        self.prompt_pipeline: PromptPipeline = create_runtime_pipeline()
        self.runtime = self._build_runtime()
        self.committed: dict[str, Memory] = {}
        self.turn_counts: dict[str, int] = {}

    def _build_runtime(self) -> RuntimeEngine:
        runtime = RuntimeEngine()
        runtime.register_hook(_PreDispatchHook(lambda: self.skills))
        runtime.register_hook(_PostDispatchHook())
        runtime.register_hook(_PreAgentBuildHook())
        runtime.register_hook(
            _PostAgentBuildHook(
                lambda: self.model,
                lambda: self.prompt_pipeline,
                lambda: self.tool_handler,
            )
        )
        runtime.register_hook(_PreExecuteHook())
        runtime.register_hook(_PostResponseHook())

        async def _executor(ctx: HookContext) -> ReActResult:
            agent: ReActAgent = ctx.extras["react_agent"]
            ledger: GoalLedger = ctx.extras["ledger"]
            return await agent.execute_turn(ctx, ledger=ledger)

        runtime.set_executor(_executor)
        return runtime

    def list_skills(self):
        return self.skills.list_skills()

    def reload_skills(self):
        return self.skills.reload()

    async def read(self, key: str) -> Memory | None:
        memory = self.committed.get(key)
        return memory.model_copy(deep=True) if memory is not None else None

    async def run(
        self,
        key: str,
        request: ExecuteRequest,
        memory: Memory,
        materials: str,
        progress=None,
    ) -> WorkResult:
        ctx = HookContext(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            session_id=request.session_id,
            action_id=request.action_id,
            request=request,
            extras={
                "session_key": key,
                "memory": memory.model_copy(deep=True),
                "raw_materials": materials,
                "progress": progress,
            },
        )
        try:
            return await self.runtime.run(ctx)
        except ServiceError:
            raise
        except (ValidationError, ValueError) as exc:
            raise ServiceError("MODEL_OUTPUT_INVALID", 502) from exc

    async def publish(self, key: str, work: WorkResult) -> None:
        self.committed[key] = Memory.model_validate(work.memory.model_dump())
        self.turn_counts[key] = self.turn_counts.get(key, 0) + 1

    async def delete(self, key: str) -> None:
        self.committed.pop(key, None)
        self.turn_counts.pop(key, None)

    async def aclose(self) -> None:
        self.committed.clear()
        self.turn_counts.clear()
        if hasattr(self.model, "aclose"):
            await self.model.aclose()
