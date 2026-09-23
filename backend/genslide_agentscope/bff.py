"""Authenticated HTTP adapter for the BFF's authoritative action and file APIs.

All paths below are relative to ``GENSLIDE_BFF_URL`` (which includes the
``/internal/genslide/v1`` prefix). Every request uses service authentication
in ``Authorization: Bearer ...``. Claim additionally carries the one-time
authorization from the BFF and a fingerprint of every non-secret request
field. Execution tokens are sent only to BFF control/file endpoints.

Contract:

* ``POST /actions/{action_id}/claim`` binds an execution instance and returns
  ``execution_token``, ``session_version``, ``runtime_initialized``,
  ``session_expires_at``, ``lease_expires_at`` and ``deadline_at``.
* ``POST /actions/{action_id}/renew`` returns the new lease expiry and the
  unchanged deadline.
* ``PUT /actions/{action_id}/result`` atomically stores ``result``, optional
  ``content``, file references and outline metadata; it returns a committed
  receipt and the incremented session version.
* ``GET /actions/{action_id}`` reads authoritative status. ``POST
  /actions/{action_id}/settle`` reconciles lost acknowledgements or closes an
  eligible canceled execution. It never reruns or resubmits generation.
* ``POST /actions/{action_id}/files/{file_id}/download`` returns bytes with
  ``Content-Disposition``, ``Content-Type`` and ``Content-Length``.
  ``POST /actions/{action_id}/files`` accepts multipart fields for identity,
  execution token, engine and kind plus a ``file`` part, returning a BFF-owned
  ``file_id``. File sizes are checked before/during transfer.
"""
from __future__ import annotations

import asyncio
from .attachment_policy import SUPPORTED_ATTACHMENT_SUFFIXES
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
import mimetypes
from pathlib import Path
from functools import partial
from typing import Any, Mapping, Protocol
from urllib.parse import quote

import httpx

from .config import Settings
from .domain import ExecuteRequest, ServiceError, digest


