"""Request-scoped execution, lease handling, safe handoff, and CPU isolation."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from typing import Any, Awaitable, Callable, Mapping, Protocol
from uuid import uuid4

from .bff import BFF, Claim, CommitOutcome, FileReference, SettleOutcome
from .config import Settings
from .domain import Content, ExecuteRequest, Memory, ServiceError, WorkResult


class Engine(Protocol):
    name: str

    async def read(self, key: str) -> Memory | None: ...
    async def run(self, key: str, request: ExecuteRequest, memory: Memory, materials: str) -> WorkResult: ...
    async def publish(self, key: str, work: WorkResult) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def aclose(self) -> None: ...


EventSink = Callable[[Mapping[str, Any]], Awaitable[None]]
DisconnectCheck = Callable[[], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    version: int
    expires_at: datetime


@dataclass(slots=True)
class PreparedExecution:
    request: ExecuteRequest
    key: str
    execution_instance_id: str
    claim: Claim
    memory: Memory
    deadline_at: datetime
    lease_expires_at: datetime
    bucket: str
    reserved_new_session: bool = False
    engine_started: bool = False
    commit_started: bool = False
    committed: bool = False
    committed_response: dict[str, Any] | None = None
    cancel_after_commit: bool = False
    settled: bool = False
    released: bool = False
    sequence: int = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=0.5)
    except asyncio.TimeoutError:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


def _cpu_worker() -> int:
    """Subprocess entrypoint. Its stdout is a small JSON result, never a traceback."""
    try:
        if sys.platform.startswith("linux"):
            import resource

            cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)[1]
            cpu_hard = min(65, cpu_hard) if cpu_hard != resource.RLIM_INFINITY else 65
            resource.setrlimit(resource.RLIMIT_CPU, (min(60, cpu_hard), cpu_hard))

            memory_hard = resource.getrlimit(resource.RLIMIT_AS)[1]
            memory_hard = (
                min(2 * 1024 * 1024 * 1024, memory_hard)
                if memory_hard != resource.RLIM_INFINITY
                else 2 * 1024 * 1024 * 1024
            )
            memory_soft = resource.getrlimit(resource.RLIMIT_AS)[0]
            if memory_soft == resource.RLIM_INFINITY:
                memory_soft = memory_hard
            memory_soft = min(memory_soft, memory_hard)
            resource.setrlimit(resource.RLIMIT_AS, (memory_soft, memory_hard))
        request = json.load(sys.stdin)
        stage = request.get("stage")
        if stage == "parse_attachment":
            from .content_io import parse_attachment

            value = parse_attachment(Path(request["path"]))
        elif stage == "render_content":
            from .content_io import render_content

            value = str(render_content(request["kind"], request["content"], Path(request["directory"])))
        else:
            print(json.dumps({"ok": False, "code": "CPU_STAGE_UNSUPPORTED"}))
            return 2
        print(json.dumps({"ok": True, "value": value}, ensure_ascii=False))
        return 0
    except FileNotFoundError:
        print(json.dumps({"ok": False, "code": "ATTACHMENT_UNAVAILABLE"}))
        return 1
    except (TypeError, ValueError):
        print(json.dumps({"ok": False, "code": "ATTACHMENT_INVALID"}))
        return 1
    except BaseException:
        print(json.dumps({"ok": False, "code": "CPU_STAGE_FAILED"}))
        return 1


async def run_cpu_stage(
    stage: str,
    payload: Mapping[str, Any],
    *,
    slots: asyncio.Semaphore,
    timeout: float,
) -> str:
    """Run parsing/rendering in a bounded, killable child process."""
    if timeout <= 0:
        raise ServiceError("DEADLINE_EXCEEDED", 504)
    request = json.dumps({"stage": stage, **payload}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    source_root = str(Path(__file__).resolve().parents[1])
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONPATH": source_root,
    }
    for name in ("LANG", "LC_ALL"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    temporary_root = payload.get("directory")
    if not temporary_root and isinstance(payload.get("path"), str):
        temporary_root = str(Path(payload["path"]).parent)
    env["TMPDIR"] = str(Path(temporary_root).resolve()) if temporary_root else "/tmp"
    module = f"{__package__}.execution"
    async with slots:
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                module,
                "--cpu-worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                cwd=env["TMPDIR"],
                start_new_session=(os.name == "posix"),
            )
        except (OSError, ValueError) as exc:
            raise ServiceError("CPU_STAGE_UNAVAILABLE", 503) from exc
        communicate = asyncio.create_task(process.communicate(input=request))
        try:
            stdout, _ = await asyncio.wait_for(asyncio.shield(communicate), timeout=timeout)
        except asyncio.TimeoutError as exc:
            await _terminate_process(process)
            await asyncio.gather(communicate, return_exceptions=True)
            raise ServiceError("DEADLINE_EXCEEDED", 504) from exc
        except asyncio.CancelledError:
            await _terminate_process(process)
            await asyncio.gather(communicate, return_exceptions=True)
            raise
        if process.returncode != 0:
            try:
                response = json.loads(stdout)
            except (ValueError, TypeError):
                response = {}
            code = response.get("code") if isinstance(response, dict) else None
            if code not in {"ATTACHMENT_UNAVAILABLE", "ATTACHMENT_INVALID", "CPU_STAGE_UNSUPPORTED"}:
                code = "CPU_STAGE_FAILED"
            status = 422 if code == "ATTACHMENT_INVALID" else 503 if code != "ATTACHMENT_UNAVAILABLE" else 404
            raise ServiceError(code, status)
        try:
            response = json.loads(stdout)
        except (ValueError, TypeError) as exc:
            raise ServiceError("CPU_STAGE_FAILED", 500) from exc
        if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("value"), str):
            raise ServiceError("CPU_STAGE_FAILED", 500)
        return response["value"]


class ExecutionRuntime:
    """Coordinates one synchronous BFF-authorized action at a time per session."""

    def __init__(self, bff: BFF, engine: Engine, settings: Settings):
        if engine.name != settings.engine_name:
            raise ValueError("engine implementation does not match this service package")
        self.bff = bff
        self.engine = engine
        self.settings = settings
        self._lock = asyncio.Lock()
        self._busy: set[str] = set()
        self._active = {"generation": 0, "planning": 0}
        self._metadata: dict[str, SessionMetadata] = {}
        self._reserved_new: set[str] = set()
        self._cpu_slots = asyncio.Semaphore(settings.cpu_concurrency)
        self._transfer_slots = asyncio.Semaphore(settings.transfer_concurrency)

    async def prepare(self, request: ExecuteRequest) -> PreparedExecution:
        if request.engine != self.settings.engine_name or request.engine != self.engine.name:
            raise ServiceError("ENGINE_MISMATCH", 409)
        key = request.session_key()
        bucket = "generation" if request.operation == "generate" else "planning"
        await self._admit(key, bucket)
        instance_id = uuid4().hex
        try:
            try:
                claim = await asyncio.wait_for(
                    self.bff.claim(request, instance_id), timeout=self.settings.bff_timeout_seconds
                )
            except asyncio.CancelledError:
                await self._reconcile_unclaimed(request, instance_id)
                raise
            except asyncio.TimeoutError as exc:
                await self._reconcile_unclaimed(request, instance_id)
                raise ServiceError("OUTCOME_UNKNOWN", 503) from exc
            except ServiceError as exc:
                if exc.status >= 500:
                    await self._reconcile_unclaimed(request, instance_id)
                    raise ServiceError("OUTCOME_UNKNOWN", 503) from exc
                raise
            if not isinstance(claim, Claim):
                raise ServiceError("BFF_CONTRACT_ERROR", 502)
            self._validate_claim(request, instance_id, claim)
            now = _utcnow()
            operation_limit = (
                self.settings.generation_timeout_seconds
                if request.operation == "generate"
                else self.settings.planning_timeout_seconds
            )
            local_deadline = now + timedelta(seconds=operation_limit)
            deadline = min(claim.deadline_at, local_deadline)
            if deadline <= now:
                raise ServiceError("ACTION_DEADLINE_EXCEEDED", 504)
            if claim.lease_expires_at <= now or claim.lease_expires_at > claim.deadline_at:
                raise ServiceError("BFF_CONTRACT_ERROR", 502)
            memory = await self._read_committed_memory(key, request, claim)
            return PreparedExecution(
                request=request,
                key=key,
                execution_instance_id=instance_id,
                claim=claim,
                memory=memory,
                deadline_at=deadline,
                lease_expires_at=claim.lease_expires_at,
                bucket=bucket,
                reserved_new_session=(not claim.runtime_initialized),
            )
        except BaseException:
            # A known claim must be closed if local validation prevents execution.
            if "claim" in locals() and isinstance(claim, Claim):
                await self._settle_raw(request, claim, instance_id, "execution_failed")
            await self._release_key(key, bucket)
            raise

    async def accepted_event(self, execution: PreparedExecution) -> dict[str, Any]:
        execution.sequence = 1
        return {
            "event": "accepted",
            "action_id": execution.request.action_id,
            "sequence": execution.sequence,
            "stage": "accepted",
        }

    async def cancel_prepared(self, execution: PreparedExecution) -> None:
        """Close a claimed SSE request if its consumer disconnects before execution starts."""
        if not execution.committed and not execution.settled:
            await self._settle_execution(execution, "client_disconnected")
        await self._release(execution)

    async def perform(
        self,
        execution: PreparedExecution,
        *,
        emit: EventSink | None = None,
        is_disconnected: DisconnectCheck | None = None,
    ) -> dict[str, Any]:
        workspace = tempfile.TemporaryDirectory(prefix="genslide-")
        workspace_path = Path(workspace.name)
        work_task: asyncio.Task[dict[str, Any]] | None = None
        renew_task: asyncio.Task[None] | None = None
        disconnect_task: asyncio.Task[bool] | None = None
        try:
            work_task = asyncio.create_task(self._pipeline(execution, workspace_path, emit))
            renew_task = asyncio.create_task(self._renew_loop(execution))
            watched: set[asyncio.Task[Any]] = {work_task, renew_task}
            if is_disconnected is not None:
                disconnect_task = asyncio.create_task(self._disconnect_loop(is_disconnected))
                watched.add(disconnect_task)
            remaining = max(0.0, (execution.deadline_at - _utcnow()).total_seconds())
            done, _ = await asyncio.wait(watched, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                await self._cancel_work(work_task)
                if execution.committed_response is not None:
                    return execution.committed_response
                raise ServiceError("DEADLINE_EXCEEDED", 504)
            if work_task in done:
                return await work_task
            if disconnect_task is not None and disconnect_task in done and disconnect_task.result():
                await self._cancel_work(work_task)
                if execution.committed_response is not None:
                    return execution.committed_response
                raise ServiceError("CLIENT_DISCONNECTED", 499)
            if renew_task in done:
                try:
                    renew_task.result()
                except ServiceError as exc:
                    await self._cancel_work(work_task)
                    if execution.committed_response is not None:
                        return execution.committed_response
                    raise exc
                await self._cancel_work(work_task)
                if execution.committed_response is not None:
                    return execution.committed_response
                raise ServiceError("LEASE_UNCONFIRMED", 503)
            await self._cancel_work(work_task)
            if execution.committed_response is not None:
                return execution.committed_response
            raise ServiceError("RUNTIME_FAILED", 500)
        except ServiceError as exc:
            if execution.committed_response is not None:
                return execution.committed_response
            if not execution.committed:
                reason = self._settle_reason(exc.code)
                await self._cleanup_failed(execution, reason)
            await self._emit(execution, emit, "error", {"code": exc.code, "status": exc.status})
            raise
        except asyncio.CancelledError:
            if work_task is not None and not work_task.done():
                await self._cancel_work(work_task)
            if execution.committed_response is not None:
                return execution.committed_response
            if not execution.committed:
                await self._cleanup_failed(execution, "client_disconnected")
            raise
        except BaseException as exc:
            if work_task is not None and not work_task.done():
                await self._cancel_work(work_task)
            if execution.committed_response is not None:
                return execution.committed_response
            if not execution.committed:
                await self._cleanup_failed(execution, "execution_failed")
            error = ServiceError("RUNTIME_FAILED", 500)
            await self._emit(execution, emit, "error", {"code": error.code, "status": error.status})
            raise error from exc
        finally:
            for task in (renew_task, disconnect_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(t for t in (renew_task, disconnect_task) if t is not None), return_exceptions=True)
            try:
                workspace.cleanup()
            except OSError:
                pass
            await self._release(execution)

    async def _pipeline(
        self, execution: PreparedExecution, workspace: Path, emit: EventSink | None
    ) -> dict[str, Any]:
        request = execution.request
        materials_parts: list[str] = []
        downloaded_bytes = 0
        for file_id in request.current_file_ids:
            await self._emit_progress(execution, emit, "downloading_materials")
            async with self._transfer_slots:
                downloaded = await self.bff.download_file(
                    request, execution.claim, file_id, workspace, self.settings.max_download_bytes
                )
            path = self._validate_download(downloaded, workspace)
            downloaded_bytes += path.stat().st_size
            if downloaded_bytes > self.settings.max_total_download_bytes:
                raise ServiceError("MATERIALS_TOO_LARGE", 413)
            await self._emit_progress(execution, emit, "parsing_materials")
            parsed = await run_cpu_stage(
                "parse_attachment",
                {"path": str(path)},
                slots=self._cpu_slots,
                timeout=self._remaining(execution),
            )
            materials_parts.append(parsed)
            if sum(len(part) for part in materials_parts) + 2 * (len(materials_parts) - 1) > self.settings.max_material_chars:
                raise ServiceError("MATERIALS_TOO_LARGE", 413)
        materials = "\n\n".join(materials_parts)
        if (
            request.operation == "generate"
            and execution.memory.outline is not None
            and execution.memory.outline.requires_materials
            and not materials
        ):
            raise ServiceError("MISSING_CURRENT_FILES", 422)

        await self._emit_progress(execution, emit, "running_engine")
        execution.engine_started = True
        try:
            work = await self.engine.run(execution.key, request, execution.memory, materials)
            work = WorkResult.model_validate(work)
        except ServiceError:
            raise
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            raise ServiceError("ENGINE_FAILED", 502) from exc
        self._validate_work(execution, work)

        references: list[FileReference] = []
        if request.operation == "generate" and request.target_kind in {"document", "presentation"}:
            await self._emit_progress(execution, emit, "rendering_artifact")
            assert work.content is not None
            rendered_path = await self._render(execution, work.content, workspace)
            async with self._transfer_slots:
                await self._emit_progress(execution, emit, "uploading_artifact")
                reference = await self.bff.upload_file(
                    request,
                    execution.claim,
                    rendered_path,
                    request.target_kind,
                    self.settings.max_artifact_bytes,
                )
            if reference.size > self.settings.max_artifact_bytes:
                raise ServiceError("ARTIFACT_TOO_LARGE", 413)
            references.append(reference)

        await self._emit_progress(execution, emit, "committing_result")
        response = await self._commit(execution, work, references)
        await self._emit(execution, emit, "completed", response)
        return response

    def _validate_work(self, execution: PreparedExecution, work: WorkResult) -> None:
        request = execution.request
        if len(work.memory.model_dump_json().encode("utf-8")) > self.settings.max_memory_bytes:
            raise ServiceError("CONTEXT_CAPACITY", 413)
        if "memory" in work.result:
            raise ServiceError("ENGINE_CONTRACT_ERROR", 502)
        if request.operation == "generate":
            if work.content is None:
                raise ServiceError("GENERATED_CONTENT_MISSING", 502)
        elif work.content is not None:
            raise ServiceError("ENGINE_CONTRACT_ERROR", 502)
        try:
            json.dumps(work.result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ServiceError("ENGINE_CONTRACT_ERROR", 502) from exc

    async def _render(self, execution: PreparedExecution, content: Content, workspace: Path) -> Path:
        output = await run_cpu_stage(
            "render_content",
            {"kind": execution.request.target_kind, "content": content.model_dump(), "directory": str(workspace)},
            slots=self._cpu_slots,
            timeout=self._remaining(execution),
        )
        path = Path(output).resolve()
        try:
            path.relative_to(workspace.resolve())
        except ValueError as exc:
            raise ServiceError("ARTIFACT_INVALID", 500) from exc
        suffix = ".docx" if execution.request.target_kind == "document" else ".pptx"
        if path.suffix.lower() != suffix or not path.is_file():
            raise ServiceError("ARTIFACT_INVALID", 500)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ServiceError("ARTIFACT_UNAVAILABLE", 500) from exc
        if size <= 0 or size > self.settings.max_artifact_bytes:
            raise ServiceError("ARTIFACT_TOO_LARGE", 413)
        return path

    def _validate_download(self, downloaded: Any, workspace: Path) -> Path:
        path = getattr(downloaded, "path", None)
        if not isinstance(path, Path):
            raise ServiceError("BFF_CONTRACT_ERROR", 502)
        path = path.resolve()
        try:
            path.relative_to(workspace.resolve())
        except ValueError as exc:
            raise ServiceError("BFF_CONTRACT_ERROR", 502) from exc
        if path.suffix.lower() not in {".txt", ".md", ".pdf", ".docx"} or not path.is_file():
            raise ServiceError("ATTACHMENT_TYPE_UNSUPPORTED", 415)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ServiceError("ATTACHMENT_UNAVAILABLE", 404) from exc
        reported_size = getattr(downloaded, "size", size)
        if size > self.settings.max_download_bytes or reported_size != size:
            raise ServiceError("ATTACHMENT_TOO_LARGE", 413)
        return path

    async def _commit(
        self, execution: PreparedExecution, work: WorkResult, files: list[FileReference]
    ) -> dict[str, Any]:
        execution.commit_started = True
        request, claim = execution.request, execution.claim
        outline = work.memory.outline
        outline_metadata = None
        if outline is not None:
            outline_metadata = {
                "draft_id": outline.draft_id,
                "outline_version": outline.outline_version,
                "target_kind": outline.target_kind,
                "confirmed_hash": outline.confirmed_hash,
                "skill_id": outline.skill_id,
                "skill_version": outline.skill_version,
                "skill_hash": outline.skill_hash,
            }
        payload = {
            "expected_session_version": claim.session_version,
            "operation": request.operation,
            "target_kind": request.target_kind,
            "result": work.result,
            "content": work.content.model_dump() if work.content is not None else None,
            "files": [reference.as_dict() for reference in files],
            "outline_metadata": outline_metadata,
        }
        outcome: CommitOutcome | None = None
        cancellation: asyncio.CancelledError | None = None
        try:
            raw = await self.bff.commit_result(request, claim, payload)
            outcome = self._validate_commit(execution, raw)
        except asyncio.CancelledError as exc:
            cancellation = exc
            settled = await self._settle_execution(execution, "result_ack_lost")
            if settled is not None and settled.status == "committed":
                outcome = self._commit_from_settlement(execution, settled)
            else:
                raise
        except BaseException as exc:
            settled = await self._settle_execution(execution, "result_ack_lost")
            if settled is not None and settled.status == "committed":
                outcome = self._commit_from_settlement(execution, settled)
            elif settled is not None and settled.status == "closed":
                if isinstance(exc, ServiceError):
                    raise exc
                raise ServiceError("RESULT_NOT_COMMITTED", 502) from exc
            else:
                raise ServiceError("OUTCOME_UNKNOWN", 503) from exc
        assert outcome is not None

        context_available = await self._publish_committed(execution, work, outcome)
        response = {
            "status": "completed",
            "action_id": request.action_id,
            "session_version": outcome.session_version,
            "session_expires_at": outcome.session_expires_at,
            "receipt": outcome.receipt,
            "result": work.result,
            "content": work.content.model_dump() if work.content is not None else None,
            "files": [reference.as_dict() for reference in files],
            "context_available": context_available,
        }
        execution.committed_response = response
        if cancellation is not None or execution.cancel_after_commit:
            execution.cancel_after_commit = True
            raise cancellation or asyncio.CancelledError()
        return response

    def _validate_commit(self, execution: PreparedExecution, outcome: CommitOutcome) -> CommitOutcome:
        if not isinstance(outcome, CommitOutcome):
            raise ServiceError("BFF_CONTRACT_ERROR", 502)
        if (
            outcome.action_id != execution.request.action_id
            or outcome.execution_instance_id != execution.execution_instance_id
            or outcome.session_version != execution.claim.session_version + 1
            or outcome.session_expires_at <= _utcnow()
        ):
            raise ServiceError("BFF_CONTRACT_ERROR", 502)
        return outcome

    def _commit_from_settlement(self, execution: PreparedExecution, settled: SettleOutcome) -> CommitOutcome:
        if (
            settled.action_id != execution.request.action_id
            or settled.execution_instance_id != execution.execution_instance_id
            or settled.session_version != execution.claim.session_version + 1
            or settled.session_expires_at is None
            or settled.session_expires_at <= _utcnow()
        ):
            raise ServiceError("OUTCOME_UNKNOWN", 503)
        return CommitOutcome(
            status="committed",
            action_id=settled.action_id,
            execution_instance_id=execution.execution_instance_id,
            session_version=settled.session_version,
            session_expires_at=settled.session_expires_at,
            receipt=settled.receipt,
        )

    async def _publish_committed(
        self, execution: PreparedExecution, work: WorkResult, outcome: CommitOutcome
    ) -> bool:
        publish = asyncio.create_task(self.engine.publish(execution.key, work))
        while True:
            try:
                await asyncio.shield(publish)
                break
            except asyncio.CancelledError:
                execution.cancel_after_commit = True
                if publish.done():
                    if publish.cancelled() or publish.exception() is not None:
                        await self._invalidate(execution.key)
                        execution.committed = True
                        return False
                    break
                continue
            except BaseException:
                await self._invalidate(execution.key)
                execution.committed = True
                return False
        if publish.cancelled() or publish.exception() is not None:
            await self._invalidate(execution.key)
            execution.committed = True
            return False
        async with self._lock:
            self._metadata[execution.key] = SessionMetadata(
                version=outcome.session_version, expires_at=outcome.session_expires_at
            )
            self._reserved_new.discard(execution.key)
        execution.reserved_new_session = False
        execution.committed = True
        return True

    async def _read_committed_memory(self, key: str, request: ExecuteRequest, claim: Claim) -> Memory:
        now = _utcnow()
        if claim.session_expires_at <= now:
            raise ServiceError("SESSION_EXPIRED", 409)
        async with self._lock:
            metadata = self._metadata.get(key)
        if metadata is None:
            if claim.runtime_initialized or claim.session_version != 0:
                await self._invalidate(key)
                raise ServiceError("CONTEXT_LOST", 409)
            async with self._lock:
                occupied = len(self._metadata) + len(self._reserved_new)
                if key not in self._reserved_new and occupied >= self.settings.max_sessions:
                    raise ServiceError("RUNTIME_CAPACITY", 429)
                self._reserved_new.add(key)
            return Memory()
        if metadata.expires_at <= now:
            await self._invalidate(key)
            raise ServiceError("SESSION_EXPIRED", 409)
        if (
            not claim.runtime_initialized
            or metadata.version != claim.session_version
            or claim.session_version != request.expected_session_version
            or abs((metadata.expires_at - claim.session_expires_at).total_seconds()) > 1
        ):
            await self._invalidate(key)
            raise ServiceError("CONTEXT_STALE", 409)
        try:
            memory = await self.engine.read(key)
        except BaseException as exc:
            await self._invalidate(key)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ServiceError("CONTEXT_LOST", 409) from exc
        if memory is None:
            await self._invalidate(key)
            raise ServiceError("CONTEXT_LOST", 409)
        try:
            checked = Memory.model_validate(memory.model_dump())
        except BaseException as exc:
            await self._invalidate(key)
            raise ServiceError("CONTEXT_INVALID", 409) from exc
        if len(checked.model_dump_json().encode("utf-8")) > self.settings.max_memory_bytes:
            await self._invalidate(key)
            raise ServiceError("CONTEXT_CAPACITY", 413)
        return checked

    def _validate_claim(self, request: ExecuteRequest, instance_id: str, claim: Claim) -> None:
        if (
            claim.action_id != request.action_id
            or claim.execution_instance_id != instance_id
            or claim.engine != self.engine.name
            or claim.runtime_epoch != request.runtime_epoch
            or claim.session_version != request.expected_session_version
        ):
            raise ServiceError("CONTEXT_STALE", 409)

    async def _admit(self, key: str, bucket: str) -> None:
        async with self._lock:
            now = _utcnow()
            expired = [
                session_key
                for session_key, metadata in self._metadata.items()
                if metadata.expires_at <= now and session_key not in self._busy
            ]
            for session_key in expired:
                try:
                    await self.engine.delete(session_key)
                except BaseException:
                    # Remove the inaccessible entry locally; a future claim will
                    # fail closed if the engine retained any stale state.
                    pass
                self._metadata.pop(session_key, None)
            if key in self._busy:
                raise ServiceError("SESSION_BUSY", 409)
            limit = (
                self.settings.generation_concurrency if bucket == "generation" else self.settings.planning_concurrency
            )
            if self._active[bucket] >= limit:
                raise ServiceError("POD_CAPACITY", 429)
            self._busy.add(key)
            self._active[bucket] += 1

    async def _release_key(self, key: str, bucket: str) -> None:
        async with self._lock:
            if key in self._busy:
                self._busy.remove(key)
                self._active[bucket] = max(0, self._active[bucket] - 1)
            self._reserved_new.discard(key)

    async def _release(self, execution: PreparedExecution) -> None:
        if execution.released:
            return
        execution.released = True
        await self._release_key(execution.key, execution.bucket)

    async def _invalidate(self, key: str) -> None:
        async with self._lock:
            self._metadata.pop(key, None)
            self._reserved_new.discard(key)
        try:
            await asyncio.wait_for(self.engine.delete(key), timeout=self.settings.control_timeout_seconds)
        except BaseException:
            pass

    async def _cleanup_failed(self, execution: PreparedExecution, reason: str) -> None:
        if execution.committed:
            return
        if not execution.settled:
            await self._settle_execution(execution, reason)
        if execution.engine_started or execution.commit_started:
            await self._invalidate(execution.key)

    async def _reconcile_unclaimed(self, request: ExecuteRequest, instance_id: str) -> None:
        task = asyncio.create_task(self.bff.settle(request, None, instance_id, "claim_ack_lost"))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.settings.control_timeout_seconds)
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _settle_execution(self, execution: PreparedExecution, reason: str) -> SettleOutcome | None:
        outcome = await self._settle_raw(
            execution.request, execution.claim, execution.execution_instance_id, reason
        )
        execution.settled = True
        if outcome is not None and outcome.execution_instance_id not in (None, execution.execution_instance_id):
            return None
        return outcome

    async def _settle_raw(
        self, request: ExecuteRequest, claim: Claim | None, instance_id: str, reason: str
    ) -> SettleOutcome | None:
        task = asyncio.create_task(self.bff.settle(request, claim, instance_id, reason))
        try:
            outcome = await asyncio.wait_for(asyncio.shield(task), timeout=self.settings.control_timeout_seconds)
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return None
        if not isinstance(outcome, SettleOutcome) or outcome.action_id != request.action_id:
            return None
        if outcome.execution_instance_id not in (None, instance_id):
            return None
        return outcome

    async def _renew_loop(self, execution: PreparedExecution) -> None:
        while True:
            now = _utcnow()
            remaining_deadline = (execution.deadline_at - now).total_seconds()
            remaining_lease = (execution.lease_expires_at - now).total_seconds()
            if remaining_deadline <= 0:
                raise ServiceError("DEADLINE_EXCEEDED", 504)
            if remaining_lease <= 0:
                raise ServiceError("LEASE_UNCONFIRMED", 503)
            await asyncio.sleep(
                min(self.settings.renew_interval_seconds, max(0.0, remaining_lease - 1.0), remaining_deadline)
            )
            now = _utcnow()
            remaining_lease = (execution.lease_expires_at - now).total_seconds()
            remaining_deadline = (execution.deadline_at - now).total_seconds()
            if remaining_lease <= 0 or remaining_deadline <= 0:
                raise ServiceError("LEASE_UNCONFIRMED", 503)
            timeout = min(self.settings.bff_timeout_seconds, remaining_lease, remaining_deadline)
            try:
                renewed_until = await asyncio.wait_for(
                    self.bff.renew(execution.request, execution.claim), timeout=timeout
                )
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ServiceError("LEASE_UNCONFIRMED", 503) from exc
            if not isinstance(renewed_until, datetime) or renewed_until.tzinfo is None:
                raise ServiceError("BFF_CONTRACT_ERROR", 502)
            renewed_until = renewed_until.astimezone(timezone.utc)
            if renewed_until <= _utcnow() or renewed_until > execution.claim.deadline_at:
                raise ServiceError("LEASE_UNCONFIRMED", 503)
            execution.lease_expires_at = renewed_until

    async def _disconnect_loop(self, check: DisconnectCheck) -> bool:
        while True:
            try:
                if await check():
                    return True
            except asyncio.CancelledError:
                raise
            except BaseException:
                pass
            await asyncio.sleep(self.settings.disconnect_poll_seconds)

    async def _cancel_work(self, task: asyncio.Task[Any]) -> None:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def _remaining(self, execution: PreparedExecution) -> float:
        remaining = (execution.deadline_at - _utcnow()).total_seconds()
        if remaining <= 0:
            raise ServiceError("DEADLINE_EXCEEDED", 504)
        return remaining

    @staticmethod
    def _settle_reason(code: str) -> str:
        if code in {"DEADLINE_EXCEEDED", "ACTION_DEADLINE_EXCEEDED"}:
            return "deadline_exceeded"
        if code == "LEASE_UNCONFIRMED":
            return "lease_unconfirmed"
        if code == "CLIENT_DISCONNECTED":
            return "client_disconnected"
        return "execution_failed"

    async def _emit_progress(self, execution: PreparedExecution, emit: EventSink | None, stage: str) -> None:
        await self._emit(execution, emit, "progress", {"stage": stage})

    async def _emit(
        self,
        execution: PreparedExecution,
        emit: EventSink | None,
        event: str,
        fields: Mapping[str, Any],
    ) -> None:
        if emit is None:
            return
        execution.sequence += 1
        payload = {
            "event": event,
            "action_id": execution.request.action_id,
            "sequence": execution.sequence,
            **fields,
        }
        try:
            await emit(payload)
        except asyncio.CancelledError:
            raise
        except BaseException:
            # Stream back-pressure/client closure must not make a committed
            # BFF result look like a failed action.
            return

    async def aclose(self) -> None:
        bff_close = getattr(self.bff, "aclose", None)
        if bff_close is not None:
            await bff_close()
        await self.engine.aclose()

    def metrics(self) -> dict[str, int]:
        return {
            "generation_active": self._active["generation"],
            "planning_active": self._active["planning"],
            "generation_capacity": self.settings.generation_concurrency,
            "planning_capacity": self.settings.planning_concurrency,
            "sessions_cached": len(self._metadata),
            "sessions_capacity": self.settings.max_sessions,
            "sessions_busy": len(self._busy),
        }


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--cpu-worker":
    raise SystemExit(_cpu_worker())
