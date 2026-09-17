from uuid import uuid4
import httpx
import pytest
from genslide_langgraph.bff import BFFClient
from genslide_langgraph.config import Settings
from genslide_langgraph.domain import ExecuteRequest, ServiceError
from genslide_langgraph.engine import Engine
from genslide_langgraph.execution import ExecutionRuntime
from genslide_langgraph.mock_bff import create_mock_bff

TOKEN = "local-test-token-with-more-than-32-characters"

class Model:
    def __init__(self):
        self.calls = []
    async def complete(self, system, payload):
        self.calls.append(payload)
        if payload["operation"] == "clarify":
            return {"requirements":{"topic":"测试写作"}, "guidance":{}, "answer":"我们先确定大纲。"}
        if payload["operation"] == "create_outline":
            return {"title":"测试写作", "nodes":[{"node_id":"n", "title":"主题介绍"}]}
        if payload["operation"] == "generate":
            return {"title":"测试写作", "sections":[{"title":"主题介绍", "body":"这是完整正文，用于验证内容生成与交接。", "notes":"讲解主题"}]}
        raise AssertionError(payload)

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["writing", "document", "presentation"])
async def test_real_engine_bff_http_and_artifact_handoff(monkeypatch, kind):
    monkeypatch.setenv("GENSLIDE_ALLOW_MOCK", "1")
    monkeypatch.setenv("GENSLIDE_ENV", "test")
    monkeypatch.setenv("GENSLIDE_SERVICE_TOKEN", TOKEN)
    bff_app = create_mock_bff()
    transport = httpx.ASGITransport(app=bff_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bff",
                                headers={"Authorization":"Bearer " + TOKEN}) as admin:
        async with httpx.AsyncClient(transport=transport, base_url="http://bff/internal/genslide/v1") as client:
            settings = Settings(environment="test", service_token=TOKEN, bff_url="http://bff/internal/genslide/v1")
            model = Model()
            runtime = ExecutionRuntime(BFFClient(settings, client), Engine(model), settings)
            version, draft = 0, None
            last_request = None
            events = []
            async def emit(event):
                events.append(event)
            try:
                for operation in ["clarify", "create_outline", "confirm_outline", "generate"]:
                    data = dict(engine="langgraph", tenant_id="t", user_id="u", session_id="s", runtime_epoch="e",
                        action_id=uuid4().hex, authorization="placeholder", expected_session_version=version,
                        operation=operation, target_kind=kind, message="测试写作" if operation == "clarify" else "")
                    if operation in ("confirm_outline", "generate"):
                        data.update(draft_id=draft["draft_id"], expected_outline_version=draft["outline_version"])
                    response = await admin.post("/dev/begin", json=data)
                    assert response.status_code == 200, response.text
                    request = ExecuteRequest.model_validate(response.json()["request"])
                    prepared = await runtime.prepare(request)
                    result = await runtime.perform(prepared, emit=emit)
                    assert result["status"] == "completed"
                    version = result["session_version"]
                    if result["result"].get("outline"):
                        draft = result["result"]["outline"]
                    last_request = request
                assert version == 4
                assert len(model.calls) == 3  # confirmation is zero-model
                assert result["content"]["sections"][0]["body"].startswith("这是完整正文")
                assert len(result["files"]) == (0 if kind == "writing" else 1)
                if result["files"]:
                    artifact = await admin.get("/dev/artifacts/" + result["files"][0]["file_id"])
                    assert artifact.status_code == 200
                    assert artifact.content
                    assert "attachment" in artifact.headers["content-disposition"]
                assert len([e for e in events if e["event"] == "completed"]) == 4
                stored = bff_app.state.actions[last_request.action_id]
                assert stored["status"] == "committed"
                assert stored["result"]["content"] == result["content"]
                with pytest.raises(ServiceError):
                    await runtime.prepare(last_request)
                assert len(model.calls) == 3
            finally:
                await runtime.aclose()

@pytest.mark.asyncio
async def test_two_pods_cannot_execute_same_action_or_rebuild_lost_context(monkeypatch):
    monkeypatch.setenv("GENSLIDE_ALLOW_MOCK", "1")
    monkeypatch.setenv("GENSLIDE_ENV", "test")
    monkeypatch.setenv("GENSLIDE_SERVICE_TOKEN", TOKEN)
    app = create_mock_bff()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://bff/internal/genslide/v1") as http:
        settings = Settings(environment="test", service_token=TOKEN, bff_url="http://bff/internal/genslide/v1")
        one, two = Model(), Model()
        first = ExecutionRuntime(BFFClient(settings, http), Engine(one), settings)
        second = ExecutionRuntime(BFFClient(settings, http), Engine(two), settings)
        body = dict(engine="langgraph", tenant_id="t", user_id="u", session_id="s", runtime_epoch="e",
                    action_id=uuid4().hex, authorization="placeholder", expected_session_version=0,
                    operation="clarify", target_kind="writing", message="测试写作")
        async def begin(data):
            response = await http.post("http://bff/dev/begin", json=data,
                                       headers={"Authorization":"Bearer " + TOKEN})
            assert response.status_code == 200, response.text
            return ExecuteRequest.model_validate(response.json()["request"])
        try:
            request = await begin(body)
            prepared = await first.prepare(request)
            with pytest.raises(ServiceError):
                await second.prepare(request)
            assert app.state.actions[request.action_id]["status"] == "active"
            result = await first.perform(prepared)
            assert len(one.calls) == 1 and len(two.calls) == 0
            next_request = await begin(body | {"action_id":uuid4().hex, "expected_session_version":1})
            with pytest.raises(ServiceError, match="CONTEXT_LOST"):
                await second.prepare(next_request)
            assert len(two.calls) == 0
            assert result["session_version"] == 1
        finally:
            await first.aclose()
            await second.aclose()
