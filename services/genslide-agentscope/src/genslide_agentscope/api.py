"""Synchronous JSON and SSE API for the AgentScope service."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import re
from typing import Any, Mapping

import anyio
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse, PlainTextResponse

from .bff import BFF, BFFClient
from .config import Settings
from .domain import ExecuteRequest, ServiceError
from .execution import Engine, ExecutionRuntime


_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
_STOP = object()


async def _send_guard_response(send, status: int, code: str) -> None:
    body = json.dumps({"code": code}, separators=(",", ":")).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RequestGuardMiddleware:
    """Authenticate execute calls and cap the complete body before JSON parsing."""

    def __init__(self, app, settings: Settings):
        self.app = app
        self.max_body_bytes = settings.max_request_bytes
        self.auth_enabled = settings.service_auth_enabled
        self.service_token = (settings.service_token or "").encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        execute_route = scope.get("method") == "POST" and path.startswith("/v1/actions/") and path.endswith("/execute")
        if (execute_route or path == "/internal/metrics") and self.auth_enabled:
            authorization = next(
                (value for name, value in scope.get("headers", ()) if name.lower() == b"authorization"),
                b"",
            )
            scheme, separator, supplied = authorization.partition(b" ")
            if (
                not separator
                or scheme.lower() != b"bearer"
                or not hmac.compare_digest(supplied.strip(), self.service_token)
            ):
                await _send_guard_response(send, 401, "SERVICE_AUTH_REQUIRED")
                return

        content_length = next(
            (value for name, value in scope.get("headers", ()) if name.lower() == b"content-length"),
            None,
        )
        if content_length is not None:
            try:
                if int(content_length) > self.max_body_bytes:
                    await _send_guard_response(send, 413, "REQUEST_TOO_LARGE")
                    return
            except ValueError:
                await _send_guard_response(send, 400, "INVALID_CONTENT_LENGTH")
                return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > self.max_body_bytes:
                await _send_guard_response(send, 413, "REQUEST_TOO_LARGE")
                return
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay_body():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay_body, send)


def _error_response(status: int, code: str, action_id: str | None = None) -> JSONResponse:
    safe_code = code if _ERROR_CODE.fullmatch(code) else "INTERNAL_ERROR"
    payload: dict[str, Any] = {"code": safe_code}
    if action_id:
        payload["action_id"] = action_id
    return JSONResponse(jsonable_encoder(payload), status_code=status, headers={"Cache-Control": "no-store"})


def _sse_frame(event: Mapping[str, Any]) -> bytes:
    name = event.get("event")
    sequence = event.get("sequence")
    safe_name = name if name in {"accepted", "progress", "completed", "error"} else "error"
    data = json.dumps(jsonable_encoder(dict(event)), ensure_ascii=False, separators=(",", ":"))
    frame = f"event: {safe_name}\n"
    if isinstance(sequence, int) and sequence > 0:
        frame += f"id: {sequence}\n"
    frame += f"data: {data}\n\n"
    return frame.encode("utf-8")


def create_app(
    settings: Settings | None = None,
    bff: BFF | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    """Create an API app; tests may inject settings, BFF, and engine doubles."""
    settings = settings or Settings.from_env(require_model=engine is None)
    if engine is None:
        settings.require_model_configuration()
        # Import the framework-specific engine only for the default production path.
        from .engine import Engine as DefaultEngine

        engine = DefaultEngine()
    if bff is None:
        bff = BFFClient(settings)
    runtime = ExecutionRuntime(bff, engine, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = runtime
        try:
            yield
        finally:
            await runtime.aclose()

    app = FastAPI(
        title="GenSlide API",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.add_middleware(RequestGuardMiddleware, settings=settings)

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        return _error_response(exc.status, exc.code, request.path_params.get("action_id"))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        # Deliberately omit Pydantic's input/context, which can contain the BFF grant.
        return _error_response(422, "INVALID_REQUEST", request.path_params.get("action_id"))

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        return _error_response(500, "INTERNAL_ERROR", request.path_params.get("action_id"))

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        return {"status": "ready"}

    @app.get("/internal/metrics", response_class=PlainTextResponse)
    async def metrics():
        values = runtime.metrics()
        lines = []
        for name, value in values.items():
            metric = "genslide_" + name
            lines.extend([f"# TYPE {metric} gauge", f"{metric} {value}"])
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.get("/v1/skills")
    async def list_skills(request: Request):
        active_runtime: ExecutionRuntime = request.app.state.runtime
        if hasattr(active_runtime.engine, "skills") and hasattr(active_runtime.engine.skills, "list_skills"):
            return JSONResponse(jsonable_encoder({"skills": active_runtime.engine.skills.list_skills()}))
        return JSONResponse({"skills": []})

    @app.post("/v1/skills/reload")
    async def reload_skills(request: Request):
        active_runtime: ExecutionRuntime = request.app.state.runtime
        if hasattr(active_runtime.engine, "skills") and hasattr(active_runtime.engine.skills, "reload"):
            refreshed = active_runtime.engine.skills.reload()
            return JSONResponse(jsonable_encoder({"skills": refreshed, "reloaded": True}))
        return JSONResponse({"skills": [], "reloaded": False})

    @app.post("/v1/actions/{action_id}/execute")
    async def execute(action_id: str, request: Request, body: ExecuteRequest):
        if action_id != body.action_id:
            raise ServiceError("ACTION_ID_MISMATCH", 400)
        active_runtime: ExecutionRuntime = request.app.state.runtime
        execution = await active_runtime.prepare(body)

        if "text/event-stream" not in request.headers.get("accept", "").lower():
            result = await active_runtime.perform(execution, is_disconnected=request.is_disconnected)
            return JSONResponse(jsonable_encoder(result))

        accepted = await active_runtime.accepted_event(execution)

        async def event_stream():
            queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=64)
            runner_started = False
            runner: asyncio.Task[None] | None = None

            async def emit(event: Mapping[str, Any]) -> None:
                terminal = event.get("event") in {"completed", "error"}
                if terminal:
                    await queue.put(dict(event))
                    return
                # Reserve space for the terminal event even when a slow client
                # cannot keep up with progress.
                if queue.qsize() >= queue.maxsize - 2:
                    return
                try:
                    queue.put_nowait(dict(event))
                except asyncio.QueueFull:
                    # Runtime events are sparse. The cap prevents a detached slow
                    # consumer from turning event delivery into an unbounded buffer.
                    return

            async def run_action() -> None:
                nonlocal runner_started
                runner_started = True
                try:
                    await active_runtime.perform(execution, emit=emit)
                except asyncio.CancelledError:
                    raise
                except BaseException:
                    # perform emits the sanitized error event before raising.
                    pass
                finally:
                    # Two queue slots are reserved for one terminal event and
                    # this wake-up marker, so a completed stream never waits for
                    # the heartbeat interval to notice that it is finished.
                    await queue.put(_STOP)

            try:
                yield _sse_frame(accepted)
                runner = asyncio.create_task(run_action())
                while True:
                    if runner.done() and queue.empty():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=settings.sse_heartbeat_seconds)
                    except asyncio.TimeoutError:
                        if runner.done() and queue.empty():
                            break
                        yield b": keep-alive\n\n"
                        continue
                    if event is _STOP:
                        break
                    yield _sse_frame(event)
            finally:
                with anyio.CancelScope(shield=True):
                    if runner is not None:
                        if not runner.done():
                            runner.cancel()
                        await asyncio.gather(runner, return_exceptions=True)
                    if not runner_started:
                        await active_runtime.cancel_prepared(execution)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    return app
