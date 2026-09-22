from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from genslide_agentscope.api import create_app
from genslide_agentscope.bff import Claim, CommitOutcome, SettleOutcome
from genslide_agentscope.config import Settings
from genslide_agentscope.domain import ExecuteRequest, Memory, ServiceError, WorkResult
from genslide_agentscope.execution import ExecutionRuntime


ENGINE = "agentscope"

@pytest.mark.asyncio
async def test_lease_failure_stops_work_without_commit() -> None:
    class LeaseFailure(FakeBFF):
        async def renew(self, req, claim):
            raise ServiceError("BFF_UNAVAILABLE", 503)
    bff, engine = LeaseFailure(), FakeEngine()
    engine.run_gate = asyncio.Event()
    runtime = ExecutionRuntime(bff, engine, settings(renew_interval_seconds=0.01))
    prepared = await runtime.prepare(request("lease-failure"))
    with pytest.raises(ServiceError, match="LEASE_UNCONFIRMED"):
        await runtime.perform(prepared)
    assert engine.cancelled.is_set()
    assert bff.commit_count == 0
    assert bff.actions["lease-failure"]["status"] == "closed"
    assert runtime.metrics()["planning_active"] == 0
    await runtime.aclose()


@pytest.mark.asyncio
async def test_pod_capacity_is_rejected_before_second_claim() -> None:
    bff, engine = FakeBFF(), FakeEngine()
    runtime = ExecutionRuntime(bff, engine, settings(generation_concurrency=1))
    first = await runtime.prepare(request("first-capacity"))
    with pytest.raises(ServiceError, match="POD_CAPACITY"):
        await runtime.prepare(request("second-capacity", session_id="another-session"))
    assert "second-capacity" not in bff.actions
    assert engine.runs == 0
    await runtime.cancel_prepared(first)
    assert runtime.metrics()["planning_active"] == 0
    await runtime.aclose()

SERVICE_TOKEN = "test-service-token-32-characters!!"


def request(action_id: str, version: int = 0, **overrides) -> ExecuteRequest:
    values = {
        "engine": ENGINE,
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "session_id": "session-a",
        "runtime_epoch": "epoch-a",
        "action_id": action_id,
        "authorization": "bff-initial-grant",
        "expected_session_version": version,
        "expected_lifecycle_version": 1,
        "mode": "assistant",
        "requested_output": "text",
        "message": "A short guide",
    }
    values.update(overrides)
    return ExecuteRequest.model_validate(values)


def settings(**overrides) -> Settings:
    values = {
        "environment": "test",
        "engine_name": ENGINE,
        "service_token": SERVICE_TOKEN,
        "bff_timeout_seconds": 0.5,
        "control_timeout_seconds": 0.5,
        "renew_interval_seconds": 10,
        "planning_timeout_seconds": 5,
        "generation_timeout_seconds": 5,
        "max_request_bytes": 128 * 1024,
        "disconnect_poll_seconds": 0.01,
        "sse_heartbeat_seconds": 0.05,
    }
    values.update(overrides)
    return Settings(**values)


class FakeEngine:
    name = ENGINE

    def __init__(self):
        self.memories: dict[str, Memory] = {}
        self.runs = 0
        self.deletes: list[str] = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.run_gate: asyncio.Event | None = None

    async def read(self, key: str) -> Memory | None:
        return self.memories.get(key)

    async def run(self, key: str, req: ExecuteRequest, memory: Memory, materials: str,
                  progress=None) -> WorkResult:
        self.runs += 1
        self.started.set()
        if self.run_gate is not None:
            try:
                await self.run_gate.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        requirements = dict(memory.requirements)
        if req.message:
            requirements.setdefault("topic", req.message)
        return WorkResult(
            memory=Memory(requirements=requirements),
            result={"result_type": "clarification", "reply": "What audience should this guide serve?"},
        )

    async def publish(self, key: str, work: WorkResult) -> None:
        self.memories[key] = work.memory

    async def delete(self, key: str) -> None:
        self.deletes.append(key)
        self.memories.pop(key, None)

    async def aclose(self) -> None:
        return None


