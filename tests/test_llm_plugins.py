"""
Unit tests for GenSlide LLM provider plugin architecture and ChatBBC private protocol adapter.
"""

import http.server
import json
import os
import threading
import unittest
from typing import Any, Dict, List
from unittest.mock import patch

try:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
except ImportError:
    from llm.chatbbc_wrapper import AIMessage, HumanMessage, SystemMessage  # type: ignore[assignment]

from llm.base import BaseLLMAdapter, BaseProvider
from llm.chatbbc_wrapper import ChatBBCChatWrapper, ChatBBCProtocolError
from llm.llm_provider import get_llm, get_provider_name, list_available_providers
from llm.registry import ProviderRegistry, register_provider, registry


class TestProviderRegistry(unittest.TestCase):
    """Test dynamic provider registration, lookup, and aliasing."""

    def setUp(self):
        self.test_registry = ProviderRegistry()

    def test_register_and_lookup_by_name(self):
        class DummyProvider(BaseProvider):
            name = "dummy"
            description = "A dummy provider for testing"

            def build_llm(self, temperature: float = 0.3, **kwargs: Any) -> Any:
                return "dummy_instance"

        self.test_registry.register(DummyProvider)
        self.assertTrue(self.test_registry.is_registered("dummy"))
        self.assertTrue(self.test_registry.is_registered("DUMMY"))
        provider = self.test_registry.get("dummy")
        self.assertEqual(provider.name, "dummy")
        self.assertEqual(provider.build_llm(), "dummy_instance")

    def test_register_with_aliases(self):
        class DummyWithAlias(BaseProvider):
            name = "primary_name"
            aliases = ["alias_one", "alias_two"]

            def build_llm(self, temperature: float = 0.3, **kwargs: Any) -> Any:
                return "ok"

        self.test_registry.register(DummyWithAlias)
        self.assertEqual(self.test_registry.get("primary_name").name, "primary_name")
        self.assertEqual(self.test_registry.get("alias_one").name, "primary_name")
        self.assertEqual(self.test_registry.get("ALIAS_TWO").name, "primary_name")

    def test_lookup_unregistered_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.test_registry.get("non_existent_provider")

    def test_builtin_providers_loaded(self):
        available = list_available_providers()
        self.assertIn("openai", available)
        self.assertIn("local", available)
        self.assertIn("chatbbc", available)
        # Check alias lookup in global registry
        self.assertEqual(registry.get("tl").name, "chatbbc")
        self.assertEqual(registry.get("custom").name, "chatbbc")
        self.assertEqual(registry.get("gpt4all").name, "local")


class MockChatBBCHandler(http.server.BaseHTTPRequestHandler):
    """Mock HTTP handler for testing ChatBBC two-stage RPC/HTTP protocol."""

    recorded_requests: List[Dict[str, Any]] = []

    def do_POST(self):
        content_len = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_len)
        body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}

        MockChatBBCHandler.recorded_requests.append({
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })

        if self.path == "/chatbbc/init_session":
            # Check prompt_variables
            pv = body.get("data", {}).get("prompt_variables", [])
            if not pv or pv[0].get("name") != "system_prompt":
                self._send_json(400, {"code": 1001, "message": "Invalid system_prompt variable"})
                return
            self._send_json(200, {
                "code": 0,
                "message": "success",
                "data": {"session_id": "test-session-xyz-123"}
            })

        elif self.path == "/chatbbc/chat":
            session_id = body.get("data", {}).get("session_id")
            if session_id != "test-session-xyz-123":
                self._send_json(400, {"code": 1002, "message": "Invalid session_id"})
                return
            txt_in = body.get("data", {}).get("txt", "")
            # Return model response in data.txt
            self._send_json(200, {
                "code": 0,
                "message": "success",
                "data": {"txt": json.dumps({"echo": txt_in, "title": "Mock Slide Response"})}
            })

        elif self.path == "/chatbbc/error_stage":
            self._send_json(200, {
                "code": 5001,
                "message": "Gateway internal business failure",
                "data": None
            })

        else:
            self._send_json(404, {"code": 404, "message": "Not Found"})

    def _send_json(self, status: int, data: Dict[str, Any]):
        resp_bytes = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_bytes)))
        self.end_headers()
        self.wfile.write(resp_bytes)

    def log_message(self, format, *args):
        # Suppress standard HTTP server stdout logging during tests
        pass


