import json
import httpx
import pytest
from genslide_langgraph.model import Model

@pytest.mark.asyncio
async def test_tl_protocol_through_framework_model_boundary(monkeypatch):
    monkeypatch.setenv("MODEL_PROTOCOL", "tl")
    monkeypatch.setenv("MODEL_BASE_URL", "http://tl")
    monkeypatch.setenv("MODEL_API_KEY", "test-token")
    monkeypatch.setenv("MODEL_NAME", "test-model")
    seen = []
    async def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path.endswith("init_session"):
            return httpx.Response(200, json={"code":0, "data":{"session_id":"test-session"}})
        return httpx.Response(200, json={"code":0, "data":{"txt":'{"answer":"done"}'}})
    model = Model()
    transport = model.tl if hasattr(model, "tl") else model.sdk_model.transport
    await transport.client.aclose()
    transport.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await model.complete("Return JSON.", {"message":"hello"}) == {"answer":"done"}
        assert [x[0] for x in seen] == ["/chatbbc/init_session", "/chatbbc/chat"]
        assert seen[1][1]["data"]["files"] == []
        assert seen[1][1]["data"]["stream"] is False
    finally:
        await model.aclose()

