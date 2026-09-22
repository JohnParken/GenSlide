"""Explicit provider wrapper for the two-stage TL model protocol."""
from __future__ import annotations

import httpx

from .tl_transport import TLClient


class TLProvider:
    """Expose the model boundary while keeping TL transport details private."""

    name = "tl"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_name: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model_name = model_name
        self.transport = TLClient(base_url, api_key, client=client)

    @property
    def client(self) -> httpx.AsyncClient:
        """Expose the HTTP client for dependency injection in focused tests."""
        return self.transport.client

    @client.setter
    def client(self, value: httpx.AsyncClient) -> None:
        self.transport.client = value

    async def complete(self, system: str, user_text: str) -> str:
        return await self.transport.complete(system, user_text)

    async def aclose(self) -> None:
        await self.transport.aclose()
