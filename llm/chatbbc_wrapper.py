"""
LangChain-compatible adapter for corporate chatbbc two-stage RPC/HTTP private protocol.

Contract details (referenced from docs/skills/tl-llm-standalone):
- Stage 1: POST /chatbbc/init_session -> returns session_id with system prompt variables
- Stage 2: POST /chatbbc/chat -> sends user payload and session_id, returns text
- Root envelope:
    {
        "appId": str,
        "trCode": str,
        "trVersion": str,
        "timestamp": int (epoch ms),
        "requestId": str,
        "data": dict
    }
- Standard response envelope:
    {
        "code": 0,
        "message": "success",
        "data": { ... }
    }
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
except ImportError:  # pragma: no cover
    class BaseMessage:  # type: ignore[no-redef]
        def __init__(self, content: Any = "", **kwargs: Any) -> None:
            self.content = content

    class HumanMessage(BaseMessage):  # type: ignore[no-redef]
        pass

    class SystemMessage(BaseMessage):  # type: ignore[no-redef]
        pass

    class AIMessage(BaseMessage):  # type: ignore[no-redef]
        pass

logger = logging.getLogger(__name__)


class ChatBBCProtocolError(RuntimeError):
    """Raised when the chatbbc gateway returns a non-zero code or invalid envelope."""

    def __init__(self, code: int, message: str, stage: str, details: Optional[Any] = None):
        super().__init__(f"ChatBBC [{stage}] error (code={code}): {message}")
        self.code = code
        self.stage = stage
        self.details = details


class ChatBBCChatWrapper:
    """
    Adapter implementing BaseLLMAdapter for chatbbc private protocol.
    Exposes `.invoke(messages: List[BaseMessage]) -> AIMessage`.
    """

    def __init__(
        self,
        base_url: str,
        app_id: str = "genslide-app",
        tr_code: str = "agent-chat",
        tr_version: str = "1.0",
        system_variable_name: str = "system_prompt",
        auth_token: Optional[str] = None,
        timeout_seconds: float = 150.0,
        stream: bool = False,
        temperature: float = 0.3,
    ) -> None:
        # Strip trailing slashes to ensure consistent path concatenation
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.tr_code = tr_code
        self.tr_version = tr_version
        self.system_variable_name = system_variable_name
        self.auth_token = auth_token
        self.timeout_seconds = timeout_seconds
        self.stream = stream
        self.temperature = temperature

    # ------------------------------------------------------------------
    # Envelope & Message compilation
    # ------------------------------------------------------------------

    def _compile_messages(self, messages: List[BaseMessage]) -> Tuple[str, str]:
        """
        Extract system prompt and user text payload from LangChain messages.
        Returns (system_prompt, user_payload).
        """
        system_chunks: List[str] = []
        user_chunks: List[str] = []

        for msg in messages:
            if isinstance(msg, SystemMessage):
                if msg.content:
                    system_chunks.append(str(msg.content))
            elif isinstance(msg, HumanMessage):
                if msg.content:
                    user_chunks.append(str(msg.content))
            else:
                # Other message types (e.g. AIMessage in multi-turn)
                content = getattr(msg, "content", "")
                if content:
                    role = getattr(msg, "type", "assistant")
                    user_chunks.append(f"[{role}]: {content}")

        system_prompt = "\n\n".join(system_chunks) if system_chunks else "You are a helpful assistant."
        user_payload = "\n\n".join(user_chunks) if user_chunks else ""

        return system_prompt, user_payload

    def _create_envelope(self, stage: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Wrap payload in standard corporate RPC request envelope."""
        now_ms = int(time.time() * 1000)
        req_id = f"{stage}-{now_ms}-{uuid.uuid4().hex[:8]}"
        return {
            "appId": self.app_id,
            "trCode": self.tr_code,
            "trVersion": self.tr_version,
            "timestamp": now_ms,
            "requestId": req_id,
            "data": data,
        }

    def _send_http_post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send HTTP POST request using urllib and parse JSON response."""
        url = f"{self.base_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "GenSlide-ChatBBC/1.0",
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")

        logger.debug("ChatBBC POST %s (requestId=%s)", url, payload.get("requestId"))

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                status_code = resp.status
                raw_body = resp.read().decode("utf-8")

                if status_code != 200:
                    raise ChatBBCProtocolError(
                        code=status_code,
                        message=f"HTTP {status_code}: {raw_body[:200]}",
                        stage=path,
                    )

                try:
                    result = json.loads(raw_body)
                except json.JSONDecodeError as exc:
                    raise ChatBBCProtocolError(
                        code=-1,
                        message=f"Malformed JSON response from server: {raw_body[:200]}",
                        stage=path,
                    ) from exc

                code = result.get("code")
                if code is not None and code != 0:
                    msg = result.get("message", "Unknown error")
                    raise ChatBBCProtocolError(
                        code=code,
                        message=msg,
                        stage=path,
                        details=result,
                    )

                return result


        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="ignore")
            logger.error("ChatBBC HTTP error %s for %s: %s", exc.code, url, err_body[:300])
            raise ChatBBCProtocolError(
                code=exc.code,
                message=f"HTTP error {exc.code}: {err_body[:200]}",
                stage=path,
            ) from exc
        except urllib.error.URLError as exc:
            logger.error("ChatBBC network failure connecting to %s: %s", url, exc.reason)
            raise ConnectionError(f"Failed to connect to ChatBBC service at {url}: {exc.reason}") from exc

    # ------------------------------------------------------------------
    # Two-Stage Lifecycle
    # ------------------------------------------------------------------

    def _init_session(self, system_prompt: str) -> str:
        """
        Stage 1: POST /chatbbc/init_session
        Initializes a new session bound with system prompt variables.
        """
        stage = "init_session"
        payload_data = {
            "prompt_variables": [
                {
                    "name": self.system_variable_name,
                    "value": system_prompt,
                }
            ]
        }
        envelope = self._create_envelope(stage, payload_data)
        resp = self._send_http_post("/chatbbc/init_session", envelope)

        # Validate response envelope
        code = resp.get("code")
        if code != 0:
            msg = resp.get("message", "Unknown error")
            raise ChatBBCProtocolError(code=code if code is not None else -1, message=msg, stage=stage, details=resp)

        data = resp.get("data")
        if not isinstance(data, dict):
            raise ChatBBCProtocolError(code=-1, message="Response 'data' field must be an object", stage=stage)

        session_id = data.get("session_id")
        if not session_id or not isinstance(session_id, str):
            raise ChatBBCProtocolError(
                code=-1,
                message=f"Expected non-empty string 'session_id' in data, got {session_id!r}",
                stage=stage,
            )

        logger.debug("ChatBBC session initialized: session_id=%s", session_id)
        return session_id

    def _chat(self, session_id: str, user_payload: str) -> str:
        """
        Stage 2: POST /chatbbc/chat (non-streaming)
        Sends user text payload and returns model text response.
        """
        stage = "chat"
        payload_data = {
            "session_id": session_id,
            "txt": user_payload,
            "files": [],
            "stream": False,
        }
        envelope = self._create_envelope(stage, payload_data)
        resp = self._send_http_post("/chatbbc/chat", envelope)

        # Validate response envelope
        code = resp.get("code")
        if code != 0:
            msg = resp.get("message", "Unknown error")
            raise ChatBBCProtocolError(code=code if code is not None else -1, message=msg, stage=stage, details=resp)

        data = resp.get("data")
        if not isinstance(data, dict):
            raise ChatBBCProtocolError(code=-1, message="Response 'data' field must be an object", stage=stage)

        txt = data.get("txt")
        if txt is None or not isinstance(txt, str):
            raise ChatBBCProtocolError(
                code=-1,
                message=f"Expected string 'txt' in chat response data, got {txt!r}",
                stage=stage,
            )

        return txt

    # ------------------------------------------------------------------
    # Public BaseLLMAdapter interface
    # ------------------------------------------------------------------

    def invoke(self, messages: List[BaseMessage], **kwargs: Any) -> AIMessage:
        """
        Execute two-stage chatbbc protocol and return LangChain AIMessage.
        Compatible with ChatOpenAI.invoke().
        """
        system_prompt, user_payload = self._compile_messages(messages)

        logger.info(
            "ChatBBC invoke: sending request to %s (system_len=%d, user_len=%d)",
            self.base_url,
            len(system_prompt),
            len(user_payload),
        )

        session_id = self._init_session(system_prompt)
        raw_text = self._chat(session_id, user_payload)

        return AIMessage(content=raw_text)
