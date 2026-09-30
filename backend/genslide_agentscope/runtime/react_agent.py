"""General long-horizon ReAct execution loop.

Coordinates Thought -> Action -> Observation -> Reflection with stop gates,
self-healing output repair, and dynamic prompt pipeline injection.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Sequence
from pydantic import BaseModel, ConfigDict, Field

from ..gateway import loads_repaired, sanitize_model_output
from ..prompts import PromptContext, PromptPipeline, create_default_pipeline
from ..planning import GoalLedger, CompositeGate, GateDecision
from .hooks import HookContext

logger = logging.getLogger(__name__)

ToolHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


class ReActStep(BaseModel):
    """Structured representation of one ReAct reasoning and action step."""
    model_config = ConfigDict(extra="ignore")

    thought: str = Field(default="", description="Internal reasoning about current situation and next step")
    action: str = Field(
        default="final_reply",
        description="Action to take: 'call_tool', 'update_task', or 'final_reply'",
    )
    action_input: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters for the selected action",
    )
    observation: str = Field(default="", description="Outcome observed from executing the action")


class ReActResult(BaseModel):
    """Final outcome of the long-horizon ReAct agent run."""
    model_config = ConfigDict(extra="ignore")

    status: str = "completed"
    final_reply: str = ""
    iterations: int = 0
    history: list[dict[str, Any]] = Field(default_factory=list)
    ledger: GoalLedger | None = None
    stop_reason: str = ""


class ReActAgent:
    """General ReAct loop coordinator for cloud long-horizon tasks."""

    def __init__(
        self,
        model: Any,
        prompt_pipeline: PromptPipeline | None = None,
        gates: CompositeGate | None = None,
        tool_handler: ToolHandler | None = None,
    ) -> None:
        self.model = model
        self.prompt_pipeline = prompt_pipeline if prompt_pipeline is not None else create_default_pipeline()
        self.gates = gates if gates is not None else CompositeGate()
        self.tool_handler = tool_handler

    async def execute_turn(
        self,
        ctx: HookContext,
        ledger: GoalLedger | None = None,
        max_turns: int = 20,
    ) -> ReActResult:
        """Execute the multi-step ReAct loop until a stop condition is reached."""
        active_ledger = ledger if ledger is not None else GoalLedger(goal=getattr(ctx.request, "message", ""))
        step_history: list[dict[str, Any]] = []
        iteration = 0
        final_reply = ""
        stop_reason = ""
        run_status = "continue"

        while True:
            # 0. Enforce explicit invocation-level hard turn budget
            if iteration >= max_turns:
                run_status = "max_iterations_exceeded"
                stop_reason = f"Reached turn-level maximum iterations limit ({max_turns})"
                break

            # 1. Evaluate termination gates
            gate_decision: GateDecision = self.gates.evaluate(iteration, step_history, active_ledger)
            if gate_decision.should_stop:
                logger.info(
                    "ReAct loop stopped at iteration %s: %s (status: %s)",
                    iteration, gate_decision.reason, gate_decision.status
                )
                run_status = gate_decision.status
                stop_reason = gate_decision.reason
                break

            # 2. Build prioritized system prompt
            prompt_ctx = PromptContext(
                session_id=ctx.session_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                goal=active_ledger.goal,
                milestones=active_ledger.to_milestones_tuple(),
                user_message=getattr(ctx.request, "message", ""),
            )
            system_prompt = self.prompt_pipeline.build_system_prompt(prompt_ctx)

            # 3. Assemble current iteration payload
            payload = {
                "iteration": iteration,
                "goal": active_ledger.goal,
                "history": step_history,
                "instruction": (
                    "Choose next action. Return JSON conforming to ReActStep: "
                    "{'thought': str, 'action': 'call_tool'|'update_task'|'final_reply', "
                    "'action_input': dict}"
                ),
            }

            # 4. Invoke model with cancellation checking
            if asyncio.current_task() and asyncio.current_task().cancelling():
                raise asyncio.CancelledError()

            try:
                raw_response = await self.model.complete(system_prompt, payload)
            except Exception as exc:
                logger.error("Model invocation failed during ReAct step %s: %s", iteration, exc)
                raise

            # 5. Parse and self-heal model output
            if isinstance(raw_response, dict):
                step_dict = raw_response
            else:
                step_dict = loads_repaired(str(raw_response))

            step = ReActStep.model_validate(step_dict)

            # 6. Execute action
            observation = ""
            action = step.action.strip().lower()

            if action == "final_reply":
                reply_val = step.action_input.get("reply")
                if not reply_val or not str(reply_val).strip():
                    step.observation = (
                        "Error: 'final_reply' requires a non-empty 'reply' parameter in action_input. "
                        "Do not leak internal thoughts; provide the explicit user-facing response."
                    )
                    step_history.append(step.model_dump())
                    iteration += 1
                    continue

                final_reply = str(reply_val).strip()
                step.observation = "Completed final reply."
                step_history.append(step.model_dump())
                iteration += 1
                run_status = "completed"
                stop_reason = "Agent provided final reply"
                break

            elif action == "update_task":
                task_id = str(step.action_input.get("task_id", ""))
                new_status = str(step.action_input.get("status", "completed")).lower()
                summary = str(step.action_input.get("summary", ""))

                try:
                    if new_status == "completed":
                        active_ledger.complete_task(task_id, summary)
                        observation = f"Task {task_id} successfully marked as completed."
                    elif new_status == "in_progress":
                        active_ledger.start_task(task_id)
                        observation = f"Task {task_id} marked as in_progress."
                    elif new_status == "blocked":
                        active_ledger.block_task(task_id, summary)
                        observation = f"Task {task_id} marked as blocked: {summary}"
                    else:
                        observation = f"Unknown task status {new_status}."
                except KeyError:
                    observation = f"Task {task_id} not found in ledger."

            elif action == "call_tool":
                tool_name = str(step.action_input.get("tool_name", ""))
                tool_params = step.action_input.get("parameters", {})
                if self.tool_handler is not None:
                    try:
                        tool_res = await self.tool_handler(tool_name, tool_params)
                        observation = f"Tool '{tool_name}' output: {tool_res}"
                    except Exception as tool_exc:
                        observation = f"Tool '{tool_name}' failed with error: {tool_exc}"
                else:
                    observation = f"Tool '{tool_name}' executed (mock)."

            else:
                observation = f"Unrecognized action: {action}. Please use 'call_tool', 'update_task', or 'final_reply'."

            step.observation = observation
            step_history.append(step.model_dump())
            iteration += 1

        return ReActResult(
            status=run_status,
            final_reply=final_reply,
            iterations=iteration,
            history=step_history,
            ledger=active_ledger,
            stop_reason=stop_reason,
        )