class FakeBFF:
    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.actions: dict[str, dict] = {}
        self.settle_reasons: list[str] = []
        self.claim_entered = asyncio.Event()
        self.claim_gate: asyncio.Event | None = None
        self.claim_version_override: int | None = None
        self.commit_written = asyncio.Event()
        self.commit_ack_gate: asyncio.Event | None = None
        self.lose_commit_ack = False
        self.commit_count = 0

    def _session(self, req: ExecuteRequest) -> dict:
        return self.sessions.setdefault(
            req.session_key(),
            {"version": 0, "expires": datetime.now(timezone.utc) + timedelta(hours=4), "snapshot": None},
        )

    async def claim(self, req: ExecuteRequest, instance_id: str) -> Claim:
        session = self._session(req)
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(minutes=5)
        action = {
            "request": req,
            "instance": instance_id,
            "token": f"token-{instance_id}",
            "status": "active",
            "deadline": deadline,
            "lease": min(now + timedelta(seconds=45), deadline),
        }
        self.actions[req.action_id] = action
        self.claim_entered.set()
        if self.claim_gate is not None:
            await self.claim_gate.wait()
        version = session["version"] if self.claim_version_override is None else self.claim_version_override
        return Claim(
            req.action_id, instance_id, action["token"], req.engine, req.runtime_epoch,
            version, session["version"] > 0, session["expires"], action["lease"], deadline,
            1, session["snapshot"],
        )

    async def renew(self, req: ExecuteRequest, claim: Claim) -> datetime:
        action = self.actions[req.action_id]
        action["lease"] = min(datetime.now(timezone.utc) + timedelta(seconds=45), claim.deadline_at)
        return action["lease"]

    async def commit_result(self, req: ExecuteRequest, claim: Claim, payload) -> CommitOutcome:
        self.commit_count += 1
        action = self.actions[req.action_id]
        if action["status"] != "committed":
            session = self._session(req)
            assert payload["expected_session_version"] == session["version"]
            session["version"] += 1
            session["expires"] = datetime.now(timezone.utc) + timedelta(hours=4)
            session["snapshot"] = payload.get("snapshot")
            action.update(status="committed", version=session["version"], receipt=f"receipt-{req.action_id}")
        self.commit_written.set()
        if self.commit_ack_gate is not None:
            await self.commit_ack_gate.wait()
        if self.lose_commit_ack:
            raise ServiceError("BFF_UNAVAILABLE", 503)
        session = self._session(req)
        return CommitOutcome(
            "committed", req.action_id, claim.execution_instance_id,
            action["version"], session["expires"], action["receipt"],
        )

    async def settle(self, req, claim, instance_id: str, reason: str) -> SettleOutcome:
        self.settle_reasons.append(reason)
        action = self.actions.get(req.action_id)
        if action is None:
            return SettleOutcome("closed", req.action_id, None)
        if action["status"] == "committed":
            session = self._session(req)
            return SettleOutcome(
                "committed", req.action_id, action["instance"],
                session["version"], session["expires"], action["receipt"], 1,
                session["snapshot"],
            )
        action["status"] = "closed"
        return SettleOutcome("closed", req.action_id, action.get("instance"))

    async def download_file(self, *args, **kwargs):
        raise AssertionError("unexpected download")

    async def upload_file(self, *args, **kwargs):
        raise AssertionError("unexpected upload")

    async def get_action(self, *args, **kwargs):
        raise AssertionError("unexpected status query")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_claim_version_commit_and_next_turn() -> None:
    bff, engine = FakeBFF(), FakeEngine()
    runtime = ExecutionRuntime(bff, engine, settings())
    first = await runtime.prepare(request("first"))
    assert first.claim.session_version == 0 and not first.claim.runtime_initialized
    await runtime.perform(first)
    assert runtime._metadata[first.key].version == 1

    await runtime.aclose()
    engine = FakeEngine()
    runtime = ExecutionRuntime(bff, engine, settings())
    second = await runtime.prepare(request("second", version=1))
    assert second.memory.requirements["topic"] == "A short guide"
    assert second.claim.runtime_initialized
    await runtime.perform(second)
    assert bff.sessions[second.key]["version"] == 2
    assert engine.runs == 1
    await runtime.aclose()


@pytest.mark.asyncio
async def test_wrong_claim_version_settles_and_releases_admission() -> None:
    bff, engine = FakeBFF(), FakeEngine()
    bff.claim_version_override = 9
    runtime = ExecutionRuntime(bff, engine, settings())
    with pytest.raises(ServiceError, match="CONTEXT_STALE"):
        await runtime.prepare(request("stale"))
    assert bff.settle_reasons == ["execution_failed"]
    assert not runtime._busy
    bff.claim_version_override = None
    prepared = await runtime.prepare(request("good"))
    await runtime.perform(prepared)
    assert engine.runs == 1
    await runtime.aclose()


@pytest.mark.asyncio
async def test_commit_ack_loss_reconciles_without_rerunning() -> None:
    bff, engine = FakeBFF(), FakeEngine()
    bff.lose_commit_ack = True
    runtime = ExecutionRuntime(bff, engine, settings())
    result = await runtime.perform(await runtime.prepare(request("lost-ack")))
    assert result["status"] == "completed" and result["receipt"] == "receipt-lost-ack"
    assert bff.settle_reasons == ["result_ack_lost"]
    assert bff.commit_count == engine.runs == 1
    await runtime.aclose()


