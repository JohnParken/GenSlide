"""Bounded JSON model boundary. Credentials stay outside request/retained state."""
import asyncio
import json
import os
from .config import model_provider_from_env
from .domain import ServiceError
from .tl_provider import TLProvider

MAX_INPUT_BYTES = 60000
MAX_OUTPUT_BYTES = 160000

def encode_payload(payload):
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded.encode()) > MAX_INPUT_BYTES:
        raise ServiceError("MODEL_CONTEXT_TOO_LARGE", 413)
    return encoded

def decode_output(raw):
    if not isinstance(raw, str) or len(raw.encode()) > MAX_OUTPUT_BYTES:
        raise ServiceError("MODEL_OUTPUT_INVALID", 502)
    text = raw.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ServiceError("MODEL_OUTPUT_INVALID", 502) from exc
    if not isinstance(value, dict):
        raise ServiceError("MODEL_OUTPUT_INVALID", 502)
    return value

def model_settings():
    values = [os.environ.get(k, "").strip() for k in ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME")]
    if not all(values):
        raise RuntimeError("MODEL_BASE_URL, MODEL_API_KEY and MODEL_NAME are required")
    return values

from agentscope.agent import Agent, ModelConfig, ReActConfig, InjectionConfig
from agentscope.credential import OpenAICredential
from agentscope.model import OpenAIChatModel, ChatModelBase, ChatResponse
from agentscope.message import Msg, TextBlock
from agentscope.state import AgentState
from agentscope.tool import Toolkit
from agentscope.formatter import OpenAIChatFormatter

class SDKTLModel(ChatModelBase):
    """Native AgentScope model adapter for the existing two-step TL protocol."""
    def __init__(self, provider: TLProvider):
        super().__init__(credential=OpenAICredential(
                             api_key=provider.transport.key,
                             base_url=provider.transport.base_url),
                         model=provider.model_name, parameters=self.Parameters(), stream=False,
                         max_retries=0, context_size=128000)
        self.provider = provider
        self.transport = provider.transport
        self.formatter = OpenAIChatFormatter()

    async def _call_api(self, model_name, messages, tools=None, tool_choice=None, **kwargs):
        if tools:
            raise ServiceError("TOOLS_NOT_ALLOWED", 422)
        system = "\n".join(m.get_text_content() or "" for m in messages if m.role == "system")
        user = "\n".join(m.get_text_content() or "" for m in messages if m.role != "system")
        text = await self.provider.complete(system, user)
        return ChatResponse(content=[TextBlock(text=text)], is_last=True)

class Model:
    def __init__(self):
        base, key, name = model_settings()
        self.provider_name = model_provider_from_env()
        self.tl_provider = None
        if self.provider_name == "tl":
            self.tl_provider = TLProvider(base, key, name)
            self.sdk_model = SDKTLModel(self.tl_provider)
            return
        self.sdk_model = OpenAIChatModel(
            credential=OpenAICredential(api_key=key, base_url=base),
            model=name, stream=False, max_retries=0,
            parameters=OpenAIChatModel.Parameters(temperature=0.2, max_tokens=12000),
            client_kwargs={"timeout": 120.0, "max_retries": 0},
        )

    async def complete(self, system, payload):
        encoded = encode_payload(payload)
        # Agent, state and empty toolkit are request-private. No default workspace,
        # scripts, offloading, memory plugin, task tools or background service.
        agent = Agent(
            name="GenSlide", system_prompt=system, model=self.sdk_model,
            state=AgentState(), toolkit=Toolkit(tools=[]),
            model_config=ModelConfig(max_retries=0),
            react_config=ReActConfig(max_iters=1, interruption_raise_cancelled_error=True),
            injection_config=InjectionConfig(inject_runtime_state=False),
        )
        try:
            reply = await agent.reply(Msg(name="user", role="user",
                                          content=[{"type": "text", "text": encoded}]))
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError()
            return decode_output(reply.get_text_content())
        except asyncio.CancelledError:
            raise
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("MODEL_UNAVAILABLE", 502) from exc

    async def aclose(self):
        tl_provider = getattr(self, "tl_provider", None)
        if tl_provider is not None:
            await tl_provider.aclose()
        else:
            await self.sdk_model.client.close()
