"""Stop handlers, budget gates, and doom-loop prevention for long-horizon loops.

Prevents runaway iterations, token exhaustion, and repetitive failure loops.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Protocol, Sequence

from .ledger import GoalLedger


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Decision produced by a stop gate after each reasoning iteration."""
    should_stop: bool
    status: str = "continue"  # e.g., "completed", "max_iterations_exceeded", "doom_loop", "budget_exceeded"
    reason: str = ""


class StopGate(Protocol):
    """Protocol for loop stopping evaluators."""
    name: str

    def evaluate(self, iteration: int, history: Sequence[dict[str, Any]], ledger: GoalLedger | None) -> GateDecision:
        ...


class MaxIterationsGate:
    """Stops execution when loop exceeds maximum allowed turns."""
    name = "max_iterations_gate"

    def __init__(self, max_iterations: int = 20) -> None:
        if max_iterations <= 0:
            raise ValueError("max_iterations must be positive")
        self.max_iterations = max_iterations

    def evaluate(self, iteration: int, history: Sequence[dict[str, Any]], ledger: GoalLedger | None) -> GateDecision:
        if iteration >= self.max_iterations:
            return GateDecision(
                should_stop=True,
                status="max_iterations_exceeded",
                reason=f"Reached maximum allowed iterations ({self.max_iterations})",
            )
        return GateDecision(should_stop=False)


class DoomLoopGate:
    """Detects repetitive, identical failing actions in consecutive turns."""
    name = "doom_loop_gate"

    def __init__(self, repetition_threshold: int = 3) -> None:
        self.repetition_threshold = repetition_threshold

    def _hash_action(self, step: dict[str, Any]) -> str:
        # Check standard ReActStep action_input first, then fallback to parameters
        params = step.get("action_input")
        if params is None:
            params = step.get("parameters")
        payload = {
            "action": step.get("action"),
            "params": params,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
        ).hexdigest()

    def evaluate(self, iteration: int, history: Sequence[dict[str, Any]], ledger: GoalLedger | None) -> GateDecision:
        if len(history) < self.repetition_threshold:
            return GateDecision(should_stop=False)

        recent = history[-self.repetition_threshold:]
        hashes = [self._hash_action(step) for step in recent]

        # If recent consecutive turns have identical actions
        if len(set(hashes)) == 1:
            return GateDecision(
                should_stop=True,
                status="doom_loop",
                reason=f"Doom loop detected: identical action repeated {self.repetition_threshold} times consecutively",
            )

        return GateDecision(should_stop=False)


class CompletionRubricGate:
    """Evaluates whether all planned tasks in the ledger are completed.

    Allows one summary step for the agent to deliver a final user-facing reply
    after marking all tasks complete, preventing premature termination with an empty reply.
    """
    name = "completion_rubric_gate"

    def __init__(self, allow_summary_turn: bool = True) -> None:
        self.allow_summary_turn = allow_summary_turn

    def evaluate(self, iteration: int, history: Sequence[dict[str, Any]], ledger: GoalLedger | None) -> GateDecision:
        if ledger is None or not ledger.is_all_completed():
            return GateDecision(should_stop=False)

        # Stop immediately only if a VALID user-facing final reply with non-empty content was delivered
        def _is_valid_final_reply_step(step: dict[str, Any]) -> bool:
            if step.get("action") != "final_reply":
                return False
            params = step.get("action_input")
            if params is None:
                params = step.get("parameters")
            if isinstance(params, dict):
                reply_text = params.get("reply")
                return bool(reply_text and str(reply_text).strip())
            return False

        has_valid_final_reply = any(_is_valid_final_reply_step(step) for step in history)
        if has_valid_final_reply:
            return GateDecision(
                should_stop=True,
                status="completed",
                reason="All milestone tasks in the goal ledger are marked as completed and valid final reply provided",
            )

        # If allow_summary_turn is True, check when the last task was completed
        if self.allow_summary_turn:
            # Count steps after all tasks were completed
            # If this is the immediate next step after task completion, allow one summary step
            # Find the step index where ledger became all-completed
            # We allow 1 follow-up turn to deliver the summary reply
            update_steps = [i for i, step in enumerate(history) if step.get("action") == "update_task"]
            last_update_idx = update_steps[-1] if update_steps else -1
            steps_after_completion = len(history) - (last_update_idx + 1)

            if steps_after_completion == 0:
                # Give the agent one opportunity to deliver the final reply
                return GateDecision(should_stop=False)

            # Allow 1 recovery turn if the agent attempted final_reply but needs to fix missing reply content
            if steps_after_completion == 1 and history and history[-1].get("action") == "final_reply":
                return GateDecision(should_stop=False)

        return GateDecision(
            should_stop=True,
            status="completed",
            reason="All milestone tasks in the goal ledger are marked as completed",
        )


class CompositeGate:
    """Combines multiple gates, stopping if any gate indicates termination."""

    def __init__(self, gates: Sequence[StopGate] | None = None) -> None:
        if gates is None:
            self.gates = [
                MaxIterationsGate(),
                DoomLoopGate(),
                CompletionRubricGate(),
            ]
        else:
            self.gates = list(gates)

    def evaluate(self, iteration: int, history: Sequence[dict[str, Any]], ledger: GoalLedger | None) -> GateDecision:
        for gate in self.gates:
            decision = gate.evaluate(iteration, history, ledger)
            if decision.should_stop:
                return decision
        return GateDecision(should_stop=False)
