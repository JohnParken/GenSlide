"""Deterministic scheduling regressions for request ownership and cleanup."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from genslide_agentscope.domain import ServiceError
from genslide_agentscope.execution import ExecutionRuntime, SessionMetadata, run_cpu_stage
from test_runtime import FakeBFF, FakeEngine, request, settings


@pytest.mark.asyncio
@pytest.mark.parametrize("prepare_failed", [False, True])
async def test_repeated_cancel_during_claim_cleanup_releases_slot(tmp_path, prepare_failed):
    entered, finish = asyncio.Event(), asyncio.Event()
    class BFF(FakeBFF):
        async def settle(self, *args, **kwargs):
            entered.set()
            await finish.wait()
            return await super().settle(*args, **kwargs)
    bff = BFF()
    runtime = ExecutionRuntime(bff, FakeEngine(), settings(workspace_root=tmp_path / "ws"))
    if prepare_failed:
        bff.claim_version_override = 1
        task = asyncio.create_task(runtime.prepare(request("bad-claim")))
    else:
        prepared = await runtime.prepare(request("prepared"))
        task = asyncio.create_task(runtime.cancel_prepared(prepared))
    await asyncio.wait_for(entered.wait(), 1)
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()
    assert runtime.metrics()["generation_active"] == 1
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert runtime.metrics()["generation_active"] == 0
    assert not runtime._busy
    await runtime.aclose()


@pytest.mark.asyncio
async def test_repeated_cancel_during_release_waits_for_lock(tmp_path):
    runtime = ExecutionRuntime(FakeBFF(), FakeEngine(), settings(workspace_root=tmp_path / "ws"))
    prepared = await runtime.prepare(request("release"))
    await runtime._lock.acquire()
    task = asyncio.create_task(runtime._release(prepared))
    try:
        for _ in range(3):
            await asyncio.sleep(0)
            task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not prepared.released
        assert prepared.key in runtime._busy
    finally:
        runtime._lock.release()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert prepared.released and not runtime._busy
    assert runtime.metrics()["generation_active"] == 0
    # An old release must not remove a new action's ownership of the same key.
    await runtime._admit(prepared.key, "generation")
    await runtime._release(prepared)
    assert prepared.key in runtime._busy
    await runtime._release_key(prepared.key, "generation")
    await runtime.aclose()


@pytest.mark.asyncio
async def test_repeated_cancel_joins_pipeline_before_workspace_and_slot_release(tmp_path):
    stopping, finish = asyncio.Event(), asyncio.Event()
    class Engine(FakeEngine):
        async def run(self, *args, **kwargs):
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopping.set()
                await finish.wait()

    engine, bff = Engine(), FakeBFF()
    runtime = ExecutionRuntime(bff, engine, settings(workspace_root=tmp_path / "ws"))
    prepared = await runtime.prepare(request("cancel"))
    task = asyncio.create_task(runtime.perform(prepared))
    await asyncio.wait_for(engine.started.wait(), 1)
    task.cancel()
    await asyncio.wait_for(stopping.wait(), 1)
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()
    assert prepared.key in runtime._busy
    assert list((tmp_path / "ws").glob("ws-*"))
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert not list((tmp_path / "ws").glob("ws-*"))
    assert runtime.metrics()["generation_active"] == 0
    assert bff.commit_count == 0
    assert bff.actions["cancel"]["status"] == "closed"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_repeated_cancel_still_reaps_cpu_child(monkeypatch, tmp_path):
    import genslide_agentscope.execution as execution
    import sys
    created, terminating, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    spawn, terminate = asyncio.create_subprocess_exec, execution._terminate_process
    children = []
    async def create(*args, **kwargs):
        child = await spawn(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        children.append(child)
        created.set()
        return child
    async def delayed_terminate(child):
        terminating.set()
        await finish.wait()
        await terminate(child)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(execution, "_terminate_process", delayed_terminate)
    slots = asyncio.Semaphore(1)
    task = asyncio.create_task(run_cpu_stage("render_content", {"directory": str(tmp_path)}, slots=slots, timeout=5))
    try:
        await asyncio.wait_for(created.wait(), 2)
        task.cancel()
        await asyncio.wait_for(terminating.wait(), 1)
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        assert slots.locked()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert children[0].returncode is not None
        assert slots._value == 1
    finally:
        finish.set()
        for child in children:
            await terminate(child)


@pytest.mark.asyncio
async def test_expiry_delete_does_not_block_other_admissions_or_race_same_key(tmp_path):
    entered, finish = asyncio.Event(), asyncio.Event()
    class Engine(FakeEngine):
        async def delete(self, key):
            entered.set()
            await finish.wait()
            await super().delete(key)
    engine = Engine()
    runtime = ExecutionRuntime(FakeBFF(), engine, settings(workspace_root=tmp_path / "ws", generation_concurrency=3))
    runtime._metadata["expired"] = SessionMetadata(0, datetime.now(timezone.utc) - timedelta(seconds=1))
    await runtime._admit("first", "generation")
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(runtime._admit("second", "generation"), 0.1)
    with pytest.raises(ServiceError, match="SESSION_BUSY"):
        await runtime._admit("expired", "generation")
    deletion = runtime._evictions["expired"]
    finish.set()
    await deletion
    await runtime._admit("expired", "generation")
    assert engine.deletes == ["expired"]
    for key in ("first", "second", "expired"):
        await runtime._release_key(key, "generation")
    await runtime.aclose()


@pytest.mark.asyncio
async def test_expiry_delete_timeout_releases_tombstone(tmp_path):
    class Engine(FakeEngine):
        async def delete(self, key):
            await asyncio.Event().wait()
    runtime = ExecutionRuntime(FakeBFF(), Engine(), settings(workspace_root=tmp_path / "ws", control_timeout_seconds=0.01))
    runtime._metadata["expired"] = SessionMetadata(0, datetime.now(timezone.utc) - timedelta(seconds=1))
    await runtime._admit("other", "generation")
    await asyncio.wait_for(runtime._evictions["expired"], 1)
    assert "expired" not in runtime._evictions and "expired" not in runtime._metadata
    await runtime._release_key("other", "generation")
    await runtime.aclose()