def _datetime(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ServiceError("BFF_CONTRACT_ERROR", 502) from exc
    else:
        raise ServiceError("BFF_CONTRACT_ERROR", 502)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ServiceError("BFF_CONTRACT_ERROR", 502)
    return parsed.astimezone(timezone.utc)


def _required_string(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ServiceError("BFF_CONTRACT_ERROR", 502)
    return value


@dataclass(frozen=True, slots=True)
class Claim:
    action_id: str
    execution_instance_id: str
    execution_token: str
    engine: str
    runtime_epoch: str
    session_version: int
    runtime_initialized: bool
    session_expires_at: datetime
    lease_expires_at: datetime
    deadline_at: datetime
    lifecycle_version: int = 1
    snapshot: Mapping[str, Any] | None = None

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "Claim":
        try:
            version = value["session_version"]
            lifecycle = value["lifecycle_version"]
            initialized = value["runtime_initialized"]
            if (isinstance(version, bool) or not isinstance(version, int) or version < 0
                    or isinstance(lifecycle, bool) or not isinstance(lifecycle, int) or lifecycle < 1):
                raise ValueError
            if not isinstance(initialized, bool):
                raise ValueError
            snapshot = value.get("snapshot")
            if snapshot is not None and not isinstance(snapshot, Mapping):
                raise ValueError
            return cls(
                action_id=_required_string(value.get("action_id")),
                execution_instance_id=_required_string(value.get("execution_instance_id")),
                execution_token=_required_string(value.get("execution_token")),
                engine=_required_string(value.get("engine")),
                runtime_epoch=_required_string(value.get("runtime_epoch")),
                session_version=version,
                runtime_initialized=initialized,
                session_expires_at=_datetime(value.get("session_expires_at"), "session_expires_at"),
                lease_expires_at=_datetime(value.get("lease_expires_at"), "lease_expires_at"),
                deadline_at=_datetime(value.get("deadline_at"), "deadline_at"),
                lifecycle_version=lifecycle,
                snapshot=dict(snapshot) if snapshot is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceError("BFF_CONTRACT_ERROR", 502) from exc


@dataclass(frozen=True, slots=True)
class CommitOutcome:
    status: str
    action_id: str
    execution_instance_id: str
    session_version: int
    session_expires_at: datetime
    receipt: Any
    lifecycle_version: int = 1
    snapshot: Mapping[str, Any] | None = None

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "CommitOutcome":
        try:
            version = value["session_version"]
            lifecycle = value["lifecycle_version"]
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise ValueError
            if isinstance(lifecycle, bool) or not isinstance(lifecycle, int) or lifecycle < 1:
                raise ValueError
            if value.get("status") != "committed":
                raise ValueError
            receipt = value.get("receipt", {})
            if not isinstance(receipt, (Mapping, str)):
                raise ValueError
            snapshot = value.get("snapshot")
            if snapshot is not None and not isinstance(snapshot, Mapping):
                raise ValueError
            return cls(
                status="committed",
                action_id=_required_string(value.get("action_id")),
                execution_instance_id=_required_string(value.get("execution_instance_id")),
                session_version=version,
                session_expires_at=_datetime(value.get("session_expires_at"), "session_expires_at"),
                receipt=receipt,
                lifecycle_version=lifecycle,
                snapshot=dict(snapshot) if snapshot is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ServiceError("BFF_CONTRACT_ERROR", 502) from exc


@dataclass(frozen=True, slots=True)
class SettleOutcome:
    status: str
    action_id: str
    execution_instance_id: str | None
    session_version: int | None = None
    session_expires_at: datetime | None = None
    receipt: Any = None
    lifecycle_version: int | None = None
    snapshot: Mapping[str, Any] | None = None

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "SettleOutcome":
        status = value.get("status")
        if status not in {"committed", "closed", "active", "unknown"}:
            raise ServiceError("BFF_CONTRACT_ERROR", 502)
        version = value.get("session_version")
        if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 0):
            raise ServiceError("BFF_CONTRACT_ERROR", 502)
        expiry = value.get("session_expires_at")
        receipt = value.get("receipt")
        if receipt is not None and not isinstance(receipt, (Mapping, str)):
            raise ServiceError("BFF_CONTRACT_ERROR", 502)
        lifecycle = value.get("lifecycle_version")
        if lifecycle is not None and (isinstance(lifecycle, bool) or not isinstance(lifecycle, int) or lifecycle < 1):
            raise ServiceError("BFF_CONTRACT_ERROR", 502)
        snapshot = value.get("snapshot")
        if snapshot is not None and not isinstance(snapshot, Mapping):
            raise ServiceError("BFF_CONTRACT_ERROR", 502)
        return cls(
            status=status,
            action_id=_required_string(value.get("action_id")),
            execution_instance_id=(
                _required_string(value["execution_instance_id"])
                if value.get("execution_instance_id") is not None
                else None
            ),
            session_version=version,
            session_expires_at=_datetime(expiry, "session_expires_at") if expiry is not None else None,
            receipt=receipt,
            lifecycle_version=lifecycle,
            snapshot=dict(snapshot) if snapshot is not None else None,
        )


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    path: Path
    filename: str
    content_type: str
    size: int


@dataclass(frozen=True, slots=True)
class FileReference:
    file_id: str
    filename: str
    content_type: str
    size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "size": self.size,
        }


class BFF(Protocol):
    async def claim(self, request: ExecuteRequest, execution_instance_id: str) -> Claim: ...
    async def renew(self, request: ExecuteRequest, claim: Claim) -> datetime: ...
    async def download_file(
        self, request: ExecuteRequest, claim: Claim, file_id: str, directory: Path, max_bytes: int
    ) -> DownloadedFile: ...
    async def upload_file(
        self, request: ExecuteRequest, claim: Claim, path: Path, kind: str, max_bytes: int
    ) -> FileReference: ...
    async def commit_result(
        self, request: ExecuteRequest, claim: Claim, payload: Mapping[str, Any]
    ) -> CommitOutcome: ...
    async def get_action(self, request: ExecuteRequest, execution_instance_id: str) -> Mapping[str, Any]: ...
    async def settle(
        self, request: ExecuteRequest, claim: Claim | None, execution_instance_id: str, reason: str
    ) -> SettleOutcome: ...
    async def aclose(self) -> None: ...


class BFFClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        if not settings.bff_url:
            raise ValueError("GENSLIDE_BFF_URL is required when no BFF client is injected")
        self.settings = settings
        self._owns_client = client is None
        self._io_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="genslide-file-io")
        self._client = client or httpx.AsyncClient(
            base_url=settings.bff_url.rstrip("/"),
            timeout=httpx.Timeout(settings.bff_timeout_seconds),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
        )

    async def _local_io(self, function) -> Any:
        future = asyncio.get_running_loop().run_in_executor(self._io_executor, function)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if future.done() and not future.cancelled():
                try:
                    future.result()
                except BaseException:
                    pass
            raise

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.settings.service_token:
            headers["Authorization"] = f"Bearer {self.settings.service_token}"
        return headers

    @staticmethod
    def _identity(request: ExecuteRequest) -> dict[str, Any]:
        return request.model_dump(
            mode="json",
            include={
                "api_contract_version",
                "engine",
                "tenant_id",
                "user_id",
                "session_id",
                "runtime_epoch",
                "action_id",
                "expected_session_version",
                "expected_lifecycle_version",
                "mode",
                "requested_output",
                "requested_skill_id",
            },
        )

    @staticmethod
    def _claim_fields(request: ExecuteRequest, claim: Claim) -> dict[str, Any]:
        return {
            **BFFClient._identity(request),
            "execution_instance_id": claim.execution_instance_id,
            "execution_token": claim.execution_token,
            "lifecycle_version": claim.lifecycle_version,
        }

    @staticmethod
    def _check_response(response: httpx.Response) -> None:
        if response.is_success:
            return
        code = "BFF_UNAVAILABLE" if response.status_code >= 500 else "BFF_REQUEST_REJECTED"
        try:
            payload = response.json()
            candidate = (payload.get("code") or payload.get("detail")) if isinstance(payload, dict) else None
            if isinstance(candidate, str) and candidate.isascii() and candidate.replace("_", "").isalnum() and candidate.isupper() and len(candidate) <= 80:
                code = candidate
        except (ValueError, TypeError):
            pass
        raise ServiceError(code, response.status_code if 400 <= response.status_code <= 599 else 502)

    @staticmethod
    def _object(response: httpx.Response) -> Mapping[str, Any]:
        try:
            value = response.json()
        except (ValueError, TypeError) as exc:
            raise ServiceError("BFF_CONTRACT_ERROR", 502) from exc
        if not isinstance(value, Mapping):
            raise ServiceError("BFF_CONTRACT_ERROR", 502)
        return value

    async def claim(self, request: ExecuteRequest, execution_instance_id: str) -> Claim:
        body = {
            **request.identity(),
            "execution_instance_id": execution_instance_id,
            "expected_session_version": request.expected_session_version,
            "expected_lifecycle_version": request.expected_lifecycle_version,
            "authorization": request.authorization.get_secret_value(),
            "mode": request.mode,
            "requested_output": request.requested_output,
            "requested_skill_id": request.requested_skill_id,
            "current_file_ids": list(request.current_file_ids),
            "request_fingerprint": digest(request.model_dump(mode="json", exclude={"authorization"})),
        }
        path = f"/actions/{quote(request.action_id, safe='')}/claim"
        try:
            response = await self._client.post(path, json=body, headers=self._headers())
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ServiceError("BFF_UNAVAILABLE", 503) from exc
        self._check_response(response)
        return Claim.from_payload(self._object(response))

    async def renew(self, request: ExecuteRequest, claim: Claim) -> datetime:
        path = f"/actions/{quote(request.action_id, safe='')}/renew"
        body = self._claim_fields(request, claim)
        try:
            response = await self._client.post(path, json=body, headers=self._headers())
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ServiceError("BFF_UNAVAILABLE", 503) from exc
        self._check_response(response)
        payload = self._object(response)
        if payload.get("action_id") != request.action_id or payload.get("execution_instance_id") != claim.execution_instance_id:
            raise ServiceError("BFF_CONTRACT_ERROR", 502)
        deadline = _datetime(payload.get("deadline_at"), "deadline_at")
        if deadline != claim.deadline_at:
            raise ServiceError("BFF_CONTRACT_ERROR", 502)
        return _datetime(payload.get("lease_expires_at"), "lease_expires_at")

    async def commit_result(
        self, request: ExecuteRequest, claim: Claim, payload: Mapping[str, Any]
    ) -> CommitOutcome:
        path = f"/actions/{quote(request.action_id, safe='')}/result"
        body = {**self._claim_fields(request, claim), **payload}
        try:
            response = await self._client.put(path, json=body, headers=self._headers())
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ServiceError("BFF_UNAVAILABLE", 503) from exc
        self._check_response(response)
        return CommitOutcome.from_payload(self._object(response))

    async def get_action(self, request: ExecuteRequest, execution_instance_id: str) -> Mapping[str, Any]:
        path = f"/actions/{quote(request.action_id, safe='')}"
        try:
            response = await self._client.get(
                path,
                params=self._identity(request) | {"execution_instance_id": execution_instance_id},
                headers=self._headers(),
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ServiceError("BFF_UNAVAILABLE", 503) from exc
        self._check_response(response)
        return self._object(response)

    async def settle(
        self, request: ExecuteRequest, claim: Claim | None, execution_instance_id: str, reason: str
    ) -> SettleOutcome:
        path = f"/actions/{quote(request.action_id, safe='')}/settle"
        body = {
            **self._identity(request),
            "execution_instance_id": execution_instance_id,
            "reason": reason,
        }
        if claim is None:
            body["authorization"] = request.authorization.get_secret_value()
        else:
            body["execution_token"] = claim.execution_token
        try:
            response = await self._client.post(path, json=body, headers=self._headers())
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ServiceError("BFF_UNAVAILABLE", 503) from exc
        self._check_response(response)
        return SettleOutcome.from_payload(self._object(response))

    async def download_file(
        self, request: ExecuteRequest, claim: Claim, file_id: str, directory: Path, max_bytes: int
    ) -> DownloadedFile:
        path = f"/actions/{quote(request.action_id, safe='')}/files/{quote(file_id, safe='')}/download"
        body = self._claim_fields(request, claim)
        try:
            async with self._client.stream("POST", path, json=body, headers=self._headers()) as response:
                if not response.is_success:
                    await response.aread()
                    self._check_response(response)
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        if int(length) > max_bytes:
                            raise ServiceError("ATTACHMENT_TOO_LARGE", 413)
                    except ValueError as exc:
                        raise ServiceError("BFF_CONTRACT_ERROR", 502) from exc
                disposition = Message()
                disposition["content-disposition"] = response.headers.get("content-disposition", "")
                remote_name = disposition.get_filename()
                if not remote_name:
                    raise ServiceError("BFF_CONTRACT_ERROR", 502)
                filename = Path(remote_name.replace("\\", "/")).name
                suffix = Path(filename).suffix.lower()
                if suffix not in SUPPORTED_ATTACHMENT_SUFFIXES:
                    raise ServiceError("ATTACHMENT_TYPE_UNSUPPORTED", 415)
                content_type = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0]
                content = bytearray()
                async for chunk in response.aiter_bytes(64 * 1024):
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise ServiceError("ATTACHMENT_TOO_LARGE", 413)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ServiceError("BFF_UNAVAILABLE", 503) from exc
        safe_name = f"attachment-{digest([request.action_id, file_id])[:16]}{suffix}"
        target = directory / safe_name
        await self._local_io(partial(target.write_bytes, bytes(content)))
        return DownloadedFile(path=target, filename=filename, content_type=content_type, size=len(content))

    async def upload_file(
        self, request: ExecuteRequest, claim: Claim, path: Path, kind: str, max_bytes: int
    ) -> FileReference:
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ServiceError("ARTIFACT_UNAVAILABLE", 500) from exc
        if size > max_bytes:
            raise ServiceError("ARTIFACT_TOO_LARGE", 413)
        expected_suffix = {"document": ".docx", "presentation": ".pptx"}.get(kind)
        if expected_suffix is None or path.suffix.lower() != expected_suffix:
            raise ServiceError("ARTIFACT_TYPE_UNSUPPORTED", 415)
        try:
            content = await self._local_io(path.read_bytes)
        except OSError as exc:
            raise ServiceError("ARTIFACT_UNAVAILABLE", 500) from exc
        if len(content) != size or len(content) > max_bytes:
            raise ServiceError("ARTIFACT_TOO_LARGE", 413)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = {
            **self._identity(request),
            "execution_instance_id": claim.execution_instance_id,
            "execution_token": claim.execution_token,
            "engine": request.engine,
            "kind": kind,
            "filename": path.name,
        }
        headers = self._headers()
        headers.pop("Accept", None)
        try:
            response = await self._client.post(
                f"/actions/{quote(request.action_id, safe='')}/files",
                data=data,
                files={"file": (path.name, content, mime)},
                headers=headers,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ServiceError("BFF_UNAVAILABLE", 503) from exc
        self._check_response(response)
        result = self._object(response)
        file_id = _required_string(result.get("file_id"))
        filename = _required_string(result.get("filename", path.name))
        content_type = _required_string(result.get("content_type", mime))
        returned_size = result.get("size", size)
        if isinstance(returned_size, bool) or not isinstance(returned_size, int) or returned_size != size:
            raise ServiceError("BFF_CONTRACT_ERROR", 502)
        return FileReference(file_id=file_id, filename=filename, content_type=content_type, size=returned_size)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        await asyncio.get_running_loop().run_in_executor(
            None, partial(self._io_executor.shutdown, wait=True, cancel_futures=True)
        )
