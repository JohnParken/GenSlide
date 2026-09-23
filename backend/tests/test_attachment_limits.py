import asyncio
from pathlib import Path

import pytest

from genslide_agentscope.domain import ServiceError
from genslide_agentscope.execution import run_cpu_stage


def _run(payload):
    return run_cpu_stage("parse_attachment", payload, slots=asyncio.Semaphore(1), timeout=10)


@pytest.mark.asyncio
async def test_cpu_stage_propagates_configured_attachment_limits(tmp_path: Path) -> None:
    source = tmp_path / "large.txt"
    source.write_bytes(b" " * (20 * 1024 * 1024 + 1))
    assert await _run({"path": str(source), "max_input_bytes": 21 * 1024 * 1024, "max_output_chars": 1}) == ""


@pytest.mark.asyncio
async def test_cpu_stage_reports_oversized_input(tmp_path: Path) -> None:
    source = tmp_path / "large.txt"
    source.write_bytes(b"x" * 16)
    with pytest.raises(ServiceError, match="ATTACHMENT_TOO_LARGE") as error:
        await _run({"path": str(source), "max_input_bytes": 8})
    assert error.value.status == 413


@pytest.mark.asyncio
async def test_cpu_stage_reports_oversized_extracted_text(tmp_path: Path) -> None:
    source = tmp_path / "text.txt"
    source.write_text("x" * 16, encoding="utf-8")
    with pytest.raises(ServiceError, match="MATERIALS_TOO_LARGE") as error:
        await _run({"path": str(source), "max_input_bytes": 1024, "max_output_chars": 8})
    assert error.value.status == 413
