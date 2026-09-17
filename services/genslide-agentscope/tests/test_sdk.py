import asyncio

import pytest
from agentscope.credential import OpenAICredential
from agentscope.message import TextBlock
from agentscope.model import ChatResponse, OpenAIChatModel

from genslide_agentscope.model import Model


def sdk_model():
    return OpenAIChatModel(
        credential=OpenAICredential(api_key="test-key", base_url="http://127.0.0.1:1/v1"),
        model="test-model", stream=False, max_retries=0,
        parameters=OpenAIChatModel.Parameters(temperature=0, max_tokens=32),
        client_kwargs={"timeout": 1.0, "max_retries": 0},
    )


@pytest.mark.asyncio
async def test_real_agentscope_agent_uses_mocked_model_and_returns_json(monkeypatch):
    model = Model.__new__(Model)
    model.sdk_model = sdk_model()
    seen = []

    async def mocked_call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        seen.append((model_name, messages, tools))
        return ChatResponse(content=[TextBlock(text='{"answer":"ready"}')], is_last=True)

    monkeypatch.setattr(OpenAIChatModel, "_call_api", mocked_call_api)
    try:
        result = await model.complete("Return JSON.", {"operation": "clarify"})
        assert result == {"answer": "ready"}
        assert len(seen) == 1
        assert seen[0][0] == "test-model"
        assert seen[0][2] == []
        assert "operation" in seen[0][1][-1].get_text_content()
    finally:
        await model.aclose()


@pytest.mark.asyncio
async def test_model_boundary_propagates_task_cancellation(monkeypatch):
    model = Model.__new__(Model)
    model.sdk_model = sdk_model()
    entered = asyncio.Event()

    async def blocked_call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(OpenAIChatModel, "_call_api", blocked_call_api)
    task = asyncio.create_task(model.complete("Return JSON.", {"operation": "clarify"}))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await model.aclose()
