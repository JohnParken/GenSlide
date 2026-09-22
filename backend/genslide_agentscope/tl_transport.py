"""Small asynchronous client for the two-stage TL chat protocol."""
from __future__ import annotations

import json
import os
import time
import uuid

import httpx

from .domain import ServiceError

MAX_RESPONSE_BYTES = 320_000


class TLClient:
    def __init__(self, base_url: str, key: str, *, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.key = key
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=10),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=6, max_keepalive_connections=6),
        )
        self.system_variable_name = os.environ.get("TL_SYSTEM_VARIABLE", "system_prompt")

    @staticmethod
    def _envelope(stage: str, data: dict) -> dict:
        timestamp = int(time.time() * 1000)
        return {
            "appId": os.environ.get("TL_APP_ID", "genslide-app"),
            "trCode": os.environ.get("TL_TR_CODE", "agent-chat"),
            "trVersion": os.environ.get("TL_TR_VERSION", "1.0"),
            "timestamp": timestamp,
            "requestId": f"{stage}-{timestamp}-{uuid.uuid4().hex[:8]}",
            "data": data,
        }

    async def _post(self, path: str, stage: str, data: dict) -> dict:
        try:
            async with self.client.stream(
                "POST", self.base_url + path,
                headers={"Authorization": f"Bearer {self.key}", "Accept": "application/json"},
                json=self._envelope(stage, data),
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise ServiceError("MODEL_UNAVAILABLE", 502)
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ServiceError("MODEL_OUTPUT_INVALID", 502)
                    chunks.append(chunk)
            try:
                result = json.loads(b"".join(chunks))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ServiceError("MODEL_OUTPUT_INVALID", 502) from exc
            if not isinstance(result, dict) or type(result.get("code")) is not int:
                raise ServiceError("MODEL_OUTPUT_INVALID", 502)
            if result["code"] != 0:
                raise ServiceError("MODEL_UNAVAILABLE", 502)
            return result
        except ServiceError:
            raise
        except httpx.HTTPError as exc:
            raise ServiceError("MODEL_UNAVAILABLE", 502) from exc

    async def complete(self, system: str, user_text: str) -> str:
        initialized = await self._post(
            "/chatbbc/init_session", "init_session",
            {"prompt_variables": [{"name": self.system_variable_name, "value": system}]},
        )
        init_data = initialized.get("data")
        session_id = init_data.get("session_id") if isinstance(init_data, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise ServiceError("MODEL_OUTPUT_INVALID", 502)
        chatted = await self._post(
            "/chatbbc/chat", "chat",
            {"session_id": session_id, "txt": user_text, "files": [], "stream": False},
        )
        chat_data = chatted.get("data")
        result = chat_data.get("txt") if isinstance(chat_data, dict) else None
        if not isinstance(result, str):
            raise ServiceError("MODEL_OUTPUT_INVALID", 502)
        return result

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
