"""Unit tests for the standard 8-phase runtime execution engine and hook orchestration."""

import asyncio
import pytest

from genslide_agentscope.runtime import (
    Phase,
    HookAction,
    HookResult,
    HookContext,
    HookBase,
    HookRegistry,
    HookCycleError,
    RuntimeEngine,
)


class RecordingHook(HookBase):
    """Test helper that records phase execution order."""

    def __init__(
        self,
        name: str,
        phase: Phase,
        priority: int = 50,
        before: tuple[str, ...] = (),
        after: tuple[str, ...] = (),
        action: HookAction = HookAction.CONTINUE,
        response_val: str | None = None,
    ) -> None:
        self.name = name
        self.phase = phase
        self.priority = priority
        self.before = before
        self.after = after
        self.action = action
        self.response_val = response_val

    async def run(self, ctx: HookContext) -> HookResult:
        trace = ctx.extras.setdefault("trace", [])
        trace.append((self.phase, self.name))
        return HookResult(action=self.action, response=self.response_val)


@pytest.mark.asyncio
async def test_full_8_phase_lifecycle_success():
    engine = RuntimeEngine()
    ctx = HookContext(session_id="test_sess_1", action_id="act_1")

    # Register one hook per phase
    for phase in Phase:
        engine.register_hook(RecordingHook(name=f"hook_{phase.value}", phase=phase))

    async def mock_executor(c: HookContext):
        c.extras.setdefault("trace", []).append(("EXECUTOR", "mock_executor"))
        return {"result": "success"}

    engine.set_executor(mock_executor)

    res = await engine.run(ctx)
    assert res == {"result": "success"}

    trace = ctx.extras["trace"]
    phase_order = [p for p, _ in trace]

    # Expected progression: pre_dispatch -> post_dispatch -> pre_agent_build ->
    # post_agent_build -> pre_execute -> EXECUTOR -> post_response -> finally
    assert phase_order == [
        Phase.PRE_DISPATCH,
        Phase.POST_DISPATCH,
        Phase.PRE_AGENT_BUILD,
        Phase.POST_AGENT_BUILD,
        Phase.PRE_EXECUTE,
        "EXECUTOR",
        Phase.POST_RESPONSE,
        Phase.FINALLY,
    ]
    # ON_ERROR should not run in success path
    assert Phase.ON_ERROR not in phase_order


@pytest.mark.asyncio
async def test_hook_topological_sorting():
    registry = HookRegistry()
    ctx = HookContext()

    # Register in reverse order: C -> B -> A
    # Dependencies: A before B, B before C
    hook_c = RecordingHook(name="hook_c", phase=Phase.PRE_EXECUTE, after=("hook_b",))
    hook_b = RecordingHook(name="hook_b", phase=Phase.PRE_EXECUTE, after=("hook_a",))
    hook_a = RecordingHook(name="hook_a", phase=Phase.PRE_EXECUTE)

    registry.register(hook_c)
    registry.register(hook_b)
    registry.register(hook_a)

    await registry.run_phase(Phase.PRE_EXECUTE, ctx)

    trace_names = [name for _, name in ctx.extras["trace"]]
    assert trace_names == ["hook_a", "hook_b", "hook_c"]


@pytest.mark.asyncio
async def test_hook_priority_tie_breaking():
    registry = HookRegistry()
    ctx = HookContext()

    # Same phase, no before/after, but different priorities
    hook_low = RecordingHook(name="low", phase=Phase.PRE_EXECUTE, priority=10)
    hook_high = RecordingHook(name="high", phase=Phase.PRE_EXECUTE, priority=90)
    hook_mid = RecordingHook(name="mid", phase=Phase.PRE_EXECUTE, priority=50)

    # Register low first
    registry.register(hook_low).register(hook_high).register(hook_mid)

    await registry.run_phase(Phase.PRE_EXECUTE, ctx)

    trace_names = [name for _, name in ctx.extras["trace"]]
    assert trace_names == ["high", "mid", "low"]


