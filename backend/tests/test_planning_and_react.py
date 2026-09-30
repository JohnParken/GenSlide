"""Unit tests for Phase 3: GoalLedger, StopGates, and ReActAgent execution loop."""

import pytest
from genslide_agentscope.planning import (
    TaskStatus,
    TaskItem,
    GoalLedger,
    MaxIterationsGate,
    DoomLoopGate,
    CompletionRubricGate,
    CompositeGate,
)
from genslide_agentscope.prompts import PromptContext, GoalLedgerContributor
from genslide_agentscope.runtime import (
    HookContext,
    RuntimeEngine,
    ReActAgent,
    ReActStep,
    ReActResult,
)


class MockModel:
    """Mock model that returns sequential canned responses."""

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.call_history: list[tuple[str, dict]] = []

    async def complete(self, system: str, payload: dict) -> dict:
        self.call_history.append((system, payload))
        if self.responses:
            return self.responses.pop(0)
        return {"action": "final_reply", "action_input": {"reply": "Fallback finish"}}


def test_goal_ledger_lifecycle():
    ledger = GoalLedger(goal="Deploy Autonomous Agent")
    assert ledger.goal == "Deploy Autonomous Agent"
    assert len(ledger.tasks) == 0

    t1 = ledger.add_task("Collect requirements", "Gather initial input")
    t2 = ledger.add_task("Develop core logic", "Write runtime code")

    assert t1.status == TaskStatus.PENDING
    assert ledger.progress_ratio() == 0.0
    assert not ledger.is_all_completed()

    # Start task 1
    ledger.start_task(t1.task_id)
    assert ledger.active_task is not None
    assert ledger.active_task.task_id == t1.task_id
    assert ledger.active_task.status == TaskStatus.IN_PROGRESS

    # Complete task 1
    ledger.complete_task(t1.task_id, result_summary="Requirements collected")
    assert t1.status == TaskStatus.COMPLETED
    assert ledger.progress_ratio() == 0.5

    # Start and complete task 2
    ledger.start_task(t2.task_id)
    ledger.complete_task(t2.task_id, result_summary="Code finished")
    assert ledger.is_all_completed()
    assert ledger.progress_ratio() == 1.0


def test_goal_ledger_compatibility_with_prompt_contributor():
    ledger = GoalLedger(goal="Build Cloud Pipeline")
    t1 = ledger.add_task("Design architecture")
    ledger.start_task(t1.task_id)

    milestones_tuple = ledger.to_milestones_tuple()
    ctx = PromptContext(goal=ledger.goal, milestones=milestones_tuple)

    contributor = GoalLedgerContributor()
    rendered = contributor.contribute(ctx)

    assert "Build Cloud Pipeline" in rendered
    assert "[/] (IN PROGRESS - CURRENT FOCUS)" in rendered
    assert "Design architecture" in rendered


def test_max_iterations_gate():
    gate = MaxIterationsGate(max_iterations=3)
    history = [{"step": 0}, {"step": 1}]
    # iteration 2 < 3
    dec = gate.evaluate(iteration=2, history=history, ledger=None)
    assert not dec.should_stop

    # iteration 3 >= 3
    dec2 = gate.evaluate(iteration=3, history=history, ledger=None)
    assert dec2.should_stop
    assert dec2.status == "max_iterations_exceeded"


def test_doom_loop_gate():
    gate = DoomLoopGate(repetition_threshold=3)
    history = [
        {"action": "call_tool", "parameters": {"cmd": "retry"}},
        {"action": "call_tool", "parameters": {"cmd": "retry"}},
    ]
    # 2 repeats < 3
    assert not gate.evaluate(iteration=2, history=history, ledger=None).should_stop

    # 3 identical repeats
    history.append({"action": "call_tool", "parameters": {"cmd": "retry"}})
    dec = gate.evaluate(iteration=3, history=history, ledger=None)
    assert dec.should_stop
    assert dec.status == "doom_loop"


