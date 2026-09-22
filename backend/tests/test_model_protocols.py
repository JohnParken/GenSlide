import json
import httpx
import pytest
from genslide_agentscope.config import Settings
from genslide_agentscope.domain import ServiceError
from genslide_agentscope.model import Model
from genslide_agentscope.tl_provider import TLProvider
from genslide_agentscope.model import SDKTLModel
from agentscope.model import OpenAIChatModel


def _model_env(monkeypatch, *, provider=None, protocol=None):
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("MODEL_PROTOCOL", raising=False)
    monkeypatch.setenv("MODEL_BASE_URL", "http://tl")
    monkeypatch.setenv("MODEL_API_KEY", "test-token")
    monkeypatch.setenv("MODEL_NAME", "test-model")
    if provider is not None:
        monkeypatch.setenv("MODEL_PROVIDER", provider)
    if protocol is not None:
        monkeypatch.setenv("MODEL_PROTOCOL", protocol)


def test_settings_validate_and_resolve_provider(monkeypatch):
    assert Settings(environment="test").provider == "tl"
    assert Settings(environment="test", provider="tl").provider == "tl"
    with pytest.raises(ValueError, match="provider must be openai or tl"):
        Settings(environment="test", provider="invalid")
    _model_env(monkeypatch, protocol="tl")
    monkeypatch.setenv("GENSLIDE_ENV", "test")
    assert Settings.from_env().provider == "tl"


def test_model_provider_selection_and_conflicts(monkeypatch):
    _model_env(monkeypatch)
    model = Model()
    try:
        assert model.provider_name == "tl"
        assert isinstance(model.tl_provider, TLProvider)
        assert isinstance(model.sdk_model, SDKTLModel)
        assert model.tl_provider.transport.base_url == "http://tl"
    finally:
        import asyncio
        asyncio.run(model.aclose())

    _model_env(monkeypatch, provider="tl")
    model = Model()
    try:
        assert model.provider_name == "tl"
        assert isinstance(model.tl_provider, TLProvider)
    finally:
        import asyncio
        asyncio.run(model.aclose())

    _model_env(monkeypatch, provider="openai")
    model = Model()
    try:
        assert model.provider_name == "openai"
        assert model.tl_provider is None
        assert isinstance(model.sdk_model, OpenAIChatModel)
    finally:
        import asyncio
        asyncio.run(model.aclose())

    _model_env(monkeypatch, provider="tl", protocol="openai")
    with pytest.raises(ValueError, match="must match"):
        Model()

    _model_env(monkeypatch, provider="unsupported")
    with pytest.raises(ValueError, match="MODEL_PROVIDER must be openai or tl"):
        Model()


@pytest.mark.asyncio
async def test_legacy_protocol_alias_selects_tl(monkeypatch):
    _model_env(monkeypatch, protocol="tl")
    model = Model()
    try:
        assert model.provider_name == "tl"
        assert isinstance(model.tl_provider, TLProvider)
    finally:
        await model.aclose()

@pytest.mark.asyncio
async def test_tl_protocol_through_framework_model_boundary(monkeypatch):
    _model_env(monkeypatch, provider="tl")
    seen = []
    async def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path.endswith("init_session"):
            return httpx.Response(200, json={"code":0, "data":{"session_id":"test-session"}})
        return httpx.Response(200, json={"code":0, "data":{"txt":'{"answer":"done"}'}})
    model = Model()
    transport = model.tl_provider
    await transport.client.aclose()
    transport.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await model.complete("Return JSON.", {"message":"hello"}) == {"answer":"done"}
        assert [x[0] for x in seen] == ["/chatbbc/init_session", "/chatbbc/chat"]
        assert seen[1][1]["data"]["files"] == []
        assert seen[1][1]["data"]["stream"] is False
    finally:
        await model.aclose()


@pytest.mark.asyncio
async def test_invalid_json_gets_one_repair_attempt(monkeypatch):
    _model_env(monkeypatch, provider="tl")
    replies = ["not json at all", '{"answer":"repaired"}']
    seen = []
    async def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path.endswith("init_session"):
            return httpx.Response(200, json={"code":0, "data":{"session_id":"test-session"}})
        return httpx.Response(200, json={"code":0, "data":{"txt": replies.pop(0)}})
    model = Model()
    transport = model.tl_provider
    await transport.client.aclose()
    transport.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await model.complete("Return JSON.", {"message":"hello"}) == {"answer":"repaired"}
        chats = [body for path, body in seen if path.endswith("/chatbbc/chat")]
        assert len(chats) == 2
        assert "could not be parsed" in chats[1]["data"]["txt"]
    finally:
        await model.aclose()


@pytest.mark.asyncio
async def test_persistently_invalid_json_still_fails(monkeypatch):
    _model_env(monkeypatch, provider="tl")
    async def handler(request):
        if request.url.path.endswith("init_session"):
            return httpx.Response(200, json={"code":0, "data":{"session_id":"test-session"}})
        return httpx.Response(200, json={"code":0, "data":{"txt": "still not json"}})
    model = Model()
    transport = model.tl_provider
    await transport.client.aclose()
    transport.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ServiceError, match="MODEL_OUTPUT_INVALID"):
            await model.complete("Return JSON.", {"message":"hello"})
    finally:
        await model.aclose()
