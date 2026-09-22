import asyncio
from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from genslide_agentscope.execution import ExecutionRuntime
from genslide_agentscope.domain import ServiceError
from test_runtime import FakeBFF, FakeEngine, request, settings


@pytest.mark.asyncio
async def test_context_and_request_scope_are_immutable(tmp_path):
    runtime = ExecutionRuntime(FakeBFF(), FakeEngine(), settings(workspace_root=tmp_path / "ws"))
    prepared = await runtime.prepare(request("immutable", current_file_ids=["allowed"]))
    try:
        assert prepared.context.current_file_ids == ("allowed",)
        with pytest.raises(FrozenInstanceError):
            prepared.context.lifecycle_version = 2
        with pytest.raises(ValidationError):
            prepared.request.current_file_ids += ("unauthorized",)
    finally:
        await runtime.cancel_prepared(prepared)
        await runtime.aclose()


@pytest.mark.asyncio
async def test_live_workspace_quota_cancels_work_and_releases_slot(tmp_path):
    engine = FakeEngine()
    engine.run_gate = asyncio.Event()
    runtime = ExecutionRuntime(FakeBFF(), engine,
                               settings(workspace_root=tmp_path / "ws", workspace_max_bytes=4096))
    prepared = await runtime.prepare(request("quota"))
    task = asyncio.create_task(runtime.perform(prepared))
    try:
        await asyncio.wait_for(engine.started.wait(), timeout=2)
        directory = next((tmp_path / "ws").glob("ws-*"))
        (directory / "scratch" / "large").write_bytes(b"x" * 8192)
        with pytest.raises(ServiceError, match="WORKSPACE_CAPACITY"):
            await asyncio.wait_for(task, timeout=3)
        assert engine.cancelled.is_set()
        assert runtime.metrics()["generation_active"] == 0
        assert not directory.exists()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.aclose()