@pytest.mark.asyncio
async def test_hook_cycle_detection():
    registry = HookRegistry()
    ctx = HookContext()

    # Circular dependency: A -> B -> A
    hook_a = RecordingHook(name="hook_a", phase=Phase.PRE_EXECUTE, before=("hook_b",))
    hook_b = RecordingHook(name="hook_b", phase=Phase.PRE_EXECUTE, before=("hook_a",))

    registry.register(hook_a).register(hook_b)

    with pytest.raises(HookCycleError) as exc_info:
        await registry.run_phase(Phase.PRE_EXECUTE, ctx)

    assert "Cycle detected" in str(exc_info.value)


@pytest.mark.asyncio
async def test_short_circuit_behavior():
    engine = RuntimeEngine()
    ctx = HookContext()

    # PRE_AGENT_BUILD short-circuits
    engine.register_hook(RecordingHook("pre_disp", Phase.PRE_DISPATCH))
    engine.register_hook(
        RecordingHook(
            "short_circuit_hook",
            Phase.PRE_AGENT_BUILD,
            action=HookAction.SHORT_CIRCUIT,
            response_val="cached_response",
        )
    )
    engine.register_hook(RecordingHook("post_resp", Phase.POST_RESPONSE))
    engine.register_hook(RecordingHook("finally_hook", Phase.FINALLY))

    executor_called = False

    async def mock_executor(c: HookContext):
        nonlocal executor_called
        executor_called = True
        return "unreachable"

    engine.set_executor(mock_executor)

    res = await engine.run(ctx)
    assert res == "cached_response"
    assert not executor_called

    trace_phases = [p for p, _ in ctx.extras["trace"]]
    assert Phase.PRE_DISPATCH in trace_phases
    assert Phase.PRE_AGENT_BUILD in trace_phases
    assert Phase.POST_RESPONSE not in trace_phases
    # FINALLY must still execute even on short-circuit
    assert Phase.FINALLY in trace_phases


@pytest.mark.asyncio
async def test_skip_agent_sticky_flag():
    engine = RuntimeEngine()
    ctx = HookContext()

    # Pre-execute sets SKIP_AGENT
    engine.register_hook(
        RecordingHook(
            "skip_hook",
            Phase.PRE_EXECUTE,
            action=HookAction.SKIP_AGENT,
        )
    )
    engine.register_hook(RecordingHook("post_resp", Phase.POST_RESPONSE))
    engine.register_hook(RecordingHook("finally_hook", Phase.FINALLY))

    executor_called = False

    async def mock_executor(c: HookContext):
        nonlocal executor_called
        executor_called = True
        return "from_executor"

    engine.set_executor(mock_executor)

    await engine.run(ctx)
    assert not executor_called
    assert ctx.skip_agent is True

    trace_phases = [p for p, _ in ctx.extras["trace"]]
    assert Phase.POST_RESPONSE in trace_phases
    assert Phase.FINALLY in trace_phases


@pytest.mark.asyncio
async def test_exception_triggers_on_error_and_finally():
    engine = RuntimeEngine()
    ctx = HookContext()

    engine.register_hook(RecordingHook("on_error_hook", Phase.ON_ERROR))
    engine.register_hook(RecordingHook("finally_hook", Phase.FINALLY))

    async def failing_executor(c: HookContext):
        raise ValueError("Simulated core failure")

    engine.set_executor(failing_executor)

    with pytest.raises(ValueError, match="Simulated core failure"):
        await engine.run(ctx)

    trace_phases = [p for p, _ in ctx.extras["trace"]]
    assert Phase.ON_ERROR in trace_phases
    assert Phase.FINALLY in trace_phases
    assert isinstance(ctx.error, ValueError)


@pytest.mark.asyncio
async def test_cancellation_triggers_on_error_and_finally():
    engine = RuntimeEngine()
    ctx = HookContext()

    engine.register_hook(RecordingHook("on_error_hook", Phase.ON_ERROR))
    engine.register_hook(RecordingHook("finally_hook", Phase.FINALLY))

    async def cancelling_executor(c: HookContext):
        raise asyncio.CancelledError()

    engine.set_executor(cancelling_executor)

    with pytest.raises(asyncio.CancelledError):
        await engine.run(ctx)

    trace_phases = [p for p, _ in ctx.extras["trace"]]
    assert Phase.ON_ERROR in trace_phases
    assert Phase.FINALLY in trace_phases
    assert isinstance(ctx.error, asyncio.CancelledError)
