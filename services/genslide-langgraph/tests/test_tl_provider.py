import httpx
import pytest

from genslide_langgraph.tl_provider import TLProvider


@pytest.mark.asyncio
async def test_provider_delegates_to_tl_client():
    async def handler(request):
        if request.url.path.endswith("init_session"):
            return httpx.Response(200, json={"code": 0, "data": {"session_id": "sid"}})
        return httpx.Response(200, json={"code": 0, "data": {"txt": "answer"}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TLProvider("http://tl", "secret", "model", client=http)
    try:
        assert await provider.complete("system", "user") == "answer"
        assert provider.name == "tl"
        assert provider.model_name == "model"
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_provider_closes_owned_client():
    provider = TLProvider("http://tl", "secret", "model")
    client = provider.client
    assert not client.is_closed
    await provider.aclose()
    assert client.is_closed
