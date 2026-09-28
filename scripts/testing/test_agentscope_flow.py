#!/usr/bin/env python3
"""
AgentScope 单回合直接创作冒烟测试脚本。
直接调用运行中的 mock_bff (8010) 与 genslide-agentscope (8002)。
"""
import sys
import os
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from frontend.service_chat_client import ChatClient, ChatState

def main():
    bff_url = os.getenv("GENSLIDE_DEMO_BFF_URL", "http://127.0.0.1:8010")
    service_url = os.getenv("GENSLIDE_SERVICE_URL", "http://127.0.0.1:8002")
    token = os.getenv("GENSLIDE_SERVICE_TOKEN", "local-development-token-at-least-32-characters")

    print("==========================================================")
    print("  开始 AgentScope 端到端交互链路冒烟测试")
    print("==========================================================")

    client = ChatClient(bff_url, service_url, token, engine="agentscope")
    state = ChatState(engine="agentscope")

    result = client.turn("直接写一份合成示例项目总结，不使用真实客户数据，输出完整正文并生成 Word。",
                         state=state, action_id=uuid4().hex, requested_output="document",
                         requested_skill_id="document")
    assert result["effect"] == "deliverable", result
    assert result["content"]["sections"], result
    assert result.get("files"), "Expected a committed Word artifact"
    for reference in result["files"]:
        assert client.artifact(reference["file_id"]), "Artifact download was empty"
    print(f"Direct writing succeeded: session version {state.session_version}")

if __name__ == "__main__":
    main()
