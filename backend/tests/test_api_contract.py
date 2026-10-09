import json
from uuid import uuid4
import httpx
import pytest
from genslide_agentscope.api import create_app
from genslide_agentscope.bff import BFFClient
from genslide_agentscope.config import Settings
from genslide_agentscope.engine import Engine
from genslide_agentscope.mock_bff import create_mock_bff
from genslide_agentscope.skills import SkillRegistry

TOKEN = "test-internal-token-with-at-least-32-characters"

class Model:
    def __init__(self):
        self.calls = 0
    async def complete(self, system, payload):
        self.calls += 1
        return {
            "thought": "Ask user about audience using ReAct final_reply.",
            "action": "final_reply",
            "action_input": {
                "effect": "reply",
                "reply": "What audience should this guide serve?",
                "target_kind": "writing",
                "skill_id": "writing",
            },
        }

@pytest.mark.asyncio
async def test_authenticated_api_sse_and_redacted_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("GENSLIDE_ALLOW_MOCK", "1")
    monkeypatch.setenv("GENSLIDE_ENV", "test")
    monkeypatch.setenv("GENSLIDE_SERVICE_TOKEN", TOKEN)
    bff_app = create_mock_bff()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bff_app),
                                base_url="http://bff/internal/genslide/v1") as bff_http:
        settings = Settings(environment="test", service_token=TOKEN, bff_url="http://bff/internal/genslide/v1")
        model = Model()
        skill = tmp_path / "writing" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("---\nname: writing\ndescription: Test writing skill\nmetadata:\n  target_kind: writing\n---\nTest instructions", encoding="utf-8")
        engine = Engine(model)
        engine.skills = SkillRegistry(tmp_path)
        app = create_app(settings=settings, bff=BFFClient(settings, bff_http), engine=engine)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as client:
                body = dict(engine="agentscope", tenant_id="t", user_id="u", session_id="s", runtime_epoch="e",
                            action_id=uuid4().hex, authorization="initial", expected_session_version=0,
                            expected_lifecycle_version=1, mode="assistant", requested_output="text", message="Guide")
                path = "/v1/actions/" + body["action_id"] + "/execute"
                denied = await client.post(path, json=body)
                assert denied.status_code in (401, 403)
                assert model.calls == 0
                granted = await bff_http.post("http://bff/dev/begin", json=body,
                                             headers={"Authorization":"Bearer " + TOKEN})
                assert granted.status_code == 200
                body = granted.json()["request"]
                response = await client.post(path, json=body,
                    headers={"Authorization":"Bearer " + TOKEN, "Accept":"text/event-stream"})
                assert response.status_code == 200, response.text
                assert response.headers["content-type"].startswith("text/event-stream")
                assert "event: completed" in response.text
                assert "event: accepted" in response.text
                assert "execution_token" not in response.text
                assert body["authorization"] not in response.text
                assert model.calls == 1
                invalid = body | {"requested_output":"unknown", "authorization":"secret-never-reflect-this"}
                response = await client.post(path, json=invalid, headers={"Authorization":"Bearer " + TOKEN})
                assert response.status_code == 422
                assert "secret-never-reflect-this" not in response.text
                assert model.calls == 1
                assert (await client.get("/internal/metrics")).status_code == 401
                metrics = await client.get("/internal/metrics", headers={"Authorization":"Bearer " + TOKEN})
                assert metrics.status_code == 200
                assert "genslide_sessions_cached 1" in metrics.text
                assert "genslide_generation_active 0" in metrics.text


def test_create_app_with_explicit_settings_without_environment_variables(monkeypatch):
    monkeypatch.delenv("MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_NAME", raising=False)

    settings = Settings(
        bff_url="http://127.0.0.1:8000",
        service_token=TOKEN,
        model_base_url="https://api.test/v1",
        model_api_key="sk-test-key-12345",
        model_name="custom-gpt-4o",
    )
    app = create_app(settings=settings)
    assert app.state.runtime.engine.model.sdk_model.model == "custom-gpt-4o"

