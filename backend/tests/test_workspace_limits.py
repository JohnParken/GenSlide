import asyncio
from pathlib import Path

import pytest

from genslide_agentscope.config import Settings
from genslide_agentscope.domain import ServiceError
from genslide_agentscope.workspace import WorkspaceManager


def test_request_capacity_isolated_from_global_capacity(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path, 2_000, 0, 60, request_max_bytes=512)
    workspace = manager.create("a", "i")
    other = manager.create("b", "i")
    (workspace.outputs / "large").write_bytes(b"x" * 600)
    with pytest.raises(ServiceError, match="WORKSPACE_REQUEST_CAPACITY"):
        workspace.check_usage()
    other.check_usage()
    manager.check_usage()
    manager.max_bytes = 1
    with pytest.raises(ServiceError, match="WORKSPACE_CAPACITY"):
        manager.check_usage()
    workspace.cleanup()
    other.cleanup()


def test_workspace_request_capacity_is_configurable_from_env(monkeypatch) -> None:
    monkeypatch.setenv("GENSLIDE_ENV", "test")
    monkeypatch.setenv("GENSLIDE_WORKSPACE_REQUEST_MAX_BYTES", "4096")
    assert Settings.from_env().workspace_request_max_bytes == 4096
    monkeypatch.setenv("GENSLIDE_WORKSPACE_REQUEST_MAX_BYTES", "invalid")
    with pytest.raises(ValueError):
        Settings.from_env()
    monkeypatch.setenv("GENSLIDE_WORKSPACE_REQUEST_MAX_BYTES", "0")
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.asyncio
async def test_live_request_quota_cancels_work_and_releases_slot(tmp_path: Path) -> None:
    from genslide_agentscope.execution import ExecutionRuntime
    from test_execution_context import FakeBFF, FakeEngine, request, settings

    engine = FakeEngine()
    engine.run_gate = asyncio.Event()
    runtime = ExecutionRuntime(FakeBFF(), engine, settings(
        workspace_root=tmp_path / "ws", workspace_max_bytes=10_000, workspace_request_max_bytes=512,
    ))
    prepared = await runtime.prepare(request("request-quota"))
    task = asyncio.create_task(runtime.perform(prepared))
    try:
        await asyncio.wait_for(engine.started.wait(), timeout=2)
        directory = next((tmp_path / "ws").glob("ws-*"))
        (directory / "scratch" / "large").write_bytes(b"x" * 600)
        with pytest.raises(ServiceError, match="WORKSPACE_REQUEST_CAPACITY"):
            await asyncio.wait_for(task, timeout=3)
        assert engine.cancelled.is_set()
        assert runtime.metrics()["generation_active"] == 0
        assert not directory.exists()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await runtime.aclose()
