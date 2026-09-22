"""In-memory BFF contract simulator. NEVER use for production authority/storage."""
from __future__ import annotations
import hmac
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import Response
from .domain import ExecuteRequest, ExecutionSnapshot, digest

PREFIX = "/internal/genslide/v1"
MAX_BYTES = 20 * 1024 * 1024
ASSISTANT_MODE = "assistant"

def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()

def create_mock_bff():
    if os.environ.get("GENSLIDE_ALLOW_MOCK") != "1" or os.environ.get("GENSLIDE_ENV") == "production":
        raise RuntimeError("Mock BFF requires GENSLIDE_ALLOW_MOCK=1 and non-production environment")
    token = os.environ.get("GENSLIDE_SERVICE_TOKEN", "")
    if len(token) < 16:
        raise RuntimeError("Set GENSLIDE_SERVICE_TOKEN (16+ characters)")
    app = FastAPI(title="GenSlide DEV-ONLY BFF simulator")
    actions, sessions, files = {}, {}, {}
    app.state.actions, app.state.sessions, app.state.files = actions, sessions, files

    @app.middleware("http")
    async def auth(request, call_next):
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, "Bearer " + token):
            return Response(status_code=401)
        return await call_next(request)

    def expire():
        now = time.time()
        for action in actions.values():
            if action["status"] in ("authorized", "active") and (
                action["deadline"] <= now or action.get("lease", action["deadline"]) <= now
            ):
                action["status"] = "closed"

    def lookup(action_id, body, leased=True):
        expire()
        action = actions.get(action_id)
        if action is None:
            raise HTTPException(404, "ACTION_NOT_FOUND")
        req = action["request"]
        session = sessions.get(req.session_key())
        if not session or session.get("status", "active") != "active":
            raise HTTPException(410, "SESSION_DELETED")
        if session["lifecycle_version"] != req.expected_lifecycle_version:
            raise HTTPException(409, "LIFECYCLE_VERSION_CONFLICT")
        for field in ("tenant_id", "user_id", "session_id", "runtime_epoch", "action_id", "engine"):
            if body.get(field) != getattr(req, field):
                raise HTTPException(403, "IDENTITY_MISMATCH")
        if leased:
            if action["status"] not in ("active", "committed"):
                raise HTTPException(409, "ACTION_CLOSED")
            if body.get("execution_instance_id") != action.get("instance") or not hmac.compare_digest(
                str(body.get("execution_token", "")), action.get("execution_token", "")
            ):
                raise HTTPException(403, "TOKEN_MISMATCH")
        return action

    def public_status(action):
        reply = {"action_id": action["request"].action_id,
                 "execution_instance_id": action.get("instance"),
                 "status": action["status"]}
        if action["status"] == "committed":
            reply.update(action["receipt"])
            reply["result"] = action.get("result")
        return reply

    @app.post("/dev/begin")
    async def begin(body: ExecuteRequest):
        expire()
        fingerprint = digest(body.model_dump(mode="json", exclude={"authorization"}))
        previous = actions.get(body.action_id)
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
            session = sessions[previous["request"].session_key()]
            if session.get("status", "active") != "active":
                raise HTTPException(410, "SESSION_DELETED")
            return {"request": previous["request"].model_dump(mode="json", exclude={"authorization"}) |
                    {"authorization": previous["authorization"]}, **public_status(previous)}
        if len(actions) >= 1000:
            raise HTTPException(429, "MOCK_CAPACITY_RESTART_REQUIRED")
        key = body.session_key()
        session = sessions.get(key)
        if session is None:
            if body.expected_session_version != 0 or body.expected_lifecycle_version != 1:
                raise HTTPException(409, "SESSION_VERSION_CONFLICT")
            session = {"version": 0, "lifecycle_version": 1, "expires": time.time() + 14400,
                       "engine": body.engine, "snapshot": None, "status": "active"}
        if session.get("status", "active") != "active":
            raise HTTPException(410, "SESSION_DELETED")
        if session["lifecycle_version"] != body.expected_lifecycle_version:
            raise HTTPException(409, "LIFECYCLE_VERSION_CONFLICT")
        if (session["engine"] != body.engine
                or session["version"] != body.expected_session_version
                or session["lifecycle_version"] != body.expected_lifecycle_version):
            raise HTTPException(409, "SESSION_VERSION_CONFLICT")
        if session["expires"] <= time.time():
            raise HTTPException(409, "RUNTIME_EXPIRED")
        active = [a for a in actions.values() if a["status"] in ("authorized", "active")]
        if any(a["request"].session_key() == key for a in active):
            raise HTTPException(409, "SESSION_BUSY")
        if sum(a["request"].tenant_id == body.tenant_id and a["request"].user_id == body.user_id for a in active) >= 2:
            raise HTTPException(429, "USER_CAPACITY")
        sessions[key] = session
        authorization = secrets.token_urlsafe(32)
        # Keep the validated body; initial authorization is held separately.
        actions[body.action_id] = {"request": body, "fingerprint": fingerprint,
            "authorization": authorization, "status": "authorized",
            "deadline": time.time() + 1800}
        return {"request": body.model_dump(mode="json", exclude={"authorization"}) |
                {"authorization": authorization}, "status": "authorized", "action_id": body.action_id}

    @app.post(PREFIX + "/actions/{action_id}/claim")
    async def claim(action_id: str, body: dict):
        action = lookup(action_id, body, leased=False)
        if action["status"] == "committed":
            raise HTTPException(409, "ALREADY_COMMITTED")
        if action["status"] == "closed":
            raise HTTPException(409, "ACTION_CLOSED")
        if not hmac.compare_digest(str(body.get("authorization", "")), action["authorization"]):
            raise HTTPException(403, "AUTHORIZATION_MISMATCH")
        if body.get("request_fingerprint") != action["fingerprint"]:
            raise HTTPException(409, "REQUEST_MISMATCH")
        if action.get("instance") and action["instance"] != body.get("execution_instance_id"):
            raise HTTPException(409, "ALREADY_CLAIMED")
        if not body.get("execution_instance_id"):
            raise HTTPException(422, "INSTANCE_REQUIRED")
        action["instance"] = body["execution_instance_id"]
        action.setdefault("execution_token", secrets.token_urlsafe(32))
        action["status"] = "active"
        action["lease"] = min(time.time() + 45, action["deadline"])
        session = sessions[action["request"].session_key()]
        return {"action_id": action_id, "execution_instance_id": action["instance"],
                "execution_token": action["execution_token"], "engine": action["request"].engine,
                "runtime_epoch": action["request"].runtime_epoch, "session_version": session["version"],
                "runtime_initialized": session["version"] > 0,
                "lifecycle_version": session["lifecycle_version"],
                "snapshot": session.get("snapshot"),
                "session_expires_at": stamp(session["expires"]), "lease_expires_at": stamp(action["lease"]),
                "deadline_at": stamp(action["deadline"])}

    @app.post(PREFIX + "/actions/{action_id}/renew")
    async def renew(action_id: str, body: dict):
        action = lookup(action_id, body)
        if action["status"] != "active":
            raise HTTPException(409, "ACTION_CLOSED")
        action["lease"] = min(time.time() + 45, action["deadline"])
        return {"action_id": action_id, "execution_instance_id": action["instance"],
                "lease_expires_at": stamp(action["lease"]), "deadline_at": stamp(action["deadline"])}

    @app.put(PREFIX + "/actions/{action_id}/result")
    async def result(action_id: str, body: dict):
        action = lookup(action_id, body)
        if action["status"] == "committed":
            if digest(body) != action["result_fingerprint"]:
                raise HTTPException(409, "RESULT_MISMATCH")
            return public_status(action)
        req = action["request"]
        session = sessions[req.session_key()]
        if (body.get("expected_session_version") != session["version"]
                or body.get("expected_lifecycle_version") != session["lifecycle_version"]
                or body.get("lifecycle_version") != session["lifecycle_version"]):
            raise HTTPException(409, "SESSION_VERSION_CONFLICT")
        if body.get("mode") != req.mode:
            raise HTTPException(409, "RESULT_MISMATCH")
        for ref in body.get("files", []):
            file = files.get(ref.get("file_id"))
            if not file or file.get("action_id") != action_id:
                raise HTTPException(409, "FILE_NOT_UPLOADED")
        effect = body.get("effect")
        if effect not in {"reply", "outline", "deliverable"}:
            raise HTTPException(409, "RESULT_MISMATCH")
        kind = body.get("target_kind") or ("document" if req.requested_output == "auto" else req.requested_output)
        if effect == "deliverable" and kind != "writing" and not body.get("files"):
            raise HTTPException(409, "FILE_REQUIRED")
        snapshot = body.get("snapshot")
        if not isinstance(snapshot, dict):
            raise HTTPException(409, "SNAPSHOT_REQUIRED")
        try:
            ExecutionSnapshot.model_validate(snapshot)
        except ValueError:
            raise HTTPException(409, "SNAPSHOT_INVALID")
        # Atomic within this event loop: no await between validation and commit.
        session["version"] += 1
        session["expires"] = time.time() + 14400
        session["snapshot"] = snapshot
        action["status"] = "committed"
        action["result"] = body
        action["result_fingerprint"] = digest(body)
        action["receipt"] = {"status": "committed", "action_id": action_id,
            "execution_instance_id": action["instance"], "session_version": session["version"],
            "lifecycle_version": session["lifecycle_version"], "snapshot": snapshot,
            "session_expires_at": stamp(session["expires"]), "receipt": uuid4().hex}
        for ref in body.get("files", []):
            files[ref["file_id"]]["status"] = "ready"
        return public_status(action)

    @app.post("/dev/sessions/{session_id}/delete")
    async def delete_session(session_id: str, body: dict):
        # Simulator only: production derives identity from login, never from this body.
        matched = [a for a in actions.values() if a["request"].session_id == session_id
                   and a["request"].tenant_id == body.get("tenant_id")
                   and a["request"].user_id == body.get("user_id")]
        if not matched:
            raise HTTPException(404, "SESSION_NOT_FOUND")
        for action in matched:
            session = sessions[action["request"].session_key()]
            if session["status"] == "active":
                session["status"] = "deleting"
                session["lifecycle_version"] += 1
            if action["status"] != "committed":
                action["status"] = "closed"
        return {"status": "deleting"}

    @app.get(PREFIX + "/actions/{action_id}")
    async def status(action_id: str, request: Request):
        body = dict(request.query_params)
        action = lookup(action_id, body, leased=False)
        reply = public_status(action)
        if action["status"] == "committed":
            reply["result"] = action["result"]
        return reply

    @app.post(PREFIX + "/actions/{action_id}/settle")
    async def settle(action_id: str, body: dict):
        action = lookup(action_id, body, leased=False)
        if action.get("instance") not in (None, body.get("execution_instance_id")):
            raise HTTPException(403, "INSTANCE_MISMATCH")
        if body.get("execution_token"):
            if not hmac.compare_digest(str(body["execution_token"]), action.get("execution_token", "")):
                raise HTTPException(403, "TOKEN_MISMATCH")
        elif not hmac.compare_digest(str(body.get("authorization", "")), action["authorization"]):
            raise HTTPException(403, "AUTHORIZATION_MISMATCH")
        reasons = {"claim_ack_lost", "result_ack_lost", "client_disconnected",
                   "deadline_exceeded", "lease_unconfirmed", "execution_failed"}
        if body.get("reason") not in reasons:
            raise HTTPException(422, "INVALID_SETTLE_REASON")
        if action["status"] != "committed":
            action["status"] = "closed"
        reply = public_status(action)
        if action["status"] == "committed":
            reply["snapshot"] = sessions[action["request"].session_key()].get("snapshot")
            reply["lifecycle_version"] = sessions[action["request"].session_key()]["lifecycle_version"]
        return reply

    @app.post("/dev/files")
    async def add_input(request: Request):
        form = await request.form()
        upload = form.get("file")
        if not isinstance(upload, UploadFile) and not hasattr(upload, "read"):
            raise HTTPException(422, "FILE_REQUIRED")
        data = await upload.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES or len(files) >= 100:
            raise HTTPException(413, "FILE_TOO_LARGE_OR_MOCK_FULL")
        fid = uuid4().hex
        files[fid] = {"data": data, "filename": PurePosixPath(upload.filename).name,
                      "owner": tuple(str(form.get(k, "")) for k in ("tenant_id", "user_id", "session_id"))}
        return {"file_id": fid}

    @app.get("/dev/artifacts/{file_id}")
    async def get_artifact(file_id: str):
        """Download a committed generated artifact from the development simulator."""
        file = files.get(file_id)
        action = actions.get(file.get("action_id")) if file else None
        committed_files = action.get("result", {}).get("files", []) if action else []
        if (
            not file
            or not action
            or action.get("status") != "committed"
            or sessions[action["request"].session_key()].get("status") != "active"
            or not any(ref.get("file_id") == file_id for ref in committed_files)
        ):
            raise HTTPException(404, "ARTIFACT_NOT_FOUND")
        suffix = PurePosixPath(file["filename"]).suffix.lower()
        filename = "generated" + suffix if suffix in (".docx", ".pptx") else "generated.bin"
        return Response(
            file["data"],
            media_type=file["content_type"],
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post(PREFIX + "/actions/{action_id}/files/{file_id}/download")
    async def download(action_id: str, file_id: str, body: dict):
        action = lookup(action_id, body)
        if action["status"] != "active":
            raise HTTPException(409, "ACTION_CLOSED")
        req = action["request"]
        file = files.get(file_id)
        if file_id not in req.current_file_ids or not file or file["owner"] != (req.tenant_id, req.user_id, req.session_id):
            raise HTTPException(403, "FILE_SCOPE_MISMATCH")
        # Filename encoded ASCII for this simulator; body carries no object-store URL.
        suffix = PurePosixPath(file["filename"]).suffix
        if suffix not in (".txt", ".md", ".pdf", ".docx"):
            raise HTTPException(415, "FILE_TYPE_UNSUPPORTED")
        return Response(file["data"], media_type="application/octet-stream",
                        headers={"Content-Disposition": 'attachment; filename="input' + suffix + '"'})

    @app.post(PREFIX + "/actions/{action_id}/files")
    async def upload(action_id: str, request: Request):
        form = await request.form()
        body = {k: str(v) for k, v in form.items() if k != "file"}
        action = lookup(action_id, body)
        if action["status"] != "active":
            raise HTTPException(409, "ACTION_CLOSED")
        file = form.get("file")
        if not hasattr(file, "read"):
            raise HTTPException(422, "FILE_REQUIRED")
        data = await file.read(MAX_BYTES + 1)
        # Recheck after the transfer await: deletion/expiry may have raced the upload.
        action = lookup(action_id, body)
        if action["status"] != "active":
            raise HTTPException(409, "ACTION_CLOSED")
        if len(data) > MAX_BYTES or len(files) >= 100:
            raise HTTPException(413, "FILE_TOO_LARGE_OR_MOCK_FULL")
        fid = uuid4().hex
        filename = PurePosixPath(file.filename).name
        content_type = file.content_type or "application/octet-stream"
        files[fid] = {"data": data, "action_id": action_id, "filename": filename,
                      "content_type": content_type, "status": "staged"}
        return {"file_id": fid, "filename": filename,
                "content_type": content_type, "size": len(data)}

    return app