class TestChatBBCChatWrapper(unittest.TestCase):
    """Test ChatBBCChatWrapper against mock server."""

    @classmethod
    def setUpClass(cls):
        MockChatBBCHandler.recorded_requests = []
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), MockChatBBCHandler)
        cls.port = cls.server.server_port
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        MockChatBBCHandler.recorded_requests.clear()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.wrapper = ChatBBCChatWrapper(
            base_url=self.base_url,
            app_id="test-app",
            tr_code="slide-chat",
            tr_version="2.0",
            auth_token="secret-bearer-token",
            timeout_seconds=5.0,
        )

    def test_successful_two_stage_invoke(self):
        messages = [
            SystemMessage(content="You are an expert presentation generator."),
            HumanMessage(content="Generate an outline for AI trends in 2026."),
        ]

        result = self.wrapper.invoke(messages)

        self.assertIsInstance(result, AIMessage)
        parsed = json.loads(result.content)
        self.assertEqual(parsed.get("title"), "Mock Slide Response")
        self.assertEqual(parsed.get("echo"), "Generate an outline for AI trends in 2026.")

        # Verify recorded requests
        reqs = MockChatBBCHandler.recorded_requests
        self.assertEqual(len(reqs), 2)

        # Stage 1: init_session
        init_req = reqs[0]
        self.assertEqual(init_req["path"], "/chatbbc/init_session")
        self.assertEqual(init_req["body"]["appId"], "test-app")
        self.assertEqual(init_req["body"]["trCode"], "slide-chat")
        self.assertEqual(init_req["body"]["trVersion"], "2.0")
        self.assertIn("Bearer secret-bearer-token", init_req["headers"].get("Authorization", ""))
        pvs = init_req["body"]["data"]["prompt_variables"]
        self.assertEqual(pvs[0]["name"], "system_prompt")
        self.assertEqual(pvs[0]["value"], "You are an expert presentation generator.")

        # Stage 2: chat
        chat_req = reqs[1]
        self.assertEqual(chat_req["path"], "/chatbbc/chat")
        self.assertEqual(chat_req["body"]["data"]["session_id"], "test-session-xyz-123")
        self.assertEqual(chat_req["body"]["data"]["txt"], "Generate an outline for AI trends in 2026.")
        self.assertFalse(chat_req["body"]["data"]["stream"])

    def test_non_zero_business_code_raises_error(self):
        wrapper = ChatBBCChatWrapper(base_url=self.base_url, timeout_seconds=2.0)
        with self.assertRaises(ChatBBCProtocolError) as ctx:
            # Force target to hit error_stage
            wrapper._send_http_post("/chatbbc/error_stage", {})
        self.assertEqual(ctx.exception.code, 5001)
        self.assertIn("Gateway internal business failure", str(ctx.exception))


class TestFactoryAndEnvIntegration(unittest.TestCase):
    """Test get_llm and get_provider_name factory integration."""

    def test_get_provider_name_fallback_on_unknown(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "non_existent_random_provider"}):
            name = get_provider_name()
            self.assertEqual(name, "openai")

    def test_get_provider_name_canonicalization(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "TL"}):
            name = get_provider_name()
            self.assertEqual(name, "chatbbc")

        with patch.dict(os.environ, {"LLM_PROVIDER": "gpt4all"}):
            name = get_provider_name()
            self.assertEqual(name, "local")

    def test_chatbbc_provider_builds_wrapper(self):
        with patch.dict(
            os.environ,
            {
                "LLM_PROVIDER": "chatbbc",
                "CHATBBC_BASE_URL": "http://127.0.0.1:9999",
                "CHATBBC_APP_ID": "demo-app",
            },
        ):
            # Clear cache for get_llm to avoid cached instance
            get_llm.cache_clear()
            llm = get_llm(temperature=0.7)
            self.assertIsInstance(llm, ChatBBCChatWrapper)
            self.assertEqual(llm.base_url, "http://127.0.0.1:9999")
            self.assertEqual(llm.app_id, "demo-app")
            self.assertEqual(llm.temperature, 0.7)


if __name__ == "__main__":
    unittest.main()
