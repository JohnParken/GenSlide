import asyncio
import sys
import pytest
from genslide_langgraph.domain import ServiceError
from genslide_langgraph.execution import run_cpu_stage
from genslide_langgraph.config import Settings

@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_cpu_stage_is_killed_and_does_not_inherit_credentials(monkeypatch, tmp_path, cancel):
    original = asyncio.create_subprocess_exec
    started = asyncio.Event()
    children = []
    monkeypatch.setenv("MODEL_API_KEY", "secret-not-for-parser")
    monkeypatch.setenv("GENSLIDE_SERVICE_TOKEN", "secret-not-for-parser")
    monkeypatch.setenv("PYTHONPATH", "/not-the-service")
    async def create(*args, **kwargs):
        assert "MODEL_API_KEY" not in kwargs["env"]
        assert "GENSLIDE_SERVICE_TOKEN" not in kwargs["env"]
        assert "/not-the-service" not in kwargs["env"]["PYTHONPATH"]
        process = await original(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)
        children.append(process)
        started.set()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    slots = asyncio.Semaphore(1)
    task = asyncio.create_task(run_cpu_stage("render_content", {"directory":str(tmp_path)},
                                            slots=slots, timeout=5 if cancel else 0.05))
    await asyncio.wait_for(started.wait(), 2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(ServiceError, match="DEADLINE_EXCEEDED"):
            await task
    assert children[0].returncode is not None
    assert slots._value == 1

def test_production_fails_closed_and_nonfinite_limits_rejected(monkeypatch):
    with pytest.raises(ValueError, match="production configuration"):
        Settings(environment="production")
    with pytest.raises(ValueError, match="limits"):
        Settings(environment="test", planning_timeout_seconds=float("inf"))
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(ValueError, match="exactly one"):
        Settings(environment="test")

