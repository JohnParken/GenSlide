"""Hook lifecycle management with topological sorting and priority tie-breaking.

Guarantees deterministic execution order and cycle detection across all phases.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass, field
import logging
from typing import Any, Sequence

from .phases import Phase, HookAction, HookResult

logger = logging.getLogger(__name__)


class HookCycleError(RuntimeError):
    """Raised when dependencies between hooks form a circular dependency graph."""


@dataclass
class HookContext:
    """Execution context passed through all lifecycle hooks."""
    tenant_id: str = ""
    user_id: str = ""
    session_id: str = ""
    action_id: str = ""
    phase: Phase = Phase.PRE_DISPATCH
    request: Any = None
    workspace_dir: Any = None
    mode_state: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    skip_agent: bool = False
    response: Any = None
    error: Exception | None = None

    def inject_context(self, key: str, value: Any) -> None:
        """Inject state into extras."""
        self.extras[key] = value


class HookBase:
    """Base class for request lifecycle hooks."""
    name: str = ""
    phase: Phase = Phase.PRE_DISPATCH
    priority: int = 50  # 0 to 100, higher runs earlier on equal dependency level
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()

    async def run(self, ctx: HookContext) -> HookResult:
        """Execute hook logic. Must return HookResult."""
        return HookResult(action=HookAction.CONTINUE)


class HookRegistry:
    """Central registry and topological orchestrator for lifecycle hooks."""

    def __init__(self, hooks: Sequence[HookBase] | None = None) -> None:
        self._hooks_by_phase: dict[Phase, list[HookBase]] = defaultdict(list)
        if hooks:
            for hook in hooks:
                self.register(hook)

    def register(self, hook: HookBase) -> HookRegistry:
        """Register a hook instance."""
        if not hook.name:
            raise ValueError(f"Hook of type {type(hook).__name__} must define a non-empty name")
        self._hooks_by_phase[hook.phase].append(hook)
        return self

    def get_hooks(self, phase: Phase) -> list[HookBase]:
        """Return raw registered hooks for a phase."""
        return list(self._hooks_by_phase.get(phase, []))

    def _topo_sort(self, hooks: list[HookBase]) -> list[HookBase]:
        """Topologically sort hooks based on before/after dependencies and priority.

        Graph edges: A -> B means A must execute before B.
        """
        if len(hooks) <= 1:
            return hooks

        hook_map = {h.name: h for h in hooks}
        graph: dict[str, set[str]] = defaultdict(set)
        in_degree: dict[str, int] = {h.name: 0 for h in hooks}

        for hook in hooks:
            name = hook.name
            # 1. hook declared `before`: this hook must execute before target
            for target in hook.before:
                if target in hook_map:
                    if target not in graph[name]:
                        graph[name].add(target)
                        in_degree[target] += 1

            # 2. hook declared `after`: target must execute before this hook
            for predecessor in hook.after:
                if predecessor in hook_map:
                    if name not in graph[predecessor]:
                        graph[predecessor].add(name)
                        in_degree[name] += 1

        # Kahn's algorithm with priority tie-breaker:
        # Sort queue by (-priority, registration_index)
        reg_index = {h.name: idx for idx, h in enumerate(hooks)}

        def sort_key(name: str):
            return (-hook_map[name].priority, reg_index[name])

        ready = sorted([name for name, deg in in_degree.items() if deg == 0], key=sort_key)
        sorted_names: list[str] = []

        while ready:
            curr = ready.pop(0)
            sorted_names.append(curr)

            for neighbor in sorted(graph[curr], key=sort_key):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    ready.append(neighbor)
            ready.sort(key=sort_key)

        if len(sorted_names) < len(hooks):
            unresolved = set(hook_map.keys()) - set(sorted_names)
            raise HookCycleError(f"Cycle detected in phase hooks among: {sorted(unresolved)}")

        return [hook_map[name] for name in sorted_names]

    async def run_phase(self, phase: Phase, ctx: HookContext) -> HookResult:
        """Run all hooks for the specified phase in topological order.

        Handles SHORT_CIRCUIT and sticky SKIP_AGENT semantics.
        Does NOT swallow exceptions: propagates them so Runtime enters ON_ERROR.
        """
        ctx.phase = phase
        hooks = self._hooks_by_phase.get(phase, [])
        if not hooks:
            return HookResult(action=HookAction.CONTINUE)

        sorted_hooks = self._topo_sort(hooks)
        result = HookResult(action=HookAction.CONTINUE)

        if phase == Phase.FINALLY:
            # Best-effort execution for FINALLY cleanup hooks:
            # An error or cancellation in one hook must NOT prevent subsequent cleanup hooks from running!
            first_exc: BaseException | None = None
            cancelled: bool = False
            for hook in sorted_hooks:
                try:
                    res = await hook.run(ctx)
                    if res.metadata:
                        ctx.extras.update(res.metadata)
                except asyncio.CancelledError as exc:
                    cancelled = True
                    logger.warning("Cleanup hook %s cancelled in phase finally", hook.name)
                    if first_exc is None:
                        first_exc = exc
                except BaseException as exc:
                    logger.error("Cleanup hook %s failed in phase finally: %s", hook.name, exc)
                    if first_exc is None:
                        first_exc = exc

            if cancelled:
                ctx.error = first_exc or asyncio.CancelledError()
                raise asyncio.CancelledError()
            if first_exc is not None:
                ctx.error = first_exc
                raise first_exc
            return result

        for hook in sorted_hooks:
            try:
                res = await hook.run(ctx)
            except Exception as exc:
                ctx.error = exc
                logger.error("Hook %s failed in phase %s: %s", hook.name, phase.value, exc)
                raise

            # Propagate metadata or response if set
            if res.metadata:
                ctx.extras.update(res.metadata)
            if res.response is not None:
                ctx.response = res.response

            # 1. SHORT_CIRCUIT: break phase and overall execution immediately
            if res.action == HookAction.SHORT_CIRCUIT:
                return res

            # 2. SKIP_AGENT: sticky flag
            if res.action == HookAction.SKIP_AGENT:
                ctx.skip_agent = True
                result.action = HookAction.SKIP_AGENT

        return result
