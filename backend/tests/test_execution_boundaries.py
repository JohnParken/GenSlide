from pathlib import Path
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from genslide_agentscope.bff import DownloadedFile
from genslide_agentscope.domain import Content, ServiceError
from genslide_agentscope.execution import ExecutionRuntime
from genslide_agentscope.config import Settings
from test_runtime import FakeBFF, FakeEngine, request, settings


def runtime(root: Path) -> ExecutionRuntime:
    class Engine:
        name = "agentscope"

        async def aclose(self):
            return None

    engine = Engine()
    values = Settings(environment="test", workspace_root=root)
    return ExecutionRuntime(SimpleNamespace(), engine, values)


def test_validate_download_rejects_path_escape_and_symlink(tmp_path: Path) -> None:
    rt = runtime(tmp_path / "workspace")
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    with pytest.raises(ServiceError, match="BFF_CONTRACT_ERROR") as error:
        rt._validate_download(DownloadedFile(outside, "x.txt", "text/plain", outside.stat().st_size), inputs)
    assert error.value.status == 502
    link = inputs / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(ServiceError, match="BFF_CONTRACT_ERROR") as error:
        rt._validate_download(DownloadedFile(link, "x.txt", "text/plain", outside.stat().st_size), inputs)
    assert error.value.status == 502


def test_validate_download_rejects_unsupported_suffix(tmp_path: Path) -> None:
    rt = runtime(tmp_path / "workspace")
    path = tmp_path / "input.csv"
    path.write_text("a,b")
    with pytest.raises(ServiceError) as error:
        rt._validate_download(DownloadedFile(path, path.name, "text/csv", path.stat().st_size), tmp_path)
    assert error.value.status == 415


@pytest.mark.parametrize("symlink", [False, True])
def test_render_rejects_output_outside_workspace_before_upload(tmp_path: Path, monkeypatch, symlink) -> None:
    rt = runtime(tmp_path / "workspace")
    prepared = SimpleNamespace(deadline_at=datetime.now(timezone.utc) + timedelta(seconds=30))
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    outside = tmp_path / "escape.docx"
    outside.write_bytes(b"artifact")
    output = outside
    if symlink:
        output = outputs / "link.docx"
        output.symlink_to(outside)
    workspace = SimpleNamespace(outputs=outputs, check_usage=lambda: SimpleNamespace(used_bytes=0, free_bytes=1))

    async def escaped_stage(*args, **kwargs):
        return str(output)

    monkeypatch.setattr("genslide_agentscope.execution.run_cpu_stage", escaped_stage)
    content = Content(title="Title", sections=[{"title": "S", "body": "Body"}])
    with pytest.raises(ServiceError, match="ARTIFACT_INVALID"):
        asyncio.run(rt._render(prepared, content, workspace, "document"))
    asyncio.run(rt.aclose())


@pytest.mark.asyncio
async def test_attachment_pipeline_succeeds_and_parses_text(tmp_path: Path) -> None:
    captured = []
    class Engine(FakeEngine):
        async def run(self, key, req, memory, materials, **kwargs):
            captured.append(materials)
            return await super().run(key, req, memory, materials, **kwargs)
    bff, engine = FakeBFF(), Engine()
    payload = b"hello attachment"

    async def download(req, claim, file_id, directory, maximum):
        path = directory / f"{file_id}.txt"
        path.write_bytes(payload)
        return DownloadedFile(path, path.name, "text/plain", len(payload))

    bff.download_file = download
    runtime = ExecutionRuntime(bff, engine, settings(workspace_root=tmp_path / "workspaces"))
    prepared = await runtime.prepare(request("attachment-success", current_file_ids=("one",)))
    result = await runtime.perform(prepared)
    assert result["status"] == "completed" and engine.runs == 1
    assert captured == ["hello attachment"]
    assert bff.commit_count == 1
    await runtime.aclose()


@pytest.mark.asyncio
async def test_attachment_pipeline_enforces_cumulative_byte_limit(tmp_path: Path) -> None:
    bff, engine = FakeBFF(), FakeEngine()

    async def download(req, claim, file_id, directory, maximum):
        path = directory / f"{file_id}.txt"
        path.write_bytes(b"1234")
        return DownloadedFile(path, path.name, "text/plain", 4)

    bff.download_file = download
    runtime = ExecutionRuntime(bff, engine, settings(
        workspace_root=tmp_path / "workspaces", max_total_download_bytes=5,
    ))
    prepared = await runtime.prepare(request("attachment-bytes", current_file_ids=("one", "two")))
    with pytest.raises(ServiceError) as error:
        await runtime.perform(prepared)
    assert error.value.status == 413 and error.value.code == "MATERIALS_TOO_LARGE"
    await runtime.aclose()


@pytest.mark.asyncio
async def test_attachment_pipeline_enforces_cumulative_text_limit(tmp_path: Path) -> None:
    bff, engine = FakeBFF(), FakeEngine()

    async def download(req, claim, file_id, directory, maximum):
        path = directory / f"{file_id}.txt"
        path.write_text("abcd")
        return DownloadedFile(path, path.name, "text/plain", 4)

    bff.download_file = download
    runtime = ExecutionRuntime(bff, engine, settings(
        workspace_root=tmp_path / "workspaces", max_material_chars=5,
    ))
    prepared = await runtime.prepare(request("attachment-text", current_file_ids=("one", "two")))
    with pytest.raises(ServiceError) as error:
        await runtime.perform(prepared)
    assert error.value.status == 413 and error.value.code == "MATERIALS_TOO_LARGE"
    await runtime.aclose()
