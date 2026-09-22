"""User-facing BFF adapter. Never accepts service credentials or trusted user IDs.

The transport owns the existing login session and CSRF policy. Production BFF and
the Vue application are separate integrations; no persistence is simulated here.
"""
from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import quote


class Transport(Protocol):
    def request(self, method: str, path: str, *, json: dict | None = None,
                headers: dict | None = None) -> Any: ...


class AssistantClient:
    def __init__(self, transport: Transport):
        self.transport = transport

    def turn(self, session_id: str, message: str, *, idempotency_key: str,
             expected_session_version: int, expected_lifecycle_version: int,
             requested_output: str = "auto", requested_skill_id: str | None = None,
             current_file_ids: list[str] | None = None) -> dict:
        if not idempotency_key or not session_id or not message.strip():
            raise ValueError("session, message and stable idempotency key are required")
        if requested_output not in {"auto", "text", "document", "presentation"}:
            raise ValueError("unsupported requested output")
        return self.transport.request(
            "POST", f"/api/v1/sessions/{quote(session_id, safe='')}/actions",
            headers={"Idempotency-Key": idempotency_key},
            json={"mode": "assistant", "message": message,
                  "requested_output": requested_output,
                  "requested_skill_id": requested_skill_id,
                  "current_file_ids": list(current_file_ids or []),
                  "expected_session_version": expected_session_version,
                  "expected_lifecycle_version": expected_lifecycle_version},
        )

    def action(self, action_id: str) -> dict:
        return self.transport.request("GET", f"/api/v1/actions/{quote(action_id, safe='')}")

    def content(self, file_id: str) -> Any:
        return self.transport.request("GET", f"/api/v1/files/{quote(file_id, safe='')}/content")
