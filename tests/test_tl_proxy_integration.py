"""
Integration test verifying GenSlide's ChatBBC / TL provider calling tl-proxy.
"""

import json
import os
import subprocess
import time
import unittest
import urllib.request
from typing import Any

from llm.chatbbc_wrapper import ChatBBCChatWrapper, HumanMessage, SystemMessage
from llm.llm_provider import get_llm


class TestTLProxyIntegration(unittest.TestCase):
    proxy_process = None
    mock_upstream_process = None
    proxy_port = 18089
    upstream_port = 18090

    @classmethod
    def setUpClass(cls):
        # 1. Start a simple mock upstream in Python
        import http.server
        import threading

        class MockUpstreamHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                content_len = int(self.headers.get("Content-Length", 0))
                _ = self.rfile.read(content_len)

                resp = {
                    "id": "mock-cmpl-1",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "Integration Test Success: Hello from mock upstream via tl-proxy!",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
                resp_bytes = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.end_headers()
                self.wfile.write(resp_bytes)

            def log_message(self, format, *args):
                pass

        cls.upstream_server = http.server.HTTPServer(("127.0.0.1", cls.upstream_port), MockUpstreamHandler)
        cls.upstream_thread = threading.Thread(target=cls.upstream_server.serve_forever, daemon=True)
        cls.upstream_thread.start()

        # 2. Start tl-proxy CLI process pointing to mock upstream
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        tl_proxy_dir = os.path.join(repo_root, "services", "tl-proxy")

        env = os.environ.copy()
        env["TL_PROXY_HOST"] = "127.0.0.1"
        env["TL_PROXY_PORT"] = str(cls.proxy_port)
        env["UPSTREAM_PROVIDER"] = "qwen"
        env["UPSTREAM_MODEL"] = "qwen3.8-flash"
        env["UPSTREAM_BASE_URL"] = f"http://127.0.0.1:{cls.upstream_port}"
        env["UPSTREAM_API_KEY"] = "mock-upstream-key"
        env["LOG_LEVEL"] = "debug"

        cls.proxy_process = subprocess.Popen(
            ["node", "dist/src/cli.js"],
            cwd=tl_proxy_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait for proxy to start listening
        proxy_ready = False
        for _ in range(30):
            time.sleep(0.1)
            try:
                # Probe proxy
                req = urllib.request.Request(
                    f"http://127.0.0.1:{cls.proxy_port}/chatbbc/init_session",
                    data=json.dumps({"data": {}}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=1.0) as resp:
                    if resp.status == 200:
                        proxy_ready = True
                        break
            except Exception:
                continue

        if not proxy_ready:
            cls.tearDownClass()
            raise RuntimeError("tl-proxy failed to start within timeout")

    @classmethod
    def tearDownClass(cls):
        if cls.proxy_process:
            cls.proxy_process.terminate()
            cls.proxy_process.wait(timeout=5)
        if hasattr(cls, "upstream_server"):
            cls.upstream_server.shutdown()
            cls.upstream_server.server_close()

    def test_genslide_wrapper_to_tl_proxy(self):
        wrapper = ChatBBCChatWrapper(
            base_url=f"http://127.0.0.1:{self.proxy_port}",
            app_id="genslide-test-app",
            tr_code="agent-chat",
            timeout_seconds=10.0,
        )

        messages = [
            SystemMessage(content="You are an expert slide designer."),
            HumanMessage(content="Create 3 bullet points on microservices."),
        ]

        response = wrapper.invoke(messages)
        self.assertIn("Integration Test Success", response.content)
        self.assertIn("Hello from mock upstream via tl-proxy!", response.content)


if __name__ == "__main__":
    unittest.main()