@pytest.mark.asyncio
async def test_cancel_claim_wait_settles_unknown_claim() -> None:
    bff, engine = FakeBFF(), FakeEngine()
    bff.claim_gate = asyncio.Event()
    runtime = ExecutionRuntime(bff, engine, settings())
    task = asyncio.create_task(runtime.prepare(request("claim-cancel")))
    await asyncio.wait_for(bff.claim_entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bff.settle_reasons == ["claim_ack_lost"]
    assert bff.actions["claim-cancel"]["status"] == "closed"
    assert not runtime._busy and engine.runs == 0
    await runtime.aclose()


@pytest.mark.asyncio
async def test_disconnect_cancels_uncommitted_work() -> None:
    bff, engine = FakeBFF(), FakeEngine()
    engine.run_gate = asyncio.Event()
    runtime = ExecutionRuntime(bff, engine, settings())
    prepared = await runtime.prepare(request("disconnect"))
    disconnected = asyncio.Event()

    async def check_disconnect() -> bool:
        return disconnected.is_set()

    task = asyncio.create_task(runtime.perform(prepared, is_disconnected=check_disconnect))
    await asyncio.wait_for(engine.started.wait(), timeout=1)
    disconnected.set()
    with pytest.raises(ServiceError, match="CLIENT_DISCONNECTED"):
        await asyncio.wait_for(task, timeout=1)
    assert engine.cancelled.is_set()
    assert bff.settle_reasons == ["client_disconnected"]
    assert engine.deletes == [prepared.key] and not runtime._busy
    await runtime.aclose()


@pytest.mark.asyncio
async def test_cancelled_commit_ack_returns_committed_receipt() -> None:
    bff, engine = FakeBFF(), FakeEngine()
    bff.commit_ack_gate = asyncio.Event()
    runtime = ExecutionRuntime(bff, engine, settings())
    prepared = await runtime.prepare(request("commit-race"))
    disconnected = asyncio.Event()

    async def check_disconnect() -> bool:
        return disconnected.is_set()

    task = asyncio.create_task(runtime.perform(prepared, is_disconnected=check_disconnect))
    await asyncio.wait_for(bff.commit_written.wait(), timeout=1)
    disconnected.set()
    result = await asyncio.wait_for(task, timeout=1)
    assert result["status"] == "completed" and result["receipt"] == "receipt-commit-race"
    assert prepared.committed_response is result
    assert bff.settle_reasons == ["result_ack_lost"]
    assert bff.commit_count == engine.runs == 1
    await runtime.aclose()


@pytest.mark.asyncio
async def test_api_auth_validation_json_and_sse() -> None:
    bff, engine = FakeBFF(), FakeEngine()
    app = create_app(settings=settings(max_request_bytes=1024), bff=bff, engine=engine)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/healthz")).status_code == 200
            headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
            body = request("json").model_dump(mode="json")
            path = "/v1/actions/json/execute"
            assert (await client.post(path, json=body)).status_code == 401
            result = await client.post(path, json=body, headers=headers)
            assert result.status_code == 200 and result.json()["status"] == "completed"

            stream = await client.post(
                "/v1/actions/sse/execute",
                json=request("sse", version=1).model_dump(mode="json"),
                headers=headers | {"Accept": "text/event-stream"},
            )
            assert stream.status_code == 200
            assert all(f"event: {name}" in stream.text for name in ("accepted", "progress", "completed"))

            too_large = await client.post(
                path, content=b"{" + b" " * 1100,
                headers=headers | {"Content-Type": "application/json"},
            )
            assert too_large.status_code == 413
            invalid = request("bad").model_dump(mode="json")
            invalid["engine"] = "invalid"
            invalid["authorization"] = "sensitive-bff-grant"
            rejected = await client.post("/v1/actions/bad/execute", json=invalid, headers=headers)
            assert rejected.status_code == 422
            assert "sensitive-bff-grant" not in rejected.text


@pytest.mark.asyncio
async def test_asgi_disconnect_cancels_sse_request() -> None:
    bff, engine = FakeBFF(), FakeEngine()
    engine.run_gate = asyncio.Event()
    app = create_app(settings=settings(), bff=bff, engine=engine)
    body = json.dumps(request("asgi-disconnect").model_dump(mode="json")).encode()
    headers = [
        (b"host", b"test"), (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"authorization", f"Bearer {SERVICE_TOKEN}".encode()),
        (b"accept", b"text/event-stream"),
    ]
    disconnect = asyncio.Event()
    request_delivered = False
    sent = []

    async def receive():
        nonlocal request_delivered
        if not request_delivered:
            request_delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message.get("type") == "http.response.body" and b"event: accepted" in message.get("body", b""):
            async def disconnect_on_execution():
                await asyncio.wait_for(engine.started.wait(), timeout=1)
                disconnect.set()
            asyncio.create_task(disconnect_on_execution())

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/v1/actions/asgi-disconnect/execute",
        "raw_path": b"/v1/actions/asgi-disconnect/execute", "query_string": b"", "root_path": "",
        "headers": headers, "client": ("test", 1234), "server": ("test", 80),
    }

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(app(scope, receive, send), timeout=3)
        await asyncio.wait_for(engine.cancelled.wait(), timeout=1)
        assert bff.settle_reasons == ["client_disconnected"]
        assert bff.actions["asgi-disconnect"]["status"] == "closed"
        assert any(item.get("status") == 200 for item in sent if item["type"] == "http.response.start")
