"""HTTP handoff, cross-instance restoration and replay fences for assistant turns."""
from uuid import uuid4
import httpx
import pytest
from genslide_agentscope.bff import BFFClient
from genslide_agentscope.config import Settings
from genslide_agentscope.domain import ExecuteRequest, ServiceError
from genslide_agentscope.engine import Engine
from genslide_agentscope.execution import ExecutionRuntime
from genslide_agentscope.mock_bff import create_mock_bff

TOKEN = "local-test-token-with-more-than-32-characters"

class Model:
    def __init__(self, kind="writing"):
        self.calls = []
        self.kind = kind

    async def complete(self, system, payload):
        self.calls.append(payload)
        if payload["phase"] == "decide":
            return {"effect": "deliverable", "target_kind": self.kind, "skill_id": self.kind}
        return {"effect": "deliverable", "reply": "完成",
                "deliverable": {"title": "测试写作", "sections": [
                    {"title": "主题介绍", "body": "这是完整正文，用于验证内容生成与交接。", "notes": "讲解主题"}]}}

def app_for(monkeypatch):
    monkeypatch.setenv("GENSLIDE_ALLOW_MOCK", "1")
    monkeypatch.setenv("GENSLIDE_ENV", "test")
    monkeypatch.setenv("GENSLIDE_SERVICE_TOKEN", TOKEN)
    return create_mock_bff()

def body(kind="writing", **extra):
    return dict(engine="agentscope", tenant_id="t", user_id="u", session_id="s", runtime_epoch="e",
                action_id=uuid4().hex, authorization="placeholder", expected_session_version=0,
                expected_lifecycle_version=1, mode="assistant",
                requested_output="text" if kind == "writing" else kind, message="直接出稿") | extra

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["writing", "document", "presentation"])
async def test_real_engine_bff_http_and_artifact_handoff(monkeypatch, tmp_path, kind):
    app = app_for(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bff/internal/genslide/v1",
                                headers={"Authorization": "Bearer " + TOKEN}) as http:
        settings = Settings(environment="test", service_token=TOKEN, bff_url=str(http.base_url),
                            workspace_root=tmp_path / "workspaces")
        model = Model(kind)
        runtime = ExecutionRuntime(BFFClient(settings, http), Engine(model), settings)
        data = body(kind)
        response = await http.post("http://bff/dev/begin", json=data)
        assert response.status_code == 200, response.text
        request = ExecuteRequest.model_validate(response.json()["request"])
        try:
            result = await runtime.perform(await runtime.prepare(request))
            assert result["session_version"] == 1
            assert result["effect"] == "deliverable"
            assert len(model.calls) == 2
            assert len(result["files"]) == (0 if kind == "writing" else 1)
            assert not list((tmp_path / "workspaces").glob("ws-*"))
            for file in result["files"]:
                artifact = await http.get("http://bff/dev/artifacts/" + file["file_id"])
                assert artifact.status_code == 200 and artifact.content
            assert app.state.sessions[request.session_key()]["snapshot"]["content"] == result["content"]
            repeat = await http.post("http://bff/dev/begin", json=data)
            assert repeat.status_code == 200 and repeat.json()["status"] == "committed"
            with pytest.raises(ServiceError):
                await runtime.prepare(request)
            assert len(model.calls) == 2
        finally:
            await runtime.aclose()

@pytest.mark.asyncio
async def test_two_pods_unique_claim_and_trusted_snapshot_restore(monkeypatch, tmp_path):
    app = app_for(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bff/internal/genslide/v1",
                                headers={"Authorization": "Bearer " + TOKEN}) as http:
        settings = Settings(environment="test", service_token=TOKEN, bff_url=str(http.base_url),
                            workspace_root=tmp_path / "workspaces")
        one, two = Model(), Model()
        first = ExecutionRuntime(BFFClient(settings, http), Engine(one), settings)
        second = ExecutionRuntime(BFFClient(settings, http), Engine(two), settings)
        async def begin(data):
            response = await http.post("http://bff/dev/begin", json=data)
            assert response.status_code == 200, response.text
            return ExecuteRequest.model_validate(response.json()["request"])
        try:
            request = await begin(body())
            prepared = await first.prepare(request)
            with pytest.raises(ServiceError):
                await second.prepare(request)
            assert app.state.actions[request.action_id]["status"] == "active"
            result = await first.perform(prepared)
            next_request = await begin(body(expected_session_version=1, message="修改当前稿"))
            restored = await second.prepare(next_request)
            assert restored.memory.content.model_dump() == result["content"]
            next_result = await second.perform(restored)
            assert next_result["session_version"] == 2
            assert two.calls[1]["current_content"] == result["content"]
        finally:
            await first.aclose()
            await second.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize("commit_first", [False, True])
async def test_deletion_blocks_late_commit_and_old_receipt(monkeypatch, tmp_path, commit_first):
    app = app_for(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bff/internal/genslide/v1",
                                headers={"Authorization": "Bearer " + TOKEN}) as http:
        settings = Settings(environment="test", service_token=TOKEN, bff_url=str(http.base_url),
                            workspace_root=tmp_path / "workspaces")
        runtime = ExecutionRuntime(BFFClient(settings, http), Engine(Model()), settings)
        data = body()
        authorized = (await http.post("http://bff/dev/begin", json=data)).json()["request"]
        request = ExecuteRequest.model_validate(authorized)
        prepared = await runtime.prepare(request)
        if commit_first:
            await runtime.perform(prepared)
        deleted = await http.post("http://bff/dev/sessions/s/delete", json={"tenant_id": "t", "user_id": "u"})
        assert deleted.status_code == 200
        if not commit_first:
            with pytest.raises(ServiceError):
                await runtime.perform(prepared)
        assert app.state.sessions[request.session_key()]["version"] == int(commit_first)
        repeat = await http.post("http://bff/dev/begin", json=data)
        assert repeat.status_code == 410
        assert not list((tmp_path / "workspaces").glob("ws-*"))
        assert runtime.metrics()["generation_active"] == 0
        await runtime.aclose()

@pytest.mark.asyncio
async def test_workspace_creation_failure_settles_and_releases_action(monkeypatch, tmp_path):
    app = app_for(monkeypatch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bff/internal/genslide/v1",
                                headers={"Authorization": "Bearer " + TOKEN}) as http:
        settings = Settings(environment="test", service_token=TOKEN, bff_url=str(http.base_url),
                            workspace_root=tmp_path / "workspaces", workspace_max_bytes=1)
        model = Model()
        runtime = ExecutionRuntime(BFFClient(settings, http), Engine(model), settings)
        request = ExecuteRequest.model_validate((await http.post("http://bff/dev/begin", json=body())).json()["request"])
        prepared = await runtime.prepare(request)
        assert prepared.context.current_file_ids == ()
        assert prepared.context.lifecycle_version == 1
        with pytest.raises(ServiceError, match="WORKSPACE_CAPACITY"):
            await runtime.perform(prepared)
        assert not model.calls
        assert app.state.actions[request.action_id]["status"] == "closed"
        assert runtime.metrics()["generation_active"] == 0
        await runtime.aclose()
