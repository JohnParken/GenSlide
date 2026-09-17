import asyncio
import json

import httpx
import pytest

from genslide_langgraph.domain import ServiceError
from genslide_langgraph.tl_transport import MAX_RESPONSE_BYTES, TLClient


@pytest.mark.asyncio
async def test_two_stage_envelope_auth_and_text(monkeypatch):
    monkeypatch.setenv("TL_SYSTEM_VARIABLE", "instructions")
    requests = []

    async def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        if request.url.path.endswith("init_session"):
            assert body["data"] == {"prompt_variables": [{"name": "instructions", "value": "system"}]}
            payload = {"code": 0, "data": {"session_id": "sid"}}
        else:
            assert body["data"] == {"session_id": "sid", "txt": "hello", "files": [], "stream": False}
            payload = {"code": 0, "data": {"txt": "answer"}}
        return httpx.Response(200, json=payload)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tl = TLClient("https://tl.example/prefix/", "secret", client=http)
    assert await tl.complete("system", "hello") == "answer"
    assert [r.url.path for r in requests] == ["/prefix/chatbbc/init_session", "/prefix/chatbbc/chat"]
    for request in requests:
        assert request.headers["Authorization"] == "Bearer secret"
        envelope = json.loads(request.content)
        assert (envelope["appId"], envelope["trCode"], envelope["trVersion"]) == ("genslide-app", "agent-chat", "1.0")
        assert isinstance(envelope["timestamp"], int) and envelope["requestId"]
    await http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"not json", b'{"code": "0"}', b'{"code": 7, "message": "secret"}'])
async def test_invalid_or_business_error_is_sanitized(body):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))
    tl = TLClient("https://tl.example", "secret", client=http)
    with pytest.raises(ServiceError) as error:
        await tl.complete("s", "u")
    assert error.value.code in {"MODEL_OUTPUT_INVALID", "MODEL_UNAVAILABLE"}
    assert "secret" not in str(error.value)
    await http.aclose()


@pytest.mark.asyncio
async def test_response_byte_limit():
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b" " * (MAX_RESPONSE_BYTES + 1))))
    tl = TLClient("https://tl.example", "secret", client=http)
    with pytest.raises(ServiceError) as error:
        await tl.complete("s", "u")
    assert error.value.code == "MODEL_OUTPUT_INVALID"
    await http.aclose()


@pytest.mark.asyncio
async def test_cancellation_propagates_and_closes_response():
    started = asyncio.Event()
    closed = asyncio.Event()

    class BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            await asyncio.Event().wait()
            yield b""

        async def aclose(self):
            closed.set()

    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=BlockingStream())))
    tl = TLClient("https://tl.example", "secret", client=http)
    task = asyncio.create_task(tl.complete("s", "u"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    await http.aclose()