def test_completion_rubric_gate():
    gate_strict = CompletionRubricGate(allow_summary_turn=False)
    ledger = GoalLedger(goal="Test Mission")
    t = ledger.add_task("Single task")

    assert not gate_strict.evaluate(0, [], ledger).should_stop

    ledger.complete_task(t.task_id)
    dec = gate_strict.evaluate(1, [], ledger)
    assert dec.should_stop
    assert dec.status == "completed"

    # With allow_summary_turn=True, allows 1 step if not yet final_reply
    gate_summary = CompletionRubricGate(allow_summary_turn=True)
    history_after_update = [{"action": "update_task", "action_input": {"status": "completed"}}]
    dec2 = gate_summary.evaluate(1, history_after_update, ledger)
    assert not dec2.should_stop  # Allows 1 summary turn

    # After final reply is provided, stops immediately
    history_with_final = [
        {"action": "update_task", "action_input": {"status": "completed"}},
        {"action": "final_reply", "action_input": {"reply": "Done"}},
    ]
    dec3 = gate_summary.evaluate(2, history_with_final, ledger)
    assert dec3.should_stop
    assert dec3.status == "completed"


@pytest.mark.asyncio
async def test_react_agent_single_turn_final_reply():
    model = MockModel([
        {
            "thought": "I have enough info to reply directly.",
            "action": "final_reply",
            "action_input": {"reply": "Hello, I am your cloud assistant!"},
        }
    ])
    agent = ReActAgent(model=model)
    ctx = HookContext(session_id="s1")

    res = await agent.execute_turn(ctx)
    assert res.status == "completed"
    assert res.final_reply == "Hello, I am your cloud assistant!"
    assert res.iterations == 1
    assert len(res.history) == 1


@pytest.mark.asyncio
async def test_react_agent_multi_step_with_tool_and_task_ledger():
    ledger = GoalLedger(goal="Analyze data and report")
    t1 = ledger.add_task("Query metrics")

    async def mock_tool(name: str, params: dict):
        if name == "metrics_query":
            return {"cpu": "25%", "mem": "40%"}
        return "unknown"

    model = MockModel([
        # Step 0: Start task and call tool
        {
            "thought": "I need to start task and query system metrics.",
            "action": "call_tool",
            "action_input": {"tool_name": "metrics_query", "parameters": {}},
        },
        # Step 1: Update task ledger to completed
        {
            "thought": "Metrics collected. Marking task completed.",
            "action": "update_task",
            "action_input": {"task_id": t1.task_id, "status": "completed", "summary": "Metrics retrieved"},
        },
        # Step 2: Final reply
        {
            "thought": "All tasks done. Ready to provide report.",
            "action": "final_reply",
            "action_input": {"reply": "Metrics report: CPU 25%, Memory 40%."},
        },
    ])

    agent = ReActAgent(model=model, tool_handler=mock_tool)
    ctx = HookContext(session_id="s_multi")

    result = await agent.execute_turn(ctx, ledger=ledger)

    assert result.status == "completed"
    assert result.iterations == 3
    assert "CPU 25%" in result.final_reply
    assert ledger.is_all_completed()
    assert "metrics_query" in result.history[0]["observation"]


@pytest.mark.asyncio
async def test_react_agent_integrated_with_runtime_engine():
    """Verify ReActAgent operates seamlessly as the core executor inside RuntimeEngine."""
    model = MockModel([
        {
            "thought": "Inside 8-phase lifecycle, delivering reply.",
            "action": "final_reply",
            "action_input": {"reply": "Delivered through 8-phase engine"},
        }
    ])
    agent = ReActAgent(model=model)

    engine = RuntimeEngine()

    async def executor(c: HookContext):
        return await agent.execute_turn(c)

    engine.set_executor(executor)
    ctx = HookContext(session_id="lifecycle_react")

    turn_outcome = await engine.run(ctx)
    assert isinstance(turn_outcome, ReActResult)
    assert turn_outcome.final_reply == "Delivered through 8-phase engine"


def test_doom_loop_gate_does_not_misclassify_different_tool_parameters():
    """Verify that different tool queries under action_input do not trigger doom_loop."""
    gate = DoomLoopGate(repetition_threshold=3)
    history = [
        {"action": "call_tool", "action_input": {"tool_name": "search", "query": "2022"}},
        {"action": "call_tool", "action_input": {"tool_name": "search", "query": "2023"}},
        {"action": "call_tool", "action_input": {"tool_name": "search", "query": "2024"}},
    ]
    dec = gate.evaluate(iteration=3, history=history, ledger=None)
    assert not dec.should_stop


@pytest.mark.asyncio
async def test_react_agent_respects_explicit_max_turns():
    """Verify that max_turns hard limits iteration count."""
    model = MockModel([
        {"action": "call_tool", "action_input": {"tool_name": "test1"}},
        {"action": "call_tool", "action_input": {"tool_name": "test2"}},
        {"action": "final_reply", "action_input": {"reply": "finished"}},
    ])
    agent = ReActAgent(model=model)
    res = await agent.execute_turn(HookContext(), max_turns=1)
    assert res.iterations == 1
    assert res.status == "max_iterations_exceeded"


