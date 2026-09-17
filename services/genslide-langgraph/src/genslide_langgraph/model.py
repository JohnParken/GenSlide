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

import httpx

class Model:
    def __init__(self):
        base, key, self.name = model_settings()
        self.provider_name = model_provider_from_env()
        self.tl_provider = None
        self.tl = None
        if self.provider_name == "tl":
            self.tl_provider = TLProvider(base, key, self.name)
            self.tl = self.tl_provider
            return
        self.client = httpx.AsyncClient(
            base_url=base.rstrip("/") + "/", headers={"Authorization": "Bearer " + key},
            timeout=httpx.Timeout(120, connect=10), follow_redirects=False,
            limits=httpx.Limits(max_connections=6, max_keepalive_connections=6),
        )

    async def complete(self, system, payload):
        if self.tl_provider is not None:
            return decode_output(await self.tl_provider.complete(system, encode_payload(payload)))
        data = {"model": self.name, "temperature": 0.2, "max_tokens": 12000,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": encode_payload(payload)}]}
        try:
            async with self.client.stream("POST", "chat/completions", json=data) as response:
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_OUTPUT_BYTES * 2:
                        raise ServiceError("MODEL_OUTPUT_INVALID", 502)
                    chunks.append(chunk)
            data = json.loads(b"".join(chunks))
            choice = data["choices"][0]
            if choice.get("finish_reason") not in ("stop", None):
                raise ServiceError("MODEL_OUTPUT_INCOMPLETE", 502)
            return decode_output(choice["message"]["content"])
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
            raise ServiceError("MODEL_UNAVAILABLE", 502) from exc

    async def aclose(self):
        tl_provider = getattr(self, "tl_provider", None)
        if tl_provider is not None:
            await tl_provider.aclose()
        else:
            await self.client.aclose()
