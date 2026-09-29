"""Standard 8-phase runtime execution engine.

Coordinates the 8-phase lifecycle, hook dispatching, executor invocation,
and safe cancellation cleanup shielding.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from .phases import Phase, HookAction, HookResult
from .hooks import HookContext, HookRegistry

logger = logging.getLogger(__name__)

ExecutorCallable = Callable[[HookContext], Awaitable[Any]]


async def _shielded_cleanup(cleanup_coro: Awaitable[Any]) -> Any:
    """Run a cleanup routine shielded from cancellation, resisting repeated cancellations.

    Shield alone is insufficient: the caller must retain and join the cleanup task
    under repeated CancelledError occurrences before relinquishing control.
    """
    task = asyncio.ensure_future(cleanup_coro)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class RuntimeEngine:
    """Standard 8-phase lifecycle runtime coordinator."""

    def __init__(
        self,
        hooks: HookRegistry | None = None,
        executor: ExecutorCallable | None = None,
    ) -> None:
        self.hooks = hooks if hooks is not None else HookRegistry()
        self.executor = executor

    def register_hook(self, hook: Any) -> RuntimeEngine:
        """Register a lifecycle hook."""
        self.hooks.register(hook)
        return self

    def set_executor(self, executor: ExecutorCallable) -> None:
        """Set the core agent execution callable."""
        self.executor = executor

    async def run(self, ctx: HookContext) -> Any:
        """Execute the complete 8-phase lifecycle for a single turn."""
        phases_sequence = [
            Phase.PRE_DISPATCH,
            Phase.POST_DISPATCH,
            Phase.PRE_AGENT_BUILD,
            Phase.POST_AGENT_BUILD,
            Phase.PRE_EXECUTE,
        ]

        try:
            # 1. Run Pre-Execute phases
            for phase in phases_sequence:
                res = await self.hooks.run_phase(phase, ctx)
                if res.action == HookAction.SHORT_CIRCUIT:
                    logger.info("Phase %s short-circuited execution", phase.value)
                    return ctx.response

            # 2. Core Execution (Skip if sticky SKIP_AGENT was flagged)
            if not ctx.skip_agent and self.executor is not None:
                ctx.response = await self.executor(ctx)

            # 3. Post-Response phase
            post_res = await self.hooks.run_phase(Phase.POST_RESPONSE, ctx)
            if post_res.action == HookAction.SHORT_CIRCUIT:
                return ctx.response

            return ctx.response

        except Exception as exc:
            ctx.error = exc
            logger.error("Exception during execution at phase %s: %s", ctx.phase.value, exc)
            try:
                await self.hooks.run_phase(Phase.ON_ERROR, ctx)
            except Exception as on_err_exc:
                logger.error("Exception inside ON_ERROR hook: %s", on_err_exc)
            raise

        except asyncio.CancelledError as cancel_exc:
            ctx.error = cancel_exc
            logger.warning("Execution cancelled at phase %s", ctx.phase.value)
            try:
                await self.hooks.run_phase(Phase.ON_ERROR, ctx)
            except Exception as on_err_exc:
                logger.error("Exception inside ON_ERROR hook during cancellation: %s", on_err_exc)
            raise

        finally:
            # 4. FINALLY phase: Guaranteed execution under cancellation shielding
            async def _run_finally():
                try:
                    await self.hooks.run_phase(Phase.FINALLY, ctx)
                except Exception as final_exc:
                    logger.critical("Exception during FINALLY phase: %s", final_exc)

            await _shielded_cleanup(_run_finally())