@pytest.mark.asyncio
async def test_react_agent_does_not_leak_internal_thought_on_empty_reply():
    """Verify that empty reply in final_reply does not leak internal thought."""
    model = MockModel([
        # Turn 1: final_reply with empty action_input
        {
            "thought": "[SECRET_STRATEGY] Internal system reasoning",
            "action": "final_reply",
            "action_input": {},
        },
        # Turn 2: Model corrects itself and provides explicit reply
        {
            "thought": "Providing proper reply now",
            "action": "final_reply",
            "action_input": {"reply": "面向用户的正常答复"},
        },
    ])
    agent = ReActAgent(model=model)
    res = await agent.execute_turn(HookContext())

    assert res.status == "completed"
    assert res.final_reply == "面向用户的正常答复"
    assert "[SECRET_STRATEGY]" not in res.final_reply
    # Verify that turn 1 recorded error observation
    assert "requires a non-empty 'reply'" in res.history[0]["observation"]


@pytest.mark.asyncio
async def test_react_agent_with_ledger_does_not_prematurely_stop_on_invalid_final_reply():
    """Verify that when tasks in ledger are all completed, an invalid empty final_reply does not cause premature gate stop."""
    ledger = GoalLedger(goal="Ledger completion test")
    t = ledger.add_task("Subtask A")
    ledger.complete_task(t.task_id)

    model = MockModel([
        # Turn 1: invalid final_reply without reply parameter
        {
            "thought": "All tasks done, closing",
            "action": "final_reply",
            "action_input": {},
        },
        # Turn 2: Self-healing valid final_reply
        {
            "thought": "Delivering explicit user reply",
            "action": "final_reply",
            "action_input": {"reply": "经过核验交付的最终用户答复"},
        },
    ])
    agent = ReActAgent(model=model)
    res = await agent.execute_turn(HookContext(), ledger=ledger)

    assert res.status == "completed"
    assert res.final_reply == "经过核验交付的最终用户答复"
    assert res.iterations == 2


@pytest.mark.asyncio
async def test_react_agent_with_ledger_fails_when_recovery_turns_exhausted():
    """Verify that when tasks in ledger are completed but agent fails recovery, status is 'failed' not 'completed'."""
    ledger = GoalLedger(goal="Ledger completion failure test")
    t = ledger.add_task("Subtask A")
    ledger.complete_task(t.task_id)

    model = MockModel([
        # Turn 1: invalid final_reply without reply parameter
        {
            "thought": "All tasks done, closing",
            "action": "final_reply",
            "action_input": {},
        },
        # Turn 2: still invalid final_reply without reply parameter
        {
            "thought": "Still failing to deliver reply",
            "action": "final_reply",
            "action_input": {},
        },
        # Turn 3: should NOT be called because recovery turn budget is exhausted!
        {
            "thought": "Never reached",
            "action": "final_reply",
            "action_input": {"reply": "不会被执行"},
        },
    ])
    agent = ReActAgent(model=model)
    res = await agent.execute_turn(HookContext(), ledger=ledger)

    # Must be marked failed, not completed!
    assert res.status == "failed"
    assert res.final_reply == ""
    assert res.iterations == 2


@pytest.mark.asyncio
async def test_react_agent_with_whitespace_and_cased_final_reply_recovers_successfully():
    """Verify that action formatting like ' FINAL_REPLY ' does not prevent the agent from taking a recovery step."""
    ledger = GoalLedger(goal="Ledger whitespace action test")
    t = ledger.add_task("Subtask 1")
    ledger.complete_task(t.task_id)

    model = MockModel([
        # Turn 1: model outputs " FINAL_REPLY " with uppercase and whitespace, missing reply
        {
            "thought": "All done",
            "action": " FINAL_REPLY ",
            "action_input": {},
        },
        # Turn 2: model fixes and outputs valid final_reply
        {
            "thought": "Providing proper answer",
            "action": "final_reply",
            "action_input": {"reply": "成功规范化并恢复的答复"},
        },
    ])
    agent = ReActAgent(model=model)
    res = await agent.execute_turn(HookContext(), ledger=ledger)

    assert res.status == "completed"
    assert res.final_reply == "成功规范化并恢复的答复"
    assert res.iterations == 2





