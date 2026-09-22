"""Small standard-library client for the GenSlide dev BFF and service."""
from __future__ import annotations

import json
import mimetypes
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


class ChatClientError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status, self.code = status, code


@dataclass
class ChatState:
    engine: str = "agentscope"
    session: str = field(default_factory=lambda: uuid.uuid4().hex)
    runtime_epoch: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_version: int = 0
    draft: dict[str, Any] | None = None
    guidance: dict[str, Any] = field(default_factory=dict)
    requirements: dict[str, str] = field(default_factory=dict)
    answer: str = ""
    content: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    autonomous_memory: dict[str, Any] = field(default_factory=dict)
    lifecycle_version: int = 1


class ChatClient:
    def __init__(self, bff_url: str, service_url: str, token: str, *, engine="agentscope", tenant_id="demo", user_id="demo", timeout=1900):
        self.bff_url = bff_url.rstrip("/") + "/"
        self.service_url = service_url.rstrip("/") + "/"
        self.token = token
        self.engine = engine
        self.tenant_id, self.user_id, self.timeout = tenant_id, user_id, timeout

    def turn(self, message: str, *, state: ChatState, action_id: str,
             requested_output="auto", requested_skill_id=None, current_file_ids=None):
        """Development-only action adapter; callers preserve action_id on retry.

        Production browsers use AssistantClient and the authenticated public BFF.
        """
        request = {"api_contract_version": "1", "engine": state.engine,
                   "tenant_id": self.tenant_id, "user_id": self.user_id,
                   "session_id": state.session, "runtime_epoch": state.runtime_epoch,
                   "action_id": action_id, "authorization": self.token,
                   "expected_session_version": state.session_version,
                   "expected_lifecycle_version": state.lifecycle_version,
                   "mode": "assistant", "message": message,
                   "requested_output": requested_output,
                   "requested_skill_id": requested_skill_id,
                   "current_file_ids": list(current_file_ids or [])}
        begun = self._request(self.bff_url, "dev/begin", payload=request)
        if begun.get("status") == "committed":
            stored = begun["result"]
            result = {"status": "completed", "action_id": action_id,
                      "session_version": begun["session_version"], "receipt": begun.get("receipt"),
                      "effect": stored["effect"], "result": stored.get("result", {}),
                      "content": stored.get("content"), "files": stored.get("files", [])}
        else:
            authorized = dict(request)
            authorized.update(begun.get("request", begun))
            result = self._request(self.service_url, f"v1/actions/{action_id}/execute", payload=authorized)
        state.session_version = result["session_version"]
        payload = result.get("result", {})
        state.answer = result.get("reply") or payload.get("reply", "")
        if payload.get("outline") is not None:
            state.draft = payload["outline"]
        if result.get("content") is not None:
            state.content = result["content"]
        state.artifacts = result.get("files", [])
        return result

    def _request(self, base: str, path: str, *, method="POST", payload=None, raw=None, headers=None):
        data = raw if raw is not None else (json.dumps(payload or {}).encode() if payload is not None else None)
        hdrs = {"Authorization": f"Bearer {self.token}"}
        if payload is not None:
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        try:
            with urlopen(Request(urljoin(base, path.lstrip("/")), data=data, headers=hdrs, method=method), timeout=self.timeout) as res:
                body = res.read()
                if "json" in res.headers.get_content_type():
                    try:
                        return json.loads(body.decode() or "{}")
                    except (UnicodeDecodeError, ValueError):
                        return body
                return body
        except HTTPError as exc:
            body = exc.read().decode(errors="replace")
            try: detail = json.loads(body)
            except ValueError: detail = {}
            code = detail.get("code") or detail.get("detail")
            raise ChatClientError(str(code or exc.reason), exc.code, code) from exc
        except URLError as exc:
            raise ChatClientError(f"connection failed: {exc.reason}") from exc

    def upload(self, name: str, content: bytes, content_type: str | None = None, *, session_id="pending") -> dict[str, Any]:
        boundary = uuid.uuid4().hex
        fields = {"tenant_id": self.tenant_id, "user_id": self.user_id, "session_id": session_id}
        chunks = []
        for key, value in fields.items():
            chunks += [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()]
        safe_name = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].replace('"', "_").replace("\r", "_").replace("\n", "_")
        ctype = content_type or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        chunks += [f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{safe_name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode(), content, f"\r\n--{boundary}--\r\n".encode()]
        return self._request(self.bff_url, "dev/files", raw=b"".join(chunks), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def execute(self, operation: str, *, target_kind="document", message="", state: ChatState | None = None, current_file_ids=None, **extra):
        state = state or ChatState(engine=self.engine)
        action_id = uuid.uuid4().hex
        request = {"api_contract_version": "1", "engine": state.engine, "tenant_id": self.tenant_id, "user_id": self.user_id,
                   "session_id": state.session, "runtime_epoch": state.runtime_epoch, "action_id": action_id,
                   "authorization": self.token, "expected_session_version": state.session_version, "operation": operation,
                   "target_kind": target_kind, "message": message, "current_file_ids": current_file_ids or []}
        request.update({k: v for k, v in extra.items() if v is not None})
        begun = self._request(self.bff_url, "dev/begin", payload=request)
        authorized = dict(request)
        authorized.update(begun.get("request", begun))
        result = self._request(self.service_url, f"v1/actions/{action_id}/execute", payload=authorized)
        receipt = result.get("receipt")
        receipt_ver = receipt.get("session_version") if isinstance(receipt, dict) else None
        state.session_version = result.get("session_version") or receipt_ver or (state.session_version + 1)
        payload = result.get("result", result)
        memory = payload.get("memory") or {}
        for key, attr in (("outline", "draft"), ("guidance", "guidance"), ("requirements", "requirements")):
            if key in payload:
                setattr(state, attr, payload[key])
            elif key in memory:
                setattr(state, attr, memory[key])
        state.answer = payload.get("answer", "")
        if "content" in result:
            state.content = result["content"]
        state.artifacts = result.get("files", [])
        return result

    def artifact(self, file_id: str) -> bytes:
        return self._request(self.bff_url, f"dev/artifacts/{file_id}", method="GET")

    def list_skills(self) -> list[dict[str, Any]]:
        try:
            res = self._request(self.service_url, "v1/skills", method="GET")
            if isinstance(res, dict) and "skills" in res:
                return res["skills"]
            if isinstance(res, list):
                return res
            return []
        except Exception:
            return []

    def reload_skills(self) -> list[dict[str, Any]]:
        try:
            res = self._request(self.service_url, "v1/skills/reload", method="POST", payload={})
            if isinstance(res, dict) and "skills" in res:
                return res["skills"]
            return []
        except Exception:
            return []
